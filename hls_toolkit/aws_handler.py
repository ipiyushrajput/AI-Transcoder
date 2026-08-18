import json
import logging
import shlex
import subprocess
import time
from datetime import datetime
from typing import Optional


def run_aws_cli(cmd_list, check=True, capture_output=True, log_output=False,
                retries=3, delay=5):
    """
    Executes an AWS CLI command with optional retries.

    Args:
        cmd_list: The command to execute as a list of strings.
        check: If True, raise an exception on a non-zero exit code.
        capture_output: If True, capture stdout and stderr.
        log_output: If True, stream stdout/stderr to the logger.
        retries: The number of times to retry the command on failure.
        delay: The delay in seconds between retries.

    Returns:
        The captured stdout as a string if capture_output is True, otherwise an empty string.
    """
    last_exception = None
    for attempt in range(retries):
        try:
            use_pipe = capture_output or log_output
            stdout_setting = subprocess.PIPE if use_pipe else None
            stderr_setting = (subprocess.STDOUT if log_output
                              else subprocess.PIPE if use_pipe else None)
            process = subprocess.Popen(cmd_list,
                                       stdout=stdout_setting,
                                       stderr=stderr_setting,
                                       universal_newlines=True,
                                       encoding="utf-8",
                                       errors="ignore")
        except FileNotFoundError:
            raise RuntimeError("AWS CLI not found. Please install AWS CLI.")

        captured_stdout = []
        captured_stderr = []
        if log_output:
            is_s3_op = any(arg in ('s3', 's3api') for arg in cmd_list)
            for line in process.stdout:
                line_strip = line.strip()
                if line_strip:
                    if is_s3_op:
                        if "failed" in line_strip:
                            logging.error(f"AWS: {line_strip}")
                        elif line_strip.startswith(('upload:', 'download:', 'copy:', 'delete:')):
                            logging.debug(f"AWS: {line_strip}")
                        else:
                            logging.info(f"AWS: {line_strip}")
                    else:
                        logging.info(f"AWS: {line_strip}")
                    if capture_output:
                        captured_stdout.append(line)
            process.wait()
        elif capture_output:
            out, err = process.communicate()
            if out:
                captured_stdout.append(out)
            if err:
                captured_stderr.append(err)
        else:
            process.wait()

        result_returncode = process.returncode
        stdout_str = "".join(captured_stdout).strip()
        stderr_str = "".join(captured_stderr).strip()
        if result_returncode == 0:
            return stdout_str

        full_command_str = " ".join(shlex.quote(arg) for arg in cmd_list)
        error_content = stderr_str or stdout_str
        final_error_details = ("(See logs above for details)" if log_output
                               else error_content or "(No output captured)")
        error_message = (f"AWS CLI command failed (exit {result_returncode}) on attempt "
                         f"{attempt + 1}/{retries}: {full_command_str}\n"
                         f"Error output: {final_error_details}")
        last_exception = RuntimeError(error_message)
        logging.warning(error_message)
        if attempt < retries - 1:
            logging.info(f"Retrying in {delay} seconds...")
            time.sleep(delay)

    if check and last_exception:
        logging.debug(f'All retry attempts failed for command: {" ".join(cmd_list)}. '
                      'Raising exception for caller.')
        raise last_exception
    return ""


def get_aws_account_id(region, debug=False):
    aws_cmd_prefix = ["aws"]
    if debug:
        aws_cmd_prefix.append("--debug")
    try:
        account_id = subprocess.run(
            aws_cmd_prefix + ['sts', 'get-caller-identity', '--query', 'Account',
                              '--output', 'text', '--region', region],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True).stdout.strip()
        return account_id
    except subprocess.CalledProcessError as e:
        logging.error(f"Error retrieving AWS account ID: {e.stderr}")
        return None
    except FileNotFoundError:
        logging.error("AWS CLI not found. Please install and configure AWS CLI.")
        return None


def wait_until(fn, timeout=600, interval=5, description='resource'):
    start = time.time()
    while True:
        try:
            result = fn()
            if result:
                logging.debug(f"wait_until {description}: condition met (result: {result})")
                return True
            logging.debug(f"wait_until {description}: condition not met (result: {result})")
        except Exception as e:
            logging.debug(f"wait_until {description}: {e}")

        if time.time() - start > timeout:
            raise TimeoutError(f"Timeout waiting for {description}")
        time.sleep(interval)


