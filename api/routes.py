"""HTTP surface of the transcoding service.

    POST   /api/v1/jobs                  submit a job (alias: /api/v1/jobs/start)
    GET    /api/v1/jobs                  list jobs (paginated, filterable)
    GET    /api/v1/jobs/<id>             full job record + ladder + clips
    GET    /api/v1/jobs/<id>/status      status and progress percentage
    GET    /api/v1/jobs/<id>/logs        tail job.log / error.log / ffmpeg.log
    POST   /api/v1/jobs/<id>/cancel      request cancellation
    DELETE /api/v1/jobs/<id>             delete a finished job's rows
    GET    /api/v1/templates             configured encoding ladders
    GET    /api/v1/config                the server's active configuration
    GET    /health                       liveness (the process is up)
    GET    /ready                        readiness: 200 when a job can run now, else 503
    GET    /metrics                      Prometheus metrics

A POST /jobs may carry an ``Idempotency-Key`` header: retrying with the same key
returns the original job (200) instead of starting another.
"""
import logging

from flask import Blueprint, Response, current_app, jsonify, request

from api import database as db
from api import job_manager

logger = logging.getLogger(__name__)

api_bp = Blueprint("api", __name__, url_prefix="/api/v1")
health_bp = Blueprint("health", __name__)

MAX_PER_PAGE = 200


def _config():
    return current_app.config["TRANSCODER_CONFIG"]


@api_bp.route("/jobs", methods=["POST"])
@api_bp.route("/jobs/start", methods=["POST"])
def start_job():
    """Queue a transcoding job.

    Body (every field optional — anything omitted falls back to config.json)::

        {
          "name": "American Pickers S10E03",
          "input_video": "s3://bucket/path/asset.mp4",
          "subtitle_file": "s3://bucket/path/asset.vtt",
          "subtitle_language": "en",
          "output_dir": "AETN_AmericanPickers_S10_E03_en",
          "template": "h264_standard",
          "resolutions": "1080p,720p",
          "esam": true,
          "audio_norm": true,
          "generate_thumbnails": true,
          "upload": true,
          "duration": 120,
          "clippings": [{"StartTimecode": "00:00:00:00", "EndTimecode": "00:10:02:27"}],
          "hls_settings": {"hls_time": 6},
          "s3_bucket": "my-bucket",
          "s3_key_prefix": "Visionular/V3",
          "esam_scc_xml": "<?xml ...",
          "esam_mcc_xml": "<?xml ..."
        }
    """
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400

    try:
        record = job_manager.submit_job(_config(), payload,
                                        idempotency_key=request.headers.get("Idempotency-Key"))
    except job_manager.ServiceUnavailable as e:
        return jsonify({"error": str(e)}), 503, {"Retry-After": "30"}
    except job_manager.QueueFull as e:
        return jsonify({"error": str(e)}), 429, {"Retry-After": "60"}
    except job_manager.IdempotencyConflict as e:
        return jsonify({"error": str(e)}), 422
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Job submission failed: {e}", exc_info=True)
        return jsonify({"error": f"Could not submit job: {e}"}), 500

    duplicate = bool(record.get("duplicate"))
    body = {
        "message": ("This Idempotency-Key was already used; returning the original job."
                    if duplicate else "Transcoding job queued."),
        "job_id": record["job_id"],
        "name": record["name"],
        "channel": record["channel"],
        "status": record["status"],
        "log_dir": record["log_dir"],
        "status_url": f"/api/v1/jobs/{record['job_id']}/status",
    }
    if duplicate:
        body["duplicate"] = True
    if record.get("queue_position"):
        body["queue_position"] = record["queue_position"]
    if record.get("warnings"):
        body["warnings"] = record["warnings"]
    return jsonify(body), 200 if duplicate else 202


@api_bp.route("/jobs/<job_id>/status", methods=["GET"])
def job_status(job_id):
    status = job_manager.get_status(job_id)
    if status is None:
        return jsonify({"error": "Job not found", "job_id": job_id}), 404

    # A failed job carries its error tail inline so callers do not need a
    # second request to find out what went wrong.
    if status.get("status") == "FAILED":
        logs = job_manager.read_logs(job_id, "error", tail=40)
        status["error_log_tail"] = logs.get("content", "")
    return jsonify(status), 200


