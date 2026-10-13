import unittest
from datetime import date, datetime, timedelta

from src.gdut_competition_watcher import (
    Notice,
    extract_registration_deadline,
    notice_is_registration_expired,
    prune_expired_notices,
    recently_expired_notice_ids,
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
            "seen": ["old", "fresh"],
        }

        removed = prune_expired_notices(state, date(2026, 9, 8))

        self.assertEqual(removed, ["old"])
        self.assertEqual([notice["id"] for notice in state["notices"]], ["fresh"])
        self.assertEqual(state["favorites"], ["fresh"])
        self.assertEqual(state["seen"], ["fresh"])
        self.assertIn("old", state["expired_checked_at"])

    def test_plain_publish_date_without_deadline_context_is_not_removed(self):
        notice = {
            "title": "2026年9月1日 关于公布竞赛获奖名单的通知",
            "summary": "这是学校发布的通知。",
        }

        self.assertFalse(notice_is_registration_expired(notice, date(2026, 9, 8)))

    def test_publish_date_near_registration_word_is_not_a_deadline(self):
        notice = {
            "title": "2026年9月1日 关于报名参加人工智能竞赛的通知",
            "date": "2026-09-01",
        }

        self.assertIsNone(extract_registration_deadline(notice, date(2026, 9, 8)))

    def test_explicit_deadline_after_date_is_detected(self):
        notice = {"detail_text": "请于2026年9月10日前提交报名表。"}

        self.assertEqual(
            extract_registration_deadline(notice, date(2026, 9, 8)),
            date(2026, 9, 10),
        )

    def test_adjusted_registration_deadline_is_detected(self):
        notice = {"detail_text": "报名截止时间调整为2026年9月20日，请按时提交材料。"}

        self.assertEqual(
            extract_registration_deadline(notice, date(2026, 9, 8)),
            date(2026, 9, 20),
        )

    def test_full_year_registration_range_uses_end_date(self):
        notice = {"detail_text": "报名时间：2026年9月1日至2026年10月10日。"}

        self.assertEqual(
            extract_registration_deadline(notice, date(2026, 9, 8)),
            date(2026, 10, 10),
        )

    def test_yearless_range_uses_notice_year(self):
        notice = {"date": "2025-08-20", "detail_text": "报名时间：9月1日至10日。"}

        self.assertEqual(
            extract_registration_deadline(notice, date(2026, 8, 25)),
            date(2025, 9, 10),
        )

    def test_cross_year_month_range_rolls_end_date_into_next_year(self):
        notice = {"date": "2026-11-20", "detail_text": "报名时间：12月20日至1月10日。"}

        self.assertEqual(
            extract_registration_deadline(notice, date(2026, 11, 20)),
            date(2027, 1, 10),
        )

    def test_recently_expired_notice_waits_a_week_before_retrying(self):
        notice = Notice(
            id="expired-item",
            title="2026年创新竞赛报名通知",
            url="https://example.com/event",
            source="测试来源",
        )
        state = {
            "expired_checked_at": {
                notice.id: {
                    "checked_at": (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"),
                    "title": notice.title,
                }
            }
        }

        self.assertEqual(recently_expired_notice_ids(state, [notice]), {notice.id})
        state["expired_checked_at"][notice.id]["checked_at"] = (
            datetime.now() - timedelta(days=8)
        ).strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(recently_expired_notice_ids(state, [notice]), set())

        state["expired_checked_at"][notice.id]["checked_at"] = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        changed_title = Notice(
            id=notice.id,
            title="2026年创新竞赛报名时间调整",
            url=notice.url,
            source=notice.source,
        )
        self.assertEqual(recently_expired_notice_ids(state, [changed_title]), set())


if __name__ == "__main__":
    unittest.main()
