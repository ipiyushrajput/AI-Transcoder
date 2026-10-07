"""Flask application factory for the AI-Transcoder service."""
import json
import logging
import os
from logging.handlers import RotatingFileHandler

from flask import Flask, jsonify

from api import database as db
from hls_toolkit.cpu_budget import get_shared_budget
from api.routes import api_bp, health_bp

CONFIG_PATH = os.getenv("TRANSCODER_CONFIG", "config.json")
SERVER_LOG_DIR = os.getenv("SERVER_LOG_DIR", "logs/_server")


def load_config(path: str) -> dict:
    """Read the server's base configuration.

    A missing or malformed file is not fatal: the API still starts so ``/health``
    answers, and every job request that needs the missing values is rejected with
    a clear 400.
    """
    if not os.path.exists(path):
        logging.warning(f"Config file '{path}' not found — every job request must "
                        f"supply its own settings.")
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        logging.error(f"Config '{path}' is not valid JSON: {e}")
        return {}
    except OSError as e:
        logging.error(f"Could not read config '{path}': {e}")
        return {}
    if not isinstance(config, dict):
        logging.error(f"Config '{path}' must contain a JSON object.")
        return {}
    logging.info(f"Loaded configuration from {path}")
    return config


def configure_logging() -> None:
    """Console + rotating file logging for the server itself.

    Per-job logs are separate: see ``logs/<channel>/<job_id>/``.
    """
    os.makedirs(SERVER_LOG_DIR, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(os.path.join(SERVER_LOG_DIR, "api.log"),
                                       maxBytes=50 * 1024 * 1024, backupCount=10,
                                       encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    error_handler = RotatingFileHandler(os.path.join(SERVER_LOG_DIR, "api-error.log"),
                                        maxBytes=20 * 1024 * 1024, backupCount=5,
                                        encoding="utf-8")
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(fmt)
    root.addHandler(error_handler)

    # boto3 is chatty at INFO during multipart transfers.
    for noisy in ("boto3", "botocore", "s3transfer", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def create_app(config_path: str = None) -> Flask:
    configure_logging()
    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False
    app.config["TRANSCODER_CONFIG"] = load_config(config_path or CONFIG_PATH)

    # The same machine-wide CPU budget the CLI uses, so API jobs and any
    # `python app.py` runs on this server share the cores instead of each
    # assuming it owns them.
    get_shared_budget(None, app.config["TRANSCODER_CONFIG"]
                      .get("parallelism", {}).get("cpu_budget"))

    try:
        from flask_cors import CORS
        CORS(app, resources={r"/api/*": {"origins": os.getenv("CORS_ORIGINS", "*")}})
    except ImportError:
        logging.warning("flask-cors is not installed — cross-origin requests will fail.")

    if not db.init_db():
        logging.warning("Running without persistence: job history and listings are "
                        "unavailable until MySQL is reachable (see docs/MYSQL_SETUP.md).")

    app.register_blueprint(api_bp)
    app.register_blueprint(health_bp)

    @app.errorhandler(400)
    def bad_request(e):
        return jsonify({"error": getattr(e, "description", "Bad request")}), 400

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"error": "Not found"}), 404

    @app.errorhandler(405)
    def method_not_allowed(e):
        return jsonify({"error": "Method not allowed for this endpoint"}), 405

    @app.errorhandler(500)
    def server_error(e):
        logging.error(f"Unhandled server error: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

    @app.errorhandler(Exception)
    def unhandled(e):
        logging.error(f"Unhandled exception: {e}", exc_info=True)
        return jsonify({"error": f"Internal server error: {type(e).__name__}"}), 500

    logging.info("AI-Transcoder API ready")
    return app


if __name__ == "__main__":
    application = create_app()
    application.run(host=os.getenv("HOST", "0.0.0.0"),
                    port=int(os.getenv("PORT", "8000")),
                    debug=False, threaded=True)
