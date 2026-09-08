from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import re
import threading
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk


APP_NAME = "广工竞赛提醒"
APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "GDUTCompetitionWatcher"
STATE_FILE = APP_DIR / "state.json"
LOG_FILE = APP_DIR / "watcher.log"

SOURCES = [
    ("学校通知公告", "https://www.gdut.edu.cn/index/tzgg.htm", None),
    ("教务处", "https://jwc.gdut.edu.cn/", "竞赛"),
    ("学生工作处", "https://xsc.gdut.edu.cn/xwzx/tzgg.htm", None),
    ("国际合作与交流处", "https://wsc.gdut.edu.cn/", "出国"),
    ("就业信息网", "https://career.gdut.edu.cn/campus/index", "实习"),
]

CATEGORY_KEYWORDS = {
    "竞赛": (
    "竞赛", "大赛", "比赛", "挑战杯", "创新创业", "数学建模", "电子设计",
    "程序设计", "建模竞赛", "学科竞赛", "校赛", "选拔赛", "作品征集",
    ),
    "出国": (
        "交换生", "交流项目", "海外交流", "境外交流", "出国交流", "联合培养",
        "访学", "留学项目", "暑期学校", "冬季学校", "国际项目", "海外项目",
        "交流生", "研修项目", "名校探访",
    ),
    "实习": (
        "实习", "实习生", "暑期实习", "寒假实习", "实践项目", "训练营",
    ),
}
KEYWORDS = tuple(dict.fromkeys(k for values in CATEGORY_KEYWORDS.values() for k in values))
IGNORE_WORDS = ("教师教学创新大赛", "教职工", "体育比赛成绩")
DEADLINE_KEYWORDS = ("报名", "申报", "申请", "提交", "截止", "截至")


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

def log(text: str) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")


def load_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        for notice in state.get("notices", []):
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
    except (FileNotFoundError, json.JSONDecodeError):
        return {"initialized": False, "seen": [], "notices": [], "favorites": [], "last_check": "从未", "last_error": ""}


def save_state(state: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _deadline_context_is_relevant(text: str, start: int, end: int) -> bool:
    context = text[max(0, start - 24):min(len(text), end + 24)]
    return any(keyword in context for keyword in DEADLINE_KEYWORDS)


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
    range_same_month_pattern = re.compile(
        r"(?:(20\d{2})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*(?:日|号)?\s*"
        r"(?:至|到|[-~～—])\s*(?!20\d{2})(\d{1,2})\s*(?:日|号)?"
    )
    full_date_pattern = re.compile(r"(20\d{2})\s*[年\-/\.]\s*(\d{1,2})\s*[月\-/\.]\s*(\d{1,2})\s*(?:日|号)?")
    month_day_pattern = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*(?:日|号)?")

    for match in range_same_month_pattern.finditer(text):
        if not _deadline_context_is_relevant(text, match.start(), match.end()):
            continue
        parsed = _safe_date(int(match.group(1) or today.year), int(match.group(2)), int(match.group(4)))
        if parsed:
            deadlines.append(parsed)

    for match in full_date_pattern.finditer(text):
        if not _deadline_context_is_relevant(text, match.start(), match.end()):
            continue
        parsed = _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if parsed:
            deadlines.append(parsed)

    occupied_ranges = [match.span() for match in full_date_pattern.finditer(text)]
    for match in month_day_pattern.finditer(text):
        if any(start <= match.start() < end for start, end in occupied_ranges):
            continue
        if not _deadline_context_is_relevant(text, match.start(), match.end()):
            continue
        parsed = _safe_date(today.year, int(match.group(1)), int(match.group(2)))
        if parsed:
            deadlines.append(parsed)

    return max(deadlines) if deadlines else None


def notice_is_registration_expired(notice: dict, today: date | None = None) -> bool:
    today = today or date.today()
    deadline = extract_registration_deadline(notice, today)
    return bool(deadline and deadline < today)


def prune_expired_notices(state: dict, today: date | None = None) -> list[str]:
    today = today or date.today()
    notices = state.get("notices", [])
    kept = [notice for notice in notices if not notice_is_registration_expired(notice, today)]
    removed_ids = {notice.get("id") for notice in notices} - {notice.get("id") for notice in kept}
    if removed_ids:
        state["notices"] = kept
        state["favorites"] = [item for item in state.get("favorites", []) if item not in removed_ids]
        state["expired_removed"] = sorted(removed_ids)
    return sorted(item for item in removed_ids if item)


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GDUT-Competition-Watcher/1.0"
    })
    with urllib.request.urlopen(req, timeout=20) as response:
        raw = response.read()
        encoding = response.headers.get_content_charset() or "utf-8"
    try:
        return raw.decode(encoding)
    except UnicodeDecodeError:
        return raw.decode("gb18030", errors="replace")


