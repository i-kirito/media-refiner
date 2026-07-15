import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import HTTPException

from app import database
from app.routers import subscribe
from app.services.emby import EmbyClient


class EmbyItemExistsTests(unittest.IsolatedAsyncioTestCase):
    async def test_item_exists_distinguishes_missing_from_transient_failure(self):
        statuses = {
            "live": 200,
            "missing": 404,
            "unavailable": 503,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            item_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(statuses[item_id], json={})

        emby = EmbyClient(host="https://emby.invalid", api_key="secret", user_id="user-1")
        await emby._client.aclose()
        emby._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            self.assertIs(await emby.item_exists("live"), True)
            self.assertIs(await emby.item_exists("missing"), False)
            self.assertIsNone(await emby.item_exists("unavailable"))
        finally:
            await emby.close()


class StalePendingReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        self.original_last_run = subscribe._stale_review_prune_last_run
        database.DB_PATH = Path(self.temp_dir.name) / "refiner.db"
        subscribe._stale_review_prune_last_run = 0.0
        await database.init_db()

    async def asyncTearDown(self):
        database.DB_PATH = self.original_db_path
        subscribe._stale_review_prune_last_run = self.original_last_run
        self.temp_dir.cleanup()

    @staticmethod
    def review(item_id: str, tmdb_id: str) -> dict:
        return {
            "rule_id": "rule-1",
            "rule_name": "测试规则",
            "item_id": item_id,
            "item_name": f"测试影片 {tmdb_id}",
            "current_quality": {
                "emby_id": item_id,
                "name": f"测试影片 {tmdb_id}",
                "year": 2025,
                "type": "Movie",
                "tmdb_id": tmdb_id,
            },
            "search_result": {
                "slug": f"resource-{tmdb_id}",
                "title": f"测试资源 {tmdb_id}",
                "_source_label": "hdhive",
            },
            "source": "hdhive",
            "action_type": "transfer",
            "candidate_rank": [0, 0],
            "message": "待审核",
        }

    async def test_prune_removes_only_confirmed_missing_reviews(self):
        stale_id = await database.add_subscribe_review(self.review("missing", "1001"))
        live_id = await database.add_subscribe_review(self.review("live", "1002"))
        unknown_id = await database.add_subscribe_review(self.review("unknown", "1003"))

        class FakeEmbyClient:
            async def item_exists(self, item_id: str) -> bool | None:
                return {"missing": False, "live": True, "unknown": None}[item_id]

            async def close(self):
                return None

        with patch.object(subscribe, "EmbyClient", return_value=FakeEmbyClient()):
            removed = await subscribe.prune_stale_pending_reviews(force=True)

        self.assertEqual(1, removed)
        reviews = await database.list_subscribe_reviews("pending")
        self.assertEqual({live_id, unknown_id}, {review["id"] for review in reviews})
        self.assertNotIn(stale_id, {review["id"] for review in reviews})

    async def test_pending_delete_does_not_touch_processed_review(self):
        review_id = await database.add_subscribe_review(self.review("processed", "2001"))
        await database.update_subscribe_review(review_id, "approved", "已完成")

        removed = await database.delete_pending_subscribe_reviews([review_id])

        self.assertEqual(0, removed)
        self.assertEqual("approved", (await database.get_subscribe_review(review_id))["status"])

    async def test_approve_removes_confirmed_missing_review_before_execution(self):
        review_id = await database.add_subscribe_review(self.review("missing", "3001"))

        class FakeEmbyClient:
            async def item_exists(self, item_id: str) -> bool | None:
                return False

            async def close(self):
                return None

        with patch.object(subscribe, "EmbyClient", return_value=FakeEmbyClient()):
            with self.assertRaises(HTTPException) as raised:
                await subscribe.approve_review(review_id)

        self.assertEqual(409, raised.exception.status_code)
        self.assertIsNone(await database.get_subscribe_review(review_id))


if __name__ == "__main__":
    unittest.main()
