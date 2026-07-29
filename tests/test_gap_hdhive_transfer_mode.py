import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import database
from app.routers import gaps


class GapHDHiveTransferModeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "refiner.db"
        await database.init_db()

    async def asyncTearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    @staticmethod
    def result() -> dict:
        return {
            "title": "种墨园 (2026)",
            "slug": "https://hdhive.com/resource/115/test-resource",
            "ui_episode_match_kind": "episode_pack",
            "description": "S01E01-E25 4K WEB-DL",
        }

    @staticmethod
    def payload(force_transfer: bool = False) -> gaps.GapDownloadPayload:
        return gaps.GapDownloadPayload(
            result=GapHDHiveTransferModeTests.result(),
            slug="https://hdhive.com/resource/115/test-resource",
            series_id="series-1",
            series_name="种墨园",
            season=1,
            episodes=[24, 25],
            targets=[{"season": 1, "episodes": [24, 25]}],
            force_transfer=force_transfer,
        )

    async def test_only_completed_mark_enables_retransfer(self):
        result = self.result()
        self.assertFalse(await gaps._gap_has_transfer_record(result, result["slug"]))

        await database.save_gap_transfer_mark(
            result["slug"],
            resource_title=result["title"],
            status="unlocked",
        )
        self.assertFalse(await gaps._gap_has_transfer_record(result, result["slug"]))

        await database.save_gap_transfer_mark(
            result["slug"],
            resource_title=result["title"],
            status="transferred",
        )
        self.assertTrue(await gaps._gap_has_transfer_record(result, result["slug"]))

    async def test_normal_transfer_never_runs_episode_recovery(self):
        class FakeHDHive:
            force_transfer = None

            async def unlock_and_transfer(self, slug, folder_id, force_transfer=False):
                self.force_transfer = force_transfer
                return {"status": "transferred", "message": "整包转存已提交", "data": {}}

            async def close(self):
                return None

        client = FakeHDHive()
        recovery = AsyncMock(side_effect=AssertionError("normal transfer must not split or recover episodes"))
        with (
            patch.object(gaps, "HDHiveClient", return_value=client),
            patch.object(gaps, "_recover_existing_gap_episode_files", recovery),
            patch.object(gaps, "_refresh_clouddrive_after_gap_transfer", AsyncMock(return_value={"skipped": True})),
            patch.object(gaps, "_notify_gap_hdhive_transfer", AsyncMock()),
            patch.object(gaps, "add_subscribe_log", AsyncMock()),
            patch.object(gaps, "save_gap_transfer_mark", AsyncMock()),
        ):
            response = await gaps.download_gap_hdhive(self.payload(force_transfer=True))

        self.assertEqual("success", response["status"])
        self.assertFalse(client.force_transfer)
        self.assertEqual("full_resource", response["data"]["transfer_mode"])
        recovery.assert_not_awaited()

    async def test_already_owned_recovery_requires_existing_mark(self):
        class FakeHDHive:
            force_transfer = None

            async def unlock_and_transfer(self, slug, folder_id, force_transfer=False):
                self.force_transfer = force_transfer
                return {"status": "already_owned", "message": "已转存过", "data": {}}

            async def close(self):
                return None

        async def run_once(force_transfer: bool, recovery: AsyncMock):
            client = FakeHDHive()
            with (
                patch.object(gaps, "HDHiveClient", return_value=client),
                patch.object(gaps, "_recover_already_owned_gap_transfer", recovery),
                patch.object(gaps, "_refresh_clouddrive_after_gap_transfer", AsyncMock(return_value={"skipped": True})),
                patch.object(gaps, "_notify_gap_hdhive_transfer", AsyncMock()),
                patch.object(gaps, "add_subscribe_log", AsyncMock()),
                patch.object(gaps, "save_gap_transfer_mark", AsyncMock()),
            ):
                response = await gaps.download_gap_hdhive(self.payload(force_transfer=force_transfer))
            return client, response

        recovery = AsyncMock(return_value={"ok": True, "message": "已重新接收"})
        client, response = await run_once(True, recovery)
        self.assertEqual("success", response["status"])
        self.assertFalse(client.force_transfer)
        recovery.assert_not_awaited()

        result = self.result()
        await database.save_gap_transfer_mark(result["slug"], resource_title=result["title"], status="transferred")
        recovery.reset_mock()
        client, response = await run_once(False, recovery)
        self.assertEqual("success", response["status"])
        self.assertFalse(client.force_transfer)
        recovery.assert_not_awaited()

        client, response = await run_once(True, recovery)
        self.assertEqual("success", response["status"])
        self.assertTrue(client.force_transfer)
        recovery.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
