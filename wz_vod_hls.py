"""Command-line front end for the AI-Transcoder HLS VOD pipeline.

Inputs may be local paths or ``s3://bucket/key`` URIs. Output is packaged into a
scratch directory, published to S3, and the local copy removed once every object
is verified. All logging for a run lands under ``logs/<channel>/`` — or
``logs/<channel>_2/`` and so on when that folder is already taken.

Several runs can go at once; they share the server's cores through one CPU
budget (see :mod:`hls_toolkit.cpu_budget`).
"""
import argparse
import json
import logging
import os
import shlex
import signal
import sys
from pathlib import Path
from typing import Any, Dict

from hls_toolkit.aws_operations import handle_s3_upload_only
from hls_toolkit.ffmpeg_wrapper import get_active_processes
from hls_toolkit.job_context import TranscodeError
from hls_toolkit.logging_utils import setup_logging
from hls_toolkit.runner import run_transcode_job
from hls_toolkit import preflight
from hls_toolkit.cpu_budget import get_shared_budget

try:
    from hls_toolkit.version import GIT_VERSION
except ImportError:
    GIT_VERSION = "dev-unknown"


def signal_handler(sig, frame):
    """Gracefully terminate all active subprocesses upon receiving a signal."""
    logging.warning(f"Signal {sig} received. Terminating all active FFmpeg processes...")
    active_processes = get_active_processes()
    if not active_processes:
        logging.info("No active processes to terminate.")
    for process in active_processes:
        try:
            pgid = os.getpgid(process.pid)
            logging.info(f"Terminating process group with PGID: {pgid}")
            os.killpg(pgid, signal.SIGTERM)
            process.wait(timeout=5)
        except ProcessLookupError:
            logging.info(f"Process with PID {process.pid} already terminated.")
        except OSError as e:
            logging.error(f"Error terminating process group: {e}")
            logging.info(f"Attempting to kill process PID {process.pid} directly.")
            try:
                process.kill()
            except Exception as kill_e:
                logging.error(f"Failed to kill process PID {process.pid}: {kill_e}")
        except Exception as e:
            logging.error(f"An unexpected error occurred during process termination: {e}")

    sys.exit(1)


def load_config(path: str) -> Dict[str, Any]:
    """Read and validate the JSON config. Raises TranscodeError on a bad file."""
    if not os.path.exists(path):
        if path != "config.json":
            raise TranscodeError(f"Config file '{path}' not found.", stage="VALIDATION")
        logging.warning(f"Config '{path}' not found. Using built-in defaults.")
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        raise TranscodeError(f"Failed to parse config '{path}': {e}",
                             stage="VALIDATION") from e
    except OSError as e:
        raise TranscodeError(f"Could not read config '{path}': {e}",
                             stage="VALIDATION") from e
    if not isinstance(config, dict):
        raise TranscodeError(f"Config '{path}' must contain a JSON object.",
                             stage="VALIDATION")
    return config


