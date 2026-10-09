"""Gunicorn settings for the AI-Transcoder API (see deploy/ai-transcoder.service).

    gunicorn -c deploy/gunicorn.conf.py "api.app:create_app()"

One worker on purpose: the live job registry that serves progress lives in the
worker process. Concurrency comes from threads, the job pool
(MAX_CONCURRENT_JOBS) and the machine-wide CPU budget.
"""
import os

bind = os.getenv("BIND", f"{os.getenv('HOST', '0.0.0.0')}:{os.getenv('PORT', '8000')}")
workers = 1
threads = int(os.getenv("API_THREADS", "8"))
worker_class = "gthread"
timeout = 0                     # requests are short; jobs run in background threads
accesslog = "-"
errorlog = "-"

# On stop or restart the worker interrupts running jobs (FFmpeg is stopped and
# each job is recorded as FAILED "interrupted, resubmit") and leaves queued jobs
# PENDING for the next start. That takes a few seconds per job; this is how
# long gunicorn waits before killing the worker outright. Keep systemd's
# TimeoutStopSec above it.
graceful_timeout = int(os.getenv("GRACEFUL_TIMEOUT", "120"))


def worker_exit(server, worker):
    """Runs in the worker process as it exits, before its threads are joined."""
    from api import job_manager
    result = job_manager.shutdown_gracefully(timeout=max(10, graceful_timeout - 20))
    server.log.info(f"Transcoder shutdown: {result}")
