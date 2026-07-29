import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.config import settings
from app.services.cloud115 import Cloud115Client
from app.services.hdhive import HDHiveClient, _extract_ed2k_urls


ED2K_ONE = "ed2k://|file|Movie.One.2026.2160p.mkv|123456|0123456789ABCDEF0123456789ABCDEF|/"
ED2K_TWO = "ed2k://|file|Movie.Two.2026.2160p.mkv|654321|FEDCBA9876543210FEDCBA9876543210|/"


def json_response(status_code: int, data: dict) -> httpx.Response:
    request = httpx.Request("POST", "http://symedia.test/api")
    return httpx.Response(status_code, json=data, request=request)


class HDHiveEd2KExtractionTests(unittest.TestCase):
    def test_extracts_nested_urls_and_deduplicates(self):
        payload = {
            "data": {
                "links": [
                    f"资源一：{ED2K_ONE}",
                    {"duplicate": ED2K_ONE.lower()},
                    {"escaped": ED2K_TWO.replace("/", r"\/")},
                ]
            },
            "message": "解锁成功",
        }

        self.assertEqual([ED2K_ONE, ED2K_TWO], _extract_ed2k_urls(payload))

    def test_ignores_non_ed2k_values(self):
        self.assertEqual([], _extract_ed2k_urls({"url": "https://115.com/s/example", "success": True}))


class Cloud115OfflineTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def client(get_response: httpx.Response, post_response: httpx.Response | None = None):
        fake_http = SimpleNamespace(
            get=AsyncMock(return_value=get_response),
            post=AsyncMock(return_value=post_response),
            aclose=AsyncMock(),
        )
        client = Cloud115Client.__new__(Cloud115Client)
        client.cookie = "COOKIE"
        client._client = fake_http
        return client, fake_http

    async def test_offline_uses_sign_time_and_target_folder(self):
        client, fake_http = self.client(
            json_response(200, {"state": True, "sign": "SIGN", "time": 123456}),
            json_response(200, {"state": True, "info_hash": "HASH"}),
        )

        result = await client.create_offline_task(f"  {ED2K_ONE}  ", "98765")

        self.assertTrue(result["state"])
        self.assertTrue(result["success"])
        self.assertEqual("98765", result["_target_folder"])
        fake_http.get.assert_awaited_once()
        get_args, get_kwargs = fake_http.get.await_args
        self.assertEqual(Cloud115Client.OFFLINE_SPACE_URL, get_args[0])
        self.assertEqual("COOKIE", get_kwargs["headers"]["Cookie"])
        fake_http.post.assert_awaited_once()
        post_args, post_kwargs = fake_http.post.await_args
        self.assertEqual(Cloud115Client.OFFLINE_ADD_URL, post_args[0])
        self.assertEqual(
            {
                "url": ED2K_ONE,
                "wp_path_id": "98765",
                "sign": "SIGN",
                "time": 123456,
            },
            post_kwargs["data"],
        )

    async def test_signature_failure_does_not_submit(self):
        client, fake_http = self.client(json_response(200, {"state": False, "error_msg": "Cookie 失效"}))

        result = await client.create_offline_task(ED2K_ONE, "0")

        self.assertFalse(result["state"])
        self.assertIn("Cookie 失效", result["error"])
        fake_http.post.assert_not_awaited()

    async def test_add_task_requires_true_state(self):
        client, _ = self.client(
            json_response(200, {"state": True, "sign": "SIGN", "time": 123456}),
            json_response(200, {"state": False, "error_msg": "错误的链接", "errcode": 10004}),
        )

        result = await client.create_offline_task(ED2K_ONE, "0")

        self.assertFalse(result["state"])
        self.assertFalse(result["success"])
        self.assertEqual(10004, result["errcode"])
        self.assertEqual("错误的链接", result["error"])

    async def test_invalid_ed2k_is_rejected_before_network(self):
        client, fake_http = self.client(json_response(200, {"state": True}))

        result = await client.create_offline_task("ed2k://invalid", "0")

        self.assertFalse(result["state"])
        self.assertIn("格式无效", result["error"])
        fake_http.get.assert_not_awaited()

    def test_success_state_compatibility(self):
        for value in (True, 1, "1", "true", "success"):
            with self.subTest(value=value):
                self.assertTrue(Cloud115Client._state_ok(value))


