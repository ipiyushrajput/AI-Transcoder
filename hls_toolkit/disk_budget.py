"""Scratch-disk reservations shared by every job on the server.

Parallel jobs used to start whenever they liked and fill the scratch disk
together, so one job running out of space mid-encode could take others down
with it. Now a job estimates the scratch space it needs (source size x factor)
and reserves it before it downloads anything:

* it starts at once if the space is there, after subtracting what other live
  jobs have reserved but not yet written;
* it waits, logging why, if the space would be there once other jobs finish;
* it fails immediately, saying how much it needs and how much exists, if it
  could never fit.

Reservations are flock'd files beside the CPU budget's, so they are shared by
every process and vanish with a process that dies.
"""
import fcntl
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from hls_toolkit.job_context import TranscodeError, get_logger

DEFAULT_SPACE_FACTOR = 3.0              # downloaded source + clips + merged + segments
DEFAULT_MIN_FREE_BYTES = 1024 ** 3      # always leave 1 GiB for everything else
DEFAULT_MAX_WAIT_SECONDS = 1800.0
_POLL_SECONDS = 5.0
_mutex = threading.Lock()


def _env_float(name: str, configured, default: float) -> float:
    for value in (os.getenv(name), configured):
        if value in (None, ""):
            continue
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            pass
    return default


def space_factor(configured=None) -> float:
    return _env_float("WZ_DISK_SPACE_FACTOR", configured, DEFAULT_SPACE_FACTOR)


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(str(path)).free


def _used_bytes(path: Optional[str]) -> int:
    """Bytes a job has already written to its scratch folder."""
    total = 0
    if not path:
        return 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _human(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} PB"


class DiskReservation:
    """Space held for one job until :meth:`release`."""

    def __init__(self, fd: Optional[int], path: Optional[Path], nbytes: int):
        self._fd, self._path, self.nbytes = fd, path, nbytes

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if self._path is not None:
                self._path.unlink()
        except FileNotFoundError:
            pass
        os.close(self._fd)
        self._fd = None


def _ledger_dir(work_root: Path) -> Path:
    from hls_toolkit.coordination import coordination_dir
    return coordination_dir() / "disk" / str(os.stat(str(work_root)).st_dev)


def _live_reservations(ledger: Path):
    """[(reserved bytes, work dir)] for live owners; stale entries are removed."""
    live = []
    for entry in ledger.glob("*.json"):
        try:
            fd = os.open(str(entry), os.O_RDWR | os.O_NOFOLLOW)
        except FileNotFoundError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                data = json.loads(entry.read_text() or "{}")
                live.append((int(data.get("bytes", 0)), data.get("work_dir")))
            except (OSError, ValueError):
                pass
        else:
            try:
                entry.unlink()               # its owner is gone
            except FileNotFoundError:
                pass
        finally:
            os.close(fd)
    return live


def reserve(work_root, work_dir, needed_bytes: int, label: str = "",
            ctx=None, should_abort: Optional[Callable[[], bool]] = None,
            max_wait_seconds: Optional[float] = None,
            min_free_bytes: Optional[int] = None) -> DiskReservation:
    """Reserve `needed_bytes` of scratch on `work_root`'s filesystem."""
    log = get_logger(ctx)
    work_root = Path(work_root)
    needed = int(max(0, needed_bytes))
    min_free = int(_env_float("WZ_DISK_MIN_FREE_BYTES", min_free_bytes,
                              DEFAULT_MIN_FREE_BYTES))
    max_wait = _env_float("WZ_DISK_WAIT_SECONDS", max_wait_seconds,
                          DEFAULT_MAX_WAIT_SECONDS)
    try:
        ledger = _ledger_dir(work_root)
        ledger.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # Without a shared ledger, at least refuse a job that cannot fit now.
        log.warning(f"Disk reservations unavailable ({e}); checking free space only.")
        free = _free_bytes(work_root)
        if needed + min_free > free:
            raise TranscodeError(
                f"Not enough scratch space on {work_root}: this job needs about "
                f"{_human(needed)} and {_human(free)} is free (keeping "
                f"{_human(min_free)} spare).", stage="FETCHING_INPUT")
        return DiskReservation(None, None, needed)

    started = time.monotonic()
    last_report = 0.0
    while True:
        if should_abort is not None and should_abort():
            from hls_toolkit.job_context import JobCancelled
            raise JobCancelled("Cancelled while waiting for disk space")
        mutex = os.open(str(ledger / "ledger.lock"), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            with _mutex:
                fcntl.flock(mutex, fcntl.LOCK_EX)
                free = _free_bytes(work_root)
                others = _live_reservations(ledger)
                used = [_used_bytes(path) for _, path in others]
                outstanding = sum(max(0, r - u) for (r, _), u in zip(others, used))
                available = free - outstanding - min_free
                if needed <= available:
                    entry = ledger / f"{uuid.uuid4().hex}.json"
                    fd = os.open(str(entry), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644)
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    os.write(fd, json.dumps({"bytes": needed, "work_dir": str(work_dir),
                                             "label": label, "pid": os.getpid()}).encode())
                    log.info(f"Reserved {_human(needed)} of scratch on {work_root} "
                             f"({_human(free)} free, {_human(outstanding)} held by "
                             f"{len(others)} other job(s)).")
                    return DiskReservation(fd, entry, needed)
                potential = free + sum(used) - min_free
                if needed > potential:
                    raise TranscodeError(
                        f"Not enough scratch space on {work_root}: this job needs about "
                        f"{_human(needed)} (source x {space_factor():g}), and even with "
                        f"every other job finished only {_human(max(0, potential))} "
                        f"would be free (keeping {_human(min_free)} spare). Free up "
                        f"space, point --work-dir / WORK_ROOT at a larger disk, or "
                        f"lower WZ_DISK_SPACE_FACTOR.", stage="FETCHING_INPUT")
        finally:
            os.close(mutex)

        waited = time.monotonic() - started
        if waited >= max_wait:
            raise TranscodeError(
                f"Waited {waited / 60:.0f} min for {_human(needed)} of scratch space on "
                f"{work_root} that other jobs are holding; gave up "
                f"(WZ_DISK_WAIT_SECONDS={max_wait:.0f}).", stage="FETCHING_INPUT")
        if waited - last_report >= 60 or last_report == 0.0:
            last_report = max(waited, 0.001)
            log.info(f"Waiting for disk space: need {_human(needed)}, "
                     f"{_human(max(0, available))} available now; {len(others)} other "
                     f"job(s) are using the rest.")
        time.sleep(min(_POLL_SECONDS, max(0.1, max_wait - waited)))


__all__ = ["reserve", "DiskReservation", "space_factor"]