def build_parser(config: Dict[str, Any], config_path: str) -> argparse.ArgumentParser:
    video_templates = config.get("video_templates", {})
    defaults = config.get("defaults", {})
    thumbnails = config.get("thumbnail_generation", {})

    parser = argparse.ArgumentParser(
        description="Generate an HLS VOD package from a local or S3 source.")
    parser.add_argument("-v", "--version", action="version",
                        version=f"Version: {GIT_VERSION}")
    parser.add_argument("--config", default=config_path,
                        help="Path to the JSON configuration file.")
    parser.add_argument("--input", default=defaults.get("input_video"),
                        help="Input video: a local path or an s3://bucket/key URI.")
    parser.add_argument("--output", default=defaults.get("output_dir", "output"),
                        help="Output folder name, e.g. AETN_S10_E03 (not a path). "
                             "It is the S3 folder under s3.key_prefix, and the "
                             "folder name for a local copy. Also the S3 "
                             "destination folder when --upload-only is used.")
    parser.add_argument("--local-output-dir",
                        default=defaults.get("local_output_dir", "hls_output"),
                        help="Where a local copy of the package is saved — with "
                             "--no-upload, with --keep-local, or when an upload "
                             "fails (default: ./hls_output). An existing folder is "
                             "never overwritten: the copy goes to <name>_2 instead.")
    parser.add_argument("--subtitle", default=defaults.get("subtitle_file"),
                        help="Subtitle (VTT) file: a local path or an s3:// URI. "
                             "Omit for no subtitles.")
    parser.add_argument("--sub-lang", default=defaults.get("subtitle_language", "en"),
                        help="Language of the subtitle track (e.g. 'en', 'es').")

    upload_group = parser.add_mutually_exclusive_group()
    upload_group.add_argument("--upload", action="store_true", dest="upload",
                              help="Publish the package to S3 (default).")
    upload_group.add_argument("--no-upload", action="store_false", dest="upload",
                              help="Do not upload; save the package under "
                                   "--local-output-dir instead.")
    parser.set_defaults(upload=defaults.get("upload", True))

    parser.add_argument("--keep-local", action="store_true",
                        help="Also save a local copy under --local-output-dir after "
                             "a successful S3 upload. By default the local package is "
                             "deleted once every object is verified in S3.")

    esam_group = parser.add_mutually_exclusive_group()
    esam_group.add_argument("--esam", action="store_true", dest="esam",
                            help="Enable ESAM marker injection (overrides config).")
    esam_group.add_argument("--no-esam", action="store_false", dest="esam",
                            help="Disable ESAM marker injection (overrides config).")
    parser.set_defaults(esam=defaults.get("esam", False))

    audio_norm_group = parser.add_mutually_exclusive_group()
    audio_norm_group.add_argument("--audio-norm", "--audio_norm", action="store_true",
                                  dest="audio_norm",
                                  help="Enable loudnorm audio normalization.")
    audio_norm_group.add_argument("--no-audio-norm", action="store_false",
                                  dest="audio_norm",
                                  help="Disable audio normalization.")
    parser.set_defaults(audio_norm=defaults.get("audio_norm", False))

    thumbnail_group = parser.add_mutually_exclusive_group()
    thumbnail_group.add_argument("--generate-thumbnails", action="store_true",
                                 dest="thumbnails_enabled",
                                 help="Enable thumbnail generation.")
    thumbnail_group.add_argument("--no-generate-thumbnails", action="store_false",
                                 dest="thumbnails_enabled",
                                 help="Disable thumbnail generation.")
    parser.set_defaults(thumbnails_enabled=thumbnails.get(
        "enabled", defaults.get("generate-thumbnails", False)))

    available_templates = list(video_templates.keys())
    template_help = (f'Video template from config.json. '
                     f'Available: {", ".join(available_templates)}'
                     if available_templates
                     else "Video template from config.json.")
    parser.add_argument("--template", default=defaults.get("template", "h264_standard"),
                        help=template_help)
    parser.add_argument("--resolution", type=str, default=None,
                        help="Override the resolutions from config, e.g. 1080p,720p.")
    parser.add_argument("--duration", type=int,
                        help="Process only the first N seconds.")
    parser.add_argument("--transcode-workers", type=int, default=None,
                        help="Cap on how many clips this job encodes at once. By "
                             "default there is no per-job cap: clips start as soon "
                             "as the machine-wide CPU budget has room.")
    parser.add_argument("--cpu-budget", type=int, default=None,
                        help="Cores shared by every transcode on this server "
                             "(default: $WZ_CPU_BUDGET, then parallelism.cpu_budget "
                             "in the config, then the CPU count). Use the same "
                             "value for every job on the machine.")
    parser.add_argument("--work-dir", default=None,
                        help="Parent directory for the scratch tree "
                             "(default: the system temp directory).")
    parser.add_argument("--log-dir", default="logs",
                        help="Root of the per-channel log tree (default: ./logs).")
    parser.add_argument("--job-id", default=None,
                        help="Job identifier. When set, logs go to "
                             "logs/<channel>/<job-id>/.")
    parser.add_argument("--debug", action="store_true",
                        help="Verbose logging; keeps the scratch directory for inspection.")
    parser.add_argument("--log-file",
                        help="Additional log file for the console-level stream.")

    parser.add_argument("--check", action="store_true",
                        help="Validate the environment (packages, FFmpeg, AWS "
                             "credentials, inputs, bucket write access, database) "
                             "and exit without transcoding.")
    parser.add_argument("--upload-only", action="store_true",
                        help="Skip transcoding and only upload a local directory to S3. "
                             "Requires --s3-upload-source-dir.")
    parser.add_argument("--s3-upload-source-dir", type=str,
                        help="Local directory to upload when --upload-only is used.")
    return parser


