"""MySQL persistence for transcoding jobs.

Every job submitted through the API is stored here along with the exact
configuration it ran with (``config_snapshot``), its rendition ladder and its
clip list — so a job can be inspected, listed or reproduced later.

Connection settings come from the environment, or from a ``.env`` file in the
project root (see ``.env.example``):

    DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME
    DATABASE_URL   (a full SQLAlchemy URL; overrides the individual settings)

The password is never given a default: it has to be supplied, and it is masked
whenever a connection string is logged.
"""
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import (JSON, Column, DateTime, Float, ForeignKey, Integer, String,
                        Text, create_engine, text)
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

logger = logging.getLogger(__name__)


def _load_dotenv() -> None:
    """Read ``.env`` from the project root when python-dotenv is installed.

    Values already in the environment win, so a systemd EnvironmentFile or an
    exported variable overrides the file.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)


_load_dotenv()

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "Visionular-Transcoder")


def _build_database_url() -> str:
    """The SQLAlchemy URL for MySQL via PyMySQL.

    ``URL.create`` escapes every part, so a password containing ``@``, ``:`` or
    ``/`` is passed through intact — with an f-string, a password such as
    ``p@ss`` would be split at the ``@`` and the login would fail.
    utf8mb4 is MySQL's real UTF-8; plain ``utf8`` cannot store every character.
    """
    explicit = os.getenv("DATABASE_URL")
    if explicit:
        return explicit
    url = URL.create("mysql+pymysql", username=DB_USER, password=DB_PASSWORD or None,
                     host=DB_HOST, port=DB_PORT, database=DB_NAME,
                     query={"charset": "utf8mb4"})
    return url.render_as_string(hide_password=False)


DATABASE_URL = _build_database_url()

# Native JSON on MySQL 5.7.8+ (and on SQLite, which the tests use).
_JSON = JSON()

JOB_STATUSES = ("PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED")


def _utcnow() -> datetime:
    """Naive UTC — MySQL DATETIME columns carry no time zone."""
    return datetime.now(timezone.utc).replace(tzinfo=None)



class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(36), unique=True, nullable=False, index=True)
    name = Column(String(255))
    channel = Column(String(255), index=True)

    status = Column(String(16), nullable=False, default="PENDING", index=True)
    stage = Column(String(32), default="QUEUED")
    progress_pct = Column(Integer, default=0)

    input_video = Column(Text)
    input_is_s3 = Column(Integer, default=0)
    subtitle_file = Column(Text)
    subtitle_language = Column(String(10), default="en")

    template = Column(String(100))
    resolutions = Column(String(255))
    esam_enabled = Column(Integer, default=0)
    audio_norm_enabled = Column(Integer, default=0)
    thumbnails_enabled = Column(Integer, default=0)
    upload_enabled = Column(Integer, default=1)

    output_dir_name = Column(String(255))
    s3_bucket = Column(String(255))
    s3_key_prefix = Column(String(500))
    output_prefix = Column(Text)
    playback_url = Column(Text)
    uploaded_files = Column(Integer, default=0)

    source_duration_seconds = Column(Float)
    source_fps = Column(String(32))

    log_dir = Column(Text)
    error_stage = Column(String(32))
    error_message = Column(Text)

    config_snapshot = Column(_JSON)
    request_payload = Column(_JSON)

    submitted_at = Column(DateTime, default=_utcnow)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    duration_seconds = Column(Float)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    def to_dict(self, include_config: bool = False) -> Dict[str, Any]:
        data = {
            "job_id": self.job_id,
            "name": self.name,
            "channel": self.channel,
            "status": self.status,
            "stage": self.stage,
            "progress_pct": self.progress_pct or 0,
            "input_video": self.input_video,
            "input_is_s3": bool(self.input_is_s3),
            "subtitle_file": self.subtitle_file,
            "subtitle_language": self.subtitle_language,
            "template": self.template,
            "resolutions": self.resolutions,
            "esam_enabled": bool(self.esam_enabled),
            "audio_norm_enabled": bool(self.audio_norm_enabled),
            "thumbnails_enabled": bool(self.thumbnails_enabled),
            "upload_enabled": bool(self.upload_enabled),
            "output_dir_name": self.output_dir_name,
            "s3_bucket": self.s3_bucket,
            "s3_key_prefix": self.s3_key_prefix,
            "output_prefix": self.output_prefix,
            "playback_url": self.playback_url,
            "uploaded_files": self.uploaded_files or 0,
            "source_duration_seconds": self.source_duration_seconds,
            "source_fps": self.source_fps,
            "log_dir": self.log_dir,
            "error_stage": self.error_stage,
            "error_message": self.error_message,
            "submitted_at": _iso(self.submitted_at),
            "started_at": _iso(self.started_at),
            "completed_at": _iso(self.completed_at),
            "duration_seconds": self.duration_seconds,
        }
        if include_config:
            data["config_snapshot"] = self.config_snapshot
            data["request_payload"] = self.request_payload
        return data


class JobVariant(Base):
    """One rendition of the ABR ladder the job actually ran."""
    __tablename__ = "job_variants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(36), ForeignKey("jobs.job_id", ondelete="CASCADE"),
                    nullable=False, index=True)
    name = Column(String(50))
    width = Column(Integer)
    height = Column(Integer)
    codec = Column(String(50))
    bitrate = Column(String(50))
    crf = Column(String(20))
    preset = Column(String(50))
    gop_size = Column(Float)
    threads = Column(Integer)
    codec_params = Column(Text)
    variant_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=_utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "width": self.width, "height": self.height,
                "codec": self.codec, "bitrate": self.bitrate, "crf": self.crf,
                "preset": self.preset, "gop_size": self.gop_size,
                "threads": self.threads, "codec_params": self.codec_params}


class JobClip(Base):
    """One entry from InputClippings."""
    __tablename__ = "job_clips"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(36), ForeignKey("jobs.job_id", ondelete="CASCADE"),
                    nullable=False, index=True)
    start_timecode = Column(String(20))
    end_timecode = Column(String(20))
    clip_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=_utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {"StartTimecode": self.start_timecode,
                "EndTimecode": self.end_timecode,
                "order": self.clip_order}


engine = None
SessionLocal = None
# Why the last init_db() failed, for `app.py --check` to explain.
last_init_error: Optional[BaseException] = None


def init_db(create_database: bool = True) -> bool:
    """Connect, create the database if needed, and ensure the tables exist.

    Returns True when the database is usable. A False return is not fatal — the
    API still runs transcodes, it just cannot persist or list them.
    """
    global engine, SessionLocal, last_init_error
    last_init_error = None
    try:
        url = make_url(DATABASE_URL)
        if url.get_backend_name() == "mysql" and not url.password:
            raise RuntimeError(
                "DB_PASSWORD is not set. Put it in .env (see .env.example) or "
                "export it before starting the API.")
        if create_database and url.get_backend_name() == "mysql":
            _ensure_database_exists()
        # pool_pre_ping replaces connections MySQL closed after wait_timeout
        # ("MySQL server has gone away") before a request can hit one;
        # pool_recycle retires them well inside MySQL's default 8 hours.
        engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True,
                               pool_recycle=3600, pool_size=5, max_overflow=10)
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info(f"Database ready: {_safe_url()}")
        return True
    except Exception as e:
        logger.error(f"Database initialisation failed ({_safe_url()}): {e}")
        last_init_error = e
        engine = None
        SessionLocal = None
        return False


# MySQL identifiers: letters, digits, '_', '$' and '-' cover every real name. The
# name is interpolated into DDL (it cannot be a bound parameter), so anything
# else is refused rather than escaped.
_SAFE_DATABASE_NAME = re.compile(r"^[A-Za-z0-9_$-]{1,64}$")


def _ensure_database_exists() -> None:
    """CREATE DATABASE when it is missing.

    Connects without selecting a database, since the target may not exist yet.
    The name is backtick-quoted: ``Visionular-Transcoder`` contains a hyphen,
    which MySQL would otherwise read as a minus sign.
    """
    url = make_url(DATABASE_URL)
    target = url.database
    if not target:
        raise RuntimeError("No database name configured (DB_NAME).")
    if not _SAFE_DATABASE_NAME.match(target):
        raise RuntimeError(
            f"Refusing to create database {target!r}: use letters, digits, "
            f"'_', '$' or '-' only.")
    # Built explicitly: URL.set(database=None) means "unchanged", not "none",
    # and would connect to the very database this is meant to create.
    server_url = URL.create(url.drivername, username=url.username,
                            password=url.password, host=url.host, port=url.port,
                            query=url.query)
    admin_engine = create_engine(server_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM information_schema.SCHEMATA "
                     "WHERE SCHEMA_NAME = :name"), {"name": target}).scalar()
            if not exists:
                conn.execute(text(
                    f"CREATE DATABASE `{target}` "
                    f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
                logger.info(f"Created database {target}")
    finally:
        admin_engine.dispose()


def get_session():
    """A new session, or None when the database is unavailable."""
    if SessionLocal is None:
        return None
    return SessionLocal()


def close_session(session) -> None:
    if session is not None:
        try:
            session.close()
        except Exception:
            pass


def is_available() -> bool:
    return SessionLocal is not None

def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() + "Z" if value else None


def _safe_url() -> str:
    """DATABASE_URL with the password masked, for logging."""
    try:
        return make_url(DATABASE_URL).render_as_string(hide_password=True)
    except Exception:
        return "(unparseable DATABASE_URL)"


__all__ = ["Base", "Job", "JobVariant", "JobClip", "init_db", "get_session",
           "close_session", "is_available", "JOB_STATUSES", "DATABASE_URL"]
