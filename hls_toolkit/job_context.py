"""Per-channel logging, progress tracking and cancellation for a transcode run.

A "channel" is the base name of the source video, e.g. an input of
``s3://bucket/Visionular/AETN_AmericanPickers_S10_E03_en.mp4`` gives the channel
``AETN_AmericanPickers_S10_E03_en``. Everything a run emits lands under::

    logs/AETN_AmericanPickers_S10_E03_en/<job_id>/
    ├── job.log       every log record (DEBUG and up)
    ├── error.log     WARNING and above only — the file to read first on failure
    ├── ffmpeg.log    raw FFmpeg/FFprobe stdout+stderr, verbatim
    └── job.json      job metadata, status, progress and timings

Without a ``job_id`` (plain CLI use) the per-job directory is skipped and the
files sit directly under ``logs/<channel>/``.

A log folder is never reused. If ``logs/<channel>/`` already exists the run logs
to ``logs/<channel>_2/``, then ``_3`` and so on; with a ``job_id``, a repeated id
becomes ``<job_id>_2``. Parallel runs on the same source each get their own.

A :class:`JobContext` is threaded explicitly through the pipeline. It is safe to
pass ``None`` anywhere a context is accepted — every helper degrades to plain
``logging`` calls so the toolkit keeps working outside the API server.
"""
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Weighted pipeline stages. `span` values sum to 100 so a stage's progress can be
# reported as a fraction of its own span and still yield a sane overall figure.
STAGES = [
    ("QUEUED", 0, 0),
    ("FETCHING_INPUT", 0, 5),
    ("PROBING", 5, 3),
    ("ANALYZING_AUDIO", 8, 4),
    ("TRANSCODING", 12, 48),
    ("MERGING", 60, 8),
    ("PACKAGING_HLS", 68, 12),
    ("SUBTITLES", 80, 4),
    ("MANIFEST", 84, 2),
    ("AD_MARKERS", 86, 2),
    ("THUMBNAILS", 88, 2),
    ("UPLOADING", 90, 9),
    ("CLEANUP", 99, 1),
]
_STAGE_INDEX = {name: (base, span) for name, base, span in STAGES}


def channel_name_for(input_uri: str) -> str:
    """Derive the channel (log folder) name from a local path or s3:// URI."""
    if not input_uri:
        return "unknown_channel"
    text = str(input_uri)
    if "://" in text:
        text = urlparse(text).path
    stem = Path(text).name
    for _ in range(2):                       # strip .mp4, and .tar.gz-style pairs
        stem, ext = os.path.splitext(stem)
        if not ext:
            break
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", stem).strip("._-")
    return safe or "unknown_channel"


def claim_unique_dir(parent: Path, name: str) -> Path:
    """Create and return ``parent/name``, or ``name_2``, ``name_3``... if taken.

    A suffix is one more than the highest suffix already present, so suffixed
    folders number in the order their runs started, even after an older one is
    deleted.

    Creation is atomic: ``mkdir`` fails when the directory exists, and on that
    failure the next number is tried. Jobs started at the same instant — in one
    process or several — therefore each get their own folder; none can pick a
    name another has just claimed.
    """
    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    suffixed = re.compile(rf"^{re.escape(name)}_(\d+)$")
    candidate = parent / name
    while True:
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            highest = 1
            for entry in parent.iterdir():
                match = suffixed.match(entry.name)
                if match:
                    highest = max(highest, int(match.group(1)))
            candidate = parent / f"{name}_{highest + 1}"


class _MaxLevelFilter(logging.Filter):
    """Drop records at or above `level` (used to keep error.log terse)."""

    def __init__(self, level):
        super().__init__()
        self.level = level

    def filter(self, record):
        return record.levelno < self.level


