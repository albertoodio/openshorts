"""Session-scoped Thumbnail Studio retention, with cross-process activity leases.

Lock files live outside public media and are deliberately never unlinked: deleting
one while another process holds its inode would allow a second, unrelated lock.
Only cooperating Thumbnail Studio operations and cleaners use these locks.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import fcntl
import os
from pathlib import Path
import re
import shutil
import stat
import time
from typing import Iterable, Iterator, Optional

_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z", re.ASCII)
_LEGACY_UPLOAD = re.compile(
    r"thumb_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})_.+\Z")


class SessionBusy(RuntimeError):
    """A cleaner currently holds the session exclusively; retry the operation."""


def legacy_upload_session_id(filename: str) -> Optional[str]:
    """Recognize old source uploads; new sources live in a session directory."""
    match = _LEGACY_UPLOAD.fullmatch(filename)
    return match.group(1) if match else None


def _valid_session(session_id: str) -> bool:
    return isinstance(session_id, str) and _SESSION.fullmatch(session_id) is not None


def _lock_file(lock_dir: str, session_id: str) -> int:
    if not _valid_session(session_id):
        raise ValueError("Invalid thumbnail session id")
    path = Path(lock_dir)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError("Thumbnail lease directory cannot be a symlink")
    fd = os.open(path / (session_id + ".lock"),
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError("Thumbnail lease must be a regular file")
    return fd


def _activity_mtime(lock_dir: str, session_id: str) -> float:
    try:
        info = (Path(lock_dir) / (session_id + ".active")).lstat()
        return info.st_mtime if stat.S_ISREG(info.st_mode) else 0.0
    except FileNotFoundError:
        return 0.0


def _touch_activity(lock_dir: str, session_id: str) -> None:
    fd = os.open(Path(lock_dir) / (session_id + ".active"),
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("Thumbnail activity marker must be a regular file")
        os.utime(fd, None)
    finally:
        os.close(fd)


@contextmanager
def session_lease(lock_dir: str, session_id: str, exclusive: bool = False) -> Iterator[None]:
    """Pin a session until work (including worker threads) has really finished.

    Shared leases allow concurrent reads/generation. Nonblocking acquisition
    keeps an async caller from blocking its event loop during a deletion.
    Touching the lease at both ends restarts the idle TTL after active work.
    """
    fd = _lock_file(lock_dir, session_id)
    acquired = False
    try:
        try:
            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise SessionBusy("Thumbnail session is being cleaned; retry") from exc
            raise
        acquired = True
        _touch_activity(lock_dir, session_id)
        yield
    finally:
        try:
            if acquired:
                try:
                    _touch_activity(lock_dir, session_id)
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@dataclass(frozen=True)
class _Session:
    session_id: str
    output_paths: tuple
    upload_paths: tuple


def _children(root: Path) -> list:
    if root.is_symlink():
        return []
    try:
        return list(root.iterdir())
    except FileNotFoundError:
        return []


def _sessions(thumbnails_dir: Path, uploads_dir: Path) -> list:
    output = {}
    uploads = {}
    for path in _children(thumbnails_dir):
        if _valid_session(path.name) and path.is_dir() and not path.is_symlink():
            output.setdefault(path.name, []).append(path)
    for path in _children(uploads_dir):
        if path.is_symlink():
            continue
        if path.name.startswith("thumb_") and path.is_dir():
            session_id = path.name[len("thumb_"):]
        elif path.is_file():
            session_id = legacy_upload_session_id(path.name)
        else:
            continue
        if _valid_session(session_id):
            uploads.setdefault(session_id, []).append(path)
    return [_Session(sid, tuple(output.get(sid, [])), tuple(uploads.get(sid, [])))
            for sid in sorted(output.keys() | uploads.keys())]


def _current_session(session_id: str, thumbnails_dir: Path, uploads_dir: Path) -> _Session:
    """Refresh paths after exclusive acquisition; a writer may have just ended."""
    output = thumbnails_dir / session_id
    reference = uploads_dir / ("thumb_" + session_id)
    output_paths = (output,) if output.is_dir() and not output.is_symlink() else ()
    upload_paths = [reference] if reference.is_dir() and not reference.is_symlink() else []
    upload_paths.extend(path for path in _children(uploads_dir)
                        if not path.is_symlink() and path.is_file()
                        and legacy_upload_session_id(path.name) == session_id)
    return _Session(session_id, output_paths, tuple(upload_paths))


def _tree_stats(path: Path) -> tuple:
    """Bytes and newest mtime, without following links out of a session."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return 0, 0.0
    if stat.S_ISLNK(info.st_mode):
        return 0, 0.0
    if stat.S_ISREG(info.st_mode):
        return info.st_size, info.st_mtime
    if not stat.S_ISDIR(info.st_mode):
        return 0, info.st_mtime
    size, newest = 0, info.st_mtime
    for child in path.iterdir():
        child_size, child_mtime = _tree_stats(child)
        size += child_size
        newest = max(newest, child_mtime)
    return size, newest


