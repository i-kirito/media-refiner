import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services.quality_scanner import QualityScanner


class MediaSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_filters_full_cache_before_pagination(self):
        items = [
            {
                "name": f"热门媒体 {index}",
                "quality_score": 100 - index,
                "library_name": "动漫",
            }
            for index in range(25)
        ]
        items.append({
            "name": "Re：从零开始的异世界生活",
            "year": 2016,
            "quality_score": 1,
            "library_name": "动漫",
            "provider_ids": {"Tmdb": "65942"},
        })
        scanner = QualityScanner(None)

        with patch(
            "app.database.load_quality_cache",
            new=AsyncMock(return_value=(items, {"total_count": len(items)}, "")),
        ):
            result = await scanner.get_items(
                min_score=0,
                max_score=100,
                search="从零开始的异世界",
                page_size=20,
            )

        self.assertEqual(1, result["total"])
        self.assertEqual("Re：从零开始的异世界生活", result["items"][0]["name"])

    async def test_search_normalizes_fullwidth_punctuation_and_spaces(self):
        item = {
            "name": "Re：从零开始的异世界生活",
            "quality_score": 40,
            "library_name": "动漫",
        }
        scanner = QualityScanner(None)

        with patch(
            "app.database.load_quality_cache",
            new=AsyncMock(return_value=([item], {}, "")),
        ):
            result = await scanner.get_items(
                min_score=0,
                max_score=100,
                search="Re: 从零",
            )

        self.assertEqual(1, result["total"])

    def test_global_search_passes_keyword_to_backend(self):
        template = (Path(__file__).resolve().parents[1] / "app/templates/base.html").read_text(encoding="utf-8")

        self.assertIn("search: query", template)
        self.assertIn("const items = json.data?.items || [];", template)

    def test_items_page_forwards_url_search_to_backend(self):
        template = (Path(__file__).resolve().parents[1] / "app/templates/items.html").read_text(encoding="utf-8")

        self.assertIn("if (searchName) filterState.search = searchName;", template)
        self.assertIn("if (filterState.search) params.set('search', filterState.search);", template)


if __name__ == "__main__":
    unittest.main()
