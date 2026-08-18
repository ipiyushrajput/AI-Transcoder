import argparse
import json
import logging
import os
import sys
import shlex
import math
import signal
from pathlib import Path
from typing import Optional, List, Dict, Any

from hls_toolkit.aws_handler import get_aws_account_id
from hls_toolkit.logging_utils import setup_logging
from hls_toolkit.aws_operations import (handle_s3_upload_only,
                                        handle_mediapackage_import_only)
from hls_toolkit.hls_generator import generate_hls_workflow
from hls_toolkit.ffmpeg_wrapper import get_active_processes

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


def _resolve_templated_vod_role_arn(vod_role_arn: Optional[str],
                                    aws_account_id: Optional[str]) -> Optional[str]:
    """Resolves templated values in vod_role_arn like {{RoleArn}}."""
    if vod_role_arn and "{{RoleArn}}" in vod_role_arn:
        if aws_account_id:
            return vod_role_arn.replace("{{RoleArn}}", aws_account_id)
        logging.warning("Cannot resolve {{RoleArn}} in vod_role_arn: "
                        "AWS account ID not available.")
        return None
    return vod_role_arn


def _calculate_default_workers(video_templates_for_run: List[Dict[str, Any]]) -> int:
    """
    Calculates a sensible default for transcode_workers based on the number of
    CPU cores and the demands of the selected video template.

    Args:
        video_templates_for_run: The list of video resolution templates being used.

    Returns:
        The recommended number of parallel workers.
    """
    try:
        total_cores = os.cpu_count()
        if not total_cores:
            return 4
        if not video_templates_for_run:
            return total_cores
        num_resolutions = len(video_templates_for_run)
        max_threads_per_res = 0
        for t in video_templates_for_run:
            threads = t.get("threads", 0)
            if threads > max_threads_per_res:
                max_threads_per_res = threads

        if max_threads_per_res == 0:
            max_threads_per_res = 1
        estimated_cores_per_process = num_resolutions * (max_threads_per_res / 2)
        if estimated_cores_per_process <= 0:
            estimated_cores_per_process = total_cores
        recommended_workers = math.floor(total_cores / estimated_cores_per_process)
        return max(1, recommended_workers)
    except Exception:
        return 4