def main() -> int:
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("--config", default="config.json")
    config_args, remaining_argv = config_parser.parse_known_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(levelname)s - %(message)s")
    try:
        config = load_config(config_args.config)
    except TranscodeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    parser = build_parser(config, config_args.config)
    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        return 1
    args = parser.parse_args(remaining_argv)

    setup_logging(args, config)
    logging.info(f'Command: {" ".join(shlex.quote(arg) for arg in sys.argv)}')

    if args.check:
        results = preflight.run_all(config, log_root=args.log_dir,
                                    work_root=args.work_dir)
        print(preflight.format_report(results))
        return 1 if any(r.failed for r in results) else 0

    s3_config = config.get("s3", {})
    if args.upload_only:
        return handle_s3_upload_only(args, s3_config.get("bucket_name"),
                                     s3_config.get("key_prefix", "").strip("/"),
                                     config.get("defaults", {}).get("output_dir", "output"))

    if not args.input:
        logging.critical("No input video given. Pass --input or set "
                         "defaults.input_video in the config.")
        return 1

    # One CPU budget for the whole server, shared with any other app.py run and
    # the API. Concurrency follows from it; see hls_toolkit/cpu_budget.py.
    get_shared_budget(args.cpu_budget,
                      config.get("parallelism", {}).get("cpu_budget"))

    overrides = {
        "input_video": args.input,
        "subtitle_file": args.subtitle,
        "subtitle_language": args.sub_lang,
        "output_dir": args.output,
        "template": args.template,
        "resolution": args.resolution,
        "esam": args.esam,
        "audio_norm": args.audio_norm,
        "thumbnails_enabled": args.thumbnails_enabled,
        "upload": args.upload,
        "delete_local_output": not args.keep_local,
        "local_output_dir": args.local_output_dir,
        "duration": args.duration,
        "transcode_workers": args.transcode_workers,
        "debug": args.debug,
    }

    try:
        result = run_transcode_job(config, overrides=overrides, job_id=args.job_id,
                                   log_root=args.log_dir, work_root=args.work_dir)
    except TranscodeError as e:
        logging.critical(f"[{e.stage}] {e}")
        return 1

    logging.info("-" * 70)
    logging.info(f"Status   : {result['status']}")
    logging.info(f"Channel  : {result['channel']}")
    logging.info(f"Elapsed  : {result['elapsed_seconds']}s")
    logging.info(f"Logs     : {result['log_dir']}")
    if result["status"] == "COMPLETED":
        logging.info(f"Output   : {result['output_prefix']} "
                     f"({result['uploaded_files']} object(s))")
        playback = result.get("metadata", {}).get("playback_url")
        if playback:
            logging.info(f"Playback : {playback}")
        return 0

    logging.error(f"Error    : {result['error_message']}")
    logging.error(f"Details  : see {result['log_dir']}/error.log")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        if logging.getLogger().hasHandlers():
            logging.error(f"An unhandled error occurred: {e}", exc_info=True)
        else:
            print(f"An unhandled error occurred: {e}", file=sys.stderr)
        sys.exit(1)
