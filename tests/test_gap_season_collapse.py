import unittest
from pathlib import Path


class GapSeasonCollapseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (Path(__file__).parents[1] / "app" / "templates" / "gaps.html").read_text()

    def test_season_header_is_accessible_collapsible_control(self):
        self.assertIn("fileCollapsedSeasons: new Set()", self.template)
        self.assertIn("function toggleGapFileSeason(season)", self.template)
        self.assertIn('class="gap-files-season-head"', self.template)
        self.assertIn('aria-expanded="${collapsed ? \'false\' : \'true\'}"', self.template)
        self.assertIn('class="gap-files-season-body"', self.template)

    def test_opening_another_series_resets_collapsed_seasons(self):
        self.assertIn("gapState.fileCollapsedSeasons.clear()", self.template)


if __name__ == "__main__":
    unittest.main()