def resource_exists(cmd_list):
    try:
        run_aws_cli(cmd_list)
        return True
    except RuntimeError as e:
        error_output = str(e).lower()
        if "notfoundexception" in error_output or (
                "describe-packaging-configuration" in " ".join(cmd_list).lower()
                and "unprocessableentityexception" in error_output):
            return False
        raise


def delete_resource(cmd_list, describe_cmd, description, polling_timeout_seconds=1800):
    try:
        run_aws_cli(cmd_list)
        wait_until(lambda: not resource_exists(describe_cmd),
                   timeout=polling_timeout_seconds,
                   interval=5,
                   description=description)
        logging.info(f"{description} deleted successfully.")
    except RuntimeError as e:
        logging.warning(f"Could not delete {description}: {str(e)}")
    except TimeoutError as e:
        logging.warning(str(e))


def manage_mediapackage_vod_asset(region,
                                  packaging_group_id,
                                  vod_role_arn,
                                  s3_source_arn,
                                  package_type='ALL',
                                  asset_id: Optional[str] = None,
                                  debug=False,
                                  polling_timeout_seconds=1800):
    logging.info(f"Managing MediaPackage VOD Asset in region: {region}")

    aws_cmd = ["aws"]
    if debug:
        aws_cmd.append("--debug")

    if asset_id is None:
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        asset_id_to_use = f"{packaging_group_id}_ASSET_{timestamp}"
    else:
        asset_id_to_use = asset_id

    resources = {
        'HLS': {
            'config_id': f"{packaging_group_id}_HLS",
            'package_json': json.dumps({
                'HlsManifests': [
                    {
                        'ManifestName': "index",
                        'AdMarkers': "PASSTHROUGH",
                        'StreamSelection': {
                            'MinVideoBitsPerSecond': 0,
                            'MaxVideoBitsPerSecond': 2147483647,
                            'StreamOrder': "ORIGINAL"}}],
                'SegmentDurationSeconds': 6,
                'UseAudioRenditionGroup': False}),
            'type_flag': "HLS"},
        'CMAF': {
            'config_id': f"{packaging_group_id}_CMAF",
            'package_json': json.dumps({
                'HlsManifests': [
                    {
                        'ManifestName': "index-cmaf",
                        'AdMarkers': "PASSTHROUGH",
                        'StreamSelection': {
                            'MinVideoBitsPerSecond': 0,
                            'MaxVideoBitsPerSecond': 2147483647,
                            'StreamOrder': "ORIGINAL"}}],
                'SegmentDurationSeconds': 6}),
            'type_flag': "CMAF"},
        'Asset': {
            'asset_id': asset_id_to_use,
            'source_arn': s3_source_arn,
            'role_arn': vod_role_arn}}

    try:
        run_aws_cli(aws_cmd + ["--version"])
    except RuntimeError as e:
        logging.error(str(e))
        return {'error': "aws_cli_not_found", 'details': str(e)}

    describe_pg = aws_cmd + ["mediapackage-vod", "describe-packaging-group",
                             "--id", packaging_group_id, "--region", region]
    try:
        if not resource_exists(describe_pg):
            logging.info(f"Creating Packaging Group {packaging_group_id}...")
            create_pg = aws_cmd + ["mediapackage-vod", "create-packaging-group",
                                   "--id", packaging_group_id, "--region", region]
            try:
                run_aws_cli(create_pg)
                logging.info(f"Packaging Group {packaging_group_id} created.")
            except RuntimeError as e:
                logging.error(f"Failed to create Packaging Group {packaging_group_id}: {e}")
                return {'error': "create_pg_failed", 'details': str(e)}
        else:
            logging.info(f"Packaging Group {packaging_group_id} exists.")
    except RuntimeError as e:
        logging.error(f"Error checking Packaging Group existence: {e}")
        return {'error': "aws_cli_error", 'details': str(e)}

    for key, cfg in resources.items():
        if key not in ('HLS', 'CMAF'):
            continue
        if package_type not in [key, "ALL"]:
            continue
        describe_cmd = aws_cmd + ["mediapackage-vod", "describe-packaging-configuration",
                                  "--id", cfg["config_id"], "--region", region]
        try:
            if not resource_exists(describe_cmd):
                logging.info(f'Creating {key} Packaging Configuration {cfg["config_id"]}...')
                create_cmd = aws_cmd + [
                    "mediapackage-vod", "create-packaging-configuration",
                    "--id", cfg["config_id"],
                    "--packaging-group-id", packaging_group_id,
                    f'--{cfg["type_flag"].lower()}-package', cfg["package_json"],
                    "--region", region]
                try:
                    run_aws_cli(create_cmd)
                    logging.info(f'{key} Packaging Configuration {cfg["config_id"]} created.')
                except RuntimeError as e:
                    logging.error(f'Failed to create {key} Packaging Configuration '
                                  f'{cfg["config_id"]}: {e}')
                    return {'error': f"{key.lower()}_config_create_failed", 'details': str(e)}
        except RuntimeError as e:
            logging.error(f"Error checking {key} Packaging Configuration existence: {e}")
            return {'error': "aws_cli_error", 'details': str(e)}

    describe_asset_cmd = aws_cmd + ["mediapackage-vod", "describe-asset",
                                    "--id", asset_id_to_use, "--region", region]

    asset_already_exists = False
    try:
        if resource_exists(describe_asset_cmd):
            logging.info(f"Asset {asset_id_to_use} already exists. Skipping creation and "
                         "proceeding to poll status.")
            asset_already_exists = True
    except RuntimeError as e:
        logging.error(f"Error checking Asset existence: {e}")
        return {'error': "aws_cli_error", 'details': str(e)}

    if not asset_already_exists:
        if s3_source_arn.startswith("arn:aws:s3:::"):
            try:
                path_part = s3_source_arn.replace("arn:aws:s3:::", "")
                if "/" in path_part:
                    bucket_name, key = path_part.split("/", 1)
                    logging.info("Verifying existence of S3 source object: "
                                 f"s3://{bucket_name}/{key}")
                    head_object_cmd = aws_cmd + ["s3api", "head-object",
                                                 "--bucket", bucket_name, "--key", key]

                    def s3_object_exists_check():
                        try:
                            run_aws_cli(head_object_cmd, check=True, capture_output=True,
                                        log_output=False, retries=0)
                            return True
                        except Exception:
                            return False

                    wait_until(s3_object_exists_check, timeout=60, interval=2,
                               description="S3 Source Object Availability")
            except TimeoutError:
                logging.warning(f"Timeout waiting for S3 object {s3_source_arn} to be visible. "
                                "Proceeding anyway, but create-asset may fail.")
            except Exception as e:
                logging.warning(f"Error while verifying S3 object existence: {e}. Proceeding...")

        logging.info(f"Creating Asset {asset_id_to_use} from S3 {s3_source_arn}...")
        create_asset_cmd = aws_cmd + [
            "mediapackage-vod", "create-asset",
            "--id", asset_id_to_use,
            "--packaging-group-id", packaging_group_id,
            "--source-arn", s3_source_arn,
            "--source-role-arn", vod_role_arn,
            "--region", region]
        try:
            run_aws_cli(create_asset_cmd, retries=1)
            logging.info(f"Asset {asset_id_to_use} created.")
        except RuntimeError as e:
            error_msg_lower = str(e).lower()
            if "unprocessableentityexception" in error_msg_lower \
                    and "already exists" in error_msg_lower:
                logging.warning(f"Asset {asset_id_to_use} already exists (creation attempted "
                                "due to race condition). Proceeding to poll status.")
                asset_already_exists = True
            else:
                logging.error(f"Failed to create Asset {asset_id_to_use}: {e}")
                return {'error': "create_asset_failed", 'details': str(e)}

    if asset_already_exists or (not asset_already_exists and "error" not in locals()):

        def asset_playable():
            try:
                desc = run_aws_cli(describe_asset_cmd + ["--output", "json"])
                asset_data = json.loads(desc)
                endpoints = asset_data.get("EgressEndpoints", [])
                if not endpoints:
                    return False

                statuses = []
                for ep in endpoints:
                    status = ep.get("Status")
                    statuses.append(status)
                    if status == "FAILED":
                        error_msg = ep.get("ErrorMessage", "Unknown error")
                        raise RuntimeError("Asset packaging failed for endpoint. "
                                           f"Status: {status}, Error: {error_msg}")

                return all(s == "PLAYABLE" for s in statuses)
            except RuntimeError:
                raise
            except Exception as e:
                logging.debug(f"Asset not yet playable or polling error: {e}")
                return False

        try:
            wait_until(asset_playable, timeout=polling_timeout_seconds, interval=15,
                       description="Asset PLAYABLE")
            logging.info("Asset is now PLAYABLE.")
        except TimeoutError as e:
            try:
                desc = run_aws_cli(describe_asset_cmd + ["--output", "json"])
                asset_data = json.loads(desc)
                endpoints = asset_data.get("EgressEndpoints", [])
                status_summary = ", ".join([
                    f'{ep.get("Status")} ({ep.get("ErrorMessage", "No error")})'
                    for ep in endpoints])
                logging.error(f"Timeout waiting for Asset {asset_id_to_use}. "
                              f"Final Statuses: {status_summary}")
            except Exception:
                logging.error(f"Timeout waiting for Asset {asset_id_to_use} to become "
                              "PLAYABLE. Could not fetch final status.")
            return {'error': "asset_not_playable", 'details': str(e)}
        except RuntimeError as e:
            logging.error(f"Asset {asset_id_to_use} failed to become PLAYABLE: {e}")
            return {'error': "asset_failed", 'details': str(e)}

        try:
            desc = run_aws_cli(describe_asset_cmd + ["--output", "json"])
            endpoints = json.loads(desc).get("EgressEndpoints", [])
            playback_urls = [ep.get("Url") for ep in endpoints]
            return {'playback_urls': playback_urls}
        except Exception as e:
            logging.error(f"Failed to retrieve playback URLs for Asset {asset_id_to_use}: {e}")
            return {'error': "get_playback_urls_failed", 'details': str(e)}
    else:
        return {'error': "unknown_asset_creation_issue"}


