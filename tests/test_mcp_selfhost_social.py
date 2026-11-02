"""Offline regressions for MCP BYOK profiles and anonymous post tracking.

The publisher and status provider are simulated: these tests never publish or
contact a provider. Cloud must still derive the target profile from the owner.
"""
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import HTTPException

import app as app_module
import mcp_server


class TestMcpPublishProfile(unittest.IsolatedAsyncioTestCase):
    async def _publish(self, billing, profile, supplied):
        resolved = []

        def handler(request):
            body = json.loads(request.content)
            try:
                target = app_module.resolve_post_profile(profile, body.get("user_id"))
            except HTTPException as exc:
                return httpx.Response(exc.status_code, json={"detail": exc.detail})
            resolved.append(target)
            return httpx.Response(200, json={"request_id": "synthetic-request"})

        args = {"job_id": "synthetic-job", "clip_index": 0, "platforms": ["youtube"]}
        if supplied is not None:
            args["user_id"] = supplied
        with patch.object(app_module, "BILLING_ENABLED", billing):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler), base_url="http://test"
            ) as client:
                result, is_error = await mcp_server._tool_publish_clip(client, args)
        return result, is_error, resolved

    def test_schema_exposes_optional_byok_profile(self):
        tool = next(t for t in mcp_server.TOOLS if t["name"] == "publish_clip")
        self.assertEqual(tool["inputSchema"]["properties"]["user_id"]["type"], "string")
        self.assertNotIn("user_id", tool["inputSchema"]["required"])

    async def test_selfhost_profile_reaches_the_publisher(self):
        result, is_error, resolved = await self._publish(False, None, "my-profile")
        self.assertFalse(is_error)
        self.assertEqual(result["request_id"], "synthetic-request")
        self.assertEqual(resolved, ["my-profile"])

    async def test_selfhost_missing_profile_is_rejected(self):
        result, is_error, resolved = await self._publish(False, None, None)
        self.assertTrue(is_error)
        self.assertEqual(result["http_status"], 400)
        self.assertEqual(resolved, [])

    async def test_cloud_ignores_the_callers_profile(self):
        _, is_error, resolved = await self._publish(True, "os_owner", "os_other")
        self.assertFalse(is_error)
        self.assertEqual(resolved, ["os_owner"])

    async def test_cloud_never_falls_back_to_the_callers_profile(self):
        result, is_error, resolved = await self._publish(True, None, "os_other")
        self.assertTrue(is_error)
        self.assertEqual(result["http_status"], 409)
        self.assertEqual(resolved, [])


class TestSelfhostPostStatus(unittest.IsolatedAsyncioTestCase):
    async def _status(self, owners, caller, upload_key="synthetic-key"):
        response = Mock()
        response.json.return_value = {
            "status": "completed",
            "results": [{"platform": "youtube", "success": True}],
        }
        vendor = AsyncMock()
        vendor.get.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = vendor
        with (
            patch.object(app_module, "_post_request_owners", owners),
            patch.object(app_module, "_owner_id", AsyncMock(return_value=caller)),
            patch.object(app_module, "resolve_upload_post", AsyncMock(return_value=(upload_key, None))),
            patch.object(app_module, "_log_post_failures", Mock()),
            patch.object(app_module.httpx, "AsyncClient", return_value=context),
        ):
            result = await app_module.social_post_status("synthetic-request", request=None)
        return result, vendor

    async def test_known_anonymous_request_reaches_the_status_provider(self):
        result, vendor = await self._status({"synthetic-request": None}, None)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["results"][0]["success"])
        vendor.get.assert_awaited_once()

    async def test_unknown_request_never_queries_the_provider(self):
        result, vendor = await self._status({}, None)
        self.assertEqual(result, {"status": "unknown"})
        vendor.get.assert_not_awaited()

    async def test_missing_key_never_queries_the_provider(self):
        result, vendor = await self._status({"synthetic-request": None}, None, None)
        self.assertEqual(result, {"status": "unknown"})
        vendor.get.assert_not_awaited()

    async def test_another_owner_is_rejected(self):
        with self.assertRaises(HTTPException) as exc:
            await self._status({"synthetic-request": "owner-a"}, "owner-b")
        self.assertEqual(exc.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