class JobContext:
    """Logging, progress and cancellation for one transcode run."""

    def __init__(self,
                 input_uri: str,
                 job_id: Optional[str] = None,
                 log_root: str = "logs",
                 debug: bool = False,
                 metadata: Optional[Dict[str, Any]] = None):
        self.job_id = job_id
        self.channel = channel_name_for(input_uri)
        self.input_uri = input_uri
        self.debug = debug
        self.metadata = dict(metadata or {})

        # Never reuse a log folder: a second run on the same source used to
        # append to the first run's job.log and overwrite its job.json, and
        # parallel runs interleaved their logs line by line.
        if job_id:
            self.log_dir = claim_unique_dir(Path(log_root) / self.channel, job_id)
        else:
            self.log_dir = claim_unique_dir(Path(log_root), self.channel)

        self.job_log_path = self.log_dir / "job.log"
        self.error_log_path = self.log_dir / "error.log"
        self.ffmpeg_log_path = self.log_dir / "ffmpeg.log"
        self.meta_path = self.log_dir / "job.json"

        self._lock = threading.Lock()
        self.cancel_event = threading.Event()

        self.stage = "QUEUED"
        self.progress_pct = 0
        self.status = "PENDING"
        self.error_message: Optional[str] = None
        self.started_at = datetime.now(timezone.utc)
        self.finished_at: Optional[datetime] = None
        self.output_prefix: Optional[str] = None
        self.uploaded_files = 0

        self.logger = self._build_logger()
        self._ffmpeg_fh = open(self.ffmpeg_log_path, "a", encoding="utf-8", errors="replace")

        self.logger.info("=" * 78)
        self.logger.info(f"Channel : {self.channel}")
        self.logger.info(f"Job id  : {job_id or '(cli)'}")
        self.logger.info(f"Input   : {input_uri}")
        self.logger.info(f"Logs    : {self.log_dir}")
        requested = job_id or self.channel
        if self.log_dir.name != requested:
            self.logger.info(f"          ('{requested}' already existed, so this run "
                             f"logs to '{self.log_dir.name}')")
        self.logger.info("=" * 78)
        self._write_meta()

    # -- logging ----------------------------------------------------------
    def _build_logger(self) -> logging.Logger:
        # Named after the claimed folder, which is unique: two runs on the same
        # source in one process (the API) would otherwise share a logger and the
        # second would take over the first one's file handlers.
        name = f"aitranscoder.{self.channel}.{self.log_dir.name}"
        logger = logging.getLogger(name)
        logger.setLevel(logging.DEBUG)
        logger.propagate = True          # still reaches the root/console handler
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(filename)s:%(lineno)d - %(message)s",
            LOG_DATE_FORMAT)

        job_fh = logging.FileHandler(self.job_log_path, encoding="utf-8")
        job_fh.setLevel(logging.DEBUG if self.debug else logging.INFO)
        job_fh.setFormatter(fmt)
        job_fh.addFilter(_MaxLevelFilter(logging.CRITICAL + 1))
        logger.addHandler(job_fh)

        err_fh = logging.FileHandler(self.error_log_path, encoding="utf-8")
        err_fh.setLevel(logging.WARNING)
        err_fh.setFormatter(fmt)
        logger.addHandler(err_fh)
        return logger

    def ffmpeg_line(self, line: str) -> None:
        """Append one raw FFmpeg/FFprobe output line to ffmpeg.log."""
        try:
            with self._lock:
                self._ffmpeg_fh.write(line if line.endswith("\n") else line + "\n")
                self._ffmpeg_fh.flush()
        except Exception:
            pass

    def ffmpeg_command(self, label: str, command: str) -> None:
        self.ffmpeg_line(f"\n{'-' * 78}\n[{_now()}] {label}\n$ {command}\n{'-' * 78}")

    def tail_errors(self, lines: int = 80) -> str:
        return _tail(self.error_log_path, lines)

    def tail_log(self, lines: int = 200) -> str:
        return _tail(self.job_log_path, lines)

    def tail_ffmpeg(self, lines: int = 200) -> str:
        return _tail(self.ffmpeg_log_path, lines)

    # -- progress ---------------------------------------------------------
    def set_stage(self, stage: str, fraction: float = 0.0) -> None:
        """Move to `stage`; `fraction` (0..1) is progress within that stage."""
        base, span = _STAGE_INDEX.get(stage, (self.progress_pct, 0))
        pct = int(base + span * max(0.0, min(1.0, fraction)))
        with self._lock:
            changed = stage != self.stage
            self.stage = stage
            self.progress_pct = max(self.progress_pct, min(99, pct))
        if changed:
            self.logger.info(f"[stage] {stage} ({self.progress_pct}%)")
        self._write_meta()

    def advance_within_stage(self, fraction: float) -> None:
        base, span = _STAGE_INDEX.get(self.stage, (self.progress_pct, 0))
        pct = int(base + span * max(0.0, min(1.0, fraction)))
        with self._lock:
            if pct > self.progress_pct:
                self.progress_pct = min(99, pct)

    def mark_running(self) -> None:
        self.status = "RUNNING"
        self._write_meta()

    def mark_completed(self, output_prefix: Optional[str] = None,
                       uploaded_files: int = 0) -> None:
        self.status = "COMPLETED"
        self.stage = "DONE"
        self.progress_pct = 100
        self.output_prefix = output_prefix or self.output_prefix
        self.uploaded_files = uploaded_files or self.uploaded_files
        self.finished_at = datetime.now(timezone.utc)
        self.logger.info(f"Job COMPLETED in {self.elapsed_seconds():.1f}s "
                         f"-> {self.output_prefix or '(no upload)'}")
        self._write_meta()
        self.close()

    def mark_failed(self, message: str) -> None:
        self.status = "FAILED"
        self.error_message = str(message)[:8000]
        self.finished_at = datetime.now(timezone.utc)
        self.logger.error(f"Job FAILED after {self.elapsed_seconds():.1f}s: {message}")
        self._write_meta()
        self.close()

    def mark_cancelled(self) -> None:
        self.status = "CANCELLED"
        self.finished_at = datetime.now(timezone.utc)
        self.logger.warning("Job CANCELLED")
        self._write_meta()
        self.close()

    def request_cancel(self) -> None:
        self.cancel_event.set()

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise JobCancelled("Job cancelled by request")

    def elapsed_seconds(self) -> float:
        end = self.finished_at or datetime.now(timezone.utc)
        return (end - self.started_at).total_seconds()

    # -- persistence ------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "channel": self.channel,
            "input": self.input_uri,
            "status": self.status,
            "stage": self.stage,
            "progress_pct": self.progress_pct,
            "error_message": self.error_message,
            "output_prefix": self.output_prefix,
            "uploaded_files": self.uploaded_files,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "elapsed_seconds": round(self.elapsed_seconds(), 2),
            "log_dir": str(self.log_dir),
            "metadata": self.metadata,
        }

    def _write_meta(self) -> None:
        try:
            self.meta_path.write_text(json.dumps(self.snapshot(), indent=2),
                                      encoding="utf-8")
        except Exception:
            pass

    def close(self) -> None:
        try:
            if not self._ffmpeg_fh.closed:
                self._ffmpeg_fh.close()
        except Exception:
            pass
        for handler in list(self.logger.handlers):
            try:
                self.logger.removeHandler(handler)
                handler.close()
            except Exception:
                pass


