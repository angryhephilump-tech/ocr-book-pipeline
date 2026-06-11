"""Work-directory job lock — one active transcription/finalize per folder."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

JOB_LOCK_FILE = ".job_lock"


class JobLockError(RuntimeError):
    """Another process holds the work-dir lock."""


def job_lock_path(work_dir: Path) -> Path:
    return work_dir / JOB_LOCK_FILE


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    else:
        return True


def _read_lock(path: Path) -> tuple[int | None, str | None]:
    if not path.is_file():
        return None, None
    pid: int | None = None
    started: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("pid="):
            try:
                pid = int(line.split("=", 1)[1])
            except ValueError:
                pid = None
        elif line.startswith("started="):
            started = line.split("=", 1)[1]
    return pid, started


def acquire_job_lock(work_dir: Path, *, force: bool = False) -> None:
    """Create lock or raise if another live process holds it."""
    work_dir.mkdir(parents=True, exist_ok=True)
    path = job_lock_path(work_dir)
    if path.is_file():
        pid, started = _read_lock(path)
        if pid is not None and _pid_alive(pid) and not force:
            when = f" since {started}" if started else ""
            raise JobLockError(
                f"Work directory is locked by process {pid}{when}. "
                "Wait for it to finish or remove a stale lock with --force."
            )
        path.unlink(missing_ok=True)
    payload = (
        f"pid={os.getpid()}\n"
        f"started={datetime.now(timezone.utc).isoformat()}\n"
    )
    path.write_text(payload, encoding="utf-8")


def release_job_lock(work_dir: Path) -> None:
    path = job_lock_path(work_dir)
    if not path.is_file():
        return
    pid, _ = _read_lock(path)
    if pid is None or pid == os.getpid():
        path.unlink(missing_ok=True)
