import unittest

from app.routers.gaps import (
    _annotate_gap_match,
    _episode_match_ratio,
    _explicit_title_seasons,
    _season_collection_count,
    _normalise_title,
)


class GapSeasonTitleMatchTests(unittest.TestCase):
    def test_chinese_consecutive_season_collection_matches_s02(self):
        title = "一路繁花一二季全（已规范命名）"
        self.assertEqual([1, 2], sorted(_explicit_title_seasons(title)))
        self.assertEqual(2, _season_collection_count(_normalise_title(title)))
        ratio, matched, kind, file_season = _episode_match_ratio(title, 2, [])
        self.assertEqual(1.0, ratio)
        self.assertEqual([], matched)
        self.assertEqual("season_pack", kind)
        self.assertEqual(2, file_season)
        annotated = _annotate_gap_match({"title": title}, 2, [])
        self.assertEqual("season_pack", annotated["ui_episode_match_kind"])
        self.assertEqual("整季包", annotated["ui_episode_match_text"])

    def test_s01_plus_s00_does_not_match_s02(self):
        title = "S01+S00 4K EDR HIVEWEB"
        ratio, matched, kind, _ = _episode_match_ratio(title, 2, [])
        self.assertEqual(0.0, ratio)
        self.assertEqual([], matched)
        self.assertEqual("none", kind)


if __name__ == "__main__":
    unittest.main()
