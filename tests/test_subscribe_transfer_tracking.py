import tempfile
import time
import unittest
from pathlib import Path

from app import database
from app.routers import subscribe


class SubscribeTransferTrackingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        self.original_verify = subscribe._verify_hdhive_transfer_visible
        database.DB_PATH = Path(self.temp_dir.name) / "refiner.db"
        await database.init_db()

    async def asyncTearDown(self):
        subscribe._verify_hdhive_transfer_visible = self.original_verify
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    async def test_processing_review_becomes_approved_after_delayed_match(self):
        review_id = await database.add_subscribe_review({
            "rule_id": "rule-1",
            "rule_name": "测试规则",
            "item_id": "emby-1",
            "item_name": "海贼王：黄金岛冒险",
            "current_quality": {"tmdb_id": "19576"},
            "search_result": {
                "hdhive_slug": "resource-1",
                "title": "海贼王：黄金岛冒险 (2000)",
            },
            "source": "hdhive",
            "action_type": "transfer",
            "candidate_rank": [0, 0],
        })
        await database.update_subscribe_review_result(
            review_id,
            "processing",
            "等待入库",
            {
                "_transfer_submitted_at": time.time() - 1000,
                "_transfer_target_folder": "staging",
            },
        )

        async def fake_verify(result, resp, **kwargs):
            self.assertGreater(kwargs.get("account_search_after", 0), 0)
            return {
                "ok": True,
                "mode": "account_search",
                "matched_names": ["海贼王：黄金岛冒险 (2026) {tmdbid=19576}"],
            }

        subscribe._verify_hdhive_transfer_visible = fake_verify
        await subscribe._watch_transfer_verification(review_id)

        review = await database.get_subscribe_review(review_id)
        self.assertEqual("approved", review["status"])
        self.assertEqual("account_search", review["search_result"]["_transfer_verify_mode"])
        logs = await database.get_subscribe_logs_for_item("emby-1")
        self.assertEqual("transfer", logs[-1]["action"])

    async def test_already_owned_review_accepts_matching_existing_account_item(self):
        review_id = await database.add_subscribe_review({
            "rule_id": "rule-1",
            "rule_name": "测试规则",
            "item_id": "emby-owned",
            "item_name": "带我去飞",
            "current_quality": {"tmdb_id": "883164"},
            "search_result": {
                "title": "带我去飞 (2023)",
                "_transfer_origin_status": "already_owned",
                "_transfer_submitted_at": time.time(),
                "_transfer_target_folder": "staging",
                "_transfer_stage": "verifying",
            },
            "source": "hdhive",
            "action_type": "transfer",
            "candidate_rank": [0, 0],
        })
        await database.update_subscribe_review(review_id, "processing", "等待确认")

        async def fake_verify(result, resp, **kwargs):
            self.assertEqual(0.0, kwargs.get("account_search_after"))
            return {
                "ok": True,
                "mode": "account_search",
                "matched_names": ["带我去飞 (2023) {tmdbid=883164}"],
            }

        subscribe._verify_hdhive_transfer_visible = fake_verify
        await subscribe._watch_transfer_verification(review_id)

        review = await database.get_subscribe_review(review_id)
        self.assertEqual("approved", review["status"])
        self.assertEqual("account_search", review["search_result"]["_transfer_verify_mode"])


if __name__ == "__main__":
    unittest.main()
