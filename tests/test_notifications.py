import json
import tempfile
import threading
import unittest
from dataclasses import asdict
from datetime import date, datetime, timedelta
from email.message import Message
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from src import gdut_competition_watcher as watcher


LANQIAO_HTML = """
<html><body><p>报名时间：2026年10月8日 10:00—2026年12月18日 18:00</p></body></html>
"""


class LanqiaoRegistrationNotificationTests(unittest.TestCase):
    def test_opening_registration_after_seen_upcoming_notice_triggers_once(self):
        upcoming = watcher.extract_lanqiao_registration_notice(
            LANQIAO_HTML, datetime(2026, 10, 8, 9, 59)
        )
        opened = watcher.extract_lanqiao_registration_notice(
            LANQIAO_HTML, datetime(2026, 10, 8, 10, 0)
        )
        self.assertEqual(upcoming.id, opened.id)
        self.assertEqual(upcoming.lifecycle_stage, "upcoming")
        self.assertEqual(opened.lifecycle_stage, "open")

        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            state_file = app_dir / "state.json"
            log_file = app_dir / "watcher.log"
            state = {
                "initialized": True,
                "schema_version": 2,
                "seen": [upcoming.id],
                "notices": [asdict(upcoming)],
                "favorites": [],
                "profile": {},
            }
            state_file.write_text(json.dumps(state), encoding="utf-8")
            with (
                patch.object(watcher, "APP_DIR", app_dir),
                patch.object(watcher, "STATE_FILE", state_file),
                patch.object(watcher, "LOG_FILE", log_file),
                patch.object(watcher, "check_all", return_value=([opened], [])),
                patch.object(watcher, "fetch") as fetch,
                patch.object(watcher, "notify_windows") as notify,
            ):
                new_items, errors, first_run = watcher.run_check()
                saved_state = watcher.load_state()
                self.assertFalse(errors)
                self.assertFalse(first_run)
                self.assertEqual([item.lifecycle_stage for item in new_items], ["open"])
                notify.assert_called_once()
                fetch.assert_not_called()

                new_items, _, _ = watcher.run_check()
                self.assertEqual(new_items, [])
                notify.assert_called_once()


