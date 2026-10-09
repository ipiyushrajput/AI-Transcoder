"""One entry point for running a transcode, shared by the CLI and the API.

:func:`run_transcode_job` owns everything around the workflow itself: it builds
the per-channel :class:`~hls_toolkit.job_context.JobContext`, fetches S3 inputs,
stages output in a scratch directory, runs the pipeline, and guarantees the
scratch directory is removed however the run ends.
"""
import os
import re
import shutil
import tempfile
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

from hls_toolkit import s3_io
from hls_toolkit.ffmpeg_wrapper import configure_watchdog
from hls_toolkit.hls_generator import generate_hls_workflow
from hls_toolkit.job_context import (JobCancelled, JobContext, TranscodeError,
                                     bind_context, channel_name_for, claim_unique_dir)

# Where a finished package is saved when it is not (only) published to S3:
# relative paths resolve against the current directory.
DEFAULT_LOCAL_OUTPUT_DIR = "hls_output"

_MAX_OUTPUT_NAME = 1024
_MAX_OUTPUT_SEGMENT = 255


def validate_output_dir_name(name: Any) -> str:
    """Return `name` if it is a safe output folder name, else raise ValueError.

    The name becomes a folder inside the job's scratch directory and the S3
    folder under ``s3.key_prefix``. An absolute path or a ``..`` segment would
    point the staging folder somewhere else on the server, and that folder is
    uploaded in full and then deleted — so both are refused outright rather
    than cleaned up. Nested names (``shows/AETN_S10_E03``) are allowed.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("The output folder name is empty.")
    text = name.strip()
    if len(text) > _MAX_OUTPUT_NAME:
        raise ValueError(f"The output folder name is longer than {_MAX_OUTPUT_NAME} "
                         f"characters.")
    if text.startswith(("/", "~")) or "\\" in text or re.match(r"^[A-Za-z]:", text):
        raise ValueError(
            f"The output folder must be a plain name such as 'AETN_S10_E03', not a "
            f"path: {name!r}. It names the folder in S3 (under s3.key_prefix); use "
            f"--local-output-dir to choose where a local copy is saved.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ValueError(f"The output folder name contains control characters: {name!r}")
    for segment in text.split("/"):
        if segment in ("", ".", ".."):
            raise ValueError(
                f"The output folder name may not contain empty, '.' or '..' parts: "
                f"{name!r}")
        if len(segment) > _MAX_OUTPUT_SEGMENT:
            raise ValueError(f"Each part of the output folder name must be at most "
                             f"{_MAX_OUTPUT_SEGMENT} characters: {name!r}")
    return text


def _ensure_within(path: Path, root: Path, what: str) -> None:
    """Refuse to work on `path` unless it really lies inside `root`."""
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        raise TranscodeError(
            f"Refusing to use {what} {path}: it is outside the job's scratch "
            f"directory {root}.", stage="VALIDATION") from None


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
        "local_output_dir": (overrides.get("local_output_dir")
                             or defaults.get("local_output_dir")
                             or DEFAULT_LOCAL_OUTPUT_DIR),
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
    defaults = config.get("defaults", {})
    configure_watchdog(defaults.get("ffmpeg_stall_timeout_seconds"),
                       defaults.get("ffmpeg_max_seconds"),
                       defaults.get("ffprobe_timeout_seconds"))
    input_uri = settings["input_video"]
    if not input_uri:
        raise TranscodeError("No input video was supplied (defaults.input_video "
                             "is empty and no override was given).", stage="VALIDATION")
    try:
        settings["output_dir_name"] = validate_output_dir_name(settings["output_dir_name"])
    except ValueError as e:
        raise TranscodeError(str(e), stage="VALIDATION") from e

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
    # Keep a local copy when nothing goes to S3, or when --keep-local asks for
    # one. Either way the package must leave the scratch directory before that
    # is removed, or it is lost.
    keep_local_copy = (not settings["upload"]) or (not settings["delete_local_output"])

    try:
        _ensure_within(output_dir, work_dir, "output folder")
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

        if keep_local_copy:
            saved = _save_package(output_dir, settings, ctx)
            if not settings["upload"]:
                ctx.output_prefix = str(saved)
                ctx.metadata["playback_url"] = str(saved / "channel.m3u8")
        ctx.mark_completed()
    except JobCancelled:
        ctx.mark_cancelled()
    except TranscodeError as e:
        ctx.stage = e.stage or ctx.stage
        message = f"[{e.stage}] {e}"
        # A failed upload leaves a finished package behind: keep it so the
        # upload can be retried without transcoding again.
        if e.stage == "UPLOADING" and output_dir.is_dir() and any(output_dir.iterdir()):
            try:
                saved = _save_package(output_dir, settings, ctx)
                message += (f"\nThe finished package was saved to {saved}. Retry the "
                            f"upload with: python app.py --upload-only "
                            f"--s3-upload-source-dir {saved} "
                            f"--output {settings['output_dir_name']}")
            except TranscodeError as save_error:
                message += f"\nThe package could not be saved locally either: {save_error}"
        ctx.mark_failed(message)
    except Exception as e:
        ctx.logger.error("Unhandled error in transcode job\n" + traceback.format_exc())
        ctx.mark_failed(f"{type(e).__name__}: {e}")
    finally:
        _remove_work_dir(work_dir, keep=settings["debug"], ctx=ctx)
        bind_context(None)

    return ctx.snapshot()


def _save_package(output_dir: Path, settings: Dict[str, Any], ctx: JobContext) -> Path:
    """Move the staged package out of scratch into the local output folder.

    The destination is ``<local_output_dir>/<output folder>``; if that already
    exists the package goes to ``<output folder>_2`` and so on, so an earlier
    package is never overwritten or merged into. Raises TranscodeError when the
    package cannot be saved — a job must not report success for output that
    did not survive.
    """
    root = Path(os.path.abspath(os.path.expanduser(str(settings["local_output_dir"]))))
    name = Path(settings["output_dir_name"])
    try:
        destination = claim_unique_dir(root / name.parent, name.name)
        for item in sorted(Path(output_dir).iterdir()):
            shutil.move(str(item), str(destination / item.name))
    except OSError as e:
        raise TranscodeError(f"Could not save the package to {root}: {e}",
                             stage="SAVING_OUTPUT") from e
    if destination.name != name.name:
        ctx.logger.info(f"'{root / name}' already exists, so this package was saved "
                        f"to '{destination}' instead.")
    ctx.logger.info(f"Package saved locally at {destination}")
    ctx.metadata["local_output"] = str(destination)
    return destination


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
