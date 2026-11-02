"""Private local tracking for accepted self-host social uploads.

Only request identifiers and their owner are stored, never provider credentials,
content, captions or media. SQLite commits survive process restarts.
"""
from pathlib import Path
from contextlib import closing
import os
import sqlite3


class SocialPostStore:
    def __init__(self, path):
        self.path = Path(path)

    def _check_path(self):
        # The default layout is .cache/social-posts/requests.sqlite3: checking
        # the file alone would still let a linked .cache redirect private state.
        if any(path.is_symlink() for path in (self.path, self.path.parent, self.path.parent.parent)):
            raise OSError("Social tracking state and its private parents must not be symlinks")

    def record(self, request_id, owner):
        if not isinstance(request_id, str) or not request_id or len(request_id) > 1024:
            raise ValueError("Invalid social request identifier")
        if owner is not None and not isinstance(owner, str):
            raise ValueError("Invalid social request owner")
        self._check_path()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Create privately before SQLite opens it; existing files are preserved.
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            fd = None
        if fd is not None:
            os.close(fd)
        with closing(sqlite3.connect(self.path, timeout=5.0)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS requests (request_id TEXT PRIMARY KEY, owner TEXT)")
            db.execute("INSERT OR IGNORE INTO requests(request_id, owner) VALUES (?, ?)", (request_id, owner))
            row = db.execute("SELECT owner FROM requests WHERE request_id = ?", (request_id,)).fetchone()
            if row != (owner,):
                raise ValueError("Social request owner cannot be changed")

    def lookup(self, request_id):
        self._check_path()
        if not self.path.exists():
            return False, None
        # Never create an empty database merely because an unknown ID is polled.
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)) as db:
            row = db.execute("SELECT owner FROM requests WHERE request_id = ?", (request_id,)).fetchone()
        return (False, None) if row is None else (True, row[0])
