import unittest
from unittest.mock import AsyncMock, patch

from app.routers import gaps


class GapHDHiveCompleteSeasonFilterTests(unittest.IsolatedAsyncioTestCase):
    def test_helper_keeps_season_pack_and_full_series(self):
        self.assertTrue(
            gaps._is_complete_season_gap_hdhive_result(
                {"ui_episode_match_kind": "season_pack", "title": "Demo S01 Complete"}
            )
        )
        self.assertTrue(
            gaps._is_complete_season_gap_hdhive_result(
                {"ui_episode_match_kind": "full_series", "ui_full_series": True},
                full_series=True,
            )
        )
        self.assertFalse(
            gaps._is_complete_season_gap_hdhive_result(
                {"ui_episode_match_kind": "episode", "title": "Demo S01E03"}
            )
        )
        self.assertFalse(
            gaps._is_complete_season_gap_hdhive_result(
                {"ui_episode_match_kind": "episode_pack", "title": "Demo E01-E05"}
            )
        )

    async def test_search_returns_only_complete_season_resources(self):
        class FakeHDHive:
            async def search(self, **kwargs):
                return [
                    {"title": "蜡笔小新 合集 S01 Complete 1080p", "share_size": "20GB", "unlock_points": 0},
                    {"title": "蜡笔小新 S01E672 单集", "share_size": "1GB", "unlock_points": 0},
                    {"title": "蜡笔小新 E670-E675 多集包", "share_size": "3GB", "unlock_points": 0},
                    {"title": "Crayon Shin-chan Season 1 Complete", "share_size": "25GB", "unlock_points": 0},
                ]

            async def close(self):
                return None

        targets = [{"season": 1, "episodes": [672, 673, 674]}]
        payload = gaps.GapSearchPayload(
            series_id="emby-1",
            series_name="蜡笔小新",
            season=1,
            episodes=[672, 673, 674],
            targets=targets,
            tmdb_id=30623,
        )
        # 绕过 Pydantic model，保证 _normalise_gap_targets 路径与线上 dict 一致
        object.__setattr__(payload, "targets", targets)

        with patch.object(gaps, "HDHiveClient", return_value=FakeHDHive()), patch.object(
            gaps, "_limit_gap_targets_by_tmdb", AsyncMock(return_value=(targets, ""))
        ), patch.object(gaps, "_apply_gap_transfer_marks", AsyncMock(side_effect=lambda items: items)):
            data, message = await gaps._search_gap_hdhive_data(payload)

        titles = [item.get("title") for item in data.get("results") or []]
        kinds = {item.get("ui_episode_match_kind") for item in data.get("results") or []}

        self.assertEqual(2, len(titles))
        self.assertTrue(all("Complete" in title or "合集" in title for title in titles))
        self.assertEqual({"season_pack"}, kinds)
        self.assertEqual(2, data.get("filtered_out"))
        self.assertIn("过滤", message)

    async def test_search_empty_message_when_no_complete_season(self):
        class FakeHDHive:
            async def search(self, **kwargs):
                return [
                    {"title": "蜡笔小新 S01E01", "share_size": "800MB"},
                    {"title": "蜡笔小新 E02-E04", "share_size": "1.2GB"},
                ]

            async def close(self):
                return None

        targets = [{"season": 1, "episodes": [1, 2, 3]}]
        payload = gaps.GapSearchPayload(
            series_name="蜡笔小新",
            season=1,
            episodes=[1, 2, 3],
            targets=targets,
            tmdb_id=30623,
        )
        object.__setattr__(payload, "targets", targets)

        with patch.object(gaps, "HDHiveClient", return_value=FakeHDHive()), patch.object(
            gaps, "_limit_gap_targets_by_tmdb", AsyncMock(return_value=(targets, ""))
        ), patch.object(gaps, "_apply_gap_transfer_marks", AsyncMock(side_effect=lambda items: items)):
            data, message = await gaps._search_gap_hdhive_data(payload)

        self.assertEqual([], data.get("results"))
        self.assertEqual("影巢暂无完整整季资源", message)


if __name__ == "__main__":
    unittest.main()
