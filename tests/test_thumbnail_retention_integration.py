"""Exercise the API's cleanup wiring without providers or real media."""
import asyncio
from io import BytesIO
from pathlib import Path
import threading
import time

import pytest
from starlette.datastructures import UploadFile

import app


@pytest.fixture()
def storage(tmp_path, monkeypatch):
    output = tmp_path / "output"
    thumbs = output / "thumbnails"
    uploads = tmp_path / "uploads"
    thumbs.mkdir(parents=True)
    uploads.mkdir()
    monkeypatch.setattr(app, "OUTPUT_DIR", str(output))
    monkeypatch.setattr(app, "THUMBNAILS_DIR", str(thumbs))
    monkeypatch.setattr(app, "UPLOAD_DIR", str(uploads))
    monkeypatch.setattr(app, "THUMBNAIL_LEASE_DIR", str(tmp_path / "leases"))
    monkeypatch.setattr(app, "BILLING_ENABLED", False)
    monkeypatch.setattr(app, "thumbnail_sessions", {})
    monkeypatch.setattr(app, "JOB_RETENTION_SECONDS", 1)
    monkeypatch.setattr(app, "OUTPUT_MAX_GB", 0)
    monkeypatch.setattr(app, "UPLOADS_MAX_GB", 0)
    return thumbs, uploads


def test_new_uploads_and_references_share_session_retention(storage):
    thumbs, uploads = storage
    upload = UploadFile(file=BytesIO(b"synthetic video"), filename="../../video.mp4")
    with app._thumbnail_lease("session-1"):
        path, minutes = asyncio.run(app._save_thumbnail_upload(upload, "session-1"))
    assert Path(path) == uploads / "thumb_session-1/source_video.mp4"
    assert minutes is None
    (thumbs / "session-1").mkdir()
    (thumbs / "session-1/image.jpg").write_bytes(b"image")
    (uploads / "thumb_session-1/face.jpg").write_bytes(b"reference")
    app.thumbnail_sessions["session-1"] = {"video_path": path}
    assert app._sweep_thumbnail_sessions(time.time() + 10) == ["session-1"]
    assert not (uploads / "thumb_session-1").exists()
    assert not (thumbs / "session-1").exists()
    assert thumbs.is_dir()
    assert app.thumbnail_sessions == {}


def test_generic_upload_sweeps_preserve_legacy_sources_while_active(storage, monkeypatch):
    _, uploads = storage
    path = uploads / "historical-untracked-title.mp4"
    path.write_bytes(b"source")
    with app._thumbnail_lease("legacy-session"):
        app._sweep_expired_uploads(time.time() + 10)
        monkeypatch.setattr(app, "UPLOADS_MAX_GB", 1 / 1024 ** 3)
        app._enforce_uploads_size_cap()
        assert path.exists()
    app._sweep_expired_uploads(time.time() + 10)
    assert not path.exists()


def test_cancellation_keeps_worker_pinned_until_thread_finishes(storage):
    thumbs, _ = storage
    target = thumbs / "running-session"
    target.mkdir()
    (target / "image.jpg").write_bytes(b"image")
    started = threading.Event()
    finish = threading.Event()

    def worker():
        started.set()
        assert finish.wait(5)
        assert (target / "image.jpg").exists()

    async def scenario():
        task = asyncio.create_task(app._thumbnail_in_executor("running-session", worker))
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert app._sweep_thumbnail_sessions(time.time() + 10) == []
            assert target.exists()
        finally:
            finish.set()
        # asyncio.run waits for the executor to shut down before returning.
    asyncio.run(scenario())
    assert app._sweep_thumbnail_sessions(time.time() + 10) == ["running-session"]


def test_cancel_before_background_start_releases_lease(storage):
    async def never_started():
        pytest.fail("Cancelled coroutine should not start")

    async def scenario():
        task = app._thumbnail_background_task("queued-session", never_started())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)  # task's lease-release callback
        with app._thumbnail_upload_cleanup() as idle:
            assert idle
    asyncio.run(scenario())


def test_publish_is_scheduled_before_response_and_releases_lease_on_completion(storage, monkeypatch):
    from fastapi import Request
    from unittest import mock
    thumbs, uploads = storage
    target = thumbs / "publishing-session"
    target.mkdir()
    (target / "image.jpg").write_bytes(b"synthetic image")
    source = uploads / "video.mp4"
    source.write_bytes(b"synthetic video")
    app.thumbnail_sessions["publishing-session"] = {"video_path": str(source)}
    monkeypatch.setattr(app, "resolve_upload_post", mock.AsyncMock(return_value=("test-key", "profile")))
    monkeypatch.setattr(app, "_check_social_post_rate", mock.AsyncMock())
    client = mock.MagicMock()
    client.__enter__.return_value.post.return_value = mock.Mock(status_code=200, json=lambda: {"id": "simulated"})
    monkeypatch.setattr(app.httpx, "Client", lambda **kwargs: client)

    async def scenario():
        request = Request({"type": "http", "headers": []})
        response = await app.thumbnail_publish(request, session_id="publishing-session", title="Title",
                                               description="Test", thumbnail_url="publishing-session/image.jpg",
                                               api_key=None, user_id=None)
        # No Starlette response or BackgroundTasks object was ever sent/run.
        tasks = list(app._thumbnail_background_tasks)
        assert tasks
        await asyncio.gather(*tasks)
        assert app.publish_jobs[response["publish_id"]]["status"] == "done"
        with app._thumbnail_upload_cleanup() as idle:
            assert idle
        app.publish_jobs.pop(response["publish_id"])
    asyncio.run(scenario())
    client.__enter__.return_value.post.assert_called_once()