@api_bp.route("/jobs/<job_id>", methods=["GET"])
def job_detail(job_id):
    detail = job_manager.get_job_detail(job_id)
    if detail is None:
        return jsonify({"error": "Job not found", "job_id": job_id}), 404
    return jsonify(detail), 200


@api_bp.route("/jobs", methods=["GET"])
def list_jobs():
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(MAX_PER_PAGE, max(1, int(request.args.get("per_page", 20))))
    except ValueError:
        return jsonify({"error": "page and per_page must be integers."}), 400

    status = request.args.get("status")
    if status and status.upper() not in db.JOB_STATUSES:
        return jsonify({"error": f"Unknown status '{status}'. "
                                 f"Valid: {', '.join(db.JOB_STATUSES)}"}), 400
    try:
        result = job_manager.list_jobs(page=page, per_page=per_page, status=status,
                                       channel=request.args.get("channel"))
    except Exception as e:
        return jsonify({"error": f"Could not list jobs: {e}"}), 500
    return jsonify(result), 200


@api_bp.route("/jobs/<job_id>/logs", methods=["GET"])
def job_logs(job_id):
    which = request.args.get("type", "job")
    try:
        tail = min(5000, max(1, int(request.args.get("tail", 200))))
    except ValueError:
        return jsonify({"error": "tail must be an integer."}), 400
    try:
        result = job_manager.read_logs(job_id, which, tail)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not result.get("found"):
        return jsonify({"error": "Job not found", "job_id": job_id}), 404
    return jsonify({"job_id": job_id, **result}), 200


@api_bp.route("/jobs/<job_id>/cancel", methods=["POST"])
def cancel_job(job_id):
    result = job_manager.cancel_job(job_id)
    if not result["cancelled"] and result["message"] == "Job not found.":
        return jsonify({"error": "Job not found", "job_id": job_id}), 404
    return jsonify({"job_id": job_id, **result}), 200 if result["cancelled"] else 409


@api_bp.route("/jobs/<job_id>", methods=["DELETE"])
def delete_job(job_id):
    result = job_manager.delete_job(job_id)
    if not result["deleted"] and result["message"] == "Job not found.":
        return jsonify({"error": "Job not found", "job_id": job_id}), 404
    return jsonify({"job_id": job_id, **result}), 200 if result["deleted"] else 409


@api_bp.route("/templates", methods=["GET"])
def list_templates():
    templates = _config().get("video_templates", {})
    return jsonify({
        "templates": [
            {"name": name,
             "resolutions": [r.get("name") for r in ladder],
             "codec": ladder[0].get("codec") if ladder else None,
             "ladder": ladder}
            for name, ladder in templates.items()]
    }), 200


@api_bp.route("/config", methods=["GET"])
def show_config():
    """The active server configuration, with ESAM XML bodies elided for size."""
    config = dict(_config())
    esam = config.get("Esam")
    if isinstance(esam, dict):
        config["Esam"] = {
            "SignalProcessingNotification": {
                "SccXml": _elide(esam.get("SignalProcessingNotification", {}).get("SccXml"))},
            "ManifestConfirmConditionNotification": {
                "MccXml": _elide(esam.get("ManifestConfirmConditionNotification", {})
                                 .get("MccXml"))},
        }
    return jsonify(config), 200


@health_bp.route("/health", methods=["GET"])
def health():
    payload = {
        "status": "ok",
        "service": "ai-transcoder-api",
        "database": "connected" if db.is_available() else "unavailable",
        "queue": job_manager.queue_stats(),
    }
    return jsonify(payload), 200


@health_bp.route("/ready", methods=["GET"])
def ready():
    from api.ops import readiness
    ok, payload = readiness(_config())
    return jsonify(payload), 200 if ok else 503


@health_bp.route("/metrics", methods=["GET"])
def metrics():
    from api.ops import metrics_text
    return Response(metrics_text(_config()),
                    mimetype="text/plain; version=0.0.4; charset=utf-8")


def _elide(value, keep: int = 120):
    if not value:
        return value
    return value[:keep] + f"... ({len(value)} chars total)" if len(value) > keep else value
