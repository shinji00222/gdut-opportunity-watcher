from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import html as html_lib
import json
import os
import re
import tempfile
import threading
import urllib.parse
import urllib.request
import webbrowser
import ctypes
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk


APP_NAME = "广工竞赛提醒"
APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "GDUTCompetitionWatcher"
STATE_FILE = APP_DIR / "state.json"
LOG_FILE = APP_DIR / "watcher.log"
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_LOG_BYTES = 1024 * 1024
DETAIL_REFRESH_INTERVAL = timedelta(days=7)
DETAIL_RETRY_INTERVAL = timedelta(days=1)
MAX_DETAIL_TARGETS = 30
INTERNATIONAL_INTERNSHIP_SOURCE = "国际组织实习平台"
INTERNATIONAL_INTERNSHIP_URL = "https://gj.ncss.cn/job.html"
NATIONAL_INTERNSHIP_SOURCE = "国家大学生就业服务平台实习岗位"
NATIONAL_INTERNSHIP_URL = "https://job.ncss.cn/student/jobs/internindex.html"
CHECK_ALREADY_RUNNING = "已有一次检测正在运行，本次跳过"
_THREAD_CHECK_LOCK = threading.Lock()
_THREAD_STATE_LOCK = threading.Lock()

SOURCES = [
    ("学校通知公告", "https://www.gdut.edu.cn/index/tzgg.htm", None),
    ("教务处", "https://jwc.gdut.edu.cn/", "竞赛"),
    ("学生工作处", "https://xsc.gdut.edu.cn/xwzx/tzgg.htm", None),
    ("学生就业指导中心赛事", "https://career.gdut.edu.cn/competition/competitions", "竞赛"),
    ("创新创业学院", "https://cxcy.gdut.edu.cn/", "竞赛"),
    ("广东省工科大学生实验综合技能竞赛", "https://jnds.gdut.edu.cn/", "竞赛"),
    ("工程训练中心学科竞赛", "https://etc.gdut.edu.cn/index/xkjs.htm", "竞赛"),
    ("蓝桥杯大赛官网", "https://dasai.lanqiao.cn/pages/v7/dasai/competition/individual_competition.html", "竞赛"),
    ("国际合作与交流处", "https://wsc.gdut.edu.cn/", "出国"),
    (NATIONAL_INTERNSHIP_SOURCE, NATIONAL_INTERNSHIP_URL, "实习"),
    (INTERNATIONAL_INTERNSHIP_SOURCE, INTERNATIONAL_INTERNSHIP_URL, "实习"),
]

CATEGORY_KEYWORDS = {
    "竞赛": (
    "竞赛", "大赛", "比赛", "挑战杯", "大挑", "小挑", "创业计划竞赛",
    "课外学术科技作品", "蓝桥杯", "创新创业", "中国国际大学生创新大赛", "互联网+",
    "数学建模", "大学生数学竞赛", "电子设计", "大学生工程训练", "服务外包创新创业",
    "网络安全创新大赛", "计算机设计大赛", "实验综合技能竞赛", "工科大学生实验综",
    "工科大学生实验综合技能",
    "工科大学生综合实验技能", "外研社·国才杯",
    "词达人杯", "程序设计", "建模竞赛", "学科竞赛", "校赛", "选拔赛", "作品征集",
    "ACM-ICPC", "ICPC", "CCPC", "RoboMaster", "RoboCup", "RAICOM", "MathorCup", "CTF",
    ),
    "出国": (
        "交换生", "交流项目", "海外交流", "境外交流", "出国交流", "联合培养",
        "访学", "留学项目", "暑期学校", "冬季学校", "国际项目", "海外项目",
        "交流生", "研修项目", "名校探访", "国际寒假项目", "海外寒假项目",
        "国际暑期项目", "海外暑期项目", "短期海外游学",
    ),
    "实习": (
        "实习", "实习生", "暑期实习", "寒假实习", "实践项目", "训练营",
    ),
}
KEYWORDS = tuple(dict.fromkeys(k for values in CATEGORY_KEYWORDS.values() for k in values))
IGNORE_WORDS = (
    "教师教学创新大赛", "教职工", "体育比赛成绩", "创新创业教育",
    "竞赛场地", "就餐地点", "住宿信息",
)
NON_ACTIONABLE_COMPETITION_MARKERS = (
    "成绩公示", "成绩公布", "获奖名单", "获奖结果", "获奖公示", "拟获奖",
    "名单公示", "拟入驻名单", "喜获", "荣获", "获佳绩", "再捧", "夺冠",
    "圆满落幕", "圆满结束", "赛事回顾",
)


@dataclass
class Notice:
    id: str
    title: str
    url: str
    source: str
    date: str = ""
    first_seen: str = ""
    summary: str = ""
    category: str = "竞赛"
    detail_text: str = ""
    hard_conditions: list[str] = field(default_factory=list)
    lifecycle_stage: str = ""
    detail_checked_at: str = ""
    detail_attempted_at: str = ""


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.href: str | None = None
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self.href = dict(attrs).get("href")
            self.parts = []

    def handle_data(self, data):
        if self.href is not None:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.href is not None:
            text = re.sub(r"\s+", " ", "".join(self.parts)).strip()
            if text:
                self.links.append((self.href, text))
            self.href = None
            self.parts = []


class CareerCompetitionParser(HTMLParser):
    """Read the public competition cards whose title anchors use javascript:void()."""
    def __init__(self):
        super().__init__()
        self.card: dict | None = None
        self.title_parts: list[str] = []
        self.paragraph_parts: list[str] | None = None
        self.in_title = False
        self.cards: list[dict] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == "li":
            self._finish_card()
            self.card = {"id": "", "title": "", "paragraphs": []}
        elif self.card is not None and tag.lower() == "div":
            match = re.search(r"gotoDetail\((\d+)\)", attrs.get("onclick", ""))
            if match:
                self.card["id"] = match.group(1)
        elif self.card is not None and tag.lower() == "a" and "title" in attrs.get("class", "").split():
            self.in_title = True
            self.title_parts = []
        elif self.card is not None and tag.lower() == "p":
            self.paragraph_parts = []

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        if self.paragraph_parts is not None:
            self.paragraph_parts.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "a" and self.in_title:
            self.card["title"] = re.sub(r"\s+", " ", "".join(self.title_parts)).strip()
            self.in_title = False
            self.title_parts = []
        elif tag == "p" and self.paragraph_parts is not None:
            paragraph = re.sub(r"\s+", " ", "".join(self.paragraph_parts)).strip()
            if paragraph:
                self.card["paragraphs"].append(paragraph)
            self.paragraph_parts = None
        elif tag == "li":
            self._finish_card()

    def _finish_card(self):
        if self.card and self.card.get("id") and self.card.get("title"):
            self.cards.append(self.card)
        self.card = None
        self.in_title = False
        self.paragraph_parts = None


class ParagraphParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_p = 0
        self.skip = 0
        self.parts: list[str] = []
        self.paragraphs: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("script", "style", "nav", "footer"):
            self.skip += 1
        if tag == "p" and not self.skip:
            self.in_p += 1
            self.parts = []

    def handle_data(self, data):
        if self.in_p and not self.skip:
            self.parts.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "p" and self.in_p:
            text = re.sub(r"\s+", " ", "".join(self.parts)).strip()
            if text:
                self.paragraphs.append(text)
            self.in_p -= 1
            self.parts = []
        if tag in ("script", "style", "nav", "footer") and self.skip:
            self.skip -= 1


