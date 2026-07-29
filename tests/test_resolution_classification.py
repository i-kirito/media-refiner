import unittest
from pathlib import Path

from app.routers.subscribe import _height_from_resolution
from app.services.quality_scanner import calculate_quality_score, classify_resolution
from app.services.telegram import _fmt_resolution


class ResolutionClassificationTests(unittest.TestCase):
    def setUp(self):
        self.video_stream = {
            "Type": "Video",
            "Width": 3840,
            "Height": 1560,
            "Codec": "hevc",
            "VideoRange": "DolbyVision",
        }
        self.item = {
            "Path": "/media/白蛇：缘起 (2019) - 2160p.H265.DoVi.mkv",
            "MediaSources": [{
                "Bitrate": 20_000_001,
                "MediaStreams": [self.video_stream],
            }],
        }

    def test_scope_4k_dimensions_use_4k_tier(self):
        self.assertEqual("4k", classify_resolution(self.item, self.video_stream))
        self.assertEqual(2160, _height_from_resolution("3840x1560"))
        self.assertEqual("4K", _fmt_resolution("3840x1560"))

    def test_scope_4k_receives_4k_resolution_score(self):
        non_4k_item = {
            **self.item,
            "MediaSources": [{
                **self.item["MediaSources"][0],
                "MediaStreams": [{**self.video_stream, "Width": 2560}],
            }],
        }
        self.assertGreater(
            calculate_quality_score(self.item),
            calculate_quality_score(non_4k_item),
        )

    def test_web_badge_classifies_scope_4k_by_width(self):
        template = (
            Path(__file__).resolve().parents[1] / "app/templates/base.html"
        ).read_text(encoding="utf-8")
        self.assertIn("width >= 3800 || height >= 2000", template)


if __name__ == "__main__":
    unittest.main()
