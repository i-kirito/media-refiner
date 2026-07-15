import json
import tempfile
import unittest
from pathlib import Path

from app import database
from app.routers.subscribe import _candidate_sort_key


class SubscribeReviewDedupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "refiner.db"
        await database.init_db()

    async def asyncTearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    @staticmethod
    def review(item_id: str, source: str, result: dict, rank: list[int]) -> dict:
        return {
            "rule_id": "rule-1",
            "rule_name": "测试规则",
            "item_id": item_id,
            "item_name": "带我去飞",
            "current_quality": {
                "emby_id": item_id,
                "name": "带我去飞",
                "year": 2025,
                "type": "Movie",
                "tmdb_id": "1560688",
                "path": "/media/带我去飞 (2025) {tmdbid=1560688}/movie.strm",
            },
            "search_result": {**result, "_source_label": source},
            "source": source,
            "action_type": "transfer" if source == "hdhive" else "download",
            "candidate_rank": rank,
            "message": "待审核",
        }

    def test_media_key_uses_stable_tmdb_identity(self):
        first = self.review("emby-old", "moviepilot", {}, [1, 0])
        second = self.review("emby-new", "hdhive", {}, [0, 0])
        self.assertEqual(
            database.build_subscribe_media_key(first["current_quality"], first["item_name"], first["item_id"]),
            database.build_subscribe_media_key(second["current_quality"], second["item_name"], second["item_id"]),
        )

    def test_rule_sort_prefers_configured_hdhive_source(self):
        rule = {
            "prefer_order": ["4k", "subtitle", "remux"],
            "source_priority": "hdhive",
        }
        hdhive = {
            "_source_label": "hdhive",
            "video_resolution": ["4K"],
            "subtitle_language": ["简中"],
        }
        moviepilot = {
            "_source_label": "moviepilot",
            "resolution": "2160p",
            "subtitle": "中字",
            "seeders": 100,
        }
        self.assertLess(_candidate_sort_key(rule, hdhive), _candidate_sort_key(rule, moviepilot))

    async def test_only_best_source_remains_across_emby_id_changes(self):
        moviepilot_id = await database.add_subscribe_review(self.review(
            "emby-1",
            "moviepilot",
            {"page_url": "https://tracker/details?id=1", "title": "MP 4K", "seeders": 7},
            [0, 0, 1, -7],
        ))
        hdhive_id = await database.add_subscribe_review(self.review(
            "emby-2",
            "hdhive",
            {"hdhive_slug": "best-hd", "title": "影巢 4K HDR"},
            [0, 0, 0, 0],
        ))
        ignored_id = await database.add_subscribe_review(self.review(
            "emby-3",
            "moviepilot",
            {"page_url": "https://tracker/details?id=2", "title": "MP 4K", "seeders": 20},
            [0, 0, 1, -20],
        ))

        self.assertIsInstance(moviepilot_id, int)
        self.assertIsInstance(hdhive_id, int)
        self.assertIsNone(ignored_id)
        reviews = await database.list_subscribe_reviews("pending")
        self.assertEqual(1, len(reviews))
        self.assertEqual("hdhive", reviews[0]["source"])
        self.assertEqual("emby-3", reviews[0]["item_id"])
        self.assertEqual(1, await database.count_pending_reviews())

    async def test_same_resource_refreshes_url_without_new_review(self):
        first_id = await database.add_subscribe_review(self.review(
            "emby-1",
            "moviepilot",
            {
                "page_url": "https://tracker/details?id=1",
                "enclosure": "https://tracker/download?token=old",
                "seeders": 9,
            },
            [0, -9],
        ))
        second_id = await database.add_subscribe_review(self.review(
            "emby-2",
            "moviepilot",
            {
                "page_url": "https://tracker/details?id=1",
                "enclosure": "https://tracker/download?token=new",
                "seeders": 4,
            },
            [0, -4],
        ))

        self.assertIsNone(second_id)
        reviews = await database.list_subscribe_reviews("pending")
        self.assertEqual([first_id], [review["id"] for review in reviews])
        self.assertEqual("emby-2", reviews[0]["item_id"])
        self.assertEqual("https://tracker/download?token=new", reviews[0]["search_result"]["enclosure"])

    async def test_reconcile_keeps_ranked_resource_and_latest_emby_item(self):
        media_key = "movie:tmdb:1560688"
        db = await database.get_db()
        try:
            first = await db.execute(
                """
                INSERT INTO subscribe_reviews
                (rule_id, item_id, item_name, current_quality, search_result, source, media_key, candidate_rank)
                VALUES (?, ?, ?, ?, ?, ?, ?, '[]')
                """,
                (
                    "rule-1",
                    "emby-old",
                    "带我去飞",
                    json.dumps({"tmdb_id": "1560688", "emby_id": "emby-old"}),
                    json.dumps({"title": "影巢 4K", "_source_label": "hdhive"}),
                    "hdhive",
                    media_key,
                ),
            )
            second = await db.execute(
                """
                INSERT INTO subscribe_reviews
                (rule_id, item_id, item_name, current_quality, search_result, source, media_key, candidate_rank)
                VALUES (?, ?, ?, ?, ?, ?, ?, '[]')
                """,
                (
                    "rule-1",
                    "emby-new",
                    "带我去飞",
                    json.dumps({"tmdb_id": "1560688", "emby_id": "emby-new"}),
                    json.dumps({"title": "MP 4K", "_source_label": "moviepilot"}),
                    "moviepilot",
                    media_key,
                ),
            )
            await db.commit()
        finally:
            await db.close()

        removed = await database.reconcile_pending_subscribe_reviews([
            {"id": first.lastrowid, "media_key": media_key, "candidate_rank": [0, 0]},
            {"id": second.lastrowid, "media_key": media_key, "candidate_rank": [1, -10]},
        ])

        reviews = await database.list_subscribe_reviews("pending")
        self.assertEqual(1, removed)
        self.assertEqual(1, len(reviews))
        self.assertEqual("hdhive", reviews[0]["source"])
        self.assertEqual("emby-new", reviews[0]["item_id"])


if __name__ == "__main__":
    unittest.main()