class VisibleTextParser(HTMLParser):
    """Collect readable page text for official pages with non-paragraph layouts."""
    def __init__(self):
        super().__init__()
        self.skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in ("script", "style", "noscript"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag.lower() in ("script", "style", "noscript") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            clean = re.sub(r"\s+", " ", data).strip()
            if clean:
                self.parts.append(clean)


def extract_lanqiao_registration_notice(html: str, now: datetime | None = None) -> Notice | None:
    """Turn the Lanqiao official registration window into a season-specific notice."""
    parser = VisibleTextParser()
    parser.feed(html)
    text = " ".join(parser.parts)
    match = re.search(
        r"报名时间\s*[：:]\s*(20\d{2})年(\d{1,2})月(\d{1,2})日\s*"
        r"(\d{1,2}:\d{2})\s*[—–-]\s*(20\d{2})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}:\d{2})",
        text,
    )
    if not match:
        return None
    start_year, start_month, start_day, start_time, end_year, end_month, end_day, end_time = match.groups()
    start_date = f"{start_year}-{int(start_month):02d}-{int(start_day):02d}"
    end_date = f"{end_year}-{int(end_month):02d}-{int(end_day):02d}"
    period = (f"{start_year}年{int(start_month)}月{int(start_day)}日 {start_time} 至 "
              f"{end_year}年{int(end_month)}月{int(end_day)}日 {end_time}")
    source_url = next(url for name, url, _ in SOURCES if name == "蓝桥杯大赛官网")
    now = now or datetime.now()
    start_at = datetime.strptime(f"{start_date} {start_time}", "%Y-%m-%d %H:%M")
    end_at = datetime.strptime(f"{end_date} {end_time}", "%Y-%m-%d %H:%M")
    if now < start_at:
        lifecycle_stage = "upcoming"
        title = f"{start_year}年蓝桥杯报名将于{int(start_month)}月{int(start_day)}日开始"
    elif now <= end_at:
        lifecycle_stage = "open"
        title = f"{start_year}年蓝桥杯报名已开放（截止{int(end_month)}月{int(end_day)}日）"
    else:
        lifecycle_stage = "closed"
        title = f"{start_year}年蓝桥杯报名已截止"
    notice_id = hashlib.sha256(f"{source_url}|{start_date}".encode("utf-8")).hexdigest()[:20]
    return Notice(
        id=notice_id,
        title=title,
        url=source_url,
        source="蓝桥杯大赛官网",
        date=start_date,
        summary=f"蓝桥杯官网报名时间：{period}。请登录官网查看赛项、组别和报名要求。",
        category="竞赛",
        detail_text=(f"官网公布的报名时间：{period}。报名截止日期："
                     f"{end_year}年{int(end_month)}月{int(end_day)}日 {end_time}。"
                     "报名、缴费及准考证等事项以蓝桥杯官网说明为准。"),
        hard_conditions=["报名组别及参赛资格请以蓝桥杯大赛官网当届通知为准。"],
        lifecycle_stage=lifecycle_stage,
    )


def extract_career_competition_notices(html: str, base_url: str) -> list[Notice]:
    parser = CareerCompetitionParser()
    parser.feed(html)
    if not parser.cards:
        raise ValueError("页面中没有可解析的赛事卡片，可能网站结构已变化")
    notices = []
    for card in parser.cards:
        if any(marker in card["title"] for marker in NON_ACTIONABLE_COMPETITION_MARKERS):
            continue
        detail = "\n".join(card["paragraphs"])
        url = urllib.parse.urljoin(base_url, f"/competition/view/competition_id/{card['id']}")
        notice_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
        notices.append(Notice(
            id=notice_id,
            title=card["title"],
            url=url,
            source="学生就业指导中心赛事",
            summary=detail[:220],
            category="竞赛",
            detail_text=detail,
        ))
    return notices


def _international_platform_date(value) -> str:
    if not isinstance(value, (int, float)) or value <= 0:
        return ""
    try:
        timestamp = value / 1000 if value > 100_000_000_000 else value
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return ""


def extract_international_internship_notices(payload: str) -> list[Notice]:
    try:
        rows = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("国际组织实习平台岗位接口返回了无效 JSON") from exc
    if not isinstance(rows, list):
        raise ValueError("国际组织实习平台岗位接口格式异常")

    notices = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        job_id = str(row.get("id", "")).strip()
        title = " ".join(html_lib.unescape(str(row.get("title", ""))).split())
        if not job_id or not title:
            continue
        job_url = urllib.parse.urljoin(
            INTERNATIONAL_INTERNSHIP_URL,
            f"/job/{urllib.parse.quote(job_id, safe='')}.html",
        )
        publish_date = _international_platform_date(row.get("publishDate"))
        deadline = _international_platform_date(row.get("endDate"))
        organization = " ".join(html_lib.unescape(str(row.get("orgName", ""))).split())
        location = " ".join(html_lib.unescape(str(row.get("address", ""))).split())
        details = [part for part in (organization, location) if part]
        if publish_date:
            details.append(f"发布日期：{publish_date}")
        if deadline:
            details.append(f"截止日期：{deadline}")
        summary = "；".join(details) or "岗位详情请打开国际组织实习平台原文查看。"
        notice_id = hashlib.sha256(job_url.encode("utf-8")).hexdigest()[:20]
        notices.append(Notice(
            id=notice_id,
            title=title,
            url=job_url,
            source=INTERNATIONAL_INTERNSHIP_SOURCE,
            date=publish_date,
            summary=summary,
            category="实习",
        ))
    return notices


def fetch_international_internship_notices() -> list[Notice]:
    endpoint = urllib.parse.urljoin(INTERNATIONAL_INTERNSHIP_URL, "/job/job.json")
    notices_by_id = {}
    for page in (0, 1):
        params = urllib.parse.urlencode({
            "page": page,
            "size": 20,
            "key": "",
            "orgField": "",
            "jobType": "实习",
            "jobField": "",
            "jobGrade": "",
            "eduLevel": "",
            "area": "",
        })
        notices = extract_international_internship_notices(fetch(f"{endpoint}?{params}"))
        notices_by_id.update((notice.id, notice) for notice in notices)
    if not notices_by_id:
        raise ValueError("国际组织实习平台未返回可解析岗位，可能接口或筛选结构已变化")
    return list(notices_by_id.values())


def fetch_national_internship_notices() -> list[Notice]:
    """Read a bounded set of active Guangdong internships from the official NCSS feed."""
    endpoint = "https://job.ncss.cn/student/jobs/jobslist/ajax/"
    notices_by_id: dict[str, Notice] = {}
    for page in (1, 2, 3):
        params = urllib.parse.urlencode({
            "jobType": "03",
            "areaCode": "44",
            "jobName": "",
            "monthPay": "",
            "industrySectors": "",
            "property": "",
            "categoryCode": "",
            "memberLevel": "",
            "offset": str(page),
            "limit": "20",
            "keyUnits": "",
            "degreeCode": "",
            "sourcesName": "0",
            "sourcesType": "",
        })
        request_url = f"{endpoint}?{params}"
        request = urllib.request.Request(request_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GDUT-Competition-Watcher/1.0",
            "Referer": NATIONAL_INTERNSHIP_URL,
        })
        payload = fetch(request.full_url)
        try:
            response = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("国家大学生就业服务平台实习接口返回了无效 JSON") from exc
        data = response.get("data") if isinstance(response, dict) else None
        rows = data.get("list") if isinstance(data, dict) else None
        if not isinstance(response, dict) or response.get("flag") is not True or not isinstance(rows, list):
            raise ValueError("国家大学生就业服务平台实习接口格式异常")
        for row in rows:
            if not isinstance(row, dict):
                continue
            job_id = str(row.get("jobId", "")).strip()
            title = " ".join(html_lib.unescape(str(row.get("jobName", ""))).split())
            if not job_id or not title:
                continue
            job_url = f"https://job.ncss.cn/student/jobs/{urllib.parse.quote(job_id, safe='')}/detail.html"
            company = " ".join(html_lib.unescape(str(row.get("recName", ""))).split())
            city = " ".join(html_lib.unescape(str(row.get("areaCodeName", ""))).split())
            salary_low, salary_high = row.get("lowMonthPay"), row.get("highMonthPay")
            salary = ""
            if isinstance(salary_low, (int, float)) and isinstance(salary_high, (int, float)):
                salary = "面议" if salary_low == salary_high == 0 else f"{salary_low:g}-{salary_high:g}K/月"
            education = " ".join(html_lib.unescape(str(row.get("degreeName", ""))).split())
            major = " ".join(html_lib.unescape(str(row.get("major", ""))).split())
            published = _international_platform_date(row.get("publishDate"))
            details = [part for part in (company, city, salary, education, f"专业：{major}" if major else "") if part]
            if published:
                details.append(f"发布日期：{published}")
            notice_id = hashlib.sha256(job_url.encode("utf-8")).hexdigest()[:20]
            notices_by_id[notice_id] = Notice(
                id=notice_id,
                title=title,
                url=job_url,
                source=NATIONAL_INTERNSHIP_SOURCE,
                date=published,
                summary="；".join(details),
                category="实习",
            )
    if not notices_by_id:
        raise ValueError("国家大学生就业服务平台未返回可解析的广东实习岗位")
    return list(notices_by_id.values())


def extract_national_job_detail_data(html: str, title: str) -> tuple[str, str, list[str]]:
    match = re.search(
        r'<pre\b[^>]*class=["\'][^"\']*\bmainContent\b[^"\']*["\'][^>]*>(.*?)</pre>',
        html,
        re.I | re.S,
    )
    if not match:
        raise ValueError("国家大学生就业服务平台职位详情中未找到岗位正文")
    body = re.sub(r"<br\s*/?>", "\n", match.group(1), flags=re.I)
    body = html_lib.unescape(re.sub(r"<[^>]+>", " ", body))
    lines = [re.sub(r"\s+", " ", line).strip() for line in body.splitlines()]
    lines = [line for line in lines if line]
    detail_text = "\n".join(lines)[:12000]
    if len(detail_text) < 20:
        raise ValueError("国家大学生就业服务平台职位正文为空")
    summary = " ".join(lines[:2])[:220]
    markers = ("要求", "须", "必须", "本科", "硕士", "博士", "专业", "出勤", "实习", "经验", "语言")
    conditions = [line for line in lines if any(marker in line for marker in markers)]
    if not conditions:
        conditions = ["岗位正文未明确提取到硬性条件，请打开原职位查看详情。"]
    return summary, detail_text, conditions[:20]


