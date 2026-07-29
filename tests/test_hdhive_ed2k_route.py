import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.config import settings
from app.routers import download


ED2K_URL = "ed2k://|file|Movie.One.2026.2160p.mkv|123456|0123456789ABCDEF0123456789ABCDEF|/"


class HDHiveEd2KRouteTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def client(result: dict):
        return SimpleNamespace(
            unlock_and_transfer=AsyncMock(return_value=result),
            close=AsyncMock(),
        )

    async def test_quick_transfer_uses_configured_target_folder(self):
        client = self.client(
            {
                "status": "transferred",
                "message": "ED2K 已提交到 115 离线目录",
                "data": {
                    "transfer_mode": "ed2k_offline",
                    "ed2k_url": ED2K_URL,
                    "_target_folder": "98765",
                },
            }
        )

        with (
            patch.object(settings, "cloud115_folder_id", "98765"),
            patch.object(download, "HDHiveClient", return_value=client),
            patch.object(download, "add_subscribe_log", AsyncMock()) as add_log,
            patch.object(download, "_notify_download_result", AsyncMock()) as notify,
        ):
            response = await download.transfer_via_hdhive(
                plan_id="",
                resource_id=ED2K_URL,
                folder_id="",
            )

        self.assertEqual("success", response["status"])
        self.assertEqual("ed2k_offline", response["data"]["data"]["transfer_mode"])
        client.unlock_and_transfer.assert_awaited_once_with(ED2K_URL, "98765")
        client.close.assert_awaited_once()
        add_log.assert_awaited_once()
        self.assertEqual("transfer", add_log.await_args.args[2])
        notify.assert_awaited_once()

    async def test_explicit_folder_overrides_configured_folder(self):
        client = self.client(
            {
                "status": "transferred",
                "message": "ED2K 已提交到 115 离线目录",
                "data": {"transfer_mode": "ed2k_offline"},
            }
        )

        with (
            patch.object(settings, "cloud115_folder_id", "configured"),
            patch.object(download, "HDHiveClient", return_value=client),
            patch.object(download, "add_subscribe_log", AsyncMock()),
            patch.object(download, "_notify_download_result", AsyncMock()),
        ):
            await download.transfer_via_hdhive(
                plan_id="",
                resource_id=ED2K_URL,
                folder_id="selected-folder",
            )

        client.unlock_and_transfer.assert_awaited_once_with(ED2K_URL, "selected-folder")
        client.close.assert_awaited_once()

    async def test_offline_rejection_is_exposed_as_http_error(self):
        client = self.client(
            {
                "status": "error",
                "message": "ED2K 提交 115 离线失败: Cookie 失效",
                "data": {"transfer_mode": "ed2k_offline"},
            }
        )

        with (
            patch.object(download, "HDHiveClient", return_value=client),
            patch.object(download, "add_subscribe_log", AsyncMock()) as add_log,
            patch.object(download, "_notify_download_result", AsyncMock()) as notify,
        ):
            with self.assertRaises(HTTPException) as raised:
                await download.transfer_via_hdhive(
                    plan_id="",
                    resource_id=ED2K_URL,
                    folder_id="0",
                )

        self.assertEqual(500, raised.exception.status_code)
        self.assertIn("Cookie 失效", raised.exception.detail)
        client.close.assert_awaited_once()
        self.assertIn("Cookie 失效", add_log.await_args.args[-1])
        self.assertIn("Cookie 失效", notify.await_args.args[-1])


if __name__ == "__main__":
    unittest.main()
