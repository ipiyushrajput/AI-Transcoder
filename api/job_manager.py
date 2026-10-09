"""Background execution and bookkeeping for API-submitted transcode jobs.

Jobs run on a bounded worker pool. Progress is mirrored from the live
:class:`~hls_toolkit.job_context.JobContext` into MySQL on a short interval,
so ``GET /jobs/<id>/status`` is accurate whether it is served from the in-memory
registry (job still running on this process) or from the database (finished, or
started by another worker process).
"""
import copy
import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from api import database as db
from hls_toolkit import s3_io
from hls_toolkit.coordination import JobLock, job_is_owned
from hls_toolkit.job_context import JobContext, channel_name_for
from hls_toolkit.runner import (build_run_settings, run_transcode_job,
                                validate_output_dir_name)

logger = logging.getLogger(__name__)

MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "2"))
LOG_ROOT = os.getenv("LOG_ROOT", "logs")
WORK_ROOT = os.getenv("WORK_ROOT") or None
PROGRESS_SYNC_SECONDS = float(os.getenv("PROGRESS_SYNC_SECONDS", "5"))
REQUEUE_PENDING_ON_START = os.getenv("REQUEUE_PENDING_ON_START", "1") not in ("0", "false",
                                                                             "no")
# Jobs allowed to wait for a worker; past this, submissions get 429.
MAX_QUEUED_JOBS = int(os.getenv("MAX_QUEUED_JOBS", "100"))
# Finished jobs kept in memory (the database keeps them all).
FINISHED_JOBS_IN_MEMORY = int(os.getenv("FINISHED_JOBS_IN_MEMORY", "200"))
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "").strip()

_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
_registry: Dict[str, Dict[str, Any]] = {}
_registry_lock = threading.Lock()
_shutting_down = threading.Event()
_submit_lock = threading.Lock()
_counters = {"submitted": 0, "completed": 0, "failed": 0, "cancelled": 0,
             "rejected_queue_full": 0, "duplicate_submissions": 0}
_FINAL_STATUSES = ("COMPLETED", "FAILED", "CANCELLED")

INTERRUPTED_MESSAGE = (
    "Interrupted: the server shut down while this job was {where}. Its output was "
    "not published. Resubmit the job to run it again.")


class ServiceUnavailable(Exception):
    """The server is shutting down and is not accepting new jobs."""


class QueueFull(Exception):
    """Too many jobs are already waiting; the client should retry later."""


class IdempotencyConflict(ValueError):
    """An Idempotency-Key was reused for a different request."""


def _count(name: str) -> None:
    with _registry_lock:
        _counters[name] = _counters.get(name, 0) + 1


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS,
                                           thread_name_prefix="transcode")
        return _executor


def _release_lock(job_id: str) -> None:
    """Give up ownership of a job once its final state is recorded."""
    with _registry_lock:
        entry = _registry.get(job_id)
        lock = entry.pop("lock", None) if entry else None
    if lock is not None:
        lock.release()


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------
def submit_job(base_config: Dict[str, Any], payload: Dict[str, Any],
               idempotency_key: Optional[str] = None) -> Dict[str, Any]:
    """Validate a request, persist it as PENDING and queue it for execution.

    Returns the job record; ``record["duplicate"]`` is True when an earlier
    submission with the same `idempotency_key` is returned instead. Raises
    ValueError with a human-readable message when the payload is not runnable
    (the route turns that into a 400), QueueFull when too many jobs are
    waiting, and ServiceUnavailable while shutting down.
    """
    if _shutting_down.is_set():
        raise ServiceUnavailable("The server is shutting down and is not accepting "
                                 "new jobs. Retry shortly.")
    warnings = validate_payload(payload)
    idempotency_key = _check_idempotency_key(idempotency_key)
    config = _merge_config(base_config, payload)
    overrides = _overrides_from_payload(payload)
    settings = build_run_settings(config, overrides)

    _validate(config, settings)

    with _submit_lock:
        if idempotency_key:
            existing = _find_by_idempotency_key(idempotency_key)
            if existing is not None:
                if _jsonable(existing.get("request_payload")) != _jsonable(payload):
                    raise IdempotencyConflict(
                        f"Idempotency-Key {idempotency_key!r} was already used for a "
                        f"different request (job {existing['job_id']}). Use a new key "
                        f"for a new job.")
                _count("duplicate_submissions")
                existing.pop("request_payload", None)
                return {**existing, "duplicate": True}
        queued = _queued_job_ids()
        if MAX_QUEUED_JOBS > 0 and len(queued) >= MAX_QUEUED_JOBS:
            _count("rejected_queue_full")
            raise QueueFull(f"{len(queued)} jobs are already waiting to run "
                            f"(MAX_QUEUED_JOBS={MAX_QUEUED_JOBS}). Retry later.")
        record = _create_job(config, payload, overrides, settings, idempotency_key)
    record["warnings"] = warnings
    record["queue_position"] = _queue_position(record["job_id"])
    return record