def log(text: str) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if LOG_FILE.exists() and LOG_FILE.stat().st_size >= MAX_LOG_BYTES:
        os.replace(LOG_FILE, LOG_FILE.with_name(f"{LOG_FILE.name}.1"))
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")


def _empty_state() -> dict:
    return {"initialized": False, "seen": [], "notices": [], "favorites": [], "last_check": "从未", "last_error": ""}


@contextmanager
def _named_mutex(scope: str, blocking: bool):
    """Coordinate short state transactions and long checks across app processes."""
    thread_lock = _THREAD_CHECK_LOCK if scope == "Check" else _THREAD_STATE_LOCK
    local_check_guard = False
    if scope == "Check":
        local_check_guard = thread_lock.acquire(blocking=False)
        if not local_check_guard:
            yield False
            return

    if os.name != "nt":
        if scope == "Check":
            try:
                yield True
            finally:
                thread_lock.release()
            return
        acquired = thread_lock.acquire(blocking=blocking)
        try:
            yield acquired
        finally:
            if acquired:
                thread_lock.release()
        return

    mutex_hash = hashlib.sha256(str(APP_DIR).casefold().encode("utf-8")).hexdigest()[:16]
    mutex_name = f"Local\\GDUTCompetitionWatcher.{scope}.{mutex_hash}"
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, mutex_name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "无法创建应用互斥锁")
        timeout = 10_000 if blocking else 0
        result = kernel32.WaitForSingleObject(handle, timeout)
        acquired = result in (0, 0x80)  # WAIT_OBJECT_0 or WAIT_ABANDONED
        if result == 0xFFFFFFFF:  # WAIT_FAILED
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise OSError(error, "无法等待应用互斥锁")
        if not acquired:
            kernel32.CloseHandle(handle)
            yield False
            return
        try:
            yield True
        finally:
            kernel32.ReleaseMutex(handle)
            kernel32.CloseHandle(handle)
    finally:
        if local_check_guard:
            thread_lock.release()


def _preserve_corrupt_state() -> None:
    if not STATE_FILE.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = STATE_FILE.with_name(f"state.corrupt-{stamp}.json")
    suffix = 1
    while backup.exists():
        backup = STATE_FILE.with_name(f"state.corrupt-{stamp}-{suffix}.json")
        suffix += 1
    os.replace(STATE_FILE, backup)
    log(f"状态文件格式无效，已保留副本：{backup.name}")


def load_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        valid_state = isinstance(state, dict)
        valid_state = valid_state and isinstance(state.get("notices", []), list)
        valid_state = valid_state and isinstance(state.get("profile", {}), dict)
        valid_state = valid_state and isinstance(state.get("seen", []), list)
        valid_state = valid_state and isinstance(state.get("favorites", []), list)
        valid_state = valid_state and (
            "schema_version" not in state
            or (isinstance(state["schema_version"], int) and not isinstance(state["schema_version"], bool))
        )
        valid_state = valid_state and ("initialized" not in state or isinstance(state["initialized"], bool))
        if valid_state:
            valid_state = all(isinstance(item, str) for item in state.get("seen", []))
            valid_state = valid_state and all(isinstance(item, str) for item in state.get("favorites", []))
            valid_state = valid_state and all(
                isinstance(notice, dict)
                and all(isinstance(notice.get(key), str) for key in ("id", "title", "url", "source"))
                for notice in state.get("notices", [])
            )
        profile = state.get("profile", {}) if isinstance(state, dict) else {}
        if valid_state:
            valid_state = all(value is None or isinstance(value, (str, int, float)) for value in profile.values())
        if not valid_state:
            _preserve_corrupt_state()
            return _empty_state()
        for key, value in profile.items():
            profile[key] = "" if value is None else str(value)
        for notice in state.get("notices", []):
            for key in (
                "date", "first_seen", "summary", "detail_text", "category",
                "lifecycle_stage", "detail_checked_at", "detail_attempted_at",
            ):
                if key in notice and not isinstance(notice[key], str):
                    notice[key] = str(notice[key] or "")
            hard_conditions = notice.get("hard_conditions", [])
            if not isinstance(hard_conditions, list):
                hard_conditions = []
            notice["hard_conditions"] = [item for item in hard_conditions if isinstance(item, str)]
            if notice.get("category") == "出国交流":
                notice["category"] = "出国"
            if not notice.get("hard_conditions"):
                notice["hard_conditions"] = ["该项目尚未从原通知中识别出明确硬性条件，请打开来源链接及附件人工核对。"]
        profile = state.get("profile", {})
        if profile.get("graduation_year") and not profile.get("enrollment_year"):
            try:
                profile["enrollment_year"] = str(int(profile["graduation_year"]) - 4)
            except (TypeError, ValueError):
                pass
            profile.pop("graduation_year", None)
        return state
    except FileNotFoundError:
        return _empty_state()
    except (json.JSONDecodeError, UnicodeDecodeError):
        _preserve_corrupt_state()
        return _empty_state()


def _write_state_atomic(state: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix="state-", suffix=".tmp", dir=APP_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, STATE_FILE)
    except Exception:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


def save_state(state: dict) -> None:
    with _named_mutex("State", blocking=True) as acquired:
        if not acquired:
            raise TimeoutError("等待状态文件写入锁超时，请稍后重试")
        _write_state_atomic(state)


def update_state(mutator) -> dict:
    """Apply a short read-modify-write transaction without losing another window's edits."""
    with _named_mutex("State", blocking=True) as acquired:
        if not acquired:
            raise TimeoutError("等待状态文件写入锁超时，请稍后重试")
        state = load_state()
        mutator(state)
        _write_state_atomic(state)
        return state


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _deadline_context_is_relevant(text: str, start: int, end: int) -> bool:
    prefix = text[max(0, start - 36):start]
    suffix = text[end:min(len(text), end + 18)]
    explicit_deadline_before = re.search(
        r"(?:报名|申报|申请|提交|材料)?(?:截止|截至)(?:时间|日期)?\s*"
        r"(?:(?:已)?(?:调整|变更|延期|延长)(?:为|至|到)?|为|是|：|:|到)?\s*$",
        prefix,
    )
    explicit_deadline_after = re.match(
        r"\s*(?:前|之前|截止(?:前)?|为止)(?:报名|申报|申请|提交|缴费)?",
        suffix,
    )
    return bool(explicit_deadline_before or explicit_deadline_after)


def _registration_range_context(text: str, start: int) -> bool:
    prefix = text[max(0, start - 30):start]
    return bool(re.search(r"(?:报名|申报|申请)(?:时间|日期|期限)\s*(?:为|是|：|:)?\s*$", prefix))


def extract_registration_deadline(notice: dict, today: date | None = None) -> date | None:
    """Extract the latest explicit registration/application deadline from notice text."""
    today = today or date.today()
    text = " ".join(
        str(notice.get(key, "") or "")
        for key in ("title", "summary", "detail_text")
    )
    if not text:
        return None

    deadlines: list[date] = []
    reference_year = today.year
    source_date = re.search(r"(20\d{2})", str(notice.get("date", "")))
    if source_date:
        reference_year = int(source_date.group(1))
    covered_ranges: list[tuple[int, int]] = []
    full_range_pattern = re.compile(
        r"(20\d{2})\s*(?:年|[-/.])\s*(\d{1,2})\s*(?:月|[-/.])\s*(\d{1,2})\s*(?:日|号)?\s*(?:\d{1,2}:\d{2})?\s*"
        r"(?:至|到|[-~～—])\s*(20\d{2})\s*(?:年|[-/.])\s*(\d{1,2})\s*(?:月|[-/.])\s*(\d{1,2})\s*(?:日|号)?"
    )
    for match in full_range_pattern.finditer(text):
        if not _registration_range_context(text, match.start()):
            continue
        parsed = _safe_date(int(match.group(4)), int(match.group(5)), int(match.group(6)))
        if parsed:
            deadlines.append(parsed)
            covered_ranges.append(match.span())
    cross_month_range_pattern = re.compile(
        r"(?:(20\d{2})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*(?:日|号)?\s*"
        r"(?:至|到|[-~～—])\s*(?:(20\d{2})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*(?:日|号)?"
    )
    for match in cross_month_range_pattern.finditer(text):
        if not _registration_range_context(text, match.start()):
            continue
        if any(start <= match.start() < end for start, end in covered_ranges):
            continue
        start_year = int(match.group(1) or reference_year)
        start_month = int(match.group(2))
        end_year = int(match.group(4) or start_year)
        end_month = int(match.group(5))
        if start_month == end_month:
            continue  # The same-month parser below handles this form.
        if not match.group(4) and end_month < start_month:
            end_year += 1
        parsed = _safe_date(end_year, end_month, int(match.group(6)))
        if parsed:
            deadlines.append(parsed)
            covered_ranges.append(match.span())
    range_same_month_pattern = re.compile(
        r"(?:(20\d{2})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*(?:日|号)?\s*"
        r"(?:至|到|[-~～—])\s*(?!20\d{2})(\d{1,2})\s*(?:日|号)?"
    )
    full_date_pattern = re.compile(r"(20\d{2})\s*[年\-/\.]\s*(\d{1,2})\s*[月\-/\.]\s*(\d{1,2})\s*(?:日|号)?")
    month_day_pattern = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*(?:日|号)?")

    for match in range_same_month_pattern.finditer(text):
        if not _registration_range_context(text, match.start()):
            continue
        parsed = _safe_date(int(match.group(1) or reference_year), int(match.group(2)), int(match.group(4)))
        if parsed:
            deadlines.append(parsed)
            covered_ranges.append(match.span())

    for match in full_date_pattern.finditer(text):
        if any(start <= match.start() < end for start, end in covered_ranges):
            continue
        if not _deadline_context_is_relevant(text, match.start(), match.end()):
            continue
        parsed = _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if parsed:
            deadlines.append(parsed)

    occupied_ranges = [match.span() for match in full_date_pattern.finditer(text)]
    for match in month_day_pattern.finditer(text):
        if any(start <= match.start() < end for start, end in occupied_ranges + covered_ranges):
            continue
        if not _deadline_context_is_relevant(text, match.start(), match.end()):
            continue
        parsed = _safe_date(reference_year, int(match.group(1)), int(match.group(2)))
        if parsed:
            deadlines.append(parsed)

    return max(deadlines) if deadlines else None


