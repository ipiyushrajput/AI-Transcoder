"""Background execution and bookkeeping for API-submitted transcode jobs.

Jobs run on a bounded worker pool. Progress is mirrored from the live
:class:`~hls_toolkit.job_context.JobContext` into PostgreSQL on a short interval,
so ``GET /jobs/<id>/status`` is accurate whether it is served from the in-memory
registry (job still running on this process) or from the database (finished, or
started by another worker process).
"""
import copy
import json
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from api import database as db
from hls_toolkit import s3_io
from hls_toolkit.job_context import JobContext, channel_name_for
from hls_toolkit.runner import build_run_settings, run_transcode_job

logger = logging.getLogger(__name__)

MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "2"))
LOG_ROOT = os.getenv("LOG_ROOT", "logs")
WORK_ROOT = os.getenv("WORK_ROOT") or None
PROGRESS_SYNC_SECONDS = float(os.getenv("PROGRESS_SYNC_SECONDS", "5"))

_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS,
                               thread_name_prefix="transcode")
_registry: Dict[str, Dict[str, Any]] = {}
_registry_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------
def submit_job(base_config: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a request, persist it as PENDING and queue it for execution.

    Returns the job record. Raises ValueError with a human-readable message when
    the payload is not runnable — the route turns that into a 400.
    """
    config = _merge_config(base_config, payload)
    overrides = _overrides_from_payload(payload)
    settings = build_run_settings(config, overrides)

    _validate(config, settings)

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

    _persist_new_job(record, config, payload, settings)

    with _registry_lock:
        _registry[job_id] = {"record": dict(record), "ctx": None, "future": None}

    future = _executor.submit(_run_job, job_id, config, overrides)
    with _registry_lock:
        if job_id in _registry:
            _registry[job_id]["future"] = future

    logger.info(f"[{job_id}] queued job for channel '{channel}' "
                f"(input={settings['input_video']})")
    return dict(record)


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

    logger.info(f"[{job_id}] finished with status {snapshot.get('status')}")


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

    row = _fetch_job(job_id)
    if row is None:
        with _registry_lock:
            entry = _registry.get(job_id)
        if entry:
            record = dict(entry["record"])
            record["source"] = "memory"
            return record
        return None
    data = row.to_dict()
    data["source"] = "database"
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
                "note": "Log file does not exist yet."}
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


def queue_stats() -> Dict[str, Any]:
    with _registry_lock:
        running = sum(1 for e in _registry.values() if e["ctx"] is not None)
        tracked = len(_registry)
    return {"max_concurrent_jobs": MAX_CONCURRENT_JOBS,
            "running": running, "tracked_in_process": tracked}


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------
def _persist_new_job(record: Dict[str, Any], config: Dict[str, Any],
                     payload: Dict[str, Any], settings: Dict[str, Any]) -> None:
    session = db.get_session()
    if session is None:
        logger.warning(f"[{record['job_id']}] database unavailable — "
                       f"the job will run but is not persisted.")
        return
    try:
        job = db.Job(**record,
                     config_snapshot=_jsonable(config, session),
                     request_payload=_jsonable(payload, session))
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


def _jsonable(value: Any, session) -> Any:
    """JSONB takes dicts directly; the SQLite/TEXT variant needs a string."""
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        return value
    return json.dumps(value)


def _as_float(value) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def shutdown(wait: bool = True) -> None:
    _executor.shutdown(wait=wait)
