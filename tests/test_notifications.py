import json
import tempfile
import unittest
from dataclasses import asdict
from datetime import date, datetime
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
                patch.object(watcher, "notify_windows") as notify,
            ):
                new_items, errors, first_run = watcher.run_check()
                self.assertFalse(errors)
                self.assertFalse(first_run)
                self.assertEqual([item.lifecycle_stage for item in new_items], ["open"])
                notify.assert_called_once()

                new_items, _, _ = watcher.run_check()
                self.assertEqual(new_items, [])
                notify.assert_called_once()


class StateFileSafetyTests(unittest.TestCase):
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
        ):
            notices, errors = watcher.check_all()

        self.assertEqual([notice.title for notice in notices], ["校级人工智能竞赛"])
        self.assertEqual(len(errors), 1)
        self.assertIn("分页读取失败", errors[0])


if __name__ == "__main__":
    unittest.main()
