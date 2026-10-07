"""One CPU budget for every transcode running on this server.

Each job used to size its own worker pool as if it owned the whole machine, so
three jobs started side by side asked for three machines' worth of CPU. The
encoders then fought over cores, cache and memory bandwidth, and whichever job
lost that fight ran far behind the others.

Here the machine has a single budget, measured in cores (``os.cpu_count()`` by
default). Before an encode starts it reserves the cores it is expected to use;
it waits while the budget is spent, and gives the cores back when it finishes.
Every ``python app.py`` process on the server, and every job the API runs, draws
from the same budget — so one job alone gets the whole machine, and three jobs
share it.

How it works: the budget is a directory of lock files, one per core
(``slot_000``, ``slot_001``, ...). Holding a core means holding an exclusive
``flock`` on one of those files. Two properties make this robust:

* The kernel drops a process's locks when it exits, however it exits, so a
  crashed or ``kill -9``'d job can never leak cores.
* ``flock`` locks belong to an open file description, not to a process, so the
  threads of one process compete for cores exactly as separate processes do.

Reserving several cores is all-or-nothing, and requests are served in arrival
order through a queue directory: each waiting request holds a lock on its own
queue entry, and only the oldest live entry may take cores. It keeps its place
until enough are free, so a large encode is never starved by a stream of small
ones; a job's next clip queues behind the clips other jobs are already waiting
on, so parallel jobs take turns instead of one running all its clips first; and
a job that dies while waiting leaves an unlocked entry that the next waiter
removes.

Nothing here changes how FFmpeg is invoked — only *when* an encode starts.
"""
import collections
import errno
import fcntl
import logging
import os
import random
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_SLOT_DIR = "/tmp/ai-transcoder-cpu-slots"
_POLL_SECONDS = 0.25


class BudgetAcquireAborted(Exception):
    """Waiting for cores was abandoned (the job was cancelled or failed)."""


def default_budget() -> int:
    return os.cpu_count() or 4


def resolve_budget(explicit: Optional[int] = None,
                   configured: Optional[int] = None) -> int:
    """The machine budget in cores.

    Precedence: `explicit` (``--cpu-budget``), then ``$WZ_CPU_BUDGET``, then
    `configured` (``parallelism.cpu_budget`` in the config), then cpu_count.
    """
    candidates = ((explicit, "--cpu-budget"),
                  (os.getenv("WZ_CPU_BUDGET"), "WZ_CPU_BUDGET"),
                  (configured, "parallelism.cpu_budget"))
    for value, source in candidates:
        if value in (None, ""):
            continue
        try:
            budget = int(value)
        except (TypeError, ValueError):
            logger.warning(f"Ignoring non-integer CPU budget {value!r} from {source}.")
            continue
        if budget >= 1:
            return budget
        logger.warning(f"Ignoring CPU budget {budget} from {source}; it must be >= 1.")
    return default_budget()


class Lease:
    """Cores held by one encode. Release exactly once; extra calls are no-ops."""

    def __init__(self, fds: List[int], cost: int, release_hook=None):
        self._fds = fds
        self.cost = cost
        self._release_hook = release_hook
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
            for fd in self._fds:
                try:
                    os.close(fd)           # closing the description drops its flock
                except OSError:
                    pass
            self._fds = []
            if self._release_hook:
                self._release_hook(self.cost)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


