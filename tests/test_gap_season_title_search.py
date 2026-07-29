import unittest

from app.routers import gaps


class GapSeasonTitleSearchTests(unittest.IsolatedAsyncioTestCase):
    def test_target_metadata_survives_normalisation(self):
        targets = gaps._normalise_gap_targets(
            targets=[{
                "season": 2,
                "episodes": [],
                "season_missing": True,
                "season_title": "步步惊情",
                "season_year": 2014,
            }]
        )

        self.assertEqual("步步惊情", targets[0]["season_title"])
        self.assertEqual(2014, targets[0]["season_year"])

    def test_distinct_season_title_is_a_standalone_search_alias(self):
        aliases = gaps._season_aliases_from_name("步步惊心", "步步惊情")

        self.assertIn("步步惊情", aliases)
        self.assertIn("步步惊心 步步惊情", aliases)

    async def test_cached_tmdb_season_title_builds_precise_s01_queries(self):
        class FakeMoviePilot:
            is_configured = True

            async def get_tmdb_seasons(self, tmdb_id):
                raise AssertionError("target metadata should avoid another TMDB request")

        context = await gaps._gap_season_search_context(
            FakeMoviePilot(),
            "emby-series",
            "步步惊心",
            37681,
            2,
            {"season_title": "步步惊情", "season_year": 2014},
        )

        self.assertEqual("步步惊情", context["season_title"])
        self.assertEqual(2014, context["season_year"])
        self.assertEqual("步步惊情 2014 S01", context["direct_keywords"][0])

    def test_standalone_s01_result_maps_back_to_tmdb_s02(self):
        context = {
            "season_title": "步步惊情",
            "season_year": 2014,
        }
        result = gaps._apply_season_search_hint(
            {"title": "Startling Love With Each Step 2014 S01 Complete 2160p WEB-DL"},
            2,
            context,
            "步步惊情 2014 S01",
        )
        annotated = gaps._annotate_gap_match_for_targets(
            result,
            [{"season": 2, "episodes": [], "season_missing": True}],
            series_name="步步惊心",
        )

        self.assertEqual("season_pack", annotated["ui_episode_match_kind"])
        self.assertEqual(2, annotated["ui_target_season"])
        self.assertEqual(1, annotated["ui_file_season"])
        self.assertEqual(1, annotated["ui_gap_targets"][0]["file_season"])

    def test_different_release_year_is_not_mapped_to_named_season(self):
        result = gaps._apply_season_search_hint(
            {"title": "Startling Love With Each Step 2011 Season 1 Complete"},
            2,
            {"season_title": "步步惊情", "season_year": 2014},
            "步步惊情 2014 S01",
        )

        self.assertNotIn("ui_target_season_hint", result)

    def test_season_year_replaces_series_year_for_identity_score(self):
        payload = gaps.GapSearchPayload(
            series_name="步步惊心",
            year=2011,
            season=2,
            tmdb_id=37681,
        )
        context = gaps._gap_identity_context(
            {
                "title": "Startling Love With Each Step 2014 S01 Complete",
                "year": 2014,
                "ui_season_year": 2014,
            },
            payload,
        )

        self.assertEqual(20, context["score"])
        self.assertFalse(context["mismatch"])


if __name__ == "__main__":
    unittest.main()
