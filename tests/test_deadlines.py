import unittest
from datetime import date

from src.gdut_competition_watcher import (
    extract_registration_deadline,
    notice_is_registration_expired,
    prune_expired_notices,
)


class DeadlinePruningTests(unittest.TestCase):
    def test_extracts_latest_deadline_from_registration_window(self):
        notice = {
            "title": "关于举办人工智能竞赛的通知",
            "detail_text": "报名时间：2026年9月1日至2026年9月10日，请按时提交报名表。",
        }

        self.assertEqual(
            extract_registration_deadline(notice, date(2026, 9, 8)),
            date(2026, 9, 10),
        )

    def test_deadline_day_is_still_visible(self):
        notice = {
            "title": "关于举办人工智能竞赛的通知",
            "detail_text": "报名截止时间为2026年9月8日。",
        }

        self.assertFalse(notice_is_registration_expired(notice, date(2026, 9, 8)))

    def test_same_month_range_uses_end_day(self):
        notice = {
            "title": "关于举办人工智能竞赛的通知",
            "detail_text": "报名时间：2026年9月1日至10日。",
        }

        self.assertEqual(
            extract_registration_deadline(notice, date(2026, 9, 8)),
            date(2026, 9, 10),
        )

    def test_expired_notice_is_removed_from_notices_and_favorites(self):
        state = {
            "notices": [
                {"id": "old", "title": "竞赛报名截止时间为2026年9月7日"},
                {"id": "fresh", "title": "竞赛报名截止时间为2026年9月9日"},
            ],
            "favorites": ["old", "fresh"],
        }

        removed = prune_expired_notices(state, date(2026, 9, 8))

        self.assertEqual(removed, ["old"])
        self.assertEqual([notice["id"] for notice in state["notices"]], ["fresh"])
        self.assertEqual(state["favorites"], ["fresh"])

    def test_plain_publish_date_without_deadline_context_is_not_removed(self):
        notice = {
            "title": "2026年9月1日 关于公布竞赛获奖名单的通知",
            "summary": "这是学校发布的通知。",
        }

        self.assertFalse(notice_is_registration_expired(notice, date(2026, 9, 8)))


if __name__ == "__main__":
    unittest.main()
