import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Any

from hls_toolkit.aws_handler import (run_aws_cli, get_aws_account_id,
                                     manage_mediapackage_vod_asset)

logger = logging.getLogger(__name__)


def handle_s3_upload_only(args, s3_bucket_name, s3_key_prefix, default_output_dir):
    """Handles the --upload-only functionality, uploading a local directory to S3.

    Args:
        args: The parsed command-line arguments.
        s3_bucket_name: The name of the S3 bucket to upload to.
        s3_key_prefix: The S3 key prefix for organization.
        default_output_dir: The default output directory from config.

    Returns:
        0 on success, 1 on failure.
    """
    if not args.s3_upload_source_dir:
        logger.error("--s3-upload-source-dir is required when --upload-only is used.")
        return 1
    if not s3_bucket_name:
        logger.error("S3 bucket_name not specified in config. Cannot perform S3 upload.")
        return 1
    if not Path(args.s3_upload_source_dir).is_dir():
        logger.error(f"S3 upload source directory '{args.s3_upload_source_dir}' does not "
                     "exist or is not a directory.")
        return 1

    source_dir_to_upload = os.path.abspath(args.s3_upload_source_dir)
    destination_folder_name = (args.output if args.output != default_output_dir
                               else os.path.basename(source_dir_to_upload))
    s3_destination_key = (f"{s3_key_prefix}/{destination_folder_name}" if s3_key_prefix
                          else destination_folder_name)
    s3_destination_path = f"s3://{s3_bucket_name}/{s3_destination_key}"
    logger.info(f"Uploading local directory '{source_dir_to_upload}' to S3 "
                f"'{s3_destination_path}'...")
    try:
        run_aws_cli(['aws', 's3', 'rm', '--recursive', s3_destination_path],
                    check=True, log_output=True)
        s3_cp_cmd = ["aws", "s3", "cp", "--recursive"]
        if hasattr(args, "debug") and args.debug:
            s3_cp_cmd.append("--debug")
        s3_cp_cmd.extend([source_dir_to_upload, s3_destination_path])
        run_aws_cli(s3_cp_cmd, check=True, log_output=True)
        logger.info(f"Successfully uploaded '{source_dir_to_upload}' to "
                    f"'{s3_destination_path}'.")
    except Exception as e:
        logger.error(f"Failed to upload to S3: {e}", exc_info=True)
        return 1

    return 0


def handle_mediapackage_import_only(args: Any,
                                    s3_bucket_name: str,
                                    s3_key_prefix: str,
                                    default_output_dir: str,
                                    default_region: str,
                                    default_mp_packaging_group_id: str,
                                    default_mp_vod_role_name: str,
                                    default_mp_vod_role_arn: str,
                                    default_package_type: str,
                                    mediapackage_config: Dict[str, Any]) -> Dict[str, Any]:
    """Handles the --import-only functionality, importing an HLS manifest to MediaPackage.

    Args:
        args: The parsed command-line arguments.
        s3_bucket_name: The name of the S3 bucket where the manifest resides.
        s3_key_prefix: The S3 key prefix for organization.
        default_output_dir: The default output directory from config.
        default_region: Default AWS region.
        default_mp_packaging_group_id: Default MediaPackage Packaging Group ID.
        default_mp_vod_role_name: Default MediaPackage VOD IAM role name.
        default_mp_vod_role_arn: Default MediaPackage VOD IAM role ARN.
        default_package_type: Default MediaPackage package type.
        mediapackage_config: MediaPackage specific configuration from config.

    Returns:
        A dictionary with the results of the MediaPackage operation, including playback_urls.
    """
    if not args.s3_import_folder_name:
        logger.error("--s3-import-folder-name is required when --import-only is used.")
        return {"error": "s3_import_folder_name_required"}
    if not s3_bucket_name:
        logger.error("S3 bucket_name not specified in config. Cannot perform "
                     "MediaPackage import.")
        return {"error": "s3_bucket_name_not_configured"}

    s3_prefix_part = f"{s3_key_prefix}/" if s3_key_prefix else ""
    s3_source_key_path = f"{s3_prefix_part}{args.s3_import_folder_name}/channel.m3u8"
    s3_source_arn = f"arn:aws:s3:::{s3_bucket_name}/{s3_source_key_path}"
    mediapackage_asset_id = (
        args.output if args.output != default_output_dir
        else f'{args.s3_import_folder_name}_{datetime.now().strftime("%Y%m%d%H%M%S")}')

    aws_account_id = get_aws_account_id(args.region, args.debug_aws)
    if aws_account_id is None:
        logger.error("Could not retrieve AWS account ID. Cannot perform MediaPackage import.")
        return {"error": "aws_account_id_missing"}

    if default_mp_vod_role_arn:
        vod_role_arn = default_mp_vod_role_arn
    else:
        vod_role_arn = (f'arn:aws:iam:::{aws_account_id}:role/'
                        f'{getattr(args, "vod_role_name", default_mp_vod_role_name)}')

    logger.info(f"Importing S3 HLS manifest '{s3_source_arn}' to MediaPackage "
                f"(Asset ID: {mediapackage_asset_id})...")
    mediapackage_results = manage_mediapackage_vod_asset(
        region=getattr(args, "region", default_region),
        packaging_group_id=getattr(args, "packaging_group_id", default_mp_packaging_group_id),
        vod_role_arn=vod_role_arn,
        s3_source_arn=s3_source_arn,
        package_type=getattr(args, "package_type", default_package_type),
        asset_id=mediapackage_asset_id,
        debug=getattr(args, "debug_aws", False))

    if mediapackage_results and "error" in mediapackage_results:
        logger.error(f'MediaPackage integration failed: '
                     f'{mediapackage_results.get("error")} - '
                     f'{mediapackage_results.get("details")}')
        return mediapackage_results

    logger.info("MediaPackage integration completed successfully.")
    if mediapackage_results and mediapackage_results.get("playback_urls"):
        logger.info("Detected MediaPackage Playback URLs:")
        for url in mediapackage_results["playback_urls"]:
            logger.info(f"- {url}")
    else:
        logger.info("No specific MediaPackage Playback URLs detected in the output.")

    return mediapackage_results
