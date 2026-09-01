"""One entry point for running a transcode, shared by the CLI and the API.

:func:`run_transcode_job` owns everything around the workflow itself: it builds
the per-channel :class:`~hls_toolkit.job_context.JobContext`, fetches S3 inputs,
stages output in a scratch directory, runs the pipeline, and guarantees the
scratch directory is removed however the run ends.
"""
import os
import shutil
import tempfile
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

from hls_toolkit import s3_io
from hls_toolkit.hls_generator import generate_hls_workflow
from hls_toolkit.job_context import (JobCancelled, JobContext, TranscodeError,
                                     bind_context, channel_name_for)


def build_run_settings(config: Dict[str, Any],
                       overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Flatten config + overrides into the values the workflow needs.

    `overrides` holds API payload fields or CLI flags; anything absent falls back
    to the ``defaults`` section of the config.
    """
    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
    defaults = config.get("defaults", {})
    paths = config.get("paths", {})
    s3_config = config.get("s3", {})
    thumbnails = config.get("thumbnail_generation", {})

    ffmpeg_executable = os.path.abspath(
        overrides.get("ffmpeg_executable") or paths.get("ffmpeg_executable", "./bin/ffmpeg"))
    ffprobe_executable = os.path.abspath(
        overrides.get("ffprobe_executable")
        or paths.get("ffprobe_executable", str(Path(ffmpeg_executable).with_name("ffprobe"))))

    # `generate-thumbnails` is the key used in the shipped config; the
    # thumbnail_generation section and an explicit override both win over it.
    thumbnails_enabled = overrides.get(
        "thumbnails_enabled",
        thumbnails.get("enabled", defaults.get("generate-thumbnails",
                                               defaults.get("generate_thumbnails", False))))

    return {
        "input_video": overrides.get("input_video") or defaults.get("input_video"),
        "subtitle_file": overrides.get("subtitle_file", defaults.get("subtitle_file")),
        "subtitle_language": overrides.get("subtitle_language",
                                           defaults.get("subtitle_language", "en")),
        "output_dir_name": overrides.get("output_dir") or defaults.get("output_dir", "output"),
        "template_name": overrides.get("template", defaults.get("template", "h264_standard")),
        "resolution": overrides.get("resolution", defaults.get("resolutions")),
        "esam": bool(overrides.get("esam", defaults.get("esam", False))),
        "audio_norm": bool(overrides.get("audio_norm", defaults.get("audio_norm", False))),
        "thumbnails_enabled": bool(thumbnails_enabled),
        "upload": bool(overrides.get("upload", defaults.get("upload", True))),
        "delete_local_output": bool(overrides.get("delete_local_output", True)),
        "duration": overrides.get("duration"),
        "transcode_workers": overrides.get("transcode_workers"),
        "debug": bool(overrides.get("debug", False)),
        "ffmpeg_executable": ffmpeg_executable,
        "ffprobe_executable": ffprobe_executable,
        "s3_config": s3_config,
        "s3_region": s3_config.get("region"),
    }


def run_transcode_job(config: Dict[str, Any],
                      overrides: Optional[Dict[str, Any]] = None,
                      job_id: Optional[str] = None,
                      log_root: str = "logs",
                      work_root: Optional[str] = None,
                      on_context_ready=None) -> Dict[str, Any]:
    """Run one transcode end to end.

    Args:
        config: The parsed configuration document.
        overrides: Per-job values that take precedence over ``config['defaults']``.
        job_id: Job identifier; when given, logs go to ``logs/<channel>/<job_id>/``.
        log_root: Root of the per-channel log tree.
        work_root: Parent for the scratch directory (defaults to the system temp).
        on_context_ready: Called with the :class:`JobContext` as soon as it
            exists, so a caller can register it for status/cancellation before
            the long-running work starts.

    Returns:
        The context snapshot — status, stage, progress, output prefix, timings.
        The dictionary is returned for both success and failure; check
        ``["status"]``.
    """
    settings = build_run_settings(config, overrides)
    input_uri = settings["input_video"]
    if not input_uri:
        raise TranscodeError("No input video was supplied (defaults.input_video "
                             "is empty and no override was given).", stage="VALIDATION")

    ctx = JobContext(input_uri=input_uri, job_id=job_id, log_root=log_root,
                     debug=settings["debug"],
                     metadata={"output_dir_name": settings["output_dir_name"],
                               "template": settings["template_name"],
                               "resolutions": settings["resolution"],
                               "esam": settings["esam"],
                               "audio_norm": settings["audio_norm"],
                               "thumbnails": settings["thumbnails_enabled"]})
    bind_context(ctx)
    if on_context_ready:
        try:
            on_context_ready(ctx)
        except Exception:
            ctx.logger.warning("on_context_ready callback failed", exc_info=True)

    if work_root:
        Path(work_root).mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix=f"aitx_{ctx.channel[:24]}_", dir=work_root))
    output_dir = work_dir / settings["output_dir_name"]
    temp_dir = work_dir / "temp"
    output_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    ctx.metadata["work_dir"] = str(work_dir)

    try:
        ctx.mark_running()
        ctx.set_stage("FETCHING_INPUT", 0.0)

        local_input = s3_io.resolve_input(input_uri, work_dir, "Input video",
                                          ctx=ctx, region=settings["s3_region"])
        local_subtitle = s3_io.resolve_input(settings["subtitle_file"], work_dir,
                                             "Subtitle file", ctx=ctx,
                                             region=settings["s3_region"])
        ctx.set_stage("FETCHING_INPUT", 1.0)
        ctx.raise_if_cancelled()

        defaults = config.get("defaults", {})
        generate_hls_workflow(
            config=config,
            defaults=defaults,
            paths=config.get("paths", {}),
            s3_config=settings["s3_config"],
            esam_config=config.get("Esam", {}),
            default_input_video=input_uri,
            default_output_dir=settings["output_dir_name"],
            default_subtitle_file=settings["subtitle_file"],
            default_subtitle_language=settings["subtitle_language"],
            input_video=local_input,
            output_dir=output_dir,
            output_dir_name=settings["output_dir_name"],
            subtitle_file=local_subtitle,
            ffmpeg_executable=settings["ffmpeg_executable"],
            ffprobe_executable=settings["ffprobe_executable"],
            video_templates=config.get("video_templates", {}),
            template_name=settings["template_name"],
            resolution=settings["resolution"],
            thumbnails_enabled=settings["thumbnails_enabled"],
            duration=settings["duration"],
            transcode_workers=settings["transcode_workers"],
            temp_dir=str(temp_dir),
            debug=settings["debug"],
            audio_norm=settings["audio_norm"],
            esam=settings["esam"],
            upload=settings["upload"],
            delete_local_output=settings["delete_local_output"])

        ctx.mark_completed()
    except JobCancelled:
        ctx.mark_cancelled()
    except TranscodeError as e:
        ctx.stage = e.stage or ctx.stage
        ctx.mark_failed(f"[{e.stage}] {e}")
    except Exception as e:
        ctx.logger.error("Unhandled error in transcode job\n" + traceback.format_exc())
        ctx.mark_failed(f"{type(e).__name__}: {e}")
    finally:
        _remove_work_dir(work_dir, keep=settings["debug"], ctx=ctx)
        bind_context(None)

    return ctx.snapshot()


def _remove_work_dir(work_dir: Path, keep: bool, ctx: JobContext) -> None:
    """Delete the scratch tree (downloaded input, clips, staged output)."""
    try:
        if keep:
            ctx.logger.info(f"Debug enabled: keeping working directory {work_dir}")
            return
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
            ctx.logger.info(f"Removed working directory {work_dir}")
    except Exception as e:
        ctx.logger.warning(f"Could not remove working directory {work_dir}: {e}")


__all__ = ["run_transcode_job", "build_run_settings", "channel_name_for"]
