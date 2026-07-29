import unittest
from unittest.mock import AsyncMock, patch

from app.routers import gaps


class GapCachePreservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_status = dict(gaps._scan_status)

    async def asyncTearDown(self):
        gaps._scan_status.clear()
        gaps._scan_status.update(self.original_status)

    async def test_empty_emby_library_response_preserves_previous_cache(self):
        cached_results = [
            {
                "series_id": "series-1",
                "series_name": "保留的剧集",
                "library_id": "library-1",
                "library_name": "动漫",
                "gap_count": 1,
                "gaps": [{"season": 1, "episode": 2}],
            }
        ]
        cached_summary = {"series_count": 1, "gap_count": 1, "library_count": 1}
        emby = AsyncMock()
        emby.api_key = "test-key"
        emby._get.return_value = []
        moviepilot = AsyncMock()

        with (
            patch("app.routers.gaps.EmbyClient", return_value=emby),
            patch("app.routers.gaps.MoviePilotClient", return_value=moviepilot),
            patch(
                "app.routers.gaps.load_gap_cache",
                new=AsyncMock(return_value=(cached_results, cached_summary, "2026-07-15 11:56:48")),
            ),
            patch("app.routers.gaps.get_gap_config", new=AsyncMock(return_value={"excluded_libraries": []})),
            patch("app.routers.gaps.get_gap_ignore_targets", new=AsyncMock(return_value=set())),
            patch("app.routers.gaps.asyncio.sleep", new=AsyncMock()),
            patch("app.routers.gaps.save_gap_cache", new=AsyncMock()) as save_cache,
        ):
            await gaps._scan_gaps_task()

        save_cache.assert_not_awaited()
        self.assertEqual(4, emby._get.await_count)
        self.assertEqual(1, len(gaps._scan_status["results"]))
        self.assertEqual("保留的剧集", gaps._scan_status["results"][0]["series_name"])
        self.assertEqual(1, gaps._scan_status["summary"]["series_count"])
        self.assertEqual("2026-07-15 11:56:48", gaps._scan_status["created_at"])
        self.assertIn("已保留上次缺集结果", gaps._scan_status["error"])
        self.assertEqual("扫描失败，已保留上次结果", gaps._scan_status["current_item"])
        emby.close.assert_awaited_once()
        moviepilot.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