def _create_job(config, payload, overrides, settings, idempotency_key) -> Dict[str, Any]:
    job_id = str(uuid.uuid4())
    channel = channel_name_for(settings["input_video"])
    name = payload.get("name") or channel

    record = {
        "job_id": job_id,
        "name": name,
        "channel": channel,
        "status": "PENDING",
        "stage": "QUEUED",
        "progress_pct": 0,
        "input_video": settings["input_video"],
        "input_is_s3": int(s3_io.is_s3_uri(settings["input_video"])),
        "subtitle_file": settings["subtitle_file"],
        "subtitle_language": settings["subtitle_language"],
        "template": settings["template_name"],
        "resolutions": settings["resolution"],
        "esam_enabled": int(settings["esam"]),
        "audio_norm_enabled": int(settings["audio_norm"]),
        "thumbnails_enabled": int(settings["thumbnails_enabled"]),
        "upload_enabled": int(settings["upload"]),
        "output_dir_name": settings["output_dir_name"],
        "s3_bucket": settings["s3_config"].get("bucket_name"),
        "s3_key_prefix": settings["s3_config"].get("key_prefix"),
        "log_dir": str(os.path.join(LOG_ROOT, channel, job_id)),
        "submitted_at": db._utcnow(),
    }

    _persist_new_job(record, config, payload, settings, idempotency_key)
    if idempotency_key:
        record["idempotency_key"] = idempotency_key
    _enqueue(job_id, record, config, overrides, payload=payload)
    _count("submitted")

    logger.info(f"[{job_id}] queued job for channel '{channel}' "
                f"(input={settings['input_video']})")
    return dict(record)


# ---------------------------------------------------------------------------
# Request checks
# ---------------------------------------------------------------------------
_STRING_FIELDS = ("name", "input_video", "subtitle_file", "subtitle_language",
                  "output_dir", "template", "resolutions", "s3_bucket", "s3_key_prefix",
                  "esam_scc_xml", "esam_mcc_xml")
_BOOL_FIELDS = ("esam", "audio_norm", "generate_thumbnails", "upload",
                "delete_local_output", "debug")
_OTHER_FIELDS = ("duration", "transcode_workers", "clippings", "hls_settings", "config")
_HLS_SETTINGS = {"hls_time", "hls_playlist_type", "hls_flags", "hls_segment_type"}
_SAFE_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def validate_payload(payload: Dict[str, Any]) -> List[str]:
    """Reject wrongly typed fields up front; return warnings for unknown ones.

    Without this a mistake surfaces minutes later, deep in the job, or not at
    all: ``"upload": "false"`` is a non-empty string and would mean *true*.
    """
    def is_number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    for key in _STRING_FIELDS:
        value = payload.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{key} must be a string.")
    for key in _BOOL_FIELDS:
        value = payload.get(key)
        if value is not None and not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false (JSON boolean), "
                             f"not {json.dumps(value)}.")

    duration = payload.get("duration")
    if duration is not None and (not is_number(duration) or duration <= 0):
        raise ValueError("duration must be a positive number of seconds.")
    workers = payload.get("transcode_workers")
    if workers is not None and (isinstance(workers, bool) or not isinstance(workers, int)
                                or not 1 <= workers <= 256):
        raise ValueError("transcode_workers must be a whole number from 1 to 256.")

    hls = payload.get("hls_settings")
    if hls is not None:
        if not isinstance(hls, dict):
            raise ValueError("hls_settings must be an object.")
        unknown = sorted(set(hls) - _HLS_SETTINGS)
        if unknown:
            raise ValueError(f"Unknown hls_settings: {', '.join(unknown)}. "
                             f"Allowed: {', '.join(sorted(_HLS_SETTINGS))}.")
        hls_time = hls.get("hls_time")
        if hls_time is not None and (not is_number(hls_time) or not 0.5 <= hls_time <= 60):
            raise ValueError("hls_settings.hls_time must be a number of seconds "
                             "from 0.5 to 60.")
        for key in _HLS_SETTINGS - {"hls_time"}:
            if hls.get(key) is not None and not isinstance(hls[key], str):
                raise ValueError(f"hls_settings.{key} must be a string.")

    clippings = payload.get("clippings")
    if clippings is not None:
        from hls_toolkit.time_utils import parse_timecode
        if not isinstance(clippings, list) or not clippings:
            raise ValueError("clippings must be a non-empty list.")
        for i, clip in enumerate(clippings, 1):
            if not isinstance(clip, dict):
                raise ValueError(f"clippings[{i}] must be an object with "
                                 f"StartTimecode and EndTimecode.")
            for key in ("StartTimecode", "EndTimecode"):
                value = clip.get(key)
                if not isinstance(value, str):
                    raise ValueError(f"clippings[{i}].{key} is required "
                                     f"(HH:MM:SS:FF).")
                try:
                    parse_timecode(value)
                except ValueError:
                    raise ValueError(f"clippings[{i}].{key} is not a timecode "
                                     f"(HH:MM:SS:FF): {value!r}") from None

    inline = payload.get("config")
    if inline is not None and not isinstance(inline, dict):
        raise ValueError("config must be an object.")

    known = set(_STRING_FIELDS) | set(_BOOL_FIELDS) | set(_OTHER_FIELDS)
    return [f"Unknown field {key!r} was ignored." for key in sorted(set(payload) - known)]


