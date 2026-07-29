import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app import database
from app.routers import subscribe


class SubscribeBackgroundTransferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "refiner.db"
        await database.init_db()

    async def asyncTearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    @staticmethod
    def review(item_id: str = "emby-1") -> dict:
        return {
            "rule_id": "rule-1",
            "rule_name": "后台转存测试",
            "item_id": item_id,
            "item_name": f"测试影片 {item_id}",
            "current_quality": {"emby_id": item_id, "tmdb_id": item_id},
            "search_result": {"slug": f"resource-{item_id}", "title": f"测试影片 {item_id} 4K"},
            "source": "hdhive",
            "action_type": "transfer",
            "candidate_rank": [0, 0],
        }

    async def test_approve_claims_pending_review_and_returns_immediately(self):
        review_id = await database.add_subscribe_review(self.review())
        scheduled: list[int] = []

        with (
            patch.object(subscribe, "_verify_emby_item_ids", new=AsyncMock(return_value={"emby-1": True})),
            patch.object(subscribe, "_schedule_transfer_submission", side_effect=scheduled.append),
        ):
            response = await subscribe.approve_review(review_id)
            duplicate = await subscribe.approve_review(review_id)

        review = await database.get_subscribe_review(review_id)
        self.assertEqual("success", response["status"])
        self.assertTrue(response["data"]["processing"])
        self.assertEqual("processing", review["status"])
        self.assertEqual("queued", review["search_result"]["_transfer_stage"])
        self.assertEqual([review_id], scheduled)
        self.assertTrue(duplicate["data"]["already_handled"])
        self.assertEqual("processing", duplicate["data"]["review_status"])
        logs = await database.get_subscribe_logs_for_item("emby-1", "processing")
        self.assertEqual(1, len(logs))
        self.assertIn("后台转存队列", logs[0]["message"])

    async def test_timeout_keeps_review_processing_with_nonempty_message(self):
        review_id = await database.add_subscribe_review(self.review())
        await database.claim_subscribe_review_processing(
            review_id,
            "已加入后台转存队列",
            {"_transfer_stage": "queued"},
        )
        scheduled: list[int] = []

        class TimeoutHDHiveClient:
            async def unlock_and_transfer(self, slug: str):
                raise httpx.ReadTimeout("")

            async def close(self):
                return None

        with (
            patch.object(subscribe, "_verify_emby_item_ids", new=AsyncMock(return_value={"emby-1": True})),
            patch.object(subscribe, "HDHiveClient", return_value=TimeoutHDHiveClient()),
            patch.object(subscribe, "_schedule_transfer_verification", side_effect=scheduled.append),
        ):
            await subscribe._execute_transfer_review(review_id)

        review = await database.get_subscribe_review(review_id)
        self.assertEqual("processing", review["status"])
        self.assertIn("ReadTimeout", review["message"])
        self.assertEqual("verify_after_timeout", review["search_result"]["_transfer_stage"])
        self.assertEqual([review_id], scheduled)
        logs = await database.get_subscribe_logs_for_item("emby-1", "processing")
        self.assertEqual(1, len(logs))
        self.assertIn("响应超时", logs[0]["message"])

    async def test_unexpected_preflight_error_marks_review_failed_and_logs(self):
        review_id = await database.add_subscribe_review(self.review())
        await database.claim_subscribe_review_processing(
            review_id,
            "已加入后台转存队列",
            {"_transfer_stage": "queued"},
        )

        with patch.object(
            subscribe,
            "_verify_emby_item_ids",
            new=AsyncMock(side_effect=RuntimeError("emby probe crashed")),
        ):
            await subscribe._execute_transfer_review(review_id)

        review = await database.get_subscribe_review(review_id)
        self.assertEqual("failed", review["status"])
        self.assertIn("emby probe crashed", review["message"])
        logs = await database.get_subscribe_logs_for_item("emby-1", "error")
        self.assertEqual(1, len(logs))
        self.assertIn("emby probe crashed", logs[0]["message"])

    async def test_verification_deadline_marks_failed_and_writes_activity(self):
        review_id = await database.add_subscribe_review(self.review())
        await database.claim_subscribe_review_processing(
            review_id,
            "115 转存已提交，等待资源落地入库",
            {
                "_transfer_stage": "verifying",
                "_transfer_submitted_at": 1,
            },
        )

        with (
            patch.object(
                subscribe,
                "_verify_hdhive_transfer_visible",
                new=AsyncMock(return_value={"ok": False, "pending": True, "message": "仍未落地"}),
            ),
            patch.object(subscribe, "_TRANSFER_VERIFY_TIMEOUT", 0),
        ):
            await subscribe._watch_transfer_verification(review_id)

        review = await database.get_subscribe_review(review_id)
        self.assertEqual("failed", review["status"])
        self.assertIn("未确认资源入库", review["message"])
        logs = await database.get_subscribe_logs_for_item("emby-1", "error")
        self.assertEqual(1, len(logs))
        self.assertIn("仍未落地", logs[0]["message"])

    async def test_pending_list_also_exposes_processing_reviews(self):
        pending_id = await database.add_subscribe_review(self.review("emby-pending"))
        processing_id = await database.add_subscribe_review(self.review("emby-processing"))
        await database.claim_subscribe_review_processing(processing_id, "后台处理中")

        async def passthrough(review):
            return review

        with (
            patch.object(subscribe, "_schedule_stale_pending_review_prune"),
            patch.object(subscribe, "_normalise_current_quality_for_display", side_effect=passthrough),
        ):
            response = await subscribe.list_reviews("pending")

        rows = {row["id"]: row for row in response["data"]}
        self.assertEqual({pending_id, processing_id}, set(rows))
        self.assertEqual("processing", rows[processing_id]["status"])

    async def test_pending_visibility_marks_verifying_stage_before_scheduling(self):
        review_id = await database.add_subscribe_review(self.review())
        await database.claim_subscribe_review_processing(
            review_id,
            "正在后台提交 Symedia 转存",
            {"_transfer_stage": "submitting"},
        )
        review = await database.get_subscribe_review(review_id)
        scheduled: list[int] = []

        with patch.object(
            subscribe,
            "_schedule_transfer_verification",
            side_effect=scheduled.append,
        ):
            await subscribe._mark_transfer_processing(
                review,
                {"data": {"_target_folder": "folder-1"}},
                "transferred",
            )

        updated = await database.get_subscribe_review(review_id)
        self.assertEqual("processing", updated["status"])
        self.assertEqual("verifying", updated["search_result"]["_transfer_stage"])
        self.assertEqual([review_id], scheduled)

    async def test_resume_verifying_review_never_resubmits_transfer(self):
        review_id = await database.add_subscribe_review(self.review())
        await database.claim_subscribe_review_processing(
            review_id,
            "115 转存已提交，等待资源落地入库",
            {"_transfer_stage": "verifying"},
        )
        submitted: list[int] = []
        verifying: list[int] = []

        with (
            patch.object(
                subscribe,
                "_schedule_transfer_submission",
                side_effect=submitted.append,
            ),
            patch.object(
                subscribe,
                "_schedule_transfer_verification",
                side_effect=verifying.append,
            ),
        ):
            resumed = await subscribe.resume_processing_transfer_verifications()

        self.assertEqual(1, resumed)
        self.assertEqual([], submitted)
        self.assertEqual([review_id], verifying)


class PlansBackgroundStateMarkupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (
            Path(__file__).parents[1] / "app" / "templates" / "plans.html"
        ).read_text(encoding="utf-8")

    def test_server_processing_state_keeps_card_busy_and_visible(self):
        self.assertIn("const activeCount = reviews.length;", self.html)
        self.assertIn("if (activeCount === 0)", self.html)
        self.assertIn("const isProcessing = r.status === 'processing';", self.html)
        self.assertIn("const isBusy = isProcessing || _busyReviewIds.has(String(r.id));", self.html)
        self.assertIn("${isBusy ? '处理中' : (isEd2k ? '离线到目录' : '批准')}", self.html)

    def test_processing_card_shows_persisted_stage_message(self):
        start = self.html.index("const isProcessing = r.status === 'processing';")
        end = self.html.index("async function approveReview", start)
        processing_markup = self.html[start:end]
        self.assertIn("r.message", processing_markup)


if __name__ == "__main__":
    unittest.main()
