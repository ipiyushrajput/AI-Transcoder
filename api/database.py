"""PostgreSQL persistence for transcoding jobs.

Every job submitted through the API is stored here along with the exact
configuration it ran with (``config_snapshot``), its rendition ladder and its
clip list — so a job can be inspected, listed or reproduced later.

Connection settings come from the environment:

    DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME
    DATABASE_URL   (a full SQLAlchemy URL; overrides the individual settings)
"""
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

from sqlalchemy import (Column, DateTime, Float, ForeignKey, Integer, String,
                        Text, create_engine, text)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, sessionmaker

logger = logging.getLogger(__name__)

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER", "transcoder")
DB_PASSWORD = os.getenv("DB_PASSWORD", "transcoder")
DB_NAME = os.getenv("DB_NAME", "ai_transcoder")

DATABASE_URL = os.getenv("DATABASE_URL") or (
    f"postgresql+psycopg2://{DB_USER}:{quote_plus(DB_PASSWORD)}@{DB_HOST}:{DB_PORT}/{DB_NAME}")

# JSONB on PostgreSQL, plain TEXT elsewhere so the models stay testable on SQLite.
_JSON = JSONB().with_variant(Text(), "sqlite")

JOB_STATUSES = ("PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED")


def _utcnow() -> datetime:
    """Naive UTC — Postgres columns here are `timestamp without time zone`."""
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


def init_db(create_database: bool = True) -> bool:
    """Connect, create the database if needed, and ensure the tables exist.

    Returns True when the database is usable. A False return is not fatal — the
    API still runs transcodes, it just cannot persist or list them.
    """
    global engine, SessionLocal
    try:
        if create_database and DATABASE_URL.startswith("postgresql"):
            _ensure_database_exists()
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
        engine = None
        SessionLocal = None
        return False


def _ensure_database_exists() -> None:
    """CREATE DATABASE when it is missing (connects via the `postgres` db)."""
    from sqlalchemy.engine.url import make_url

    url = make_url(DATABASE_URL)
    target = url.database
    admin_url = url.set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target}).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{target}"'))
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
        from sqlalchemy.engine.url import make_url
        url = make_url(DATABASE_URL)
        return str(url.set(password="***")) if url.password else str(url)
    except Exception:
        return "(unparseable DATABASE_URL)"


__all__ = ["Base", "Job", "JobVariant", "JobClip", "init_db", "get_session",
           "close_session", "is_available", "JOB_STATUSES", "DATABASE_URL"]
