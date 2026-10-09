"""Clean up after jobs that cannot clean up after themselves.

Two kinds of leftovers build up on a long-running server:

* **Scratch folders of dead jobs.** A job removes its ``aitx_*`` scratch folder
  when it ends, but a job that is killed (``kill -9``, out of memory, power
  loss) cannot, and each one can hold a whole downloaded source plus encodes.
  Every scratch folder therefore carries an owner file that its job keeps
  flock'd while alive; the kernel drops the lock when the process dies, so a
  folder whose owner file is unlocked belongs to nobody and is removed.
  Folders kept on purpose by ``--debug`` are marked and never touched, and
  folders without an owner file (not made by this tool) are never touched.

* **Old job logs.** With ``LOG_RETENTION_DAYS`` set, a job's log folder is
  removed once nothing in it has changed for that many days. Off by default.

Both sweeps run at most once every few minutes per machine, from inside
ordinary jobs and at API start-up, so nothing extra needs scheduling.
"""
import fcntl
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

from hls_toolkit.job_context import get_logger

SCRATCH_PREFIX = "aitx_"
OWNER_FILE = ".aitx_owner"
KEEP_FILE = ".aitx_keep"
_MIN_AGE_SECONDS = 300          # never judge a folder younger than this
_SWEEP_INTERVAL_SECONDS = 600


class ScratchOwner:
    """Marks a scratch folder as belonging to a live job, until released."""

    def __init__(self, work_dir):
        self.path = Path(work_dir) / OWNER_FILE
        self._fd: Optional[int] = os.open(str(self.path),
                                          os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        os.write(self._fd, str(os.getpid()).encode())

    def keep(self) -> None:
        """Leave the folder for a person to inspect (``--debug``)."""
        try:
            (self.path.parent / KEEP_FILE).write_text("kept for debugging\n")
        except OSError:
            pass

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def log_retention_days() -> float:
    try:
        return max(0.0, float(os.getenv("LOG_RETENTION_DAYS", "0") or 0))
    except ValueError:
        return 0.0


def _abandoned(folder: Path, now: float) -> bool:
    """True for a scratch folder whose job is gone."""
    owner = folder / OWNER_FILE
    if (folder / KEEP_FILE).exists() or not owner.is_file() or owner.is_symlink():
        return False
    try:
        if now - owner.stat().st_mtime < _MIN_AGE_SECONDS:
            return False
        fd = os.open(str(owner), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False                 # its job is still running
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def sweep_scratch(scratch_root=None, ctx=None) -> int:
    """Remove abandoned scratch folders under `scratch_root`. Returns how many."""
    log = get_logger(ctx)
    root = Path(scratch_root or tempfile.gettempdir())
    now = time.time()
    removed = 0
    try:
        candidates = [p for p in root.iterdir()
                      if p.name.startswith(SCRATCH_PREFIX) and p.is_dir()
                      and not p.is_symlink()]
    except OSError:
        return 0
    for folder in candidates:
        if not _abandoned(folder, now):
            continue
        shutil.rmtree(folder, ignore_errors=True)
        if not folder.exists():
            removed += 1
            log.info(f"Removed scratch folder left by a job that died: {folder}")
    return removed


def _newest_mtime(folder: Path) -> float:
    newest = folder.stat().st_mtime
    for path in folder.rglob("*"):
        try:
            newest = max(newest, path.lstat().st_mtime)
        except OSError:
            pass
    return newest


def sweep_logs(log_root, days: Optional[float] = None, ctx=None) -> int:
    """Remove job log folders untouched for `days` days. Returns how many."""
    log = get_logger(ctx)
    days = log_retention_days() if days is None else days
    root = Path(log_root)
    if days <= 0 or not root.is_dir():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    # Job folders are logs/<channel>_N/ (CLI) or logs/<channel>/<job_id>/ (API).
    for meta in list(root.glob("*/job.json")) + list(root.glob("*/*/job.json")):
        folder = meta.parent
        try:
            if folder.is_symlink() or _newest_mtime(folder) > cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(folder, ignore_errors=True)
        if not folder.exists():
            removed += 1
            parent = folder.parent
            if parent != root:
                try:
                    parent.rmdir()               # only if that channel is now empty
                except OSError:
                    pass
    if removed:
        log.info(f"Removed {removed} job log folder(s) older than {days:g} day(s) "
                 f"(LOG_RETENTION_DAYS).")
    return removed


def run_periodic(scratch_root=None, log_root=None, ctx=None, force: bool = False) -> None:
    """Run both sweeps, at most once per interval across the whole machine."""
    log = get_logger(ctx)
    try:
        from hls_toolkit.coordination import coordination_dir
        stamp = coordination_dir() / "housekeeping.stamp"
        stamp.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(stamp), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as e:
        log.debug(f"Housekeeping skipped: {e}")
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return                       # another job is sweeping right now
        last = os.fstat(fd).st_size and os.stat(str(stamp)).st_mtime
        if not force and last and time.time() - last < _SWEEP_INTERVAL_SECONDS:
            return
        os.ftruncate(fd, 0)
        os.write(fd, str(time.time()).encode())
        os.utime(str(stamp))
        sweep_scratch(scratch_root, ctx=ctx)
        if log_root:
            sweep_logs(log_root, ctx=ctx)
    except Exception as e:
        log.warning(f"Housekeeping failed (the job is unaffected): {e}")
    finally:
        os.close(fd)


__all__ = ["ScratchOwner", "sweep_scratch", "sweep_logs", "run_periodic",
           "log_retention_days"]