class HDHiveSymediaEd2KTransferTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_token = settings.symedia_token
        self.original_cookie = settings.symedia_cookie
        settings.symedia_token = "TOKEN"
        settings.symedia_cookie = ""

    def tearDown(self):
        settings.symedia_token = self.original_token
        settings.symedia_cookie = self.original_cookie

    @staticmethod
    def client(response: httpx.Response):
        fake_http = SimpleNamespace(
            post=AsyncMock(return_value=response),
            aclose=AsyncMock(),
        )
        client = HDHiveClient.__new__(HDHiveClient)
        client.api_key = ""
        client.mode = "symedia"
        client.symedia_url = "http://symedia.test"
        client.symedia_cloud_type = "channel_115"
        client.proxy = ""
        client._client = fake_http
        return client, fake_http

    async def test_search_queries_115_and_ed2k_and_merges_results(self):
        client, _ = self.client(json_response(200, {}))
        calls: list[dict] = []

        async def search_request(payload: dict):
            calls.append(payload)
            if payload["cloud_type"] == "channel_115":
                return json_response(
                    200,
                    {
                        "hdhive": {
                            "items": [
                                {
                                    "title": "115 资源",
                                    "pan_type": "115",
                                    "slug": "original-115",
                                    "resource_url": "https://hdhive.com/resource/115/shared",
                                },
                                {
                                    "title": "应过滤的夸克资源",
                                    "pan_type": "quark",
                                    "resource_url": "https://hdhive.com/resource/quark/skip",
                                },
                            ]
                        }
                    },
                )
            return json_response(
                200,
                {
                    "hdhive": {
                        "items": [
                            {
                                "title": "ED2K 资源",
                                "resource_url": "https://hdhive.com/resource/ed2k/unique",
                            },
                            {
                                "title": "聚合响应里的 115 资源",
                                "resource_url": "https://hdhive.com/resource/115/inferred",
                            },
                            {
                                "title": "重复资源",
                                "pan_type": "ed2k",
                                "resource_url": "https://hdhive.com/resource/115/shared/",
                            },
                        ]
                    }
                },
            )

        client._symedia_search_request = AsyncMock(side_effect=search_request)

        results = await client._symedia_search("Demo", tmdb_id=123, media_type="movie")

        self.assertEqual({"channel_115", "ed2k"}, {call["cloud_type"] for call in calls})
        self.assertEqual(3, len(results))
        self.assertEqual("115", results[0]["pan_type"])
        self.assertEqual("115", results[0]["resource_kind"])
        self.assertFalse(results[0]["is_ed2k"])
        self.assertEqual("ed2k", results[1]["pan_type"])
        self.assertEqual("ed2k", results[1]["resource_kind"])
        self.assertTrue(results[1]["is_ed2k"])
        self.assertEqual(results[1]["resource_url"], results[1]["slug"])
        self.assertEqual("115", results[2]["resource_kind"])
        self.assertFalse(results[2]["is_ed2k"])

    async def test_search_keeps_115_results_when_ed2k_branch_fails(self):
        client, _ = self.client(json_response(200, {}))

        async def search_request(payload: dict):
            if payload["cloud_type"] == "ed2k":
                return json_response(503, {"message": "ED2K 暂时不可用"})
            return json_response(
                200,
                {
                    "hdhive": {
                        "items": [
                            {
                                "title": "115 资源",
                                "pan_type": "115",
                                "resource_url": "https://hdhive.com/resource/115/available",
                            }
                        ]
                    }
                },
            )

        client._symedia_search_request = AsyncMock(side_effect=search_request)

        results = await client._symedia_search("Demo", tmdb_id=123, media_type="movie")

        self.assertEqual(1, len(results))
        self.assertEqual("115", results[0]["resource_kind"])

    async def test_plain_115_response_does_not_submit_offline_task(self):
        client, _ = self.client(json_response(200, {"success": True, "message": "115 转存成功"}))

        with patch(
            "app.services.cloud115.Cloud115Client",
            side_effect=AssertionError("must not create offline client"),
        ):
            result = await client._symedia_transfer("https://hdhive.com/resource/115/demo", "123")

        self.assertEqual("transferred", result["status"])
        self.assertNotIn("transfer_mode", result["data"])

    async def test_nested_ed2k_response_submits_to_requested_folder(self):
        response = json_response(
            200,
            {
                "success": True,
                "message": "解锁成功",
                "data": {"links": [ED2K_ONE, ED2K_ONE]},
            },
        )
        client, _ = self.client(response)
        offline = SimpleNamespace(
            create_offline_task=AsyncMock(return_value={"state": True, "task_id": "TASK"}),
            close=AsyncMock(),
        )

        with patch("app.services.cloud115.Cloud115Client", return_value=offline):
            result = await client._symedia_transfer("https://hdhive.com/resource/ed2k/demo", "456")

        self.assertEqual("transferred", result["status"])
        self.assertEqual("ed2k_offline", result["data"]["transfer_mode"])
        self.assertEqual(ED2K_ONE, result["data"]["ed2k_url"])
        self.assertEqual("456", result["data"]["_target_folder"])
        offline.create_offline_task.assert_awaited_once_with(ED2K_ONE, "456")
        offline.close.assert_awaited_once()

    async def test_ed2k_resource_transfer_uses_ed2k_cloud_type(self):
        response = json_response(200, {"success": True, "message": "ED2K 转存已提交"})
        client, fake_http = self.client(response)

        result = await client._symedia_transfer(
            "https://hdhive.com/resource/ed2k/demo-resource",
            "456",
        )

        self.assertEqual("transferred", result["status"])
        _, request_kwargs = fake_http.post.await_args
        self.assertEqual("ed2k", request_kwargs["json"]["cloud_type"])
        self.assertEqual("456", request_kwargs["json"]["parent_id"])

    async def test_string_success_state_is_accepted(self):
        client, _ = self.client(json_response(200, {"success": True, "data": {"url": ED2K_ONE}}))
        offline = SimpleNamespace(
            create_offline_task=AsyncMock(return_value={"state": "1", "task_id": "TASK"}),
            close=AsyncMock(),
        )

        with patch("app.services.cloud115.Cloud115Client", return_value=offline):
            result = await client._symedia_transfer("https://hdhive.com/resource/ed2k/demo", "456")

        self.assertEqual("transferred", result["status"])

    async def test_offline_exception_is_mapped_to_error(self):
        client, _ = self.client(json_response(200, {"success": True, "data": {"url": ED2K_ONE}}))
        offline = SimpleNamespace(
            create_offline_task=AsyncMock(side_effect=RuntimeError("connection reset")),
            close=AsyncMock(),
        )

        with patch("app.services.cloud115.Cloud115Client", return_value=offline):
            result = await client._symedia_transfer("https://hdhive.com/resource/ed2k/demo", "456")

        self.assertEqual("error", result["status"])
        self.assertIn("connection reset", result["message"])

    async def test_offline_failure_is_returned_as_error(self):
        client, _ = self.client(json_response(200, {"success": True, "data": {"url": ED2K_ONE}}))
        offline = SimpleNamespace(
            create_offline_task=AsyncMock(return_value={"state": False, "error_msg": "错误的链接"}),
            close=AsyncMock(),
        )

        with patch("app.services.cloud115.Cloud115Client", return_value=offline):
            result = await client._symedia_transfer("https://hdhive.com/resource/ed2k/demo", "789")

        self.assertEqual("error", result["status"])
        self.assertIn("错误的链接", result["message"])
        self.assertEqual("789", result["data"]["_target_folder"])

    async def test_http_failure_does_not_submit_embedded_link(self):
        client, _ = self.client(json_response(500, {"success": False, "data": {"url": ED2K_ONE}}))

        with patch(
            "app.services.cloud115.Cloud115Client",
            side_effect=AssertionError("must not submit failed response"),
        ):
            result = await client._symedia_transfer("https://hdhive.com/resource/ed2k/demo", "0")

        self.assertEqual("error", result["status"])
        self.assertIn("HTTP 500", result["message"])

    async def test_business_failure_in_success_response_is_error(self):
        client, _ = self.client(
            json_response(
                200,
                {
                    "success": True,
                    "message": "转存失败，没有成功转存的链接",
                    "data": {"failed_links": [ED2K_ONE]},
                },
            )
        )

        with patch(
            "app.services.cloud115.Cloud115Client",
            side_effect=AssertionError("must not submit a failed Symedia response"),
        ):
            result = await client._symedia_transfer("https://hdhive.com/resource/ed2k/demo", "0")

        self.assertEqual("error", result["status"])
        self.assertIn("转存失败", result["message"])


class HDHiveOpenAPIEd2KTransferTests(unittest.IsolatedAsyncioTestCase):
    async def test_openapi_ed2k_unlock_uses_offline_submit(self):
        client = HDHiveClient.__new__(HDHiveClient)
        client.mode = "openapi"
        client.unlock = AsyncMock(return_value={"data": {"full_url": ED2K_ONE}})
        client._submit_ed2k_offline = AsyncMock(
            return_value={"status": "transferred", "message": "ED2K 已提交", "data": {}}
        )

        result = await client.unlock_and_transfer("resource-slug", "folder-123")

        self.assertEqual("transferred", result["status"])
        client._submit_ed2k_offline.assert_awaited_once_with(
            [ED2K_ONE],
            "folder-123",
            ED2K_ONE,
            transfer_data={"data": {"full_url": ED2K_ONE}},
        )


if __name__ == "__main__":
    unittest.main()