class CpuBudget:
    """A machine-wide pool of cores, shared through lock files."""

    def __init__(self, budget: int, slot_dir: str = DEFAULT_SLOT_DIR):
        self.budget = max(1, int(budget))
        self.slot_dir = Path(slot_dir)
        self.slot_dir.mkdir(parents=True, exist_ok=True)
        self._queue_dir = self.slot_dir / "queue"
        self._queue_dir.mkdir(exist_ok=True)
        self._slot_paths = [self.slot_dir / f"slot_{i:03d}" for i in range(self.budget)]
        # Fail now, at construction, if the files cannot be used — not mid-job.
        for path in self._slot_paths:
            os.close(self._open(path))

    @staticmethod
    def _open(path: Path) -> int:
        # O_NOFOLLOW: never open through a symlink planted in a shared directory.
        return os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)

    def clamp(self, cost: int) -> int:
        """A request can never exceed the budget, or it would wait forever."""
        return max(1, min(int(cost), self.budget))

    def acquire(self, cost: int,
                should_abort: Optional[Callable[[], bool]] = None) -> Lease:
        """Block until `cost` cores are reserved, and return the lease.

        Requests are served strictly in arrival order, across every process on
        the machine. A job's next clip joins the back of the line, behind clips
        other jobs are already waiting on, so jobs take turns rather than one
        job's clips running back to back while the others wait.

        Raises :class:`BudgetAcquireAborted` as soon as `should_abort` returns
        true, so a cancelled or failed job stops waiting promptly.
        """
        cost = self.clamp(cost)
        _check_abort(should_abort)
        entry_fd, entry_path = self._join_queue()
        try:
            while True:
                _check_abort(should_abort)
                if self._at_head_of_queue(entry_path):
                    fds = self._try_take(cost)
                    if fds is not None:
                        return Lease(fds, cost)
                time.sleep(_POLL_SECONDS)
        finally:
            try:
                os.unlink(entry_path)
            except FileNotFoundError:
                pass
            os.close(entry_fd)            # also drops the entry's flock

    def _join_queue(self):
        """Add a locked entry to the queue and return (fd, path).

        The entry is created and locked under a hidden name, then renamed into
        place, so no other process ever sees it unlocked and mistakes it for an
        abandoned one. Its name sorts by arrival time.
        """
        token = f"{os.getpid()}-{threading.get_ident()}-{random.getrandbits(32):08x}"
        hidden = self._queue_dir / f".joining-{token}"
        fd = os.open(str(hidden), os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        path = self._queue_dir / f"{time.time_ns():020d}-{token}"
        os.rename(hidden, path)
        return fd, path

    def _at_head_of_queue(self, mine: Path) -> bool:
        """True when no live entry arrived before `mine`.

        An entry whose lock nobody holds belongs to a process that died while
        waiting; it is removed so it cannot block the line.
        """
        for name in sorted(os.listdir(self._queue_dir)):
            if name.startswith("."):
                continue
            if name >= mine.name:
                return True
            other = self._queue_dir / name
            try:
                fd = os.open(str(other), os.O_RDWR | os.O_NOFOLLOW)
            except FileNotFoundError:
                continue                  # served or abandoned since the listing
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                    return False          # a live request is ahead of us
                raise
            else:
                try:
                    os.unlink(other)      # its owner is gone
                except FileNotFoundError:
                    pass
            finally:
                os.close(fd)
        return True

    def _try_take(self, cost: int) -> Optional[List[int]]:
        """Lock `cost` free slots, or none at all."""
        taken: List[int] = []
        order = list(range(self.budget))
        random.shuffle(order)             # spread holders across the slot files
        for index in order:
            fd = self._open(self._slot_paths[index])
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                continue
            taken.append(fd)
            if len(taken) == cost:
                return taken
        for fd in taken:
            os.close(fd)
        return None

    def in_use(self) -> int:
        """Cores currently held by anyone on the machine (for logs and tests)."""
        busy = 0
        for path in self._slot_paths:
            fd = self._open(path)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                busy += 1
            finally:
                os.close(fd)
        return busy


class LocalCpuBudget:
    """In-process fallback when the shared slot directory cannot be used.

    Jobs inside this process still share a budget, served in arrival order;
    separate processes do not see each other.
    """

    def __init__(self, budget: int):
        self.budget = max(1, int(budget))
        self.slot_dir = None
        self._free = self.budget
        self._waiting = collections.deque()
        self._cond = threading.Condition()

    def clamp(self, cost: int) -> int:
        return max(1, min(int(cost), self.budget))

    def acquire(self, cost: int,
                should_abort: Optional[Callable[[], bool]] = None) -> Lease:
        cost = self.clamp(cost)
        ticket = object()
        with self._cond:
            self._waiting.append(ticket)
            try:
                while self._waiting[0] is not ticket or self._free < cost:
                    _check_abort(should_abort)
                    self._cond.wait(_POLL_SECONDS)
                self._free -= cost
            finally:
                self._waiting.remove(ticket)
                self._cond.notify_all()
        return Lease([], cost, release_hook=self._give_back)

    def _give_back(self, cost: int) -> None:
        with self._cond:
            self._free += cost
            self._cond.notify_all()

    def in_use(self) -> int:
        with self._cond:
            return self.budget - self._free


def _check_abort(should_abort: Optional[Callable[[], bool]]) -> None:
    if should_abort is not None and should_abort():
        raise BudgetAcquireAborted("stopped waiting for CPU budget")


_shared = None
_shared_lock = threading.Lock()


def get_shared_budget(explicit_budget: Optional[int] = None,
                      configured_budget: Optional[int] = None,
                      slot_dir: Optional[str] = None):
    """The process-wide budget object, created on first use.

    Entry points (the CLI, the API) call this early with their settings; later
    calls from the pipeline get the same object back and their arguments are
    ignored.
    """
    global _shared
    with _shared_lock:
        if _shared is None:
            budget = resolve_budget(explicit_budget, configured_budget)
            directory = slot_dir or os.getenv("WZ_CPU_SLOT_DIR") or DEFAULT_SLOT_DIR
            try:
                _shared = CpuBudget(budget, directory)
                logger.info(f"CPU budget: {budget} core(s), shared machine-wide "
                            f"through {directory}")
            except OSError as e:
                _shared = LocalCpuBudget(budget)
                logger.warning(
                    f"Cannot use the shared CPU budget directory {directory} ({e}). "
                    f"Falling back to a budget of {budget} core(s) for this process "
                    f"only — jobs in other processes will not be counted. Set "
                    f"WZ_CPU_SLOT_DIR to a directory every job can write to.")
        return _shared


def reset_shared_budget() -> None:
    """Forget the process-wide budget (tests use this between scenarios)."""
    global _shared
    with _shared_lock:
        _shared = None


__all__ = ["CpuBudget", "LocalCpuBudget", "Lease", "BudgetAcquireAborted",
           "get_shared_budget", "reset_shared_budget", "resolve_budget",
           "default_budget", "DEFAULT_SLOT_DIR"]