def notice_is_registration_expired(notice: dict, today: date | None = None) -> bool:
    today = today or date.today()
    deadline = extract_registration_deadline(notice, today)
    return bool(deadline and deadline < today)


def detail_refresh_due(
    last_checked_at: str,
    last_attempted_at: str = "",
    now: datetime | None = None,
) -> bool:
    now = now or datetime.now()
    if last_attempted_at:
        try:
            attempted_at = datetime.strptime(last_attempted_at, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            attempted_at = None
        if attempted_at and now - attempted_at < DETAIL_RETRY_INTERVAL:
            return False
    if not last_checked_at:
        return True
    try:
        checked_at = datetime.strptime(last_checked_at, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return True
    return now - checked_at >= DETAIL_REFRESH_INTERVAL


def select_detail_targets(notices: list[Notice]) -> list[Notice]:
    eligible = [
        n for n in notices
        if n.source != "蓝桥杯大赛官网"
        and (
            ((not n.detail_text or not n.hard_conditions) and detail_refresh_due("", n.detail_attempted_at))
            or detail_refresh_due(n.detail_checked_at, n.detail_attempted_at)
        )
    ]
    # 先处理未尝试项，再按截止日从近到远处理，避免旧失败项或远期岗位挡住紧急机会。
    eligible.sort(key=lambda n: (
        bool(n.detail_attempted_at),
        extract_registration_deadline(asdict(n)) or date.max,
        n.detail_attempted_at,
        n.first_seen,
        n.id,
    ))
    return eligible[:MAX_DETAIL_TARGETS]


def recently_expired_notice_ids(state: dict, notices: list[Notice], today: date | None = None) -> set[str]:
    today = today or date.today()
    cache = state.get("expired_checked_at", {})
    if not isinstance(cache, dict):
        return set()
    suppressed = set()
    for notice in notices:
        entry = cache.get(notice.id)
        if not isinstance(entry, dict) or detail_refresh_due(
            entry.get("checked_at", ""), entry.get("attempted_at", "")
        ):
            continue
        if entry.get("title") and entry["title"] != notice.title:
            continue
        deadline = extract_registration_deadline(asdict(notice), today)
        if deadline and deadline >= today:
            continue
        suppressed.add(notice.id)
    return suppressed


def notice_is_stale_competition(notice: dict, today: date | None = None) -> bool:
    today = today or date.today()
    if notice.get("category") != "竞赛":
        return False
    title = str(notice.get("title", ""))
    if any(marker in title for marker in NON_ACTIONABLE_COMPETITION_MARKERS):
        return True
    year = re.search(r"(?<!\d)(20\d{2})(?!\d)", title)
    return bool(year and int(year.group(1)) < today.year - 1)


def prune_irrelevant_notices(state: dict, today: date | None = None) -> list[str]:
    today = today or date.today()
    notices = state.get("notices", [])
    kept = [
        notice for notice in notices
        if not any(word in str(notice.get("title", "")) for word in IGNORE_WORDS)
        and not notice_is_stale_competition(notice, today)
    ]
    removed_ids = {notice.get("id") for notice in notices} - {notice.get("id") for notice in kept}
    if removed_ids:
        state["notices"] = kept
        state["favorites"] = [item for item in state.get("favorites", []) if item not in removed_ids]
    return sorted(item for item in removed_ids if item)


def prune_expired_notices(state: dict, today: date | None = None) -> list[str]:
    today = today or date.today()
    notices = state.get("notices", [])
    kept = [notice for notice in notices if not notice_is_registration_expired(notice, today)]
    removed_ids = {notice.get("id") for notice in notices} - {notice.get("id") for notice in kept}
    if removed_ids:
        state["notices"] = kept
        state["favorites"] = [item for item in state.get("favorites", []) if item not in removed_ids]
        state["expired_removed"] = sorted(removed_ids)
        checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cache = state.get("expired_checked_at", {})
        if not isinstance(cache, dict):
            cache = {}
        cache = {
            notice_id: entry
            for notice_id, entry in cache.items()
            if isinstance(notice_id, str) and isinstance(entry, dict)
        }
        removed_by_id = {notice.get("id"): notice for notice in notices if notice.get("id") in removed_ids}
        for notice_id in removed_ids:
            removed = removed_by_id[notice_id]
            cache[notice_id] = {
                "checked_at": removed.get("detail_checked_at") or checked_at,
                "attempted_at": removed.get("detail_attempted_at") or removed.get("detail_checked_at") or checked_at,
                "title": str(removed.get("title", "")),
            }
        if len(cache) > 500:
            cache = dict(sorted(cache.items(), key=lambda item: str(item[1].get("checked_at", "")))[-500:])
        state["expired_checked_at"] = cache
        state["seen"] = [item for item in state.get("seen", []) if item not in removed_ids]
    return sorted(item for item in removed_ids if item)


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GDUT-Competition-Watcher/1.0"
    })
    with urllib.request.urlopen(req, timeout=20) as response:
        raw = response.read(MAX_PAGE_BYTES + 1)
        if len(raw) > MAX_PAGE_BYTES:
            raise ValueError(f"网页超过 {MAX_PAGE_BYTES // (1024 * 1024)} MB，已停止读取")
        encoding = response.headers.get_content_charset() or "utf-8"
    try:
        html = raw.decode(encoding)
    except UnicodeDecodeError:
        html = raw.decode("gb18030", errors="replace")
    if re.search(r"安全验证|人机验证|请输入验证码|访问过于频繁|403 Forbidden|Access Denied", html, re.I):
        raise ValueError("网站返回了访问验证或拦截页面，暂时无法采集")
    if not html.strip():
        raise ValueError("网站返回空页面")
    return html


def classify(title: str, source_default: str | None) -> str | None:
    # 国际组织实习等交叉项目优先归到“实习”。
    normalized_title = title.casefold()
    if re.search(r"\bintern(?:ship)?\b", normalized_title):
        return "实习"
    for category in ("实习", "出国", "竞赛"):
        if any(k.casefold() in normalized_title for k in CATEGORY_KEYWORDS[category]):
            return category
    return source_default if source_default and any(k.casefold() in normalized_title for k in KEYWORDS) else None


def extract_notices(
    source: str,
    base_url: str,
    html: str,
    source_default: str | None = None,
    today: date | None = None,
) -> list[Notice]:
    today = today or date.today()
    parser = LinkParser()
    parser.feed(html)
    if not parser.links:
        raise ValueError("页面中没有可解析的链接，可能网站结构已变化或内容由脚本动态加载")
    found: dict[str, Notice] = {}
    for href, title in parser.links:
        title = re.sub(r"[\u200b\ufeff]", "", title).strip(" -|·")
        category = classify(title, source_default)
        if len(title) < 6 or not category:
            continue
        if any(k in title for k in IGNORE_WORDS):
            continue
        if notice_is_stale_competition({"category": category, "title": title}, today):
            continue
        url = urllib.parse.urljoin(base_url, href)
        if not url.startswith(("http://", "https://")):
            continue
        clean_url = url.split("#", 1)[0]
        nid = hashlib.sha256(clean_url.encode("utf-8")).hexdigest()[:20]
        date_match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", title)
        published_date = "-".join(date_match.groups()) if date_match else ""
        found[nid] = Notice(nid, title, clean_url, source, published_date, category=category)
    return list(found.values())


