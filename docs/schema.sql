-- AI-Transcoder schema (PostgreSQL).
-- The API creates these tables automatically on start; this file is for
-- provisioning them explicitly, or for reviewing the shape of the data.
--
--   psql -h 127.0.0.1 -U transcoder -d ai_transcoder -f docs/schema.sql

CREATE TABLE IF NOT EXISTS jobs (
    id                      SERIAL PRIMARY KEY,
    job_id                  VARCHAR(36) UNIQUE NOT NULL,
    name                    VARCHAR(255),
    channel                 VARCHAR(255),

    status                  VARCHAR(16)  NOT NULL DEFAULT 'PENDING',
    stage                   VARCHAR(32)  DEFAULT 'QUEUED',
    progress_pct            INTEGER      DEFAULT 0,

    input_video             TEXT,
    input_is_s3             INTEGER      DEFAULT 0,
    subtitle_file           TEXT,
    subtitle_language       VARCHAR(10)  DEFAULT 'en',

    template                VARCHAR(100),
    resolutions             VARCHAR(255),
    esam_enabled            INTEGER      DEFAULT 0,
    audio_norm_enabled      INTEGER      DEFAULT 0,
    thumbnails_enabled      INTEGER      DEFAULT 0,
    upload_enabled          INTEGER      DEFAULT 1,

    output_dir_name         VARCHAR(255),
    s3_bucket               VARCHAR(255),
    s3_key_prefix           VARCHAR(500),
    output_prefix           TEXT,
    playback_url            TEXT,
    uploaded_files          INTEGER      DEFAULT 0,

    source_duration_seconds DOUBLE PRECISION,
    source_fps              VARCHAR(32),

    log_dir                 TEXT,
    error_stage             VARCHAR(32),
    error_message           TEXT,

    config_snapshot         JSONB,
    request_payload         JSONB,

    submitted_at            TIMESTAMP DEFAULT (now() AT TIME ZONE 'utc'),
    started_at              TIMESTAMP,
    completed_at            TIMESTAMP,
    duration_seconds        DOUBLE PRECISION,
    created_at              TIMESTAMP DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at              TIMESTAMP DEFAULT (now() AT TIME ZONE 'utc'),

    CONSTRAINT jobs_status_check
        CHECK (status IN ('PENDING','RUNNING','COMPLETED','FAILED','CANCELLED'))
);

CREATE INDEX IF NOT EXISTS idx_jobs_job_id       ON jobs (job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status       ON jobs (status);
CREATE INDEX IF NOT EXISTS idx_jobs_channel      ON jobs (channel);
CREATE INDEX IF NOT EXISTS idx_jobs_submitted_at ON jobs (submitted_at DESC);

-- One row per rendition of the ABR ladder the job ran.
CREATE TABLE IF NOT EXISTS job_variants (
    id            SERIAL PRIMARY KEY,
    job_id        VARCHAR(36) NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    name          VARCHAR(50),
    width         INTEGER,
    height        INTEGER,
    codec         VARCHAR(50),
    bitrate       VARCHAR(50),
    crf           VARCHAR(20),
    preset        VARCHAR(50),
    gop_size      DOUBLE PRECISION,
    threads       INTEGER,
    codec_params  TEXT,
    variant_order INTEGER DEFAULT 0,
    created_at    TIMESTAMP DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE INDEX IF NOT EXISTS idx_job_variants_job_id ON job_variants (job_id);

-- One row per InputClippings entry.
CREATE TABLE IF NOT EXISTS job_clips (
    id             SERIAL PRIMARY KEY,
    job_id         VARCHAR(36) NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    start_timecode VARCHAR(20),
    end_timecode   VARCHAR(20),
    clip_order     INTEGER DEFAULT 0,
    created_at     TIMESTAMP DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE INDEX IF NOT EXISTS idx_job_clips_job_id ON job_clips (job_id);