def _check_idempotency_key(key: Optional[str]) -> Optional[str]:
    if key is None or key == "":
        return None
    if not _SAFE_KEY.match(key):
        raise ValueError("Idempotency-Key must be 1-128 characters of letters, digits, "
                         "'.', '_', ':' or '-'.")
    return key


def _find_by_idempotency_key(key: str) -> Optional[Dict[str, Any]]:
    """The earlier job submitted with `key`, with its request payload."""
    with _registry_lock:
        for entry in _registry.values():
            if entry["record"].get("idempotency_key") == key:
                return {**entry["record"], "request_payload": entry.get("payload")}
    session = db.get_session()
    if session is None:
        return None
    try:
        job = session.query(db.Job).filter(db.Job.idempotency_key == key).first()
        if job is None:
            return None
        return {**job.to_dict(), "request_payload": job.request_payload}
    finally:
        db.close_session(session)


def _queued_job_ids() -> List[str]:
    """Jobs waiting for a worker, oldest first."""
    with _registry_lock:
        return [job_id for job_id, e in _registry.items()
                if e.get("ctx") is None and e.get("lock") is not None
                and e["record"].get("status") == "PENDING"]


def _queue_position(job_id: str) -> Optional[int]:
    """1 for the next job to start, None when not waiting."""
    queued = _queued_job_ids()
    return queued.index(job_id) + 1 if job_id in queued else None


def _prune_finished() -> None:
    """Forget the oldest finished jobs; the database still has them."""
    with _registry_lock:
        finished = [job_id for job_id, e in _registry.items()
                    if e.get("ctx") is None and e.get("lock") is None
                    and e["record"].get("status") in _FINAL_STATUSES]
        for job_id in finished[:max(0, len(finished) - FINISHED_JOBS_IN_MEMORY)]:
            _registry.pop(job_id, None)


def _enqueue(job_id: str, record: Dict[str, Any], config: Dict[str, Any],
             overrides: Dict[str, Any], payload: Optional[Dict[str, Any]] = None) -> None:
    """Own the job (lock file) and hand it to the worker pool."""
    lock = JobLock(job_id)
    with _registry_lock:
        _registry[job_id] = {"record": dict(record), "ctx": None, "future": None,
                             "lock": lock, "payload": _jsonable(payload)}
    future = _get_executor().submit(_run_job, job_id, config, overrides)
    with _registry_lock:
        if job_id in _registry:
            _registry[job_id]["future"] = future


def _merge_config(base_config: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """Overlay any per-request config sections onto the server's base config."""
    config = copy.deepcopy(base_config)
    inline = payload.get("config")
    if isinstance(inline, dict):
        for section, value in inline.items():
            if isinstance(value, dict) and isinstance(config.get(section), dict):
                config[section] = {**config[section], **value}
            else:
                config[section] = value

    defaults = config.setdefault("defaults", {})
    if payload.get("clippings") is not None:
        defaults["InputClippings"] = payload["clippings"]
    if payload.get("hls_settings") is not None:
        defaults["hls_settings"] = {**defaults.get("hls_settings", {}),
                                    **payload["hls_settings"]}
    if payload.get("esam_scc_xml") or payload.get("esam_mcc_xml"):
        esam = config.setdefault("Esam", {})
        if payload.get("esam_scc_xml"):
            esam.setdefault("SignalProcessingNotification", {})
            esam["SignalProcessingNotification"]["SccXml"] = payload["esam_scc_xml"]
        if payload.get("esam_mcc_xml"):
            esam.setdefault("ManifestConfirmConditionNotification", {})
            esam["ManifestConfirmConditionNotification"]["MccXml"] = payload["esam_mcc_xml"]
    if payload.get("s3_bucket") or payload.get("s3_key_prefix"):
        s3 = config.setdefault("s3", {})
        if payload.get("s3_bucket"):
            s3["bucket_name"] = payload["s3_bucket"]
        if payload.get("s3_key_prefix") is not None:
            s3["key_prefix"] = payload["s3_key_prefix"]
    return config


def _overrides_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "input_video": payload.get("input_video"),
        "subtitle_file": payload.get("subtitle_file"),
        "subtitle_language": payload.get("subtitle_language"),
        "output_dir": payload.get("output_dir"),
        "template": payload.get("template"),
        "resolution": payload.get("resolutions"),
        "esam": payload.get("esam"),
        "audio_norm": payload.get("audio_norm"),
        "thumbnails_enabled": payload.get("generate_thumbnails"),
        "upload": payload.get("upload"),
        "delete_local_output": payload.get("delete_local_output"),
        "duration": payload.get("duration"),
        "transcode_workers": payload.get("transcode_workers"),
        "debug": payload.get("debug"),
    }