def upload_directory_to_s3(local_directory, bucket_name, s3_key_prefix, output_dir_name):
    """Uploads a local directory to an S3 bucket."""
    s3_destination_key = (f"{s3_key_prefix}/{output_dir_name}" if s3_key_prefix
                          else output_dir_name)
    s3_destination_path = f"s3://{bucket_name}/{s3_destination_key}"
    try:
        run_aws_cli(['aws', 's3', 'rm', '--recursive', s3_destination_path],
                    capture_output=True, check=True)
    except RuntimeError as e:
        logging.error(f"Failed to clear existing S3 directory '{s3_destination_path}': {e}. "
                      "Please check your AWS CLI configuration and S3 permissions for this "
                      "bucket/prefix. Exiting.")
        raise

    run_aws_cli(['aws', 's3', 'cp', '--recursive', local_directory, s3_destination_path],
                capture_output=False, check=True)


def import_to_mediapackage(region, packaging_group_id, vod_role_name, bucket_name,
                           key_prefix, output_dir_name, package_type, debug_aws):
    """Imports the HLS content into AWS MediaPackage VOD."""
    aws_account_id = get_aws_account_id(region, debug_aws)
    s3_source_arn = (f'arn:aws:s3:::{bucket_name}/{key_prefix}'
                     f'{"/" if key_prefix else ""}{output_dir_name}/channel.m3u8')
    mediapackage_results = manage_mediapackage_vod_asset(
        region=region,
        packaging_group_id=packaging_group_id,
        vod_role_arn=f"arn:aws:iam::{aws_account_id}:role/{vod_role_name}",
        s3_source_arn=s3_source_arn,
        package_type=package_type,
        debug=debug_aws)

    if mediapackage_results and "error" in mediapackage_results:
        logging.error(f'MediaPackage integration failed: '
                      f'{mediapackage_results.get("error")} - '
                      f'{mediapackage_results.get("details")}')
        return

    logging.info("MediaPackage integration completed successfully.")
    if mediapackage_results and mediapackage_results.get("playback_urls"):
        logging.info("\nDetected MediaPackage Playback URLs:")
        for url in mediapackage_results["playback_urls"]:
            logging.info(f"- {url}")
    else:
        logging.info("\nNo specific MediaPackage Playback URLs detected in the output.")
