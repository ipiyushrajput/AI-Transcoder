"""Standalone S3 operations exposed through the CLI's ``--upload-only`` flag.

The main workflow publishes its own output; this module covers the case where a
package already exists on disk and only needs pushing to S3.
"""
import logging
import os
from pathlib import Path
from typing import Any, Optional

from hls_toolkit import s3_io
from hls_toolkit.job_context import TranscodeError
from hls_toolkit.runner import validate_output_dir_name

logger = logging.getLogger(__name__)


def handle_s3_upload_only(args: Any,
                          s3_bucket_name: Optional[str],
                          s3_key_prefix: str,
                          default_output_dir: str) -> int:
    """Upload a local directory to S3 and report the result.

    Args:
        args: Parsed command-line arguments.
        s3_bucket_name: Destination bucket, from ``s3.bucket_name`` in the config.
        s3_key_prefix: Key prefix, from ``s3.key_prefix``.
        default_output_dir: The config default, used to tell whether ``--output``
            was given explicitly.

    Returns:
        0 on success, 1 on failure.
    """
    if not args.s3_upload_source_dir:
        logger.error("--s3-upload-source-dir is required when --upload-only is used.")
        return 1
    if not s3_bucket_name:
        logger.error("s3.bucket_name is not set in the config. Cannot perform S3 upload.")
        return 1

    source_dir = Path(os.path.abspath(args.s3_upload_source_dir))
    if not source_dir.is_dir():
        logger.error(f"S3 upload source directory '{source_dir}' does not exist "
                     f"or is not a directory.")
        return 1

    destination_folder_name = (args.output if args.output != default_output_dir
                               else source_dir.name)
    try:
        destination_folder_name = validate_output_dir_name(destination_folder_name)
    except ValueError as e:
        logger.error(f"Invalid --output: {e}")
        return 1
    prefix = s3_io.build_output_prefix(s3_key_prefix, destination_folder_name)
    logger.info(f"Uploading '{source_dir}' to s3://{s3_bucket_name}/{prefix} ...")

    try:
        result = s3_io.upload_directory(
            source_dir, s3_bucket_name, prefix,
            delete_local=bool(getattr(args, "delete_local", False)))
        s3_io.remove_stale_objects(s3_bucket_name, prefix, result["keys"])
    except TranscodeError as e:
        logger.error(f"Upload failed: {e}")
        return 1
    except Exception as e:
        logger.error(f"Upload failed: {e}", exc_info=True)
        return 1

    logger.info(f"Uploaded {result['uploaded']} object(s) to {result['prefix']}")
    return 0
