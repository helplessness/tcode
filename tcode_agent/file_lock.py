"""Cross-process exclusive locks; no thread-only or unlocked fallback.

Keep POSIX flock semantics. Windows locks byte zero via the standard CRT;
the byte may lie beyond EOF, so acquiring a lock never modifies the file.
Callers own the handle, flush buffered writes before unlocking, and retain
their existing thread/reentrancy guards. Lock files must not be unlinked.
"""

import asyncio
import errno
import math
import os
import time
from contextlib import contextmanager
from pathlib import Path

_WINDOWS = os.name == "nt"
if _WINDOWS:
    import msvcrt
else:
    import fcntl


def _windows_lock(fd: int, mode: int) -> None:
    position = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, mode, 1)
    finally:
        os.lseek(fd, position, os.SEEK_SET)


class FileLockTimeout(TimeoutError):
    """Only an exhausted lock-contention deadline, not a filesystem I/O timeout."""


def lock_file(handle, *, blocking: bool = True, timeout: float | None = None) -> None:
    """Acquire exclusively; contention raises BlockingIOError or TimeoutError.

    The default is an unbounded wait, matching flock(LOCK_EX). Do not use
    CRT LK_LOCK: its implicit ten retries would change that contract.
    """
    if timeout is not None and (not math.isfinite(timeout) or timeout < 0 or not blocking):
        raise ValueError("timeout requires blocking=True and must be nonnegative")
    fd = handle.fileno()
    if not _WINDOWS and blocking and timeout is None:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        try:
            if _WINDOWS:
                _windows_lock(fd, msvcrt.LK_NBLCK)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            if not blocking:
                raise BlockingIOError(errno.EAGAIN, "file lock is held") from exc
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise FileLockTimeout("timed out acquiring file lock") from exc
            time.sleep(0.05 if remaining is None else min(0.05, remaining))


def unlock_file(handle) -> None:
    """Release a successfully acquired lock; errors are not suppressed."""
    if _WINDOWS:
        _windows_lock(handle.fileno(), msvcrt.LK_UNLCK)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class StateLockBusy(TimeoutError):
    """A state transaction did not acquire ownership; do not report a saved state."""

    code = "state_lock_busy"

    def __init__(self):
        # Do not expose workspace paths, identifiers or credentials to clients.
        super().__init__("状态正在被其他操作占用，本次操作未取得写入锁，请稍后重试。")


def _in_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def lock_state_file(handle, *, timeout: float = 30.0) -> None:
    """Bound short state transactions; never sleep on a running event loop.

    Background callers wait up to timeout. Direct async callers fail before
    entering the transaction on contention; no detached worker is created.
    Long-running build/maintenance locks keep the explicit low-level API.
    """
    try:
        lock_file(handle, timeout=0.0 if _in_event_loop() else timeout)
    except FileLockTimeout as exc:
        raise StateLockBusy() from exc


@contextmanager
def state_thread_lock(lock):
    """Match the file-lock policy for thread guards, preserving RLock reentry."""
    acquired = lock.acquire(blocking=False) if _in_event_loop() else lock.acquire(timeout=30.0)
    if not acquired:
        raise StateLockBusy()
    try:
        yield
    finally:
        lock.release()


def append_locked_text(path, text: str) -> None:
    """Short append under a sidecar; retain the legacy data lock on POSIX.

    Windows readers must not encounter a mandatory byte lock on log contents.
    Windows upgrades require stopping old data-lock writers before restart.
    """
    path = Path(path)
    with Path(str(path) + ".lock").open("a+b") as guard:
        lock_state_file(guard, timeout=1.0)
        try:
            with path.open("a", encoding="utf-8") as stream:
                if not _WINDOWS:
                    lock_state_file(stream, timeout=1.0)
                try:
                    stream.write(text)
                    stream.flush()
                finally:
                    if not _WINDOWS:
                        unlock_file(stream)
        finally:
            unlock_file(guard)