def extract_detail_data(html: str, title: str) -> tuple[str, str, list[str]]:
    if re.search(
        r'<div\b[^>]*class=["\'][^"\']*\baContent\b[^"\']*["\'][^>]*>'
        r'\s*<section\b[^>]*id=["\']content\d+["\'][^>]*>\s*</section>',
        html,
        re.I,
    ):
        return "学校原通知详情由动态区域加载，当前未能读取正文。", "", []
    parser = ParagraphParser()
    parser.feed(html)
    candidates = []
    for paragraph in parser.paragraphs:
        clean = paragraph.replace(title, "").strip(" ：:，,。")
        if len(clean) < 25:
            continue
        if any(x in clean for x in ("版权所有", "当前位置", "关闭窗口", "浏览次数")):
            continue
        candidates.append(clean)
    if not candidates:
        return ("学校网站暂未提供可提取的简介，请打开原通知查看详情。", "", [])
    detail_text = "\n".join(candidates)[:12000]
    summary = " ".join(candidates[:2])
    summary = summary[:220] + ("……" if len(summary) > 220 else "")
    condition_markers = (
        "参赛对象", "报名条件", "申请条件", "申请资格", "资格要求", "招募对象", "选拔条件",
        "报名要求", "申报条件", "基本要求", "任职要求", "岗位要求", "专业要求", "语言要求",
        "成绩要求", "报名资格", "申请人须", "申请者须", "应聘要求", "限本科", "限研究生",
        "qualifications", "requirements", "eligibility", "education", "experience",
        "languages", "competencies", "skills", "enrolled in", "currently pursuing",
        "applicants must", "must be enrolled", "minimum education", "degree in",
    )

    def has_condition_marker(value: str) -> bool:
        normalized = value.casefold()
        return any(marker.casefold() in normalized for marker in condition_markers)

    hard_conditions: list[str] = []
    capture_following = 0
    for paragraph in candidates:
        # 将过长段落按句号切开，保留包含条件标志的句子及其相邻短句。
        pieces = re.split(r"(?<=[。；;])", paragraph)
        paragraph_has_marker = has_condition_marker(paragraph)
        for piece in pieces:
            piece = piece.strip()
            should_capture = has_condition_marker(piece) or capture_following > 0
            if 8 <= len(piece) <= 500 and should_capture:
                if piece not in hard_conditions:
                    hard_conditions.append(piece)
        if paragraph_has_marker:
            capture_following = 3
        elif capture_following > 0:
            capture_following -= 1
    attachment_note = ""
    if re.search(r"附件|详见.*(?:PDF|报名表|通知)", detail_text, re.I):
        attachment_note = "部分要求可能位于附件或图片中，需打开原通知核对。"
    if attachment_note:
        hard_conditions.append(attachment_note)
    if not hard_conditions:
        hard_conditions.append("网页正文未明确列出硬性条件，请打开原通知确认是否有附件、图片或后续补充要求。")
    return summary, detail_text, hard_conditions[:20]


def check_all() -> tuple[list[Notice], list[str]]:
    notices: dict[str, Notice] = {}
    errors: list[str] = []

    def load_source(item):
        source, url, source_default = item
        try:
            if source == INTERNATIONAL_INTERNSHIP_SOURCE:
                return source, fetch_international_internship_notices(), ""
            if source == NATIONAL_INTERNSHIP_SOURCE:
                return source, fetch_national_internship_notices(), ""
            html = fetch(url)
            if source == "学生就业指导中心赛事":
                items = extract_career_competition_notices(html, url)
                parser = LinkParser()
                parser.feed(html)
                page_urls = []
                page_errors = []
                for href, _title in parser.links:
                    if re.search(r"/page/\d+(?:/|$)", href):
                        page_url = urllib.parse.urljoin(url, href)
                        if urllib.parse.urlparse(page_url).netloc == "career.gdut.edu.cn" and page_url not in page_urls:
                            page_urls.append(page_url)
                for page_url in page_urls[:3]:
                    try:
                        items.extend(extract_career_competition_notices(fetch(page_url), page_url))
                    except Exception as exc:
                        page_errors.append(f"分页读取失败：{exc}")
                return source, items, "；".join(page_errors)
            if source == "学生工作处":
                items = extract_notices(source, url, html, source_default)
                parser = LinkParser()
                parser.feed(html)
                page_urls = []
                page_errors = []
                for href, _title in parser.links:
                    if not re.search(r"(?:^|/)tzgg/\d+\.htm(?:$|[?#])", href):
                        continue
                    page_url = urllib.parse.urljoin(url, href)
                    parsed_url = urllib.parse.urlparse(page_url)
                    if parsed_url.netloc == "xsc.gdut.edu.cn" and page_url not in page_urls:
                        page_urls.append(page_url)
                for page_url in page_urls[:3]:
                    try:
                        items.extend(extract_notices(source, page_url, fetch(page_url), source_default))
                    except Exception as exc:
                        page_errors.append(f"分页读取失败：{exc}")
                return source, items, "；".join(page_errors)
            else:
                items = extract_notices(source, url, html, source_default)
            if source == "蓝桥杯大赛官网":
                registration_notice = extract_lanqiao_registration_notice(html)
                if registration_notice:
                    items.append(registration_notice)
            return source, items, ""
        except Exception as exc:
            return source, [], str(exc)

    with ThreadPoolExecutor(max_workers=min(6, len(SOURCES))) as pool:
        for source, items, error in pool.map(load_source, SOURCES):
            notices.update((item.id, item) for item in items)
            if error:
                errors.append(f"{source}: {error}")
                log(f"ERROR {source}: {error}")
    return list(notices.values()), errors


def notify_windows(title: str, message: str) -> None:
    try:
        from winotify import Notification
        toast = Notification(app_id=APP_NAME, title=title, msg=message, duration="long")
        toast.show()
    except Exception as exc:
        log(f"通知发送失败: {exc!r}")


@contextmanager
def _check_mutex():
    """Prevent a scheduled run and a manual run from overwriting each other's state."""
    with _named_mutex("Check", blocking=False) as acquired:
        yield acquired


def run_check(notify: bool = True) -> tuple[list[Notice], list[str], bool]:
    with _check_mutex() as acquired:
        if not acquired:
            log(CHECK_ALREADY_RUNNING)
            return [], [CHECK_ALREADY_RUNNING], False
        return _run_check_locked(notify)