def _validate(config: Dict[str, Any], settings: Dict[str, Any]) -> None:
    """Reject requests that cannot run, before a job id is handed out."""
    input_video = settings["input_video"]
    if not input_video:
        raise ValueError("input_video is required (no default is configured).")

    settings["output_dir_name"] = validate_output_dir_name(settings["output_dir_name"])

    if s3_io.is_s3_uri(input_video):
        try:
            s3_io.parse_s3_uri(input_video)
        except ValueError as e:
            raise ValueError(str(e)) from e
    elif not os.path.exists(input_video):
        raise ValueError(f"Local input_video not found on the server: {input_video}")

    subtitle = settings["subtitle_file"]
    if subtitle and not s3_io.is_s3_uri(subtitle) and not os.path.exists(subtitle):
        raise ValueError(f"Local subtitle_file not found on the server: {subtitle}")

    templates = config.get("video_templates", {})
    template_name = settings["template_name"]
    if template_name not in templates:
        available = ", ".join(sorted(templates)) or "(none configured)"
        raise ValueError(f"Unknown template '{template_name}'. Available: {available}")

    ladder = templates[template_name]
    requested = [r.strip() for r in (settings["resolution"] or "").split(",") if r.strip()]
    if requested:
        known = {r["name"] for r in ladder}
        missing = [r for r in requested if r not in known]
        if missing:
            raise ValueError(
                f"Resolutions not in template '{template_name}': {', '.join(missing)}. "
                f"Available: {', '.join(sorted(known))}")

    if settings["upload"] and not settings["s3_config"].get("bucket_name"):
        raise ValueError("upload is enabled but s3.bucket_name is not configured.")

    for executable, label in ((settings["ffmpeg_executable"], "ffmpeg"),
                              (settings["ffprobe_executable"], "ffprobe")):
        if not os.path.exists(executable):
            raise ValueError(f"{label} executable not found at {executable}. "
                             f"Check paths.{label}_executable in the config.")


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def _run_job(job_id: str, config: Dict[str, Any], overrides: Dict[str, Any]) -> None:
    """Worker body: run the transcode and keep the DB in step with it."""
    syncer: Optional[threading.Thread] = None
    stop_sync = threading.Event()

    def _on_context_ready(ctx: JobContext):
        with _registry_lock:
            if job_id in _registry:
                _registry[job_id]["ctx"] = ctx
        _update_job(job_id, status="RUNNING", stage=ctx.stage,
                    started_at=db._utcnow(), log_dir=str(ctx.log_dir))
        nonlocal syncer
        syncer = threading.Thread(target=_sync_progress, args=(job_id, ctx, stop_sync),
                                  name=f"sync-{job_id[:8]}", daemon=True)
        syncer.start()

    try:
        snapshot = run_transcode_job(config, overrides=overrides, job_id=job_id,
                                     log_root=LOG_ROOT, work_root=WORK_ROOT,
                                     on_context_ready=_on_context_ready)
    except Exception as e:
        logger.error(f"[{job_id}] job crashed outside the pipeline: {e}", exc_info=True)
        snapshot = {"status": "FAILED", "stage": "UNKNOWN", "progress_pct": 0,
                    "error_message": f"{type(e).__name__}: {e}",
                    "metadata": {}, "elapsed_seconds": 0, "uploaded_files": 0,
                    "output_prefix": None}
    finally:
        stop_sync.set()
        if syncer is not None:
            syncer.join(timeout=5)

    metadata = snapshot.get("metadata") or {}
    _update_job(
        job_id,
        status=snapshot.get("status", "FAILED"),
        stage=snapshot.get("stage"),
        progress_pct=100 if snapshot.get("status") == "COMPLETED"
        else snapshot.get("progress_pct", 0),
        error_message=snapshot.get("error_message"),
        error_stage=snapshot.get("stage") if snapshot.get("status") == "FAILED" else None,
        output_prefix=snapshot.get("output_prefix"),
        playback_url=metadata.get("playback_url"),
        uploaded_files=snapshot.get("uploaded_files", 0),
        source_duration_seconds=metadata.get("source_duration_seconds"),
        source_fps=metadata.get("source_fps"),
        duration_seconds=snapshot.get("elapsed_seconds"),
        completed_at=db._utcnow(),
        log_dir=snapshot.get("log_dir"))

    with _registry_lock:
        entry = _registry.get(job_id)
        if entry is not None:
            entry["record"].update({
                "status": snapshot.get("status"),
                "stage": snapshot.get("stage"),
                "progress_pct": snapshot.get("progress_pct"),
                "error_message": snapshot.get("error_message"),
                "output_prefix": snapshot.get("output_prefix"),
                "log_dir": snapshot.get("log_dir"),
            })
            entry["ctx"] = None
    # Only now that the final state is recorded may another process treat the
    # job as abandoned.
    _release_lock(job_id)
    status = snapshot.get("status")
    if status in _FINAL_STATUSES:
        _count(status.lower())
    if status == "FAILED":
        _send_alert(job_id, snapshot)
    _prune_finished()

    logger.info(f"[{job_id}] finished with status {status}")


