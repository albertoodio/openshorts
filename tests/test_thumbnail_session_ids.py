"""Thumbnail IDs fail before providers; durable owners survive cold caches."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi.testclient import TestClient

import app
import thumbnail
from security_utils import validate_thumbnail_session_id

INVALID = ["", ".", "..", "../escape", "../../escape", "/absolute", "a/b", "a\\b",
           "a\x00b", "a b", "%2e%2e", "a" * 129, None, 42]


def test_safe_custom_ids_are_preserved():
    for value in ["0b6f2b8e-1c7d-4f7e-9a51-3d2f6c8e9a10", "manual-title", "session_1", "a" * 128]:
        assert validate_thumbnail_session_id(value) == value


@pytest.mark.parametrize("value", INVALID)
def test_workers_reject_before_sdk_import_or_disk(value, monkeypatch, tmp_path):
    provider = mock.Mock(side_effect=AssertionError("Provider must not run"))
    mkdir = mock.Mock(side_effect=AssertionError("Filesystem must not change"))
    monkeypatch.setattr(thumbnail.genai, "Client", provider)
    monkeypatch.setattr(thumbnail.os, "makedirs", mkdir)
    for worker, args in [(thumbnail.generate_thumbnail, ("test-key", "Title", value)),
                         (thumbnail.extract_face_frames, ("video.mp4", value))]:
        with pytest.raises(ValueError):
            worker(*args)
    provider.assert_not_called()
    mkdir.assert_not_called()


@pytest.fixture()
def cloud(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "BILLING_ENABLED", True)
    monkeypatch.setattr(app, "THUMBNAILS_DIR", str(tmp_path))
    monkeypatch.setattr(app, "thumbnail_sessions", {})
    monkeypatch.setattr(app, "resolve_gemini", mock.AsyncMock(return_value="test-key"))
    async def user(request):
        return SimpleNamespace(id=request.headers.get("x-test-owner", "owner-a"), plan="pro")
    monkeypatch.setattr(app, "_user_from_request", user)
    return TestClient(app.app)


def test_cold_cache_cannot_transfer_owner_and_legitimate_owner_can_continue(cloud, tmp_path):
    target = tmp_path / "owned-session"
    target.mkdir()
    marker = target / ".owner"
    marker.write_text("owner-a")
    payload = {"session_id": "owned-session", "title": "Title"}
    response = cloud.post("/api/thumbnail/titles", json=payload, headers={"x-test-owner": "owner-b"})
    assert response.status_code == 404, response.text
    assert marker.read_text() == "owner-a"
    assert app.thumbnail_sessions == {}
    response = cloud.post("/api/thumbnail/titles", json=payload)
    assert response.status_code == 200, response.text
    assert marker.read_text() == "owner-a"
    assert app.thumbnail_sessions["owned-session"]["user_id"] == "owner-a"


def test_generation_cannot_adopt_disk_owner_after_restart(cloud, tmp_path, monkeypatch):
    target = tmp_path / "owned-session"
    target.mkdir()
    (target / ".owner").write_text("owner-a")
    reserve = mock.AsyncMock(side_effect=AssertionError("No charge before ownership check"))
    generate = mock.Mock(side_effect=AssertionError("No provider call"))
    monkeypatch.setattr(app, "reserve_managed_action", reserve)
    monkeypatch.setattr(app, "generate_thumbnail", generate)
    response = cloud.post("/api/thumbnail/generate", data={"session_id": "owned-session", "title": "Title"},
                          headers={"x-test-owner": "owner-b"})
    assert response.status_code == 404, response.text
    reserve.assert_not_called()
    generate.assert_not_called()
    assert (target / ".owner").read_text() == "owner-a"


def test_owner_created_exclusively_and_symlinks_or_unowned_directories_refused(cloud, tmp_path):
    payload = {"session_id": "fresh-session", "title": "Title"}
    assert cloud.post("/api/thumbnail/titles", json=payload).status_code == 200
    marker = tmp_path / "fresh-session/.owner"
    assert marker.read_text() == "owner-a"
    assert marker.stat().st_mode & 0o777 == 0o600
    (tmp_path / "linked-session").symlink_to(tmp_path / "fresh-session", target_is_directory=True)
    (tmp_path / "unowned-session").mkdir()
    (tmp_path / "linked-marker").mkdir()
    (tmp_path / "linked-marker/.owner").symlink_to(marker)
    for sid in ["linked-session", "unowned-session", "linked-marker"]:
        response = cloud.post("/api/thumbnail/titles", json={"session_id": sid, "title": "Title"})
        assert response.status_code == 404, response.text
    assert marker.read_text() == "owner-a"


def test_simultaneous_claims_never_replace_owner(cloud, tmp_path, monkeypatch):
    async def owner(request):
        return request
    monkeypatch.setattr(app, "_owner_id", owner)
    def claim(uid):
        try:
            asyncio.run(app._ensure_thumbnail_session_owner(uid, "racing-session"))
            return uid
        except app.HTTPException as exc:
            assert exc.status_code == 404
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        winners = list(pool.map(claim, ["owner-a", "owner-b"]))
    assert sum(uid is not None for uid in winners) == 1
    marker = tmp_path / "racing-session/.owner"
    assert marker.read_text() == next(uid for uid in winners if uid)


def test_invalid_http_ids_fail_before_provider_resolution(monkeypatch):
    calls = [
        ("/api/thumbnail/generate", {"data": {"session_id": "../escape", "title": "Title"}}),
        ("/api/thumbnail/analyze", {"data": {"session_id": "../escape"}}),
        ("/api/thumbnail/titles", {"json": {"session_id": "../escape", "title": "Title"}}),
        ("/api/thumbnail/describe", {"json": {"session_id": "../escape", "title": "Title"}}),
        ("/api/thumbnail/publish", {"data": {"session_id": "../escape", "title": "Title",
                                             "description": "Test", "thumbnail_url": "/thumbnails/t.jpg"}}),
    ]
    provider = mock.AsyncMock(side_effect=AssertionError("Provider resolution must not run"))
    monkeypatch.setattr(app, "resolve_gemini", provider)
    client = TestClient(app.app)
    for url, kwargs in calls:
        response = client.post(url, **kwargs)
        assert response.status_code == 400, response.text
        assert "Invalid thumbnail session ID" in response.json()["detail"]
    provider.assert_not_called()


def test_publish_rejects_another_sessions_image_and_owner_marker(cloud, tmp_path, monkeypatch):
    own = tmp_path / "own-session"
    other = tmp_path / "other-session"
    own.mkdir()
    other.mkdir()
    (own / ".owner").write_text("owner-a")
    (other / ".owner").write_text("owner-b")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    (other / "image.jpg").write_bytes(b"other user's image")
    app.thumbnail_sessions["own-session"] = {"user_id": "owner-a", "video_path": str(source)}
    monkeypatch.setattr(app, "resolve_upload_post", mock.AsyncMock(return_value=("test-key", "own-profile")))
    monkeypatch.setattr(app, "_check_social_post_rate", mock.AsyncMock())
    schedule = mock.Mock(side_effect=AssertionError("No publish should be scheduled"))
    monkeypatch.setattr(app, "_thumbnail_background_task", schedule)
    for url in ["other-session/image.jpg", "own-session/.owner"]:
        response = cloud.post("/api/thumbnail/publish", data={
            "session_id": "own-session", "title": "Title", "description": "Test",
            "thumbnail_url": url})
        assert response.status_code == 400, response.text
    schedule.assert_not_called()
