-- AI-Transcoder schema (MySQL 8).
--
-- You do not need to run this: the API creates the database and these tables
-- on first start. It is here for reference, and for setting the schema up by
-- hand where the application user may not create tables.
--
-- Taken from SHOW CREATE TABLE on MySQL 8.0 after the API created the tables,
-- so it matches what the application builds.
--
--     mysql -u root -p < docs/schema.sql

CREATE DATABASE IF NOT EXISTS `Visionular-Transcoder`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE `Visionular-Transcoder`;

CREATE TABLE IF NOT EXISTS `jobs` (
  `id` int NOT NULL AUTO_INCREMENT,
  `job_id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `name` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `channel` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `status` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL,
  `stage` varchar(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `progress_pct` int DEFAULT NULL,
  `input_video` text COLLATE utf8mb4_unicode_ci,
  `input_is_s3` int DEFAULT NULL,
  `subtitle_file` text COLLATE utf8mb4_unicode_ci,
  `subtitle_language` varchar(10) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `template` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `resolutions` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `esam_enabled` int DEFAULT NULL,
  `audio_norm_enabled` int DEFAULT NULL,
  `thumbnails_enabled` int DEFAULT NULL,
  `upload_enabled` int DEFAULT NULL,
  `output_dir_name` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `s3_bucket` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `s3_key_prefix` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `output_prefix` text COLLATE utf8mb4_unicode_ci,
  `playback_url` text COLLATE utf8mb4_unicode_ci,
  `uploaded_files` int DEFAULT NULL,
  `source_duration_seconds` float DEFAULT NULL,
  `source_fps` varchar(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `log_dir` text COLLATE utf8mb4_unicode_ci,
  `error_stage` varchar(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `error_message` text COLLATE utf8mb4_unicode_ci,
  `config_snapshot` json DEFAULT NULL,
  `request_payload` json DEFAULT NULL,
  `submitted_at` datetime DEFAULT NULL,
  `started_at` datetime DEFAULT NULL,
  `completed_at` datetime DEFAULT NULL,
  `duration_seconds` float DEFAULT NULL,
  `created_at` datetime DEFAULT NULL,
  `updated_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `ix_jobs_job_id` (`job_id`),
  KEY `ix_jobs_channel` (`channel`),
  KEY `ix_jobs_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
;

CREATE TABLE IF NOT EXISTS `job_variants` (
  `id` int NOT NULL AUTO_INCREMENT,
  `job_id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `name` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `width` int DEFAULT NULL,
  `height` int DEFAULT NULL,
  `codec` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `bitrate` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `crf` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `preset` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `gop_size` float DEFAULT NULL,
  `threads` int DEFAULT NULL,
  `codec_params` text COLLATE utf8mb4_unicode_ci,
  `variant_order` int DEFAULT NULL,
  `created_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_job_variants_job_id` (`job_id`),
  CONSTRAINT `job_variants_ibfk_1` FOREIGN KEY (`job_id`) REFERENCES `jobs` (`job_id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
;

CREATE TABLE IF NOT EXISTS `job_clips` (
  `id` int NOT NULL AUTO_INCREMENT,
  `job_id` varchar(36) COLLATE utf8mb4_unicode_ci NOT NULL,
  `start_timecode` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `end_timecode` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `clip_order` int DEFAULT NULL,
  `created_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_job_clips_job_id` (`job_id`),
  CONSTRAINT `job_clips_ibfk_1` FOREIGN KEY (`job_id`) REFERENCES `jobs` (`job_id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
;