def _send_alert(job_id: str, snapshot: Dict[str, Any]) -> None:
    """POST a failed job's summary to ALERT_WEBHOOK_URL, without blocking.

    The body carries ``text`` (shown by Slack and Teams incoming webhooks) and
    the same facts as fields, for anything else.
    """
    if not ALERT_WEBHOOK_URL:
        return
    with _registry_lock:
        record = dict(_registry.get(job_id, {}).get("record", {}))
    error = (snapshot.get("error_message") or "").strip()
    first_line = error.splitlines()[0] if error else "no message"
    body = {
        "text": (f"Transcode job FAILED: {record.get('name') or job_id} at stage "
                 f"{snapshot.get('stage')}: {first_line[:500]}"),
        "job_id": job_id, "name": record.get("name"), "channel": snapshot.get("channel"),
        "stage": snapshot.get("stage"), "error_message": error[:4000],
        "input_video": record.get("input_video"), "log_dir": snapshot.get("log_dir"),
        "host": os.uname().nodename,
    }

    def post():
        import urllib.request
        try:
            request = urllib.request.Request(
                ALERT_WEBHOOK_URL, data=json.dumps(body, default=str).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read(1024)
        except Exception as e:
            logger.warning(f"[{job_id}] failure alert could not be sent: {e}")

    threading.Thread(target=post, name=f"alert-{job_id[:8]}", daemon=True).start()


def _sync_progress(job_id: str, ctx: JobContext, stop: threading.Event) -> None:
    """Mirror live progress into the DB until the job ends."""
    while not stop.wait(PROGRESS_SYNC_SECONDS):
        try:
            _update_job(job_id, status=ctx.status, stage=ctx.stage,
                        progress_pct=ctx.progress_pct)
        except Exception as e:
            logger.debug(f"[{job_id}] progress sync skipped: {e}")


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def get_status(job_id: str) -> Optional[Dict[str, Any]]:
    """Live status for a job: in-memory when running, else from the database."""
    with _registry_lock:
        entry = _registry.get(job_id)
        ctx = entry["ctx"] if entry else None

    if ctx is not None:
        snap = ctx.snapshot()
        return {
            "job_id": job_id,
            "name": (entry["record"].get("name") if entry else None),
            "channel": snap["channel"],
            "status": snap["status"],
            "stage": snap["stage"],
            "progress_pct": snap["progress_pct"],
            "error_message": snap["error_message"],
            "output_prefix": snap["output_prefix"],
            "playback_url": snap.get("metadata", {}).get("playback_url"),
            "uploaded_files": snap["uploaded_files"],
            "started_at": snap["started_at"],
            "finished_at": snap["finished_at"],
            "elapsed_seconds": snap["elapsed_seconds"],
            "log_dir": snap["log_dir"],
            "source": "live",
        }

    position = _queue_position(job_id)
    row = _fetch_job(job_id)
    if row is None:
        with _registry_lock:
            entry = _registry.get(job_id)
        if entry:
            record = dict(entry["record"])
            record["source"] = "memory"
            if position is not None:
                record["queue_position"] = position
            return record
        return None
    data = row.to_dict()
    data["source"] = "database"
    if position is not None and data.get("status") == "PENDING":
        data["queue_position"] = position
    return data


def get_job_detail(job_id: str) -> Optional[Dict[str, Any]]:
    """Full job record plus its ladder, clips and current live status."""
    session = db.get_session()
    if session is None:
        status = get_status(job_id)
        return {**status, "variants": [], "clips": []} if status else None
    try:
        job = session.query(db.Job).filter(db.Job.job_id == job_id).first()
        if job is None:
            return None
        data = job.to_dict(include_config=True)
        data["variants"] = [v.to_dict() for v in
                            session.query(db.JobVariant)
                            .filter(db.JobVariant.job_id == job_id)
                            .order_by(db.JobVariant.variant_order).all()]
        data["clips"] = [c.to_dict() for c in
                         session.query(db.JobClip)
                         .filter(db.JobClip.job_id == job_id)
                         .order_by(db.JobClip.clip_order).all()]
        if data["status"] == "RUNNING":
            live = get_status(job_id)
            if live and live.get("source") == "live":
                data.update({"stage": live["stage"],
                             "progress_pct": live["progress_pct"]})
        return data
    except Exception as e:
        logger.error(f"[{job_id}] detail query failed: {e}", exc_info=True)
        return None
    finally:
        db.close_session(session)


def list_jobs(page: int = 1, per_page: int = 20, status: Optional[str] = None,
              channel: Optional[str] = None) -> Dict[str, Any]:
    """Paginated job listing, newest first."""
    session = db.get_session()
    if session is None:
        with _registry_lock:
            jobs = [dict(e["record"]) for e in _registry.values()]
        return {"total": len(jobs), "page": 1, "per_page": len(jobs),
                "jobs": jobs, "source": "memory",
                "warning": "Database unavailable — showing this process's jobs only."}
    try:
        query = session.query(db.Job)
        if status:
            query = query.filter(db.Job.status == status.upper())
        if channel:
            query = query.filter(db.Job.channel == channel)
        total = query.count()
        rows = (query.order_by(db.Job.submitted_at.desc())
                .offset((page - 1) * per_page).limit(per_page).all())

        jobs = []
        for row in rows:
            item = row.to_dict()
            if item["status"] == "RUNNING":
                live = get_status(row.job_id)
                if live and live.get("source") == "live":
                    item["stage"] = live["stage"]
                    item["progress_pct"] = live["progress_pct"]
            jobs.append(item)
        return {"total": total, "page": page, "per_page": per_page,
                "jobs": jobs, "source": "database"}
    except Exception as e:
        logger.error(f"Job listing failed: {e}", exc_info=True)
        raise
    finally:
        db.close_session(session)


def cancel_job(job_id: str) -> Dict[str, Any]:
    """Request cancellation. Running FFmpeg processes are terminated."""
    with _registry_lock:
        entry = _registry.get(job_id)
        ctx = entry["ctx"] if entry else None
        future = entry["future"] if entry else None

    if ctx is not None:
        ctx.request_cancel()
        return {"cancelled": True, "message": "Cancellation requested; "
                                              "the job will stop shortly."}
    if future is not None and future.cancel():
        _update_job(job_id, status="CANCELLED", stage="QUEUED",
                    completed_at=db._utcnow(),
                    error_message="Cancelled before it started running.")
        _release_lock(job_id)
        return {"cancelled": True, "message": "Queued job cancelled."}

    row = _fetch_job(job_id)
    if row is None:
        return {"cancelled": False, "message": "Job not found."}
    if row.status in ("COMPLETED", "FAILED", "CANCELLED"):
        return {"cancelled": False,
                "message": f"Job already finished with status {row.status}."}
    return {"cancelled": False,
            "message": "Job is not running on this server process."}


def read_logs(job_id: str, which: str = "job", tail: int = 200) -> Dict[str, Any]:
    """Tail one of the job's log files: ``job``, ``error`` or ``ffmpeg``."""
    filenames = {"job": "job.log", "error": "error.log", "ffmpeg": "ffmpeg.log"}
    if which not in filenames:
        raise ValueError(f"Unknown log type '{which}'. "
                         f"Choose one of: {', '.join(filenames)}")

    log_dir = None
    with _registry_lock:
        entry = _registry.get(job_id)
        if entry:
            ctx = entry["ctx"]
            log_dir = str(ctx.log_dir) if ctx else entry["record"].get("log_dir")
    if not log_dir:
        row = _fetch_job(job_id)
        if row is None:
            return {"found": False}
        log_dir = row.log_dir

    if not log_dir:
        return {"found": True, "log_dir": None, "content": "", "type": which}

    path = os.path.join(log_dir, filenames[which])
    if not os.path.exists(path):
        return {"found": True, "log_dir": log_dir, "path": path,
                "content": "", "type": which,
                "note": ("Log file does not exist yet." if os.path.isdir(log_dir) else
                         "The job's log folder no longer exists (it may have been "
                         "removed under LOG_RETENTION_DAYS).")}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return {"found": True, "log_dir": log_dir, "path": path, "type": which,
                "lines_returned": min(len(lines), tail), "total_lines": len(lines),
                "content": "".join(lines[-tail:])}
    except OSError as e:
        return {"found": True, "log_dir": log_dir, "path": path, "type": which,
                "content": "", "error": f"Could not read log file: {e}"}


def delete_job(job_id: str) -> Dict[str, Any]:
    """Remove a finished job's database rows (log files are left in place)."""
    with _registry_lock:
        entry = _registry.get(job_id)
    if entry and entry.get("ctx") is not None:
        return {"deleted": False, "message": "Job is still running. Cancel it first."}

    session = db.get_session()
    if session is None:
        return {"deleted": False, "message": "Database unavailable."}
    try:
        job = session.query(db.Job).filter(db.Job.job_id == job_id).first()
        if job is None:
            return {"deleted": False, "message": "Job not found."}
        if job.status == "RUNNING":
            return {"deleted": False, "message": "Job is still running. Cancel it first."}
        session.query(db.JobVariant).filter(db.JobVariant.job_id == job_id).delete()
        session.query(db.JobClip).filter(db.JobClip.job_id == job_id).delete()
        session.delete(job)
        session.commit()
        with _registry_lock:
            _registry.pop(job_id, None)
        return {"deleted": True, "message": "Job deleted."}
    except Exception as e:
        session.rollback()
        logger.error(f"[{job_id}] delete failed: {e}", exc_info=True)
        return {"deleted": False, "message": f"Delete failed: {e}"}
    finally:
        db.close_session(session)


# ---------------------------------------------------------------------------
# Restarts
# ---------------------------------------------------------------------------
def shutdown_gracefully(timeout: float = 90.0) -> Dict[str, int]:
    """Stop for a restart without leaving jobs in a state nobody can explain.

    New submissions are refused. Queued jobs are dropped from this process but
    stay PENDING in the database, so the next start picks them up again.
    Running jobs are interrupted: FFmpeg is stopped and each is recorded as
    FAILED with a message saying the server shut down and the job should be
    resubmitted. Waits up to `timeout` seconds for that to be written.
    """
    _shutting_down.set()
    with _registry_lock:
        running = [(job_id, e["ctx"], e["future"]) for job_id, e in _registry.items()
                   if e.get("ctx") is not None]
        queued = [job_id for job_id, e in _registry.items()
                  if e.get("ctx") is None and e.get("future") is not None
                  and not e["future"].done()]
    for job_id, ctx, _ in running:
        where = f"at stage {ctx.stage} ({ctx.progress_pct}%)"
        ctx.request_interrupt(INTERRUPTED_MESSAGE.format(where=f"running, {where}"))
        logger.warning(f"[{job_id}] interrupting for shutdown {where}")

    global _executor
    with _executor_lock:
        executor, _executor = _executor, None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)
    for job_id in queued:
        _release_lock(job_id)            # stays PENDING; the next start requeues it
    if queued:
        logger.warning(f"{len(queued)} queued job(s) left PENDING for the next start.")

    deadline = time.monotonic() + timeout
    for job_id, _, future in running:
        remaining = deadline - time.monotonic()
        if future is None or remaining <= 0:
            continue
        try:
            future.result(timeout=remaining)
        except Exception:
            pass
    unfinished = [job_id for job_id, _, future in running
                  if future is not None and not future.done()]
    for job_id in unfinished:
        logger.error(f"[{job_id}] did not stop within {timeout:.0f}s of shutdown; the "
                     f"next start will mark it interrupted.")
    return {"interrupted": len(running), "requeued_later": len(queued),
            "unfinished": len(unfinished)}