def classify(title: str, source_default: str | None) -> str | None:
    # 国际组织实习等交叉项目优先归到“实习”。
    for category in ("实习", "出国", "竞赛"):
        if any(k in title for k in CATEGORY_KEYWORDS[category]):
            return category
    return source_default if source_default and any(k in title for k in KEYWORDS) else None


def extract_notices(source: str, base_url: str, html: str, source_default: str | None = None) -> list[Notice]:
    parser = LinkParser()
    parser.feed(html)
    found: dict[str, Notice] = {}
    for href, title in parser.links:
        title = re.sub(r"[\u200b\ufeff]", "", title).strip(" -|·")
        category = classify(title, source_default)
        if len(title) < 6 or not category:
            continue
        if any(k in title for k in IGNORE_WORDS):
            continue
        url = urllib.parse.urljoin(base_url, href)
        if not url.startswith(("http://", "https://")):
            continue
        clean_url = url.split("#", 1)[0]
        nid = hashlib.sha256(clean_url.encode("utf-8")).hexdigest()[:20]
        date_match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", title)
        date = "-".join(date_match.groups()) if date_match else ""
        found[nid] = Notice(nid, title, clean_url, source, date, category=category)
    return list(found.values())


def extract_detail_data(html: str, title: str) -> tuple[str, str, list[str]]:
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
    )
    hard_conditions: list[str] = []
    capture_following = 0
    for paragraph in candidates:
        # 将过长段落按句号切开，保留包含条件标志的句子及其相邻短句。
        pieces = re.split(r"(?<=[。；;])", paragraph)
        paragraph_has_marker = any(marker in paragraph for marker in condition_markers)
        for piece in pieces:
            piece = piece.strip()
            should_capture = any(marker in piece for marker in condition_markers) or capture_following > 0
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
            return source, extract_notices(source, url, fetch(url), source_default), ""
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


