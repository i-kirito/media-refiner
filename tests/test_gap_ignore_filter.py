import unittest
from unittest.mock import AsyncMock, patch

from app.routers import gaps


class GapIgnoreFilterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_status = dict(gaps._scan_status)

    async def asyncTearDown(self):
        gaps._scan_status.clear()
        gaps._scan_status.update(self.original_status)

    @staticmethod
    def result(series_id="series-1", merged_series_ids=None):
        return {
            "series_id": series_id,
            "series_name": "步步惊心",
            "tmdb_id": "37681",
            "library_id": "library-1",
            "library_name": "国产剧",
            "gap_count": 2,
            "merged_series_ids": merged_series_ids or [series_id],
            "gaps": [
                {
                    "id": f"{series_id}:S02:season",
                    "season": 2,
                    "episode": 0,
                    "season_missing": True,
                },
                {
                    "id": f"{series_id}:S01E03",
                    "season": 1,
                    "episode": 3,
                },
            ],
        }

    def test_series_ignore_removes_cached_card(self):
        visible = gaps._filter_gap_results_by_ignored([self.result("524026")], {"524026"})

        self.assertEqual([], visible)

    def test_season_ignore_removes_only_the_matching_gap(self):
        visible = gaps._filter_gap_results_by_ignored(
            [self.result("524026")],
            {"524026:S02:season"},
        )

        self.assertEqual(1, len(visible))
        self.assertEqual(1, visible[0]["gap_count"])
        self.assertEqual("524026:S01E03", visible[0]["gaps"][0]["id"])

    def test_ignore_for_merged_emby_id_removes_deduped_card(self):
        visible = gaps._filter_gap_results_by_ignored(
            [self.result("keeper", merged_series_ids=["keeper", "duplicate"])],
            {"duplicate"},
        )

        self.assertEqual([], visible)

    async def test_ignore_write_immediately_updates_in_memory_status(self):
        gaps._scan_status.clear()
        gaps._scan_status.update(
            {
                "results": [self.result("524026")],
                "summary": {"series_count": 1, "gap_count": 2, "library_count": 7},
            }
        )

        with patch.object(
            gaps,
            "get_gap_ignore_targets",
            new=AsyncMock(return_value={"524026"}),
        ):
            await gaps._apply_current_gap_ignores()

        self.assertEqual([], gaps._scan_status["results"])
        self.assertEqual(0, gaps._scan_status["summary"]["series_count"])
        self.assertEqual(0, gaps._scan_status["summary"]["gap_count"])
        self.assertEqual(7, gaps._scan_status["summary"]["library_count"])


if __name__ == "__main__":
    unittest.main()
