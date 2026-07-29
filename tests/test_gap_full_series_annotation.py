import unittest

from app.routers.gaps import (
    _choose_full_series_gap_result,
    _is_complete_season_gap_hdhive_result,
    _is_full_series_gap_resource,
)


class GapFullSeriesAnnotationTests(unittest.TestCase):
    def setUp(self):
        self.targets = [
            {"season": 1, "episodes": [], "season_missing": True},
            {"season": 2, "episodes": [], "season_missing": True},
        ]

    def test_single_season_pack_is_not_full_series(self):
        self.assertEqual((False, "单季资源"), _is_full_series_gap_resource({"title": "How Dare You S02 2026 Complete"}))
        self.assertEqual((False, "单季资源"), _is_full_series_gap_resource({"title": "成何体统 S01 全32集"}))

    def test_multi_season_collection_is_full_series(self):
        self.assertEqual((True, "全集"), _is_full_series_gap_resource({"title": "How Dare You S01-S02 合集"}))
        self.assertEqual((True, "全集"), _is_full_series_gap_resource({"title": "一路繁花一二季全"}))

    def test_choose_keeps_s02_season_pack(self):
        chosen = _choose_full_series_gap_result(
            {"title": "How Dare You S02 2026 Complete 2160p"},
            self.targets,
            assume_first_season_when_ambiguous=True,
            series_name="成何体统",
        )
        self.assertEqual("season_pack", chosen.get("ui_episode_match_kind"))
        self.assertEqual(2, chosen.get("ui_target_season"))
        self.assertFalse(chosen.get("ui_full_series"))
        self.assertIn("S02", chosen.get("ui_episode_match_text") or "")

    def test_choose_prefers_true_full_series_collection(self):
        chosen = _choose_full_series_gap_result(
            {"title": "How Dare You S01-S02 合集"},
            self.targets,
            assume_first_season_when_ambiguous=True,
            series_name="成何体统",
        )
        self.assertEqual("full_series", chosen.get("ui_episode_match_kind"))
        self.assertTrue(chosen.get("ui_full_series"))

    def test_hdhive_full_series_filter_keeps_season_pack(self):
        self.assertTrue(
            _is_complete_season_gap_hdhive_result(
                {"ui_episode_match_kind": "season_pack", "ui_target_season": 2},
                full_series=True,
            )
        )
        self.assertTrue(
            _is_complete_season_gap_hdhive_result(
                {"ui_episode_match_kind": "full_series", "ui_full_series": True},
                full_series=True,
            )
        )
        self.assertFalse(
            _is_complete_season_gap_hdhive_result(
                {"ui_episode_match_kind": "episode", "title": "S02E01"},
                full_series=True,
            )
        )


if __name__ == "__main__":
    unittest.main()