def recover_on_startup() -> Dict[str, int]:
    """Settle jobs a previous server process left behind.

    A job still RUNNING in the database whose owner is gone was interrupted by
    a crash or restart: it is marked FAILED with an explanation. A job still
    PENDING never started: it is queued again from the configuration stored
    with it (unless REQUEUE_PENDING_ON_START=0). Jobs some live process still
    owns are left alone, so this is safe to run while other processes work.
    """
    counts = {"interrupted": 0, "requeued": 0, "skipped_owned": 0, "unrecoverable": 0}
    session = db.get_session()
    if session is None:
        logger.warning("Database unavailable: jobs interrupted by a previous restart "
                       "cannot be settled or requeued.")
        return counts
    try:
        rows = (session.query(db.Job)
                .filter(db.Job.status.in_(("PENDING", "RUNNING")))
                .order_by(db.Job.submitted_at.asc()).all())
        for row in rows:
            session.expunge(row)
    except Exception as e:
        logger.error(f"Could not look for interrupted jobs: {e}", exc_info=True)
        return counts
    finally:
        db.close_session(session)

    for row in rows:
        if job_is_owned(row.job_id):
            counts["skipped_owned"] += 1
            continue
        if row.status == "RUNNING" or not REQUEUE_PENDING_ON_START:
            where = (f"running (last seen at stage {row.stage}, {row.progress_pct or 0}%)"
                     if row.status == "RUNNING" else "queued")
            _update_job(row.job_id, status="FAILED", error_stage="INTERRUPTED",
                        error_message=INTERRUPTED_MESSAGE.format(where=where),
                        completed_at=db._utcnow())
            counts["interrupted"] += 1
            logger.warning(f"[{row.job_id}] marked interrupted ({where})")
            continue

        config, payload = _decode_json(row.config_snapshot), _decode_json(row.request_payload)
        if not isinstance(config, dict) or not isinstance(payload, dict):
            _update_job(row.job_id, status="FAILED", error_stage="INTERRUPTED",
                        error_message="Interrupted while queued, and its stored "
                                      "configuration could not be read. Resubmit the job.",
                        completed_at=db._utcnow())
            counts["unrecoverable"] += 1
            continue
        record = row.to_dict()
        try:
            _enqueue(row.job_id, record, config, _overrides_from_payload(payload),
                     payload=payload)
        except RuntimeError:
            counts["skipped_owned"] += 1           # another process just took it
            continue
        counts["requeued"] += 1
        logger.info(f"[{row.job_id}] requeued after restart")
    if any(counts.values()):
        logger.info(f"Startup recovery: {counts}")
    return counts