def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("--config", default="config.json", help="Path to JSON config.")
    config_args, remaining_argv = config_parser.parse_known_args()

    config = {}
    if os.path.exists(config_args.config):
        try:
            with open(config_args.config, "r") as f:
                config = json.load(f)
        except json.JSONDecodeError as e:
            print(f"Failed to parse config '{config_args.config}': {e}", file=sys.stderr)
            return 1
    elif config_args.config != "config.json":
        print(f"Config '{config_args.config}' not found. Using defaults.", file=sys.stderr)

    video_templates = config.get("video_templates", {})
    defaults = config.get("defaults", {})
    paths = config.get("paths", {})
    s3_config = config.get("s3", {})
    esam_config = config.get("Esam", {})
    mediapackage_config = defaults.get("mediapackage", {})

    default_input_video = defaults.get(
        "input_video", "samsung_test_assets/JAN_CarlsCarWash2_Th_en.mp4")
    default_output_dir = defaults.get("output_dir", "hls_output_264_wz")
    default_subtitle_file = defaults.get("subtitle_file", None)
    default_subtitle_language = defaults.get("subtitle_language", "en")
    default_mp_packaging_group_id = mediapackage_config.get("packaging_group_id",
                                                            "vod_samsung_test")
    default_mp_vod_role_name = mediapackage_config.get("vod_role_name", "MediaPackage_VOD_Role")
    default_mp_vod_role_arn_from_config = mediapackage_config.get("vod_role_arn", None)
    default_region = mediapackage_config.get("region", "us-east-1")
    default_package_type = mediapackage_config.get("package_type", "CMAF")
    default_mp_vod_role_arn = default_mp_vod_role_arn_from_config

    parser = argparse.ArgumentParser(description="Generate HLS VOD from a video.")
    parser.add_argument("-v", "--version", action="version",
                        version=f"Version: {GIT_VERSION}")
    parser.add_argument("--config", default=config_args.config,
                        help="Path to the JSON configuration file.")
    parser.add_argument("--input", default=default_input_video,
                        help="Path to the input video file.")
    parser.add_argument(
        "--output", default=default_output_dir,
        help="Output directory for HLS files (for main workflow). When using --upload-only, "
             "this specifies the S3 destination folder name (defaults to source directory "
             "base name). When using --import-only, this specifies the MediaPackage asset ID "
             "(defaults to S3 folder name + timestamp).")
    parser.add_argument("--subtitle", default=default_subtitle_file,
                        help="Path to the subtitle (VTT) file. If not provided, subtitles "
                             "will be omitted.")
    parser.add_argument("--sub-lang", default=default_subtitle_language,
                        help="Language of the subtitle track (e.g., 'en', 'es').")
    parser.add_argument("--upload", action="store_true",
                        help="Upload the generated HLS files to S3 after processing.")
    if "upload" in defaults:
        parser.set_defaults(upload=defaults["upload"])

    esam_group = parser.add_mutually_exclusive_group(required=False)
    default_esam_enabled = defaults.get("esam", False)
    esam_group.add_argument("--esam", action="store_true", dest="esam",
                            help="Enable ESAM marker injection (overrides config).")
    esam_group.add_argument("--no-esam", action="store_false", dest="esam",
                            help="Disable ESAM marker injection (overrides config).")
    parser.set_defaults(esam=default_esam_enabled)

    audio_norm_group = parser.add_mutually_exclusive_group(required=False)
    default_audio_norm_enabled = defaults.get("audio_norm", False)
    audio_norm_group.add_argument("--audio-norm", "--audio_norm", action="store_true",
                                  dest="audio_norm",
                                  help="Enable audio normalization using loudnorm "
                                       "(overrides config).")
    audio_norm_group.add_argument("--no-audio-norm", action="store_false", dest="audio_norm",
                                  help="Disable audio normalization (overrides config).")
    parser.set_defaults(audio_norm=default_audio_norm_enabled)

    thumbnail_group = parser.add_mutually_exclusive_group(required=False)
    default_thumbnails_enabled = config.get("thumbnail_generation", {}).get("enabled", False)
    thumbnail_group.add_argument("--generate-thumbnails", action="store_true",
                                 dest="thumbnails_enabled",
                                 help="Enable thumbnail generation (overrides config).")
    thumbnail_group.add_argument("--no-generate-thumbnails", action="store_false",
                                 dest="thumbnails_enabled",
                                 help="Disable thumbnail generation (overrides config).")
    parser.set_defaults(thumbnails_enabled=default_thumbnails_enabled)

    available_templates = list(video_templates.keys())
    template_help_text = (
        f'Name of the video template to use from config.json. '
        f'Available: {", ".join(available_templates)}'
        if available_templates
        else "Name of the video template to use from config.json.")
    default_template = defaults.get("template", "h264_standard")
    parser.add_argument("--template", default=default_template, help=template_help_text)
    parser.add_argument("--resolution", type=str, default=None,
                        help="Override resolutions from config, e.g., 1080p,720p.")
    parser.add_argument("--packaging-group-id", default=default_mp_packaging_group_id,
                        help="The ID for the MediaPackage VOD Packaging Group.")
    parser.add_argument("--vod-role-name", default=default_mp_vod_role_name,
                        help="The IAM Role name for MediaPackage VOD to access S3. Will be "
                             "ignored if --vod-role-arn is provided.")
    parser.add_argument("--vod-role-arn", default=default_mp_vod_role_arn,
                        help="The full IAM Role ARN for MediaPackage VOD to access S3. If "
                             "provided, overrides --vod-role-name. Supports {{RoleArn}} "
                             "templating.")
    parser.add_argument("--region", default=default_region,
                        help="The AWS region for MediaPackage.")
    parser.add_argument("--package-type", default=default_package_type,
                        choices=["HLS", "CMAF", "ALL"],
                        help="Specify the package type (HLS, CMAF, or ALL) for the VOD Asset.")
    parser.add_argument("--import", dest="import_to_mediapackage", action="store_true",
                        help="Import the generated HLS files from S3 to MediaPackage after "
                             "processing and uploading.")
    if "import" in defaults:
        parser.set_defaults(import_to_mediapackage=defaults["import"])
    parser.add_argument("--debug-aws", action="store_true",
                        help="Enable AWS CLI debug logging for MediaPackage operations.")
    if "mediapackage" in defaults and "debug_aws" in defaults["mediapackage"]:
        parser.set_defaults(debug_aws=defaults["mediapackage"]["debug_aws"])
    parser.add_argument("--duration", type=int, help="Process only first N seconds.")
    parser.add_argument("--transcode-workers", type=int, default=None,
                        help="Number of parallel workers for transcoding video clips. "
                             "(Default: dynamically calculated)")
    parser.add_argument("--temp-dir", default=str(Path.cwd() / ".wz_temp"),
                        help="Directory to store temporary files (e.g., clipped segments).")
    parser.add_argument("--debug", action="store_true",
                        help="Enables verbose debug logging for the script and prevents "
                             "cleanup of temporary files.")
    parser.add_argument("--log-file",
                        help="Path to the log file. If not specified, logs are only output "
                             "to the console.")

    exclusive_actions = parser.add_mutually_exclusive_group()
    exclusive_actions.add_argument("--upload-only", action="store_true",
                                   help="Only upload a local directory to S3. Requires "
                                        "--s3-upload-source-dir and --output (used as S3 "
                                        "destination folder name).")
    exclusive_actions.add_argument("--import-only", action="store_true",
                                   help="Only import an HLS master manifest from S3 into "
                                        "MediaPackage. Requires --s3-import-folder-name and "
                                        "--output (used as MediaPackage asset ID).")
    parser.add_argument("--s3-upload-source-dir", type=str,
                        help="Local directory to upload to S3 when --upload-only is used.")
    parser.add_argument("--s3-import-folder-name", type=str,
                        help="Folder name in S3 (e.g., 'my_hls_content') containing "
                             "'channel.m3u8' when --import-only is used. Assumes bucket and "
                             "key_prefix from config.json.")

    args = parser.parse_args(remaining_argv)
    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(1)

    if args.vod_role_arn and "{{RoleArn}}" in args.vod_role_arn:
        aws_account_id = get_aws_account_id(
            args.region, mediapackage_config.get("debug_aws", False) or args.debug_aws)
        args.vod_role_arn = _resolve_templated_vod_role_arn(args.vod_role_arn, aws_account_id)

    if args.transcode_workers is None:
        selected_template_name = args.template
        templates_for_run = video_templates.get(selected_template_name, [])
        args.transcode_workers = _calculate_default_workers(templates_for_run)

    s3_bucket_name = s3_config.get("bucket_name")
    s3_key_prefix = s3_config.get("key_prefix", "").strip("/")

    try:
        os.makedirs(args.temp_dir, exist_ok=True)
    except Exception as e:
        print(f"Failed to create temp dir '{args.temp_dir}': {e}", file=sys.stderr)
        return 1

    setup_logging(args, config)
    logging.info(f'Command: {" ".join(shlex.quote(arg) for arg in sys.argv)}')

    input_video = args.input
    output_dir = os.path.abspath(args.output)
    output_dir_name = os.path.basename(os.path.normpath(output_dir))
    subtitle_file = args.subtitle

    ffmpeg_executable = os.path.abspath(paths.get("ffmpeg_executable", "./bin/ffmpeg"))
    ffprobe_executable = os.path.abspath(
        paths.get("ffprobe_executable", str(Path(ffmpeg_executable).with_name("ffprobe"))))

    if not os.path.exists(ffmpeg_executable):
        logging.critical(f"FFmpeg executable not found at: {ffmpeg_executable}")
        return 1
    if not os.path.exists(ffprobe_executable):
        logging.critical(f"FFprobe executable not found at: {ffprobe_executable}")
        return 1

    if args.upload_only:
        return handle_s3_upload_only(args, s3_bucket_name, s3_key_prefix, default_output_dir)

    if args.import_only:
        result = handle_mediapackage_import_only(
            args, s3_bucket_name, s3_key_prefix, default_output_dir, default_region,
            default_mp_packaging_group_id, default_mp_vod_role_name, args.vod_role_arn,
            default_package_type, mediapackage_config)
        if result and result.get("playback_urls"):
            return 0
        return 1

    if not os.path.exists(input_video):
        logging.critical(f"Input video file not found: {input_video}")
        return 1
    if subtitle_file and not os.path.exists(subtitle_file):
        logging.critical(f"Subtitle file not found: {subtitle_file}")
        return 1

    return generate_hls_workflow(
        config=config,
        defaults=defaults,
        paths=paths,
        s3_config=s3_config,
        esam_config=esam_config,
        mediapackage_config=mediapackage_config,
        default_input_video=default_input_video,
        default_output_dir=default_output_dir,
        default_subtitle_file=default_subtitle_file,
        default_subtitle_language=default_subtitle_language,
        default_mp_packaging_group_id=default_mp_packaging_group_id,
        default_mp_vod_role_name=default_mp_vod_role_name,
        default_region=default_region,
        default_package_type=default_package_type,
        input_video=input_video,
        output_dir=Path(output_dir),
        output_dir_name=output_dir_name,
        subtitle_file=subtitle_file,
        ffmpeg_executable=ffmpeg_executable,
        ffprobe_executable=ffprobe_executable,
        video_templates=video_templates,
        template_name=args.template,
        resolution=args.resolution,
        thumbnails_enabled=args.thumbnails_enabled,
        duration=args.duration,
        transcode_workers=args.transcode_workers,
        temp_dir=args.temp_dir,
        debug=args.debug,
        audio_norm=args.audio_norm,
        esam=args.esam,
        upload=args.upload,
        import_to_mediapackage=args.import_to_mediapackage,
        debug_aws=args.debug_aws)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        if logging.getLogger().hasHandlers():
            logging.error(f"An unhandled error occurred: {e}")
        else:
            print(f"An unhandled error occurred: {e}", file=sys.stderr)
        sys.exit(1)
