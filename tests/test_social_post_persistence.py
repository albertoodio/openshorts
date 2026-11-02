"""Offline durability regressions; providers are mocks and sockets are blocked.

The app handlers are compiled from their real AST without decorators so these
checks do not boot ML models, read dotenv or create application runtime dirs.
"""
import ast
import asyncio
import concurrent.futures
import __future__
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from social_post_store import SocialPostStore

ROOT = Path(__file__).resolve().parents[1]


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


def real_handlers(path):
    selected = {"_remember_post_request", "post_to_socials", "social_post_status"}
    body = []
    for node in ast.parse((ROOT / "app.py").read_text()).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in selected:
            node.decorator_list = []
            body.append(node)
    assert len(body) == len(selected)
    response = Mock()
    response.status_code = 202
    response.json.return_value = {"request_id": "accepted-id"}
    status_response = Mock()
    status_response.json.return_value = {"status": "completed", "results": [{"platform": "youtube", "success": True}]}
    status_client = AsyncMock()
    status_client.get.return_value = status_response
    context = AsyncMock()
    context.__aenter__.return_value = status_client
    video = path / "output" / "job" / "clip.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"synthetic video fixture, never uploaded")
    ns = {
        "os": os, "asyncio": asyncio, "HTTPException": HTTPException,
        "BILLING_ENABLED": False, "_post_request_owners": {},
        "_selfhost_post_store": SocialPostStore(path / "private" / "requests.sqlite3"),
        "_ensure_job_files": AsyncMock(), "_assert_job_owner": AsyncMock(),
        "_check_social_post_rate": AsyncMock(),
        "_platforms_needing_reconnect": AsyncMock(return_value=[]),
        "resolve_upload_post": AsyncMock(return_value=("synthetic-provider-key", None)),
        "resolve_post_profile": lambda forced, supplied: forced or supplied,
        "_owner_id": AsyncMock(return_value=None), "youtube_title": str,
        "_post_video_blocking": Mock(return_value=response),
        "_log_post_failures": Mock(), "TIKTOK_POST_MODE": "MEDIA_UPLOAD",
        "OUTPUT_DIR": str(path / "output"),
        "jobs": {"job": {"result": {"clips": [{"video_url": "/videos/job/clip.mp4", "title": "Synthetic"}]}}},
        "httpx": SimpleNamespace(AsyncClient=Mock(return_value=context)),
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), "real_app_handlers", "exec", flags=__future__.annotations.compiler_flag), ns)
    req = SimpleNamespace(job_id="job", clip_index=0, api_key=None, user_id="synthetic-profile", platforms=["youtube"], title=None, description=None, scheduled_date=None, timezone="UTC")
    return ns, req, status_client


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "private" / "requests.sqlite3"
        self.store = SocialPostStore(self.path)
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_survives_separate_process_and_stores_no_credentials(self):
        self.store.record("accepted-id", None)
        code = "from pathlib import Path; from social_post_store import SocialPostStore; import sys,json; print(json.dumps(SocialPostStore(Path(sys.argv[1])).lookup('accepted-id')))"
        result = subprocess.run([sys.executable, "-c", code, str(self.path)], cwd=ROOT, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), [True, None])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertNotIn(b"synthetic-provider-key", self.path.read_bytes())
        self.assertEqual(self.store.lookup("missing"), (False, None))

    def test_concurrent_records_are_not_lost(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda n: SocialPostStore(self.path).record(f"request-{n}", None), range(20)))
        for n in range(20):
            self.assertEqual(self.store.lookup(f"request-{n}"), (True, None))

    def test_linked_cache_grandparent_cannot_redirect_state_into_media(self):
        directory = Path(self.directory.name)
        media = directory / "served-media"
        media.mkdir()
        cache = directory / ".cache"
        cache.symlink_to(media, target_is_directory=True)
        store = SocialPostStore(cache / "social-posts" / "requests.sqlite3")
        with self.assertRaises(OSError):
            store.record("accepted-id", None)
        with self.assertRaises(OSError):
            store.lookup("accepted-id")
        self.assertEqual(list(media.iterdir()), [])

    def test_owner_cannot_change(self):
        self.store.record("accepted-id", "owner-a")
        with self.assertRaises(ValueError):
            self.store.record("accepted-id", "owner-b")
        self.assertEqual(self.store.lookup("accepted-id"), (True, "owner-a"))

    def test_symlink_state_is_rejected_without_touching_its_target(self):
        self.path.parent.mkdir()
        target = Path(self.directory.name) / "owned-file"
        target.write_text("unchanged")
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            self.store.record("accepted-id", None)
        self.assertEqual(target.read_text(), "unchanged")


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.ns, self.req, self.client = real_handlers(Path(self.directory.name))
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    async def test_accepted_post_is_found_after_memory_and_store_restart(self):
        out = await self.ns["post_to_socials"](self.req, None)
        self.assertEqual(out["request_id"], "accepted-id")
        store_path = self.ns["_selfhost_post_store"].path
        self.ns["_post_request_owners"] = {}
        self.ns["_selfhost_post_store"] = SocialPostStore(store_path)
        status = await self.ns["social_post_status"]("accepted-id", None)
        self.assertEqual(status["status"], "completed")
        self.ns["_post_video_blocking"].assert_called_once()
        self.client.get.assert_awaited_once()

    async def test_unknown_request_does_not_create_state_or_call_provider(self):
        status = await self.ns["social_post_status"]("unknown-id", None)
        self.assertEqual(status, {"status": "unknown"})
        self.client.get.assert_not_awaited()
        self.assertFalse(self.ns["_selfhost_post_store"].path.exists())

    async def test_cloud_does_not_read_selfhost_persistence(self):
        self.ns["_selfhost_post_store"].record("accepted-id", None)
        self.ns["BILLING_ENABLED"] = True
        self.ns["_owner_id"] = AsyncMock(return_value="cloud-owner")
        status = await self.ns["social_post_status"]("accepted-id", None)
        self.assertEqual(status, {"status": "unknown"})
        self.client.get.assert_not_awaited()

    async def test_cloud_cannot_read_another_owners_request(self):
        self.ns["BILLING_ENABLED"] = True
        self.ns["_post_request_owners"] = {"accepted-id": "owner-a"}
        self.ns["_owner_id"] = AsyncMock(return_value="owner-b")
        with self.assertRaises(HTTPException) as error:
            await self.ns["social_post_status"]("accepted-id", None)
        self.assertEqual(error.exception.status_code, 404)
        self.client.get.assert_not_awaited()

    async def test_disk_failure_does_not_disguise_provider_acceptance_as_failed_post(self):
        self.ns["_selfhost_post_store"].record = Mock(side_effect=OSError("disk full"))
        out = await self.ns["post_to_socials"](self.req, None)
        self.assertEqual(out["request_id"], "accepted-id")
        self.assertEqual(out["tracking_persistence"], "pending")
        self.assertIn("do not submit", out["tracking_warning"])
        self.ns["_post_video_blocking"].assert_called_once()
        self.assertIn("accepted-id", self.ns["_post_request_owners"])

    async def test_failed_state_read_tells_caller_to_retry_status_not_publication(self):
        self.ns["_selfhost_post_store"].lookup = Mock(side_effect=OSError("unavailable"))
        out = await self.ns["social_post_status"]("accepted-id", None)
        self.assertEqual(out["status"], "tracking_unavailable")
        self.assertIn("do not submit", out["message"])
        self.client.get.assert_not_awaited()

    async def test_cloud_collision_preserves_cached_owner(self):
        self.ns["BILLING_ENABLED"] = True
        self.ns["_post_request_owners"] = {"accepted-id": "owner-a"}
        self.ns["_selfhost_post_store"].record = Mock(side_effect=AssertionError("cloud ledger forbidden"))
        self.assertFalse(self.ns["_remember_post_request"]("accepted-id", "owner-b"))
        self.assertEqual(self.ns["_post_request_owners"], {"accepted-id": "owner-a"})
        self.ns["_selfhost_post_store"].record.assert_not_called()

    async def test_selfhost_collision_preserves_cached_and_durable_owner(self):
        self.ns["_selfhost_post_store"].record("accepted-id", "owner-a")
        self.ns["_post_request_owners"] = {"accepted-id": "owner-a"}
        self.assertFalse(self.ns["_remember_post_request"]("accepted-id", "owner-b"))
        self.assertEqual(self.ns["_post_request_owners"], {"accepted-id": "owner-a"})
        self.assertEqual(self.ns["_selfhost_post_store"].lookup("accepted-id"), (True, "owner-a"))

    async def test_durable_collision_never_caches_rejected_owner_after_restart(self):
        self.ns["_selfhost_post_store"].record("accepted-id", "owner-a")
        self.ns["_post_request_owners"] = {}
        self.assertFalse(self.ns["_remember_post_request"]("accepted-id", "owner-b"))
        self.assertNotIn("accepted-id", self.ns["_post_request_owners"])
        self.assertEqual(self.ns["_selfhost_post_store"].lookup("accepted-id"), (True, "owner-a"))
        self.ns["_owner_id"] = AsyncMock(return_value="owner-b")
        with self.assertRaises(HTTPException) as error:
            await self.ns["social_post_status"]("accepted-id", None)
        self.assertEqual(error.exception.status_code, 404)
        self.client.get.assert_not_awaited()

    async def test_unreadable_existing_ledger_does_not_cache_unverified_owner(self):
        self.ns["_selfhost_post_store"].record = Mock(side_effect=OSError("disk unavailable"))
        self.ns["_selfhost_post_store"].lookup = Mock(side_effect=OSError("unreadable"))
        self.assertFalse(self.ns["_remember_post_request"]("accepted-id", "owner-b"))
        self.assertNotIn("accepted-id", self.ns["_post_request_owners"])

    async def test_cloud_acceptance_never_writes_selfhost_ledger(self):
        self.ns["BILLING_ENABLED"] = True
        self.ns["_owner_id"] = AsyncMock(return_value="cloud-owner")
        self.ns["_selfhost_post_store"].record = Mock(side_effect=AssertionError("cloud ledger forbidden"))
        out = await self.ns["post_to_socials"](self.req, None)
        self.assertEqual(out["request_id"], "accepted-id")
        self.ns["_selfhost_post_store"].record.assert_not_called()
        self.assertEqual(self.ns["_post_request_owners"]["accepted-id"], "cloud-owner")


if __name__ == "__main__":
    unittest.main()