def _decode_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def queue_stats() -> Dict[str, Any]:
    queued = len(_queued_job_ids())
    with _registry_lock:
        running = sum(1 for e in _registry.values() if e["ctx"] is not None)
        tracked = len(_registry)
        counters = dict(_counters)
    return {"max_concurrent_jobs": MAX_CONCURRENT_JOBS,
            "max_queued_jobs": MAX_QUEUED_JOBS,
            "running": running, "queued": queued, "tracked_in_process": tracked,
            "shutting_down": _shutting_down.is_set(), "counters": counters}


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------
def _persist_new_job(record: Dict[str, Any], config: Dict[str, Any],
                     payload: Dict[str, Any], settings: Dict[str, Any],
                     idempotency_key: Optional[str] = None) -> None:
    session = db.get_session()
    if session is None:
        logger.warning(f"[{record['job_id']}] database unavailable — "
                       f"the job will run but is not persisted.")
        return
    try:
        job = db.Job(**record,
                     config_snapshot=_jsonable(config),
                     request_payload=_jsonable(payload),
                     idempotency_key=idempotency_key)
        session.add(job)

        ladder = config.get("video_templates", {}).get(settings["template_name"], [])
        wanted = [r.strip() for r in (settings["resolution"] or "").split(",") if r.strip()]
        for order, rung in enumerate(ladder):
            if wanted and rung.get("name") not in wanted:
                continue
            session.add(db.JobVariant(
                job_id=record["job_id"], name=rung.get("name"),
                width=rung.get("width"), height=rung.get("height"),
                codec=rung.get("codec"), bitrate=rung.get("bitrate"),
                crf=str(rung["crf"]) if rung.get("crf") is not None else None,
                preset=rung.get("preset"),
                gop_size=_as_float(rung.get("GopSize")),
                threads=rung.get("threads"), codec_params=rung.get("codec_params"),
                variant_order=order))

        for order, clip in enumerate(config.get("defaults", {}).get("InputClippings", [])):
            session.add(db.JobClip(job_id=record["job_id"],
                                   start_timecode=clip.get("StartTimecode"),
                                   end_timecode=clip.get("EndTimecode"),
                                   clip_order=order))
        session.commit()
    except Exception as e:
        session.rollback()
        if idempotency_key and type(e).__name__ == "IntegrityError":
            # Another process stored the same key a moment ago.
            raise IdempotencyConflict(
                f"A job with Idempotency-Key {idempotency_key!r} was submitted at the "
                f"same time; retry the request to get that job.") from e
        logger.error(f"[{record['job_id']}] failed to persist job: {e}", exc_info=True)
    finally:
        db.close_session(session)


