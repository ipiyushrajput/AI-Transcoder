"""Readiness and metrics for operators and load balancers.

``/health`` says the process is up. ``/ready`` says it can actually run a job
right now — database reachable, FFmpeg present and licensed, scratch space and
the coordination directory usable — and answers 503 when it cannot, so a load
balancer or deploy script stops sending it work. ``/metrics`` exposes the same
facts plus job counts in Prometheus text format.
"""
import os
import shutil
import tempfile
import time
from typing import Any, Dict, Tuple

from api import database as db
from api import job_manager

_DB_CACHE = {"at": 0.0, "result": None}
_DB_CACHE_SECONDS = 5.0


def _check_database() -> Tuple[bool, str]:
    now = time.monotonic()
    if _DB_CACHE["result"] is not None and now - _DB_CACHE["at"] < _DB_CACHE_SECONDS:
        return _DB_CACHE["result"]
    if not db.is_available():
        result = (False, "not connected (see the startup log and docs/MYSQL_SETUP.md)")
    else:
        try:
            with db.engine.connect() as conn:
                conn.execute(db.text("SELECT 1"))
            result = (True, "connected")
        except Exception as e:
            result = (False, f"query failed: {type(e).__name__}: {e}")
    _DB_CACHE.update(at=now, result=result)
    return result


def _check_executable(path: str) -> Tuple[bool, str]:
    if not path or not os.path.isfile(path):
        return False, f"not found at {path}"
    if not os.access(path, os.X_OK):
        return False, f"{path} is not executable (chmod +x)"
    return True, path


def _check_scratch() -> Tuple[bool, str, int]:
    from hls_toolkit import disk_budget
    root = job_manager.WORK_ROOT or tempfile.gettempdir()
    try:
        os.makedirs(root, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=root, prefix=".ready_"):
            pass
        free = shutil.disk_usage(root).free
    except OSError as e:
        return False, f"{root} is not writable: {e}", 0
    min_free = int(disk_budget._env_float("WZ_DISK_MIN_FREE_BYTES", None,
                                          disk_budget.DEFAULT_MIN_FREE_BYTES))
    if free <= min_free:
        return False, (f"{root} has {free} bytes free, at or below the "
                       f"{min_free} kept spare"), free
    return True, f"{root} ({free // (1024 ** 2)} MB free)", free


def _check_coordination() -> Tuple[bool, str]:
    from hls_toolkit.coordination import coordination_dir
    directory = coordination_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=str(directory), prefix=".ready_"):
            pass
    except OSError as e:
        return False, f"{directory} is not writable: {e}"
    return True, str(directory)


def readiness(config: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """(ready?, per-check detail)."""
    from hls_toolkit.runner import build_run_settings
    settings = build_run_settings(config, {})
    checks: Dict[str, Dict[str, Any]] = {}

    def record(name, ok, detail):
        checks[name] = {"ok": bool(ok), "detail": detail}

    record("accepting_jobs", not job_manager._shutting_down.is_set(),
           "shutting down" if job_manager._shutting_down.is_set() else "yes")
    record("database", *_check_database())
    record("ffmpeg", *_check_executable(settings["ffmpeg_executable"]))
    record("ffprobe", *_check_executable(settings["ffprobe_executable"]))
    ok, detail, _ = _check_scratch()
    record("scratch_disk", ok, detail)
    record("coordination_dir", *_check_coordination())
    try:
        from hls_toolkit.encoder_check import cached_result
        encoder = cached_result()
    except ImportError:
        encoder = None
    if encoder is not None:
        record("encoder_license", encoder["ok"], encoder["detail"])
    stats = job_manager.queue_stats()
    full = stats["max_queued_jobs"] > 0 and stats["queued"] >= stats["max_queued_jobs"]
    record("queue", not full, f"{stats['queued']} waiting of {stats['max_queued_jobs']}")
    ready = all(c["ok"] for c in checks.values())
    return ready, {"ready": ready, "checks": checks}


def _job_counts() -> Dict[str, int]:
    if not db.is_available():
        return {}
    session = db.get_session()
    if session is None:
        return {}
    try:
        from sqlalchemy import func
        rows = (session.query(db.Job.status, func.count(db.Job.id))
                .group_by(db.Job.status).all())
        return {status: int(count) for status, count in rows}
    except Exception:
        return {}
    finally:
        db.close_session(session)


def metrics_text(config: Dict[str, Any]) -> str:
    """Prometheus exposition format (text/plain; version=0.0.4)."""
    lines = []

    def metric(name, kind, help_text, samples):
        lines.append(f"# HELP ai_transcoder_{name} {help_text}")
        lines.append(f"# TYPE ai_transcoder_{name} {kind}")
        for labels, value in samples:
            label_text = ",".join(f'{k}="{v}"' for k, v in labels.items())
            lines.append(f"ai_transcoder_{name}{{{label_text}}} {value}" if label_text
                         else f"ai_transcoder_{name} {value}")

    stats = job_manager.queue_stats()
    metric("jobs_running", "gauge", "Jobs running in this process.",
           [({}, stats["running"])])
    metric("jobs_queued", "gauge", "Jobs waiting for a worker in this process.",
           [({}, stats["queued"])])
    metric("jobs_max_concurrent", "gauge", "MAX_CONCURRENT_JOBS.",
           [({}, stats["max_concurrent_jobs"])])
    metric("jobs_max_queued", "gauge", "MAX_QUEUED_JOBS.",
           [({}, stats["max_queued_jobs"])])
    metric("shutting_down", "gauge", "1 while the server is stopping.",
           [({}, int(stats["shutting_down"]))])
    metric("process_events_total", "counter",
           "Job events since this process started.",
           [({"event": k}, v) for k, v in sorted(stats["counters"].items())])
    counts = _job_counts()
    if counts:
        metric("jobs_by_status", "gauge", "Jobs in the database by status.",
               [({"status": s}, counts.get(s, 0)) for s in db.JOB_STATUSES])
    metric("database_up", "gauge", "1 when the database answers.",
           [({}, int(_check_database()[0]))])
    try:
        from hls_toolkit.cpu_budget import get_shared_budget
        budget = get_shared_budget()
        metric("cpu_budget_cores", "gauge", "Cores in the machine-wide CPU budget.",
               [({}, budget.budget)])
        metric("cpu_budget_in_use", "gauge", "Cores currently leased by any job.",
               [({}, budget.in_use())])
    except Exception:
        pass
    _, _, free = _check_scratch()
    metric("scratch_free_bytes", "gauge", "Free bytes on the scratch disk.", [({}, free)])
    return "\n".join(lines) + "\n"


__all__ = ["readiness", "metrics_text"]