class JobCancelled(Exception):
    """Raised inside the pipeline when a cancellation has been requested."""


class TranscodeError(Exception):
    """A stage of the pipeline failed. Carries the stage for the status API."""

    def __init__(self, message: str, stage: str = "UNKNOWN"):
        super().__init__(message)
        self.stage = stage


def get_logger(ctx: Optional[JobContext]) -> logging.Logger:
    """Logger for `ctx`, or the root logger when running without a context."""
    return ctx.logger if ctx is not None else logging.getLogger()


# ---------------------------------------------------------------------------
# Ambient context
#
# The pipeline spans a dozen modules and a worker pool, so rather than threading
# a context argument through every helper the active job is bound to the thread.
# `bind_context` must be called again on any thread the job spawns — the
# transcode pool does this through its `initializer`. Unbound threads fall back
# to the root logger, which keeps the toolkit usable as a plain library.
# ---------------------------------------------------------------------------
_local = threading.local()


def bind_context(ctx: Optional[JobContext]) -> None:
    """Make `ctx` the active job context for the calling thread."""
    _local.ctx = ctx


def current_context() -> Optional[JobContext]:
    return getattr(_local, "ctx", None)


def log() -> logging.Logger:
    """Logger for the thread's active job, or the root logger."""
    ctx = current_context()
    return ctx.logger if ctx is not None else logging.getLogger()


def check_cancelled() -> None:
    """Raise :class:`JobCancelled` if the active job has been cancelled."""
    ctx = current_context()
    if ctx is not None and ctx.cancelled:
        raise JobCancelled("Job cancelled by request")


def _tail(path: Path, lines: int) -> str:
    try:
        if not Path(path).exists():
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except Exception as e:
        return f"(could not read {path}: {e})"


def _now() -> str:
    return datetime.now(timezone.utc).strftime(LOG_DATE_FORMAT)
