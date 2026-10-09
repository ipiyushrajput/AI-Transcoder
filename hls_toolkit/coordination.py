"""Cross-process ownership of jobs, through flock'd lock files.

A queued or running API job holds an exclusive lock on its own lock file for as
long as it is alive. The kernel drops the lock the moment the owning process
exits — cleanly, by crash or by ``kill -9`` — so "is anyone still running this
job?" has a reliable answer even after the server dies: whoever restarts the
server can tell an abandoned job from one another process is still working on.

The files live next to the CPU budget's (``$WZ_CPU_SLOT_DIR``), so every job on
the machine agrees on where to look.
"""
import fcntl
import os
import re
from pathlib import Path
from typing import Optional

from hls_toolkit.cpu_budget import DEFAULT_SLOT_DIR

_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def coordination_dir() -> Path:
    return Path(os.getenv("WZ_CPU_SLOT_DIR") or DEFAULT_SLOT_DIR)


def _job_lock_path(job_id: str) -> Path:
    if not _SAFE_JOB_ID.match(str(job_id)):
        raise ValueError(f"Unsafe job id for a lock file: {job_id!r}")
    return coordination_dir() / "jobs" / f"{job_id}.lock"


class JobLock:
    """Exclusive, process-lifetime ownership of one job."""

    def __init__(self, job_id: str):
        self.path = _job_lock_path(job_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd: Optional[int] = os.open(str(self.path),
                                          os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._fd)
            self._fd = None
            raise RuntimeError(f"Job {job_id} is already owned by another process.")

    def release(self) -> None:
        """Give up ownership. Call only once the job's final state is recorded."""
        if self._fd is None:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        os.close(self._fd)
        self._fd = None


def job_is_owned(job_id: str) -> bool:
    """True when some live process currently owns `job_id`."""
    try:
        path = _job_lock_path(job_id)
    except ValueError:
        return False
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


__all__ = ["JobLock", "job_is_owned", "coordination_dir"]