def run_check(notify: bool = True) -> tuple[list[Notice], list[str], bool]:
    state = load_state()
    current, errors = check_all()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    seen = set(state.get("seen", []))
    first_run = not state.get("initialized", False) or state.get("schema_version", 1) < 2
    old_by_id = {n["id"]: n for n in state.get("notices", [])}
    for n in current:
        old = old_by_id.get(n.id, {})
        n.first_seen = old.get("first_seen", now)
        n.summary = old.get("summary", "")
        n.detail_text = old.get("detail_text", "")
        n.hard_conditions = old.get("hard_conditions", [])
    # 新通知和尚未采集过正文的旧通知才读取详情，每轮最多30条。
    detail_targets = [n for n in current if not n.detail_text or not n.hard_conditions][:30]
    def load_detail(n: Notice):
        try:
            n.summary, n.detail_text, n.hard_conditions = extract_detail_data(fetch(n.url), n.title)
        except Exception as exc:
            n.summary = "详情暂时无法读取，请打开学校原通知查看。"
            n.hard_conditions = ["原通知详情暂时无法读取，硬性条件尚未完成采集；请打开来源链接人工核对。"]
            log(f"简介提取失败 {n.url}: {exc!r}")
        return n

    if detail_targets:
        with ThreadPoolExecutor(max_workers=min(6, len(detail_targets))) as pool:
            list(pool.map(load_detail, detail_targets))
    current = [n for n in current if not notice_is_registration_expired(asdict(n))]
    new_items = [n for n in current if n.id not in seen]
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
    removed_expired = prune_expired_notices(state)
    save_state(state)
    if notify and new_items and not first_run:
        preview = "；".join(n.title[:35] for n in new_items[:3])
        if len(new_items) > 3:
            preview += f"；另有 {len(new_items)-3} 条"
        counts: dict[str, int] = {}
        for item in new_items:
            counts[item.category] = counts.get(item.category, 0) + 1
        category_text = "、".join(f"{k}{v}条" for k, v in counts.items())
        notify_windows(f"发现 {len(new_items)} 条新消息（{category_text}）", preview)
    log(f"检查完成，发现 {len(current)} 条，新增 {0 if first_run else len(new_items)} 条，清理过期 {len(removed_expired)} 条，错误 {len(errors)} 个")
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
        self.root.geometry("920x560")
        self.root.minsize(720, 420)
        self.status = tk.StringVar()
        self._build()
        self.refresh()

    def _build(self):
        top = ttk.Frame(self.root, padding=12)
        top.pack(fill="x")
        ttk.Label(top, text="广工竞赛提醒", font=("Microsoft YaHei UI", 18, "bold")).pack(side="left")
        self.check_btn = ttk.Button(top, text="立即检测", command=self.start_check)
        self.check_btn.pack(side="right")
        ttk.Button(top, text="个人档案", command=self.open_profile).pack(side="right", padx=(0, 8))
        ttk.Label(self.root, textvariable=self.status, padding=(12, 0, 12, 8)).pack(fill="x")

        filter_bar = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        filter_bar.pack(fill="x")
        ttk.Label(filter_bar, text="分类：").pack(side="left", padx=(0, 6))
        self.category_filter = tk.StringVar(value="全部")
        for category in ("全部", "竞赛", "实习", "出国"):
            ttk.Radiobutton(filter_bar, text=category, value=category,
                            variable=self.category_filter, command=self.refresh).pack(side="left", padx=(0, 12))

        cols = ("favorite", "match", "category", "title", "source", "date", "seen")
        self.tree = ttk.Treeview(self.root, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("favorite", text="收藏")
        self.tree.heading("match", text="匹配度")
        self.tree.heading("category", text="分类")
        self.tree.heading("title", text="消息标题")
        self.tree.heading("source", text="来源链接")
        self.tree.heading("date", text="发布日期")
        self.tree.heading("seen", text="首次发现")
        self.tree.column("favorite", width=55, anchor="center", stretch=False)
        self.tree.column("match", width=65, anchor="center", stretch=False)
        self.tree.column("category", width=70, anchor="center")
        self.tree.column("title", width=290)
        self.tree.column("source", width=120, anchor="center")
        self.tree.column("date", width=90, anchor="center")
        self.tree.column("seen", width=150, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=12)
        self.tree.bind("<Double-1>", self.open_selected)
        self.tree.bind("<ButtonRelease-1>", self.handle_click)
        self.tree.bind("<Button-3>", self.show_conditions_popup)
        self.tree.bind("<<TreeviewSelect>>", self.show_summary)
        self.tree.tag_configure("favorite", background="#fff2a8")
        self.tree.tag_configure("hard_fail", foreground="#c62828")

        summary_box = ttk.LabelFrame(self.root, text="简略介绍与条件分析", padding=10)
        summary_box.pack(fill="x", padx=12, pady=(8, 0))
        self.summary = tk.Text(summary_box, height=6, wrap="word", relief="flat", background="#f4f4f4")
        self.summary.pack(fill="x")
        self.summary.insert("1.0", "选择一条竞赛消息，这里会显示简略介绍。")
        self.summary.tag_configure("link", foreground="#0563c1", underline=True)
        self.summary.tag_bind("link", "<Button-1>", self.open_summary_link)
        self.summary.tag_bind("link", "<Enter>", lambda _event: self.summary.config(cursor="hand2"))
        self.summary.tag_bind("link", "<Leave>", lambda _event: self.summary.config(cursor=""))
        self.summary.config(state="disabled")
        self.summary_link_url = ""

        bottom = ttk.Frame(self.root, padding=12)
        bottom.pack(fill="x")
        ttk.Label(bottom, text="双击消息打开学校原通知。计划任务每天运行两次，完成后自动退出。").pack(side="left")
        ttk.Button(bottom, text="打开日志", command=lambda: os.startfile(LOG_FILE) if LOG_FILE.exists() else None).pack(side="right")

    def refresh(self):
        state = load_state()
        self.tree.delete(*self.tree.get_children())
        notices = sorted(state.get("notices", []), key=lambda x: x.get("first_seen", ""), reverse=True)
        favorites = set(state.get("favorites", []))
        profile = state.get("profile", {})
        selected_category = self.category_filter.get() if hasattr(self, "category_filter") else "全部"
        visible = notices if selected_category == "全部" else [n for n in notices if n.get("category", "竞赛") == selected_category]
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
        detail = f"上次检测：{state.get('last_check', '从未')}　当前显示：{len(visible)} 条　全部：{len(notices)} 条"
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
        state = load_state()
        favorites = set(state.get("favorites", []))
        if notice_id in favorites:
            favorites.remove(notice_id)
        else:
            favorites.add(notice_id)
        state["favorites"] = sorted(favorites)
        save_state(state)
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
        state = load_state()
        state["profile"] = profile
        save_state(state)
        self.refresh()

    def start_check(self):
        self.check_btn.config(state="disabled")
        self.status.set("正在检查学校网站，请稍候……")
        threading.Thread(target=self._check_worker, daemon=True).start()

    def _check_worker(self):
        new_items, errors, first = run_check(notify=True)
        self.root.after(0, lambda: self._check_done(new_items, errors, first))

    def _check_done(self, new_items, errors, first):
        self.check_btn.config(state="normal")
        self.refresh()
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scheduled", action="store_true", help="执行一次静默检查并退出")
    args = parser.parse_args()
    if args.scheduled:
        run_check(notify=True)
    else:
        MainWindow().run()


if __name__ == "__main__":
    main()
