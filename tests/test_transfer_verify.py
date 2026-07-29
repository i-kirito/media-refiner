import unittest

from app.config import settings
from app.services import transfer_verify


class FakeCloud115Client:
    search_items: list[dict] = []

    async def list_share_files(self, *args, **kwargs):
        return {"state": True, "data": []}

    async def list_files(self, folder_id: str, limit: int = 115):
        return {"state": True, "data": []}

    async def search_files(self, keyword: str, limit: int = 40):
        return {"state": True, "data": list(self.search_items)}

    async def close(self):
        return None


class TransferVerifyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_cookie = settings.cloud115_cookie
        self.original_client = transfer_verify.Cloud115Client
        settings.cloud115_cookie = "test-cookie"
        transfer_verify.Cloud115Client = FakeCloud115Client

    def tearDown(self):
        transfer_verify.Cloud115Client = self.original_client
        settings.cloud115_cookie = self.original_cookie

    @staticmethod
    def result() -> dict:
        return {
            "title": "海贼王：黄金岛冒险 (2000)",
            "remark": "海贼王：黄金岛冒险.One Piece.The Movie.2000.2160p {tmdbid-19576}",
        }

    async def test_recent_account_item_confirms_delayed_library_move(self):
        FakeCloud115Client.search_items = [
            {
                "n": "海贼王：黄金岛冒险 (2000) {tmdbid=19576}",
                "cid": "old-folder",
                "t": "900",
            },
            {
                "n": "海贼王：黄金岛冒险 (2026) {tmdbid=19576}",
                "cid": "new-folder",
                "t": "1200",
            },
        ]

        verify = await transfer_verify.verify_hdhive_transfer_visible(
            self.result(),
            {"data": {"_target_folder": "staging"}},
            attempts=1,
            delay_seconds=0,
            account_search_after=1000,
        )

        self.assertTrue(verify["ok"])
        self.assertEqual("account_search", verify["mode"])
        self.assertEqual("new-folder", verify["account_item"]["file_id"])

    async def test_old_same_title_does_not_confirm_new_transfer(self):
        FakeCloud115Client.search_items = [
            {
                "n": "海贼王：黄金岛冒险 (2000) {tmdbid=19576}",
                "cid": "old-folder",
                "t": "900",
            }
        ]

        verify = await transfer_verify.verify_hdhive_transfer_visible(
            self.result(),
            {"data": {"_target_folder": "staging"}},
            attempts=1,
            delay_seconds=0,
            account_search_after=1000,
        )

        self.assertFalse(verify["ok"])
        self.assertTrue(verify["pending"])


if __name__ == "__main__":
    unittest.main()