def _update_job(job_id: str, **fields) -> None:
    """Patch a job row, ignoring keys whose value is None."""
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        return
    with _registry_lock:
        entry = _registry.get(job_id)
        if entry:
            entry["record"].update(fields)

    session = db.get_session()
    if session is None:
        return
    try:
        job = session.query(db.Job).filter(db.Job.job_id == job_id).first()
        if job is None:
            return
        for key, value in fields.items():
            if hasattr(job, key):
                setattr(job, key, value)
        session.commit()
    except Exception as e:
        session.rollback()
        logger.warning(f"[{job_id}] DB update failed: {e}")
    finally:
        db.close_session(session)


def _fetch_job(job_id: str):
    session = db.get_session()
    if session is None:
        return None
    try:
        job = session.query(db.Job).filter(db.Job.job_id == job_id).first()
        if job is not None:
            session.expunge(job)
        return job
    except Exception as e:
        logger.warning(f"[{job_id}] DB fetch failed: {e}")
        return None
    finally:
        db.close_session(session)


def _jsonable(value: Any) -> Any:
    """A JSON-safe copy of `value` for a JSON column.

    The column type serialises it, on MySQL and SQLite alike, so it must not be
    turned into a string here — that would store a quoted string, not an object.
    Round-tripping turns anything JSON cannot hold (a Path, say) into its text
    form up front, so it is stored readably instead of failing in the driver.
    """
    return json.loads(json.dumps(value, default=str))


def _as_float(value) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def shutdown(wait: bool = True) -> None:
    global _executor
    with _executor_lock:
        executor, _executor = _executor, None
    if executor is not None:
        executor.shutdown(wait=wait)