def _session_stats(session: _Session, lease_mtime: float) -> tuple:
    output_bytes, upload_bytes, newest = 0, 0, lease_mtime
    for paths, is_output in ((session.output_paths, True), (session.upload_paths, False)):
        for path in paths:
            size, mtime = _tree_stats(path)
            if is_output:
                output_bytes += size
            else:
                upload_bytes += size
            newest = max(newest, mtime)
    return output_bytes, upload_bytes, newest


def _remove(paths: tuple) -> None:
    for path in paths:
        try:
            if path.is_symlink():
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass


def cleanup_sessions(thumbnails_dir: str, uploads_dir: str, lock_dir: str,
                     retention_seconds: float, output_cap_bytes: int,
                     upload_cap_bytes: int, now: Optional[float] = None,
                     output_used_bytes: Optional[int] = None,
                     upload_used_bytes: Optional[int] = None,
                     protected_ids: Iterable[str] = ()) -> list:
    """Expire idle sessions, then evict oldest whole sessions under disk pressure.

    Caps are the existing output/uploads budgets. Pass total directory usage to
    account for non-thumbnail jobs too; omitted totals count thumbnail sessions
    only. Active sessions can temporarily keep usage above a cap. Return only
    IDs whose media was removed. Roots and stable lease files are preserved.
    """
    now = time.time() if now is None else now
    if Path(thumbnails_dir).is_symlink() or Path(uploads_dir).is_symlink():
        raise OSError("Thumbnail retention roots cannot be symlinks")
    sweep_fd = _lock_file(str(Path(lock_dir) / ".sweep"), "cleaner")
    try:
        try:
            fcntl.flock(sweep_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return []
        protected = set(protected_ids)
        sessions = _sessions(Path(thumbnails_dir), Path(uploads_dir))
        entries = []
        for session in sessions:
            lease_mtime = _activity_mtime(lock_dir, session.session_id)
            entries.append((_session_stats(session, lease_mtime), session))
        entries.sort(key=lambda entry: (entry[0][2], entry[1].session_id))
        output_used = (sum(stats[0] for stats, _ in entries)
                       if output_used_bytes is None else output_used_bytes)
        upload_used = (sum(stats[1] for stats, _ in entries)
                       if upload_used_bytes is None else upload_used_bytes)
        removed = []
        for _stats, session in entries:
            if session.session_id in protected:
                continue
            fd = _lock_file(lock_dir, session.session_id)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                # A request may have finished since discovery. Recheck under
                # the lock before acting on either age or byte accounting.
                session = _current_session(session.session_id, Path(thumbnails_dir), Path(uploads_dir))
                lease_mtime = _activity_mtime(lock_dir, session.session_id)
                out_size, up_size, newest = _session_stats(session, lease_mtime)
                output_used += out_size - _stats[0]
                upload_used += up_size - _stats[1]
                expired = now - newest > retention_seconds
                over_output = output_cap_bytes > 0 and output_used > output_cap_bytes and out_size > 0
                over_upload = upload_cap_bytes > 0 and upload_used > upload_cap_bytes and up_size > 0
                if not (expired or over_output or over_upload):
                    continue
                _remove(session.output_paths + session.upload_paths)
                output_used -= out_size
                upload_used -= up_size
                removed.append(session.session_id)
            finally:
                os.close(fd)
        return removed
    finally:
        os.close(sweep_fd)
