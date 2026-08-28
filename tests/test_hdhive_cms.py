import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from app.config import settings
from app.services.hdhive import HDHiveClient


class HDHiveCmsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.token_path = self.temp_path / "hdhive-openapi.json"
        self.cache_path = self.temp_path / "hdhive-openapi.cache.json"
        self.source_token = {
            "access_token": "source-access-token",
            "refresh_token": "source-refresh-token",
        }
        self.token_path.write_text(json.dumps(self.source_token), encoding="utf-8")
        self._original_settings = {}
        for key, value in {
            "hdhive_mode": "cms",
            "hdhive_cms_authx_url": "https://authx.test",
            "hdhive_cms_token_path": str(self.token_path),
            "tmdb_api_key": "tmdb-test-key",
            "cloud115_folder_id": "default-folder",
        }.items():
            self._original_settings[key] = getattr(settings, key, None)
            object.__setattr__(settings, key, value)
        self.clients: list[HDHiveClient] = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        for key, value in self._original_settings.items():
            object.__setattr__(settings, key, value)
        self.temp_dir.cleanup()

    async def make_client(self, handler) -> HDHiveClient:
        client = HDHiveClient()
        await client.close()
        client._cms_token_cache_path = str(self.cache_path)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.clients.append(client)
        return client

    async def test_cms_search_normalizes_allowed_resource_types(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual("POST", request.method)
            self.assertEqual("/api/hdhive/resources", request.url.path)
            self.assertEqual(
                {"resource_type": "movie", "tmdb_id": "603", "access_token": "source-access-token"},
                json.loads(request.content),
            )
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {"slug": "skip-quark", "title": "夸克", "pan_type": "quark"},
                        {"slug": "keep-115", "title": "115", "pan_type": "channel_115"},
                        {
                            "slug": "https://hdhive.com/resource/ed2k/keep-ed2k",
                            "title": "ED2K",
                            "pan_type": "ed2k",
                        },
                        {"slug": "skip-unknown", "title": "未知"},
                    ],
                },
            )

        client = await self.make_client(handler)

        results = await client.search(tmdb_id=603, media_type="movie")

        self.assertEqual(2, len(results))
        self.assertEqual("keep-115", results[0]["hdhive_slug"])
        self.assertEqual("keep-115", results[0]["slug"])
        self.assertEqual("115", results[0]["pan_type"])
        self.assertEqual("https://hdhive.com/resource/115/keep-115", results[0]["resource_url"])
        self.assertEqual("keep-ed2k", results[1]["hdhive_slug"])
        self.assertEqual("keep-ed2k", results[1]["slug"])
        self.assertEqual("ed2k", results[1]["pan_type"])
        self.assertTrue(results[1]["is_ed2k"])
        self.assertEqual("https://hdhive.com/resource/ed2k/keep-ed2k", results[1]["resource_url"])

    async def test_cms_search_refreshes_after_401_then_retries_once(self):
        requests: list[tuple[str, dict]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            requests.append((request.url.path, body))
            if request.url.path == "/api/hdhive/resources" and len(requests) == 1:
                self.assertEqual("source-access-token", body["access_token"])
                return httpx.Response(401, json={"message": "access token expired"})
            if request.url.path == "/api/hdhive/oauth/refresh":
                self.assertEqual({"refresh_token": "source-refresh-token"}, body)
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "access_token": "refreshed-access-token",
                            "refresh_token": "refreshed-refresh-token",
                        },
                    },
                )
            self.assertEqual("/api/hdhive/resources", request.url.path)
            self.assertEqual("refreshed-access-token", body["access_token"])
            return httpx.Response(
                200,
                json={"success": True, "data": [{"slug": "fresh-115", "pan_type": "115"}]},
            )

        client = await self.make_client(handler)

        results = await client.search(tmdb_id=1)

        self.assertEqual(["fresh-115"], [item["slug"] for item in results])
        self.assertEqual(
            ["/api/hdhive/resources", "/api/hdhive/oauth/refresh", "/api/hdhive/resources"],
            [path for path, _ in requests],
        )
        self.assertEqual(self.source_token, json.loads(self.token_path.read_text(encoding="utf-8")))
        self.assertEqual("refreshed-access-token", json.loads(self.cache_path.read_text(encoding="utf-8"))["access_token"])

    async def test_cms_unlock_builds_full_url_and_uses_existing_cloud115_transfer(self):
        client = await self.make_client(lambda request: httpx.Response(500, json={"unexpected": True}))
        client._cms_post = AsyncMock(
            return_value={
                "success": True,
                "data": {
                    "url": "https://115.com/s/share-code",
                    "access_code": "pick-code",
                },
            }
        )
        cloud115 = SimpleNamespace(
            extract_share_code=Mock(return_value={"share_code": "share-code", "receive_code": "pick-code"}),
            transfer_from_share=AsyncMock(return_value={"state": True}),
            close=AsyncMock(),
        )

        with patch("app.services.cloud115.Cloud115Client", return_value=cloud115):
            result = await client.unlock_and_transfer(
                "https://hdhive.com/resource/115/cms-resource",
                "target-folder",
            )

        client._cms_post.assert_awaited_once_with("/resources/unlock", {"slug": "cms-resource"})
        cloud115.extract_share_code.assert_called_once_with("https://115.com/s/share-code?password=pick-code")
        cloud115.transfer_from_share.assert_awaited_once_with("share-code", "pick-code", "target-folder")
        cloud115.close.assert_awaited_once()
        self.assertEqual("transferred", result["status"])

    async def test_cms_reauthorization_error_is_actionable_and_token_safe(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual("/api/hdhive/oauth/refresh", request.url.path)
            return httpx.Response(
                400,
                json={
                    "success": False,
                    "code": "openapi_reauth_required",
                    "message": "refresh_token=source-refresh-token was revoked",
                },
            )

        client = await self.make_client(handler)

        with self.assertRaises(RuntimeError) as raised:
            await client._cms_refresh_token()

        message = str(raised.exception)
        self.assertIn("重新完成影巢 OpenAPI 授权", message)
        self.assertNotIn("source-refresh-token", message)
        self.assertNotIn("source-access-token", message)

    async def test_keyword_search_without_tmdb_key_returns_empty_without_request(self):
        object.__setattr__(settings, "tmdb_api_key", "")
        client = await self.make_client(
            lambda request: (_ for _ in ()).throw(AssertionError(f"unexpected request: {request.url}"))
        )
        client._cms_post = AsyncMock()

        results = await client.search(keyword="沙丘", media_type="movie")

        self.assertEqual([], results)
        client._cms_post.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
