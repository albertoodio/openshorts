"""Thumbnail retention uses synthetic media only, including cross-process locks."""
import fcntl
import multiprocessing
import os
from pathlib import Path
import time

import pytest

from thumbnail_retention import (
    SessionBusy, cleanup_sessions, legacy_upload_session_id, session_lease,
)

OLD = 7200
SESSION = "00000000-1111-2222-3333-444444444444"


def _hold_lease(lock_dir, session_id, ready, release):
    with session_lease(lock_dir, session_id):
        ready.set()
        release.wait(15)


def _hold_exclusive(lock_dir, session_id, ready, release):
    path = Path(lock_dir)
    path.mkdir(parents=True, exist_ok=True)
    with (path / (session_id + ".lock")).open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        ready.set()
        release.wait(15)


@pytest.fixture
def disk(tmp_path):
    thumbs = tmp_path / "output" / "thumbnails"
    uploads = tmp_path / "uploads"
    locks = tmp_path / "private-locks"
    thumbs.mkdir(parents=True)
    uploads.mkdir()
    return thumbs, uploads, locks


def _age_tree(path, seconds=OLD):
    stamp = time.time() - seconds
    for entry in [*path.rglob("*"), path] if path.is_dir() else [path]:
        if not entry.is_symlink():
            os.utime(entry, (stamp, stamp))


def _session(disk, session_id, output=16, upload=12, age=OLD):
    thumbs, uploads, _ = disk
    out = thumbs / session_id
    up = uploads / ("thumb_" + session_id)
    out.mkdir()
    up.mkdir()
    (out / "image.jpg").write_bytes(b"x" * output)
    (up / "reference.jpg").write_bytes(b"y" * upload)
    _age_tree(out, age)
    _age_tree(up, age)
    return out, up


def _clean(disk, **options):
    thumbs, uploads, locks = disk
    return cleanup_sessions(str(thumbs), str(uploads), str(locks),
                            retention_seconds=options.pop("ttl", 3600),
                            output_cap_bytes=options.pop("output_cap", 0),
                            upload_cap_bytes=options.pop("upload_cap", 0), **options)


def test_expiry_removes_complete_session_preserving_mount_roots(disk):
    out, up = _session(disk, "expired")
    removed = _clean(disk)
    assert removed == ["expired"]
    assert not out.exists() and not up.exists()
    assert disk[0].is_dir() and disk[1].is_dir()
    assert _clean(disk) == []


def test_recent_nested_file_keeps_whole_session(disk):
    out, up = _session(disk, "recent")
    os.utime(up / "reference.jpg", None)
    assert _clean(disk) == []
    assert out.exists() and up.exists()


def test_upload_only_session_and_legacy_flat_source_expire(disk):
    _, uploads, _ = disk
    refs = uploads / ("thumb_" + SESSION)
    refs.mkdir()
    (refs / "face.jpg").write_bytes(b"x")
    source = uploads / ("thumb_" + SESSION + "_video.mp4")
    source.write_bytes(b"source")
    _age_tree(refs)
    _age_tree(source)
    assert _clean(disk) == [SESSION]
    assert not refs.exists() and not source.exists()


def test_uploaded_source_without_reference_directory_expires(disk):
    path = disk[1] / ("thumb_" + SESSION + "_video.mp4")
    path.write_bytes(b"source")
    _age_tree(path)
    assert _clean(disk) == [SESSION]
    assert not path.exists()


@pytest.mark.parametrize("kind", ["output", "upload"])
def test_cap_evicts_oldest_whole_session_and_counts_nested_bytes(disk, kind):
    old = _session(disk, "old", output=50, upload=50, age=120)
    new = _session(disk, "new", output=50, upload=50, age=60)
    opts = {kind + "_cap": 60, "ttl": 3600}
    assert _clean(disk, **opts) == ["old"]
    assert all(not path.exists() for path in old)
    assert all(path.exists() for path in new)


def test_caps_include_non_thumbnail_directory_usage(disk):
    out, up = _session(disk, "one", output=20, upload=20, age=60)
    assert _clean(disk, output_cap=100, output_used_bytes=110) == ["one"]
    assert not out.exists() and not up.exists()