def _run_check_locked(notify: bool = True) -> tuple[list[Notice], list[str], bool]:
    state = load_state()
    current, errors = check_all()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    seen = set(state.get("seen", []))
    first_run = not state.get("initialized", False) or state.get("schema_version", 1) < 2
    old_by_id = {n["id"]: n for n in state.get("notices", [])}
    expired_cache = state.get("expired_checked_at", {})
    if not isinstance(expired_cache, dict):
        expired_cache = {}
    legacy_expired = state.get("expired_removed", [])
    if isinstance(legacy_expired, list):
        titles_by_id = {n.id: n.title for n in current}
        last_check = state.get("last_check", "")
        for notice_id in legacy_expired:
            if isinstance(notice_id, str) and notice_id not in expired_cache:
                expired_cache[notice_id] = {
                    "checked_at": last_check if isinstance(last_check, str) else "",
                    "attempted_at": last_check if isinstance(last_check, str) else "",
                    "title": titles_by_id.get(notice_id, ""),
                }
    state["expired_removed"] = []
    state["expired_checked_at"] = expired_cache
    expired_recently = recently_expired_notice_ids(state, current)
    current = [n for n in current if n.id not in expired_recently]
    for n in current:
        old = old_by_id.get(n.id, {})
        n.first_seen = old.get("first_seen", now)
        if n.source != "蓝桥杯大赛官网":
            n.summary = n.summary or old.get("summary", "")
            n.detail_text = old.get("detail_text") or n.detail_text
            n.hard_conditions = old.get("hard_conditions") or n.hard_conditions
            n.detail_checked_at = old.get("detail_checked_at") or old.get("first_seen", "")
            n.detail_attempted_at = old.get("detail_attempted_at", "")
    # 先刷新详情已过期的项目，再按截止日清理，避免延期后仍按旧日期删除。
    detail_targets = select_detail_targets(current)
    def load_detail(n: Notice):
        try:
            detail_html = fetch(n.url)
            if n.source == NATIONAL_INTERNSHIP_SOURCE:
                summary, detail_text, hard_conditions = extract_national_job_detail_data(detail_html, n.title)
            else:
                summary, detail_text, hard_conditions = extract_detail_data(detail_html, n.title)
            if not detail_text:
                raise ValueError("通知正文没有可提取的有效内容")
            n.summary = summary
            n.detail_text = detail_text
            n.hard_conditions = hard_conditions
            n.detail_checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        except Exception as exc:
            n.summary = n.summary or "详情暂时无法读取，请打开学校原通知查看。"
            n.hard_conditions = n.hard_conditions or [
                "原通知详情暂时无法读取，硬性条件尚未完成采集；请打开来源链接人工核对。"
            ]
            log(f"简介提取失败 {n.url}: {exc!r}")
        finally:
            n.detail_attempted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return n

    if detail_targets:
        with ThreadPoolExecutor(max_workers=min(6, len(detail_targets))) as pool:
            list(pool.map(load_detail, detail_targets))
    expired_current = [n for n in current if notice_is_registration_expired(asdict(n))]
    expired_current_ids = {n.id for n in expired_current}
    for n in expired_current:
        old_by_id[n.id] = asdict(n)
    current = [n for n in current if n.id not in expired_current_ids]
    retired_without_reopen = set()
    for n in current:
        entry = expired_cache.get(n.id)
        if not isinstance(entry, dict):
            continue
        old_title = entry.get("title", "")
        title_changed = bool(old_title and old_title != n.title)
        deadline = extract_registration_deadline(asdict(n))
        if not title_changed and deadline is None:
            retired_without_reopen.add(n.id)
            expired_cache[n.id] = {
                "checked_at": n.detail_checked_at or now,
                "attempted_at": n.detail_attempted_at or entry.get("attempted_at") or now,
                "title": n.title or old_title,
            }
    reopened_ids = {
        n.id for n in current
        if isinstance(expired_cache.get(n.id), dict)
        and n.id not in retired_without_reopen
        and (
            bool(expired_cache[n.id].get("title") and expired_cache[n.id]["title"] != n.title)
            or extract_registration_deadline(asdict(n)) is not None
        )
    }
    current = [n for n in current if n.id not in retired_without_reopen]
    revised_ids = set()
    for n in current:
        old = old_by_id.get(n.id)
        if not old:
            continue
        title_changed = bool(old.get("title") and old["title"] != n.title)
        old_deadline = extract_registration_deadline(old)
        new_deadline = extract_registration_deadline(asdict(n))
        deadline_changed = old_deadline != new_deadline and (old_deadline is not None or new_deadline is not None)
        if title_changed or deadline_changed:
            revised_ids.add(n.id)

    new_items = [
        n for n in current
        if n.id not in seen
        or n.id in revised_ids
        or n.id in reopened_ids
        or (
            n.source == "蓝桥杯大赛官网"
            and n.lifecycle_stage == "open"
            and old_by_id.get(n.id, {}).get("lifecycle_stage") != "open"
        )
    ]
    for n in current:
        old_by_id[n.id] = asdict(n)
    merged = list(old_by_id.values())[-500:]
    state.update({
        "initialized": bool(current) or state.get("initialized", False),
        "seen": list((seen | {n.id for n in current}))[-2000:],
        "notices": merged,
        "last_check": now,
        "last_error": "；".join(errors),
        "schema_version": 2,
    })
    with _named_mutex("State", blocking=True) as acquired:
        if not acquired:
            raise TimeoutError("等待状态文件写入锁超时，请稍后重试")
        latest_state = load_state()
        state["profile"] = latest_state.get("profile", state.get("profile", {}))
        state["favorites"] = latest_state.get("favorites", state.get("favorites", []))
        for notice_id in reopened_ids:
            expired_cache.pop(notice_id, None)
        state["expired_checked_at"] = expired_cache
        removed_irrelevant = prune_irrelevant_notices(state)
        removed_expired = prune_expired_notices(state)
        _write_state_atomic(state)
    if notify and new_items and not first_run:
        preview = "；".join(n.title[:35] for n in new_items[:3])
        if len(new_items) > 3:
            preview += f"；另有 {len(new_items)-3} 条"
        counts: dict[str, int] = {}
        for item in new_items:
            counts[item.category] = counts.get(item.category, 0) + 1
        category_text = "、".join(f"{k}{v}条" for k, v in counts.items())
        notify_windows(f"发现 {len(new_items)} 条新消息（{category_text}）", preview)
    log(
        f"检查完成，发现 {len(current)} 条，新增 {0 if first_run else len(new_items)} 条，"
        f"清理过期 {len(removed_expired)} 条，清理归档/无关 {len(removed_irrelevant)} 条，错误 {len(errors)} 个"
    )
    return ([] if first_run else new_items), errors, first_run


PROFILE_FIELDS = (
    ("name", "姓名", ""),
    ("education", "当前学历", "本科生"),
    ("enrollment_year", "本科入学年份", ""),
    ("college", "学院", ""),
    ("major", "专业", ""),
    ("gpa", "GPA（满分4.0）", ""),
    ("cet4", "英语四级分数", ""),
    ("cet6", "英语六级分数", ""),
    ("ielts", "雅思分数", ""),
    ("toefl", "托福分数", ""),
    ("interests", "意向关键词", "人工智能,互联网,海外交流"),
)


def evaluate_notice(notice: dict, profile: dict) -> tuple[int, list[tuple[str, bool, bool]], bool]:
    """返回匹配分、(条件, 是否未达到, 是否硬条件)和硬条件不符标记。"""
    if not profile:
        return 50, [("尚未填写个人档案，暂时无法精确匹配", True, False)], False
    text = f"{notice.get('title', '')} {notice.get('detail_text') or notice.get('summary', '')}"
    conditions: list[tuple[str, bool, bool]] = []
    hard_fail = False
    score = 70
    education = profile.get("education", "")

    if re.search(r"仅限.{0,4}(硕士|博士|研究生)|参赛对象.{0,12}研究生", text):
        failed = education == "本科生"
        conditions.append(("要求研究生", failed, True))
        hard_fail |= failed
    if re.search(r"仅限.{0,4}本科|参赛对象.{0,12}本科生", text):
        failed = education not in ("本科生", "")
        conditions.append(("要求本科生", failed, True))
        hard_fail |= failed

    years = set(re.findall(r"(20\d{2})届", text))
    if years:
        enrollment_year = str(profile.get("enrollment_year", "")).strip()
        expected_graduation = ""
        try:
            expected_graduation = str(int(enrollment_year) + 4)
        except ValueError:
            pass
        failed = bool(expected_graduation) and expected_graduation not in years
        conditions.append((f"面向 {'/'.join(sorted(years))} 届（按本科入学年份推算：{expected_graduation or '未填写'}）", failed, True))
        hard_fail |= failed

    numeric_rules = (
        (r"(?:四级|CET[- ]?4).{0,12}?(\d{3})", "cet4", "英语四级"),
        (r"(?:六级|CET[- ]?6).{0,12}?(\d{3})", "cet6", "英语六级"),
        (r"(?:雅思|IELTS).{0,12}?(\d(?:\.\d)?)", "ielts", "雅思"),
        (r"(?:托福|TOEFL).{0,12}?(\d{2,3})", "toefl", "托福"),
        (r"GPA.{0,12}?(\d(?:\.\d)?)", "gpa", "GPA"),
    )
    for pattern, field, label in numeric_rules:
        match = re.search(pattern, text, re.I)
        if match:
            required = float(match.group(1))
            try:
                actual = float(profile.get(field) or 0)
            except ValueError:
                actual = 0
            failed = actual < required
            conditions.append((f"{label}要求 ≥ {required:g}（你的档案：{actual:g}）", failed, False))

    interests = [x.strip().lower() for x in re.split(r"[,，、;；]", profile.get("interests", "")) if x.strip()]
    matched = [x for x in interests if x in text.lower()]
    if matched:
        score += min(20, len(matched) * 5)
        conditions.append((f"符合意向：{'、'.join(matched)}", False, False))

    unmet_count = sum(1 for _, unmet, hard in conditions if unmet and not hard)
    score -= unmet_count * 10
    if hard_fail:
        score = min(score, 20)
    elif not any(unmet for _, unmet, _ in conditions):
        score += 10
    return max(0, min(100, score)), conditions, hard_fail


class ProfileDialog(tk.Toplevel):
    def __init__(self, parent, profile: dict, on_save):
        super().__init__(parent)
        self.title("个人档案")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.on_save = on_save
        self.vars: dict[str, tk.StringVar] = {}
        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        for row, (key, label, default) in enumerate(PROFILE_FIELDS):
            ttk.Label(body, text=label + "：").grid(row=row, column=0, sticky="e", padx=(0, 8), pady=4)
            var = tk.StringVar(value=str(profile.get(key, default)))
            self.vars[key] = var
            if key == "education":
                widget = ttk.Combobox(body, textvariable=var, state="readonly", width=31,
                                      values=("本科生", "硕士研究生", "博士研究生"))
            else:
                widget = ttk.Entry(body, textvariable=var, width=34)
            widget.grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Label(body, text="届别默认按本科四年制推算；意向关键词可用逗号分隔。档案仅保存在本机。", foreground="#666").grid(
            row=len(PROFILE_FIELDS), column=0, columnspan=2, pady=(8, 12))
        buttons = ttk.Frame(body)
        buttons.grid(row=len(PROFILE_FIELDS) + 1, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="left", padx=6)
        ttk.Button(buttons, text="保存并重新排序", command=self.save).pack(side="left")

    def save(self):
        profile = {key: var.get().strip() for key, var in self.vars.items()}
        self.on_save(profile)
        self.destroy()


class MainWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("1120x720")
        self.root.minsize(820, 560)
        self.root.configure(background="#f3f6fb")
        self.status = tk.StringVar()
        self.search_text = tk.StringVar()
        self._configure_style()
        self._build()
        self.refresh()

    def _configure_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("App.TFrame", background="#f3f6fb")
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("Header.TFrame", background="#173b67")
        style.configure("HeaderTitle.TLabel", background="#173b67", foreground="#ffffff",
                        font=("Microsoft YaHei UI", 19, "bold"))
        style.configure("HeaderHint.TLabel", background="#173b67", foreground="#c9d8eb",
                        font=("Microsoft YaHei UI", 9))
        style.configure("Section.TLabel", background="#ffffff", foreground="#213547",
                        font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Muted.TLabel", background="#f3f6fb", foreground="#63758b",
                        font=("Microsoft YaHei UI", 9))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 9, "bold"),
                        padding=(14, 8), background="#2474c6", foreground="#ffffff")
        style.map("Primary.TButton", background=[("active", "#1d63a9"), ("disabled", "#a8bfd8")])
        style.configure("TButton", font=("Microsoft YaHei UI", 9), padding=(10, 7))
        style.configure("TRadiobutton", background="#ffffff", foreground="#42566d",
                        font=("Microsoft YaHei UI", 9), padding=(5, 5))
        style.map("TRadiobutton", foreground=[("selected", "#1769aa")])
        style.configure("Treeview", background="#ffffff", fieldbackground="#ffffff",
                        foreground="#25364a", rowheight=34, font=("Microsoft YaHei UI", 9),
                        borderwidth=0)
        style.configure("Treeview.Heading", background="#eaf0f7", foreground="#43566c",
                        font=("Microsoft YaHei UI", 9, "bold"), padding=(8, 9), relief="flat")
        style.map("Treeview", background=[("selected", "#dcecff")],
                  foreground=[("selected", "#173b67")])
        style.map("Treeview.Heading", background=[("active", "#dce7f3")])
        style.configure("Vertical.TScrollbar", background="#dce5ef", troughcolor="#f3f6fb", borderwidth=0)

    def _build(self):
        shell = ttk.Frame(self.root, style="App.TFrame", padding=18)
        shell.pack(fill="both", expand=True)

        top = ttk.Frame(shell, style="Header.TFrame", padding=(22, 18))
        top.pack(fill="x", pady=(0, 14))
        title_box = ttk.Frame(top, style="Header.TFrame")
        title_box.pack(side="left", fill="x", expand=True)
        ttk.Label(title_box, text="广工机会提醒", style="HeaderTitle.TLabel").pack(anchor="w")
        ttk.Label(title_box, text="竞赛 · 实习 · 出国交流｜公开信息汇总与个人条件匹配",
                  style="HeaderHint.TLabel").pack(anchor="w", pady=(4, 0))
        actions = ttk.Frame(top, style="Header.TFrame")
        actions.pack(side="right")
        ttk.Button(actions, text="个人档案", command=self.open_profile).pack(side="left", padx=(0, 8))
        self.check_btn = ttk.Button(actions, text="立即检测", style="Primary.TButton", command=self.start_check)
        self.check_btn.pack(side="left")

        toolbar = ttk.Frame(shell, style="Card.TFrame", padding=(14, 8))
        toolbar.pack(fill="x", pady=(0, 10))
        ttk.Label(toolbar, text="分类", style="Section.TLabel").pack(side="left", padx=(0, 8))
        self.category_filter = tk.StringVar(value="全部")
        for category in ("全部", "竞赛", "实习", "出国"):
            ttk.Radiobutton(toolbar, text=category, value=category,
                            variable=self.category_filter, command=self.refresh).pack(side="left", padx=(0, 5))
        ttk.Label(toolbar, text="搜索", style="Section.TLabel").pack(side="left", padx=(18, 8))
        search = ttk.Entry(toolbar, textvariable=self.search_text, width=28)
        search.pack(side="left", ipady=4)
        self.search_text.trace_add("write", lambda *_args: self.refresh())
        ttk.Button(toolbar, text="清除", command=lambda: self.search_text.set("")).pack(side="left", padx=(6, 0))

        cols = ("favorite", "match", "category", "title", "source", "date", "seen")
        list_card = ttk.Frame(shell, style="Card.TFrame", padding=(10, 10, 4, 8))
        list_card.pack(fill="both", expand=True, pady=(0, 10))
        list_head = ttk.Frame(list_card, style="Card.TFrame")
        list_head.pack(fill="x", padx=(4, 8), pady=(0, 8))
        ttk.Label(list_head, text="机会列表", style="Section.TLabel").pack(side="left")
        self.count_label = ttk.Label(list_head, text="", style="Muted.TLabel")
        self.count_label.pack(side="right")
        table_frame = ttk.Frame(list_card, style="Card.TFrame")
        table_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("favorite", text="收藏")
        self.tree.heading("match", text="匹配度")
        self.tree.heading("category", text="分类")
        self.tree.heading("title", text="消息标题")
        self.tree.heading("source", text="来源链接")
        self.tree.heading("date", text="发布日期")
        self.tree.heading("seen", text="首次发现")
        self.tree.column("favorite", width=58, minwidth=48, anchor="center", stretch=False)
        self.tree.column("match", width=76, minwidth=65, anchor="center", stretch=False)
        self.tree.column("category", width=80, minwidth=70, anchor="center", stretch=False)
        self.tree.column("title", width=390, minwidth=220, stretch=True)
        self.tree.column("source", width=150, minwidth=110, anchor="center", stretch=False)
        self.tree.column("date", width=100, minwidth=85, anchor="center", stretch=False)
        self.tree.column("seen", width=165, minwidth=130, anchor="center", stretch=False)
        y_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=y_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        y_scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.open_selected)
        self.tree.bind("<ButtonRelease-1>", self.handle_click)
        self.tree.bind("<Button-3>", self.show_conditions_popup)
        self.tree.bind("<<TreeviewSelect>>", self.show_summary)
        self.tree.tag_configure("favorite", background="#fff6d8")
        self.tree.tag_configure("hard_fail", foreground="#b33a3a")

        summary_box = ttk.Frame(shell, style="Card.TFrame", padding=14)
        summary_box.pack(fill="x", pady=(0, 10))
        ttk.Label(summary_box, text="详情与匹配分析", style="Section.TLabel").pack(anchor="w", pady=(0, 8))
        self.summary = tk.Text(summary_box, height=5, wrap="word", relief="flat", padx=10, pady=8,
                               background="#f7f9fc", foreground="#34465b",
                               font=("Microsoft YaHei UI", 9), highlightthickness=0)
        self.summary.pack(fill="x")
        self.summary.insert("1.0", "选择一条竞赛消息，这里会显示简略介绍。")
        self.summary.tag_configure("link", foreground="#0563c1", underline=True)
        self.summary.tag_bind("link", "<Button-1>", self.open_summary_link)
        self.summary.tag_bind("link", "<Enter>", lambda _event: self.summary.config(cursor="hand2"))
        self.summary.tag_bind("link", "<Leave>", lambda _event: self.summary.config(cursor=""))
        self.summary.config(state="disabled")
        self.summary_link_url = ""

        bottom = ttk.Frame(shell, style="App.TFrame")
        bottom.pack(fill="x")
        ttk.Label(bottom, textvariable=self.status, style="Muted.TLabel").pack(side="left", fill="x", expand=True)
        ttk.Label(bottom, text="双击打开原通知 · 右键查看条件", style="Muted.TLabel").pack(side="left", padx=12)
        ttk.Button(bottom, text="打开日志", command=lambda: os.startfile(LOG_FILE) if LOG_FILE.exists() else None).pack(side="right")

    def refresh(self):
        state = load_state()
        self.tree.delete(*self.tree.get_children())
        notices = sorted(state.get("notices", []), key=lambda x: x.get("first_seen", ""), reverse=True)
        favorites = set(state.get("favorites", []))
        profile = state.get("profile", {})
        selected_category = self.category_filter.get() if hasattr(self, "category_filter") else "全部"
        visible = notices if selected_category == "全部" else [n for n in notices if n.get("category", "竞赛") == selected_category]
        query = self.search_text.get().strip().casefold()
        if query:
            visible = [n for n in visible if query in " ".join((n.get("title", ""), n.get("summary", ""), n.get("source", ""))).casefold()]
        ranked = []
        for n in visible:
            score, conditions, hard_fail = evaluate_notice(n, profile)
            ranked.append((hard_fail, -score, n, conditions))
        ranked.sort(key=lambda item: (item[0], item[1], item[2].get("first_seen", "")), reverse=False)
        for hard_fail, neg_score, n, _conditions in ranked:
            is_favorite = n["id"] in favorites
            tags = tuple(x for x, enabled in (("favorite", is_favorite), ("hard_fail", hard_fail)) if enabled)
            self.tree.insert("", "end", iid=n["id"], tags=tags,
                             values=(("●" if is_favorite else "○"), f"{-neg_score}%", n.get("category", "竞赛"), n["title"],
                                     f"{n['source']} ↗", n.get("date", ""), n.get("first_seen", "")))
        self.count_label.config(text=f"显示 {len(visible)} / 共 {len(notices)} 条")
        detail = f"上次检测：{state.get('last_check', '从未')}"
        if state.get("last_error"):
            detail += "　部分来源暂时无法访问"
        self.status.set(detail)

    def open_selected(self, _event=None):
        selected = self.tree.selection()
        if not selected:
            return
        nid = selected[0]
        for n in load_state().get("notices", []):
            if n["id"] == nid:
                webbrowser.open(n["url"])
                break

    def open_summary_link(self, _event=None):
        if self.summary_link_url:
            webbrowser.open(self.summary_link_url)

    def handle_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        row = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if not row:
            return
        if column == "#1":
            self.toggle_favorite(row)
        elif column == "#5":
            self.tree.selection_set(row)
            self.open_selected()

    def toggle_favorite(self, notice_id: str):
        def toggle(state):
            favorites = set(state.get("favorites", []))
            if notice_id in favorites:
                favorites.remove(notice_id)
            else:
                favorites.add(notice_id)
            state["favorites"] = sorted(favorites)

        try:
            update_state(toggle)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"保存收藏失败，请重试。\n{exc}")
            return
        self.refresh()
        if self.tree.exists(notice_id):
            self.tree.selection_set(notice_id)
            self.tree.focus(notice_id)

    def show_conditions_popup(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
        self.tree.focus(row)
        state = load_state()
        notice = next((n for n in state.get("notices", []) if n["id"] == row), None)
        if not notice:
            return
        score, conditions, hard_fail = evaluate_notice(notice, state.get("profile", {}))

        popup = tk.Toplevel(self.root)
        popup.title("条件匹配详情")
        popup.geometry(f"620x430+{event.x_root + 8}+{event.y_root + 8}")
        popup.transient(self.root)
        frame = ttk.Frame(popup, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=notice["title"], font=("Microsoft YaHei UI", 12, "bold"),
                  wraplength=570).pack(anchor="w")
        status_text = f"匹配度：{score}%"
        if hard_fail:
            status_text += "　存在硬性条件不符"
        ttk.Label(frame, text=status_text, foreground=("#c62828" if hard_fail else "#333")).pack(anchor="w", pady=(6, 12))

        detail = tk.Text(frame, wrap="word", relief="solid", borderwidth=1, padx=10, pady=10)
        detail.pack(fill="both", expand=True)
        detail.tag_configure("heading_ok", foreground="#237a2c", font=("Microsoft YaHei UI", 10, "bold"))
        detail.tag_configure("heading_bad", foreground="#c62828", font=("Microsoft YaHei UI", 10, "bold"))
        detail.tag_configure("ok", foreground="#237a2c")
        detail.tag_configure("bad", foreground="#d00000", font=("Microsoft YaHei UI", 9, "bold"))
        detail.tag_configure("source_condition", foreground="#7a4b00")
        met = [item for item in conditions if not item[1]]
        unmet = [item for item in conditions if item[1]]

        detail.insert("end", "原通知识别出的硬性条件\n", "heading_bad")
        source_conditions = notice.get("hard_conditions", [])
        if source_conditions:
            for condition in source_conditions:
                detail.insert("end", f"• {condition}\n", "source_condition")
        else:
            detail.insert("end", "未从网页正文识别到明确条件；请打开原通知及附件核对。\n", "source_condition")
        detail.insert("end", "\n")

        detail.insert("end", "已达成条件\n", "heading_ok")
        if met:
            for condition, _unmet, _hard in met:
                detail.insert("end", f"✓ {condition}\n", "ok")
        else:
            detail.insert("end", "暂无可确认的已达成条件\n", "ok")
        detail.insert("end", "\n还需达成条件\n", "heading_bad")
        if unmet:
            for condition, _unmet, is_hard in unmet:
                suffix = "【硬性条件】" if is_hard else ""
                detail.insert("end", f"✕ {condition}{suffix}\n", "bad")
        else:
            detail.insert("end", "当前没有识别到未达成条件\n", "ok")
        detail.insert("end", "\n判断依据来自网页公开文字；请以项目原通知为准。", "note")
        detail.tag_configure("note", foreground="#666")
        detail.config(state="disabled")
        ttk.Button(frame, text="打开原通知", command=lambda: webbrowser.open(notice["url"])).pack(side="left", pady=(10, 0))
        ttk.Button(frame, text="关闭", command=popup.destroy).pack(side="right", pady=(10, 0))

    def show_summary(self, _event=None):
        selected = self.tree.selection()
        if not selected:
            return
        nid = selected[0]
        text = ""
        selected_notice = None
        for n in load_state().get("notices", []):
            if n["id"] == nid:
                text = n.get("summary") or f"{n['source']}发布的竞赛相关通知。双击本条消息可查看参赛对象、报名方式和截止时间。"
                selected_notice = n
                break
        self.summary.config(state="normal")
        self.summary.delete("1.0", "end")
        self.summary_link_url = ""
        self.summary.insert("end", text + "\n\n")
        if selected_notice:
            url = selected_notice.get("url", "")
            if url:
                self.summary.insert("end", "原通知链接：")
                self.summary.insert("end", url + "\n\n", "link")
                self.summary_link_url = url
            state = load_state()
            score, conditions, hard_fail = evaluate_notice(selected_notice, state.get("profile", {}))
            self.summary.insert("end", f"匹配度：{score}%" + ("（存在硬性条件不符）" if hard_fail else "") + "\n")
            for condition, unmet, _hard in conditions:
                self.summary.insert("end", ("✕ " if unmet else "✓ ") + condition + "\n", "unmet" if unmet else "met")
        self.summary.tag_configure("unmet", foreground="#d00000", font=("Microsoft YaHei UI", 9, "bold"))
        self.summary.tag_configure("met", foreground="#287a30")
        self.summary.config(state="disabled")

    def open_profile(self):
        ProfileDialog(self.root, load_state().get("profile", {}), self.save_profile)

    def save_profile(self, profile: dict):
        try:
            update_state(lambda state: state.update({"profile": profile}))
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"保存个人档案失败，请重试。\n{exc}")
            return
        self.refresh()

    def start_check(self):
        self.check_btn.config(state="disabled")
        self.status.set("正在检查学校网站，请稍候……")
        threading.Thread(target=self._check_worker, daemon=True).start()

    def _check_worker(self):
        try:
            new_items, errors, first = run_check(notify=True)
        except Exception as exc:
            log(f"检测任务异常: {exc!r}")
            self.root.after(0, lambda: self._check_failed())
            return
        self.root.after(0, lambda: self._check_done(new_items, errors, first))

    def _check_failed(self):
        self.check_btn.config(state="normal")
        self.status.set("检测意外中断，详情已写入日志。")
        messagebox.showerror(APP_NAME, "检测过程中发生异常。请打开日志查看详情，然后重试。")

    def _check_done(self, new_items, errors, first):
        self.check_btn.config(state="normal")
        self.refresh()
        if errors == [CHECK_ALREADY_RUNNING]:
            self.status.set("另一项检测正在运行，本次已跳过。")
            messagebox.showinfo(APP_NAME, "另一项检测正在运行，本次已跳过。")
            return
        if first:
            messagebox.showinfo(APP_NAME, "首次检测完成，已建立消息基线。以后只提醒新增竞赛消息。")
        elif new_items:
            messagebox.showinfo(APP_NAME, f"检测完成，发现 {len(new_items)} 条新竞赛消息。")
        elif errors:
            messagebox.showwarning(APP_NAME, "检测完成，没有发现新消息；部分网站暂时无法访问，稍后会自动重试。")
        else:
            messagebox.showinfo(APP_NAME, "检测完成，没有发现新竞赛消息。")

    def run(self):
        self.root.mainloop()


def run_scheduled_check() -> int:
    try:
        _new_items, errors, _first_run = run_check(notify=True)
    except Exception as exc:
        log(f"计划检测异常: {exc!r}")
        return 1
    if errors == [CHECK_ALREADY_RUNNING]:
        return 0
    return 1 if errors else 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scheduled", action="store_true", help="执行一次静默检查并退出")
    args = parser.parse_args()
    if args.scheduled:
        return run_scheduled_check()
    else:
        MainWindow().run()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
