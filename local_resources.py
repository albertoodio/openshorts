"""Opt-in shared render admission for a small self-hosted workstation.

The same flock file can be mounted by containers and opened by host render
commands. File locks are released by the OS on cancellation or process death;
the inode is kept so two holders can never lock different replacement files.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from typing import AsyncIterator, Optional


class ResourceWaitTimeout(TimeoutError):
    """No work started; the operator can retry after the current render ends."""


def _open_lock(path: str) -> int:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError("Resource lock must be a regular file")
    return fd


@asynccontextmanager
async def resource_slot(lock_path: Optional[str], timeout: float = 300.0,
                        label: str = "render", poll: float = 0.1) -> AsyncIterator[None]:
    """Wait without blocking the event loop; disabled when no path is set."""
    if not lock_path:
        yield
        return
    if not math.isfinite(timeout) or timeout < 0 or not math.isfinite(poll) or poll <= 0:
        raise ValueError("Resource timeout/poll must be finite and nonnegative/positive")
    fd = _open_lock(lock_path)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ResourceWaitTimeout("Render resource is busy; retry after the current task finishes") from None
                await asyncio.sleep(min(poll, max(0, deadline - time.monotonic())))
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"label": label, "started_at": time.time()}).encode())
        yield
    finally:
        os.close(fd)


def resource_status(lock_path: Optional[str]) -> dict:
    if not lock_path:
        return {"enabled": False, "busy": False}
    fd = _open_lock(lock_path)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return {"enabled": True, "busy": False}
        except BlockingIOError:
            return {"enabled": True, "busy": True}
    finally:
        os.close(fd)


async def _run(args: argparse.Namespace) -> int:
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not command:
        raise ValueError("Provide a command after --")
    async with resource_slot(args.lock, args.timeout, "host-render"):
        # subprocess.run forwards terminal interaction; the lease is held until
        # the command exits, including nonzero exits and KeyboardInterrupt.
        return subprocess.run(command).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Serialize local media tasks with the API")
    subs = parser.add_subparsers(dest="action", required=True)
    run = subs.add_parser("run", help="Wait for a slot, then run a host render command")
    run.add_argument("--lock", required=True)
    run.add_argument("--timeout", type=float, default=300)
    run.add_argument("command", nargs=argparse.REMAINDER)
    status = subs.add_parser("status")
    status.add_argument("--lock", required=True)
    args = parser.parse_args()
    try:
        if args.action == "status":
            print(json.dumps(resource_status(args.lock)))
            return 0
        return asyncio.run(_run(args))
    except ResourceWaitTimeout as exc:
        print(str(exc), file=sys.stderr)
        return 75
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