def test_active_lease_in_other_process_survives_age_and_both_caps(disk):
    out, up = _session(disk, "active")
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    process = ctx.Process(target=_hold_lease, args=(str(disk[2]), "active", ready, release))
    process.start()
    try:
        assert ready.wait(10)
        assert _clean(disk, output_cap=1, upload_cap=1) == []
        assert out.exists() and up.exists()
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
    assert process.exitcode == 0
    # Completion refreshes idle retention, but a hard cap may now reclaim it.
    assert _clean(disk) == []
    assert _clean(disk, output_cap=1) == ["active"]


def test_new_operation_fails_promptly_while_cleaner_holds_exclusive_lock(disk):
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    process = ctx.Process(target=_hold_exclusive, args=(str(disk[2]), "busy", ready, release))
    process.start()
    try:
        assert ready.wait(10)
        with pytest.raises(SessionBusy):
            with session_lease(str(disk[2]), "busy"):
                pytest.fail("exclusive lease was ignored")
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
    assert process.exitcode == 0


def test_finished_activity_refreshes_idle_clock(disk):
    _session(disk, "used")
    with session_lease(str(disk[2]), "used"):
        pass
    assert _clean(disk) == []
    assert _clean(disk, now=time.time() + OLD) == ["used"]


def test_explicitly_protected_source_session_survives(disk):
    out, up = _session(disk, "job-source")
    assert _clean(disk, output_cap=1, upload_cap=1, protected_ids={"job-source"}) == []
    assert out.exists() and up.exists()


def test_does_not_follow_symlinks_or_touch_unrelated_uploads(disk, tmp_path):
    outside = tmp_path / "keep"
    outside.mkdir()
    target = outside / "face.jpg"
    target.write_bytes(b"private")
    (disk[0] / "linked").symlink_to(outside, target_is_directory=True)
    (disk[1] / "thumb_linked").symlink_to(outside, target_is_directory=True)
    ordinary = disk[1] / "ordinary_video.mp4"
    ordinary.write_bytes(b"job")
    _age_tree(ordinary)
    out, _ = _session(disk, "expired")
    (out / "outside.jpg").symlink_to(target)
    _age_tree(out)
    assert _clean(disk) == ["expired"]
    assert target.read_bytes() == b"private"
    assert ordinary.exists()
    assert (disk[0] / "linked").is_symlink()


@pytest.mark.parametrize("session_id", ["../escape", "/absolute", "bad/name", "", "a" * 129])
def test_lease_rejects_path_traversal(disk, session_id):
    with pytest.raises(ValueError):
        with session_lease(str(disk[2]), session_id):
            pass
    assert not disk[2].exists()


def test_legacy_upload_parser_is_unambiguous():
    assert legacy_upload_session_id("thumb_" + SESSION + "_movie_name.mp4") == SESSION
    assert legacy_upload_session_id("thumb_custom_slug_movie.mp4") is None
    assert legacy_upload_session_id("other_" + SESSION + "_movie.mp4") is None


def test_completed_operation_paths_are_refreshed_after_snapshot(disk, monkeypatch):
    import thumbnail_retention
    out = disk[0] / "racing"
    out.mkdir()
    (out / "image.jpg").write_bytes(b"old")
    _age_tree(out)
    original = thumbnail_retention._lock_file
    added = False

    def lock_after_operation(lock_dir, session_id):
        nonlocal added
        if session_id == "racing" and not added:
            added = True
            with session_lease(lock_dir, session_id):
                refs = disk[1] / "thumb_racing"
                refs.mkdir()
                (refs / "face.jpg").write_bytes(b"new")
        return original(lock_dir, session_id)

    monkeypatch.setattr(thumbnail_retention, "_lock_file", lock_after_operation)
    assert _clean(disk, output_cap=1) == ["racing"]
    assert not out.exists()
    assert not (disk[1] / "thumb_racing").exists()


def test_symlinked_root_is_refused_even_with_upload_only_session(disk, tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    target = external / "orphan"
    target.mkdir()
    (target / "keep.jpg").write_bytes(b"keep")
    disk[0].rmdir()
    disk[0].symlink_to(external, target_is_directory=True)
    refs = disk[1] / "thumb_orphan"
    refs.mkdir()
    (refs / "face.jpg").write_bytes(b"face")
    _age_tree(refs)
    _age_tree(target)
    with pytest.raises(OSError, match="roots cannot be symlinks"):
        _clean(disk)
    assert (target / "keep.jpg").exists() and refs.exists()