class CheckCoordinationTests(unittest.TestCase):
    def test_seen_notice_deadline_change_is_refreshed_and_notified_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            state_file = app_dir / "state.json"
            notice_id = "stable-url-id"
            checked_at = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
            old_notice = watcher.Notice(
                id=notice_id,
                title="校内创新竞赛报名通知",
                url="https://example.com/event",
                source="测试来源",
                category="竞赛",
                summary="报名截止时间为2026年10月12日。",
                detail_text="报名截止时间为2026年10月12日，请按通知要求提交参赛材料。",
                hard_conditions=["本科生可报名"],
                detail_checked_at=checked_at,
            )
            state_file.write_text(json.dumps({
                "initialized": True,
                "schema_version": 2,
                "seen": [notice_id],
                "notices": [asdict(old_notice)],
                "favorites": [],
                "profile": {},
            }), encoding="utf-8")
            current_notice = watcher.Notice(
                id=notice_id,
                title=old_notice.title,
                url=old_notice.url,
                source=old_notice.source,
                category=old_notice.category,
            )
            revised_html = (
                "<html><body><p>报名截止时间调整为2026年10月20日，"
                "请参赛队按新时间提交报名表和相关材料。</p></body></html>"
            )

            with (
                patch.object(watcher, "APP_DIR", app_dir),
                patch.object(watcher, "STATE_FILE", state_file),
                patch.object(watcher, "LOG_FILE", app_dir / "watcher.log"),
                patch.object(watcher, "check_all", return_value=([current_notice], [])),
                patch.object(watcher, "fetch", return_value=revised_html) as fetch,
                patch.object(watcher, "notify_windows") as notify,
            ):
                new_items, errors, first_run = watcher.run_check()
                self.assertEqual(errors, [])
                self.assertFalse(first_run)
                self.assertEqual([item.id for item in new_items], [notice_id])
                self.assertEqual(notify.call_count, 1)
                saved_notice = watcher.load_state()["notices"][0]
                self.assertIn("10月20日", saved_notice["detail_text"])

                new_items, _, _ = watcher.run_check()
                self.assertEqual(new_items, [])
                self.assertEqual(fetch.call_count, 1)
                notify.assert_called_once()

    def test_failed_detail_refresh_preserves_saved_content_and_retries_next_day(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            state_file = app_dir / "state.json"
            notice_id = "detail-refresh-failure"
            checked_at = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
            old_notice = watcher.Notice(
                id=notice_id,
                title="校内创新竞赛报名通知",
                url="https://example.com/event",
                source="测试来源",
                category="竞赛",
                summary="原摘要：本科生组报名截止时间尚未变化。",
                detail_text="原正文内容：竞赛面向本科生开放报名，截止日期以学校通知为准。",
                hard_conditions=["原参赛条件：本科生可报名。"],
                detail_checked_at=checked_at,
            )
            state_file.write_text(json.dumps({
                "initialized": True,
                "schema_version": 2,
                "seen": [notice_id],
                "notices": [asdict(old_notice)],
                "favorites": [],
                "profile": {},
            }), encoding="utf-8")
            current_notice = watcher.Notice(
                id=notice_id,
                title=old_notice.title,
                url=old_notice.url,
                source=old_notice.source,
                category=old_notice.category,
            )

            with (
                patch.object(watcher, "APP_DIR", app_dir),
                patch.object(watcher, "STATE_FILE", state_file),
                patch.object(watcher, "LOG_FILE", app_dir / "watcher.log"),
                patch.object(watcher, "check_all", return_value=([current_notice], [])),
                patch.object(watcher, "fetch", side_effect=OSError("offline")) as fetch,
            ):
                watcher.run_check(notify=False)
                saved = watcher.load_state()["notices"][0]
                self.assertEqual(saved["summary"], old_notice.summary)
                self.assertEqual(saved["detail_text"], old_notice.detail_text)
                self.assertEqual(saved["hard_conditions"], old_notice.hard_conditions)
                self.assertTrue(saved["detail_attempted_at"])

                watcher.run_check(notify=False)
                fetch.assert_called_once()

    def test_expired_notice_is_refreshed_weekly_without_daily_repeat_fetches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            state_file = app_dir / "state.json"
            notice_id = "expired-stable-url"
            checked_at = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
            state_file.write_text(json.dumps({
                "initialized": True,
                "schema_version": 2,
                "seen": [],
                "notices": [],
                "favorites": [],
                "profile": {},
                "expired_checked_at": {
                    notice_id: {"checked_at": checked_at, "title": "旧竞赛报名通知"}
                },
            }), encoding="utf-8")
            notice = watcher.Notice(
                id=notice_id,
                title="旧竞赛报名通知",
                url="https://example.com/expired",
                source="测试来源",
            )
            yesterday = date.today() - timedelta(days=1)
            deadline = f"{yesterday.year}年{yesterday.month}月{yesterday.day}日"
            expired_html = (
                f"<html><body><p>报名截止时间为{deadline}，"
                "请参赛队按通知要求提交报名表和相关材料。</p></body></html>"
            )

            with (
                patch.object(watcher, "APP_DIR", app_dir),
                patch.object(watcher, "STATE_FILE", state_file),
                patch.object(watcher, "LOG_FILE", app_dir / "watcher.log"),
                patch.object(watcher, "check_all", return_value=([notice], [])),
                patch.object(watcher, "fetch", return_value=expired_html) as fetch,
            ):
                watcher.run_check(notify=False)
                state = watcher.load_state()
                self.assertEqual(state["notices"], [])
                self.assertIn(notice_id, state["expired_checked_at"])
                watcher.run_check(notify=False)
                fetch.assert_called_once()

    def test_legacy_expired_notice_with_extended_deadline_reopens_and_notifies(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            state_file = app_dir / "state.json"
            notice_id = "legacy-expired-url"
            last_check = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
            state_file.write_text(json.dumps({
                "initialized": True,
                "schema_version": 2,
                "seen": [notice_id],
                "notices": [],
                "favorites": [],
                "profile": {},
                "expired_removed": [notice_id],
                "last_check": last_check,
            }), encoding="utf-8")
            notice = watcher.Notice(
                id=notice_id,
                title="2026年创新竞赛报名通知",
                url="https://example.com/reopened",
                source="测试来源",
            )
            future = date.today() + timedelta(days=7)
            deadline = f"{future.year}年{future.month}月{future.day}日"
            revised_html = (
                f"<html><body><p>报名截止时间调整为{deadline}，"
                "请参赛队按新时间提交报名表和相关材料。</p></body></html>"
            )

            with (
                patch.object(watcher, "APP_DIR", app_dir),
                patch.object(watcher, "STATE_FILE", state_file),
                patch.object(watcher, "LOG_FILE", app_dir / "watcher.log"),
                patch.object(watcher, "check_all", return_value=([notice], [])),
                patch.object(watcher, "fetch", return_value=revised_html),
                patch.object(watcher, "notify_windows") as notify,
            ):
                new_items, errors, first_run = watcher.run_check()
                saved_state = watcher.load_state()

            self.assertEqual(errors, [])
            self.assertFalse(first_run)
            self.assertEqual([item.id for item in new_items], [notice_id])
            self.assertEqual(notify.call_count, 1)
            self.assertNotIn(notice_id, saved_state.get("expired_checked_at", {}))

    def test_check_mutex_allows_one_run_and_skips_overlap(self):
        with watcher._check_mutex() as first:
            self.assertTrue(first)
            with watcher._check_mutex() as second:
                self.assertFalse(second)

    def test_run_check_does_not_start_a_second_worker_when_locked(self):
        with watcher._check_mutex():
            with patch.object(watcher, "log"):
                new_items, errors, first_run = watcher.run_check()
        self.assertEqual(new_items, [])
        self.assertEqual(errors, [watcher.CHECK_ALREADY_RUNNING])
        self.assertFalse(first_run)

    def test_scheduled_check_reports_source_errors_but_ignores_overlap(self):
        with patch.object(watcher, "run_check", return_value=([], ["source offline"], False)):
            self.assertEqual(watcher.run_scheduled_check(), 1)
        with patch.object(
            watcher, "run_check", return_value=([], [watcher.CHECK_ALREADY_RUNNING], False)
        ):
            self.assertEqual(watcher.run_scheduled_check(), 0)
        with patch.object(watcher, "run_check", side_effect=OSError("state unavailable")):
            with patch.object(watcher, "log"):
                self.assertEqual(watcher.run_scheduled_check(), 1)

    def test_check_commit_keeps_profile_and_favorites_saved_during_fetch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            state_file = app_dir / "state.json"
            state_file.write_text(json.dumps({
                "initialized": True,
                "schema_version": 2,
                "seen": [],
                "notices": [],
                "favorites": [],
                "profile": {"education": "本科生"},
            }), encoding="utf-8")
            notice = watcher.Notice(
                id="new-notice", title="校内创新竞赛报名通知", url="https://example.com/event",
                source="测试来源", summary="报名摘要", detail_text="报名时间和参赛要求。",
                hard_conditions=["本科生可报名"],
            )

            def save_edits_during_fetch():
                watcher.update_state(lambda state: state.update({
                    "profile": {"education": "本科生", "major": "计算机"},
                    "favorites": ["user-favorite"],
                }))
                return [notice], []

            with (
                patch.object(watcher, "APP_DIR", app_dir),
                patch.object(watcher, "STATE_FILE", state_file),
                patch.object(watcher, "LOG_FILE", app_dir / "watcher.log"),
                patch.object(watcher, "check_all", side_effect=save_edits_during_fetch),
            ):
                watcher.run_check(notify=False)
                saved = watcher.load_state()

        self.assertEqual(saved["profile"]["major"], "计算机")
        self.assertEqual(saved["favorites"], ["user-favorite"])
        self.assertEqual([item["id"] for item in saved["notices"]], ["new-notice"])


class StateFileSafetyTests(unittest.TestCase):
    def test_concurrent_state_transactions_preserve_both_updates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            state_file = app_dir / "state.json"
            with (
                patch.object(watcher, "APP_DIR", app_dir),
                patch.object(watcher, "STATE_FILE", state_file),
                patch.object(watcher, "LOG_FILE", app_dir / "watcher.log"),
            ):
                watcher.save_state({"initialized": True, "profile": {}, "favorites": []})
                barrier = threading.Barrier(2)

                def write_profile(key):
                    barrier.wait()
                    watcher.update_state(lambda state: state.setdefault("profile", {}).update({key: key}))

                workers = [threading.Thread(target=write_profile, args=(key,)) for key in ("major", "college")]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(timeout=5)
                self.assertTrue(all(not worker.is_alive() for worker in workers))
                saved = watcher.load_state()

        self.assertEqual(saved["profile"], {"major": "major", "college": "college"})

    def test_corrupt_state_is_preserved_and_save_replaces_atomically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            state_file = app_dir / "state.json"
            log_file = app_dir / "watcher.log"
            state_file.write_text('{"notices": [', encoding="utf-8")
            with (
                patch.object(watcher, "APP_DIR", app_dir),
                patch.object(watcher, "STATE_FILE", state_file),
                patch.object(watcher, "LOG_FILE", log_file),
            ):
                state = watcher.load_state()
                self.assertFalse(state["initialized"])
                backups = list(app_dir.glob("state.corrupt-*.json"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_text(encoding="utf-8"), '{"notices": [')

                watcher.save_state({"initialized": True, "notices": []})
                self.assertTrue(json.loads(state_file.read_text(encoding="utf-8"))["initialized"])
                self.assertEqual(list(app_dir.glob("state-*.tmp")), [])

    def test_valid_json_with_invalid_state_shape_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            state_file = app_dir / "state.json"
            state_file.write_text('{"notices": [null]}', encoding="utf-8")
            with (
                patch.object(watcher, "APP_DIR", app_dir),
                patch.object(watcher, "STATE_FILE", state_file),
                patch.object(watcher, "LOG_FILE", app_dir / "watcher.log"),
            ):
                self.assertFalse(watcher.load_state()["initialized"])
                backups = list(app_dir.glob("state.corrupt-*.json"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_text(encoding="utf-8"), '{"notices": [null]}')

    def test_invalid_schema_version_is_preserved_before_reset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            state_file = app_dir / "state.json"
            malformed = '{"initialized": true, "schema_version": "two", "notices": []}'
            state_file.write_text(malformed, encoding="utf-8")
            with (
                patch.object(watcher, "APP_DIR", app_dir),
                patch.object(watcher, "STATE_FILE", state_file),
                patch.object(watcher, "LOG_FILE", app_dir / "watcher.log"),
            ):
                self.assertFalse(watcher.load_state()["initialized"])
                backups = list(app_dir.glob("state.corrupt-*.json"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_text(encoding="utf-8"), malformed)

    def test_invalid_utf8_state_is_preserved_before_reset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            state_file = app_dir / "state.json"
            malformed = b'{"initialized": true, "title": "\xff"}'
            state_file.write_bytes(malformed)
            with (
                patch.object(watcher, "APP_DIR", app_dir),
                patch.object(watcher, "STATE_FILE", state_file),
                patch.object(watcher, "LOG_FILE", app_dir / "watcher.log"),
            ):
                self.assertFalse(watcher.load_state()["initialized"])
                backups = list(app_dir.glob("state.corrupt-*.json"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_bytes(), malformed)

    def test_non_text_hard_conditions_are_dropped_without_losing_notice(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            state_file = app_dir / "state.json"
            state_file.write_text(json.dumps({
                "initialized": True,
                "schema_version": 2,
                "notices": [{
                    "id": "notice-1", "title": "竞赛通知", "url": "https://example.com",
                    "source": "测试来源", "hard_conditions": ["本科生可报名", {"bad": "value"}],
                }],
            }), encoding="utf-8")
            with (
                patch.object(watcher, "APP_DIR", app_dir),
                patch.object(watcher, "STATE_FILE", state_file),
                patch.object(watcher, "LOG_FILE", app_dir / "watcher.log"),
            ):
                notice = watcher.load_state()["notices"][0]
            self.assertEqual(notice["hard_conditions"], ["本科生可报名"])

    def test_log_rotates_at_the_size_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            log_file = app_dir / "watcher.log"
            log_file.write_text("x" * 20, encoding="utf-8")
            with (
                patch.object(watcher, "APP_DIR", app_dir),
                patch.object(watcher, "LOG_FILE", log_file),
                patch.object(watcher, "MAX_LOG_BYTES", 16),
            ):
                watcher.log("new entry")
            self.assertEqual(log_file.with_name("watcher.log.1").read_text(encoding="utf-8"), "x" * 20)
            self.assertIn("new entry", log_file.read_text(encoding="utf-8"))


class SourceHealthTests(unittest.TestCase):
    @staticmethod
    def _career_page(title, event_id, pagination=""):
        return f"""
        <ul><li><div class="state-column" onclick="gotoDetail({event_id})">
        <a href="javascript:void()" class="title">{title}</a>
        <p>报名时间：2026-10-01 09:00 至 2026-10-20 18:00</p>
        </div></li></ul>{pagination}
        """

    def test_engineering_training_competition_source_parses_competition_notice(self):
        source = next(item for item in watcher.SOURCES if item[0] == "工程训练中心学科竞赛")
        html = '<a href="/info/1034/2115.htm">关于举办2026年广东省大学生工程实践与创新能力大赛的通知</a>'

        with (
            patch.object(watcher, "SOURCES", [source]),
            patch.object(watcher, "fetch", return_value=html),
        ):
            notices, errors = watcher.check_all()

        self.assertEqual(errors, [])
        self.assertEqual(len(notices), 1)
        self.assertIn("工程实践与创新能力大赛", notices[0].title)
        self.assertEqual(
            notices[0].url,
            "https://etc.gdut.edu.cn/info/1034/2115.htm",
        )

    def test_truncated_engineering_competition_titles_are_not_filtered_out(self):
        html = (
            '<a href="/info/1023/1283.htm">关于举办2026年广东省工科大学生实验综...</a>'
            '<a href="/info/1023/1293.htm">2026年广东省工科大学生综合实验技能竞...</a>'
        )

        notices = watcher.extract_notices(
            "广东省工科大学生实验综合技能竞赛",
            "https://jnds.gdut.edu.cn/",
            html,
            "竞赛",
        )

        self.assertEqual(len(notices), 2)
        self.assertTrue(all(notice.category == "竞赛" for notice in notices))

    def test_international_winter_program_is_classified_as_study_abroad(self):
        source = next(item for item in watcher.SOURCES if item[0] == "国际合作与交流处")
        html = '<a href="/info/1115/4359.htm">2027韩国檀国大学国际寒假项目（资助项目）</a>'

        with (
            patch.object(watcher, "SOURCES", [source]),
            patch.object(watcher, "fetch", return_value=html),
        ):
            notices, errors = watcher.check_all()

        self.assertEqual(errors, [])
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].category, "出国")
        self.assertEqual(
            notices[0].url,
            "https://wsc.gdut.edu.cn/info/1115/4359.htm",
        )

    def test_verification_page_is_reported_as_a_fetch_failure(self):
        response = BytesIO("<html>请完成人机验证后继续</html>".encode("utf-8"))
        response.headers = Message()
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        with patch.object(watcher.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "验证或拦截"):
                watcher.fetch("https://example.com")

    def test_page_without_links_is_reported_instead_of_silent_empty_success(self):
        with self.assertRaisesRegex(ValueError, "没有可解析的链接"):
            watcher.extract_notices("测试来源", "https://example.com", "<html><body>动态页面</body></html>")

    def test_career_page_without_cards_is_reported_as_source_failure(self):
        with self.assertRaisesRegex(ValueError, "没有可解析的赛事卡片"):
            watcher.extract_career_competition_notices(
                '<a href="/competition/competitions/page/2">2</a>',
                "https://career.gdut.edu.cn/competition/competitions",
            )

    def test_archived_and_non_event_links_are_filtered(self):
        html = """
        <a href="/old">2019年广东省工科大学生实验综合技能竞赛</a>
        <a href="/current">2025年大学生数学竞赛报名通知</a>
        <a href="/nav">创新创业教育</a>
        <a href="/logistics">竞赛场地、就餐地点及住宿信息</a>
        """
        notices = watcher.extract_notices(
            "测试竞赛", "https://example.com", html, "竞赛", today=date(2026, 10, 13)
        )
        self.assertEqual([notice.title for notice in notices], ["2025年大学生数学竞赛报名通知"])

    def test_pruning_archives_removes_stale_items_and_favorites(self):
        state = {
            "notices": [
                {"id": "old", "title": "2019年大学生竞赛", "category": "竞赛"},
                {"id": "noise", "title": "创新创业教育", "category": "竞赛"},
                {"id": "current", "title": "2025年大学生竞赛", "category": "竞赛"},
            ],
            "favorites": ["old", "noise", "current"],
        }
        removed = watcher.prune_irrelevant_notices(state, date(2026, 10, 13))
        self.assertEqual(set(removed), {"old", "noise"})
        self.assertEqual([item["id"] for item in state["notices"]], ["current"])
        self.assertEqual(state["favorites"], ["current"])

    def test_career_competition_javascript_cards_and_pagination_are_parsed(self):
        first_page = self._career_page(
            "校级人工智能竞赛", 42, '<a href="/competition/competitions/page/2">2</a>'
        ).replace("2026-10-01", "2026-09-03").replace("2026-10-20", "2026-09-12")
        second_page = self._career_page("数学建模校内选拔", 43)
        with (
            patch.object(watcher, "SOURCES", [("学生就业指导中心赛事", "https://career.gdut.edu.cn/competition/competitions", "竞赛")]),
            patch.object(watcher, "fetch", side_effect=[first_page, second_page]) as fetch,
        ):
            notices, errors = watcher.check_all()

        self.assertFalse(errors)
        self.assertEqual({notice.title for notice in notices}, {"校级人工智能竞赛", "数学建模校内选拔"})
        self.assertEqual(fetch.call_count, 2)
        first = next(notice for notice in notices if notice.title == "校级人工智能竞赛")
        self.assertEqual(first.url, "https://career.gdut.edu.cn/competition/view/competition_id/42")
        self.assertEqual(
            watcher.extract_registration_deadline(asdict(first)),
            datetime(2026, 9, 12).date(),
        )

    def test_failed_career_pagination_keeps_first_page_results(self):
        first_page = self._career_page(
            "校级人工智能竞赛", 42, '<a href="/competition/competitions/page/2">2</a>'
        )
        with (
            patch.object(watcher, "SOURCES", [("学生就业指导中心赛事", "https://career.gdut.edu.cn/competition/competitions", "竞赛")]),
            patch.object(watcher, "fetch", side_effect=[first_page, OSError("offline")]),
            patch.object(watcher, "log") as mock_log,
        ):
            notices, errors = watcher.check_all()

        self.assertEqual([notice.title for notice in notices], ["校级人工智能竞赛"])
        self.assertEqual(len(errors), 1)
        self.assertIn("分页读取失败", errors[0])
        mock_log.assert_called_once_with(
            "ERROR 学生就业指导中心赛事: 分页读取失败：offline"
        )


if __name__ == "__main__":
    unittest.main()
