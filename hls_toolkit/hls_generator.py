import logging
import os
import shutil
import subprocess
import shlex
import math
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

from hls_toolkit import time_utils
from hls_toolkit.aws_handler import (run_aws_cli, get_aws_account_id,
                                     manage_mediapackage_vod_asset)
from hls_toolkit.esam_parser import (parse_esam_xml_string, parse_mcc_xml_asset_tags,
                                     process_playlist)
from hls_toolkit.ffmpeg_wrapper import (get_video_info, run_loudnorm_analysis,
                                        generate_clipped_transcoded_merged_mp4s,
                                        _generate_hls_for_resolution, generate_thumbnails,
                                        _get_video_stream_details, _get_h264_profile_idc,
                                        get_ffprobe_first_pts, validate_clippings)
from hls_toolkit.playlist_utils import create_master_playlist, parse_variant_segments
from hls_toolkit.subtitle_processor import (generate_merged_subtitle_file,
                                            segment_vtt_for_hls)
from hls_toolkit.time_utils import timecode_to_seconds, seconds_to_timecode


def generate_hls_workflow(config: Dict[str, Any],
                          defaults: Dict[str, Any],
                          paths: Dict[str, Any],
                          s3_config: Dict[str, Any],
                          esam_config: Dict[str, Any],
                          mediapackage_config: Dict[str, Any],
                          default_input_video: str,
                          default_output_dir: str,
                          default_subtitle_file: Optional[str],
                          default_subtitle_language: str,
                          default_mp_packaging_group_id: str,
                          default_mp_vod_role_name: str,
                          default_region: str,
                          default_package_type: str,
                          input_video: str,
                          output_dir: Path,
                          output_dir_name: str,
                          subtitle_file: Optional[str],
                          ffmpeg_executable: str,
                          ffprobe_executable: str,
                          video_templates: Dict[str, Any],
                          template_name: str,
                          thumbnails_enabled: bool,
                          duration: Optional[int] = None,
                          transcode_workers: Optional[int] = None,
                          temp_dir: Optional[str] = None,
                          debug: bool = False,
                          audio_norm: bool = False,
                          esam: bool = False,
                          upload: bool = False,
                          import_to_mediapackage: bool = False,
                          debug_aws: bool = False,
                          resolution: Optional[str] = None) -> int:
    """Executes the main HLS VOD generation workflow.

    This function encapsulates the entire process of clipping, transcoding,
    HLS packaging, subtitle processing, ESAM ad-insertion, thumbnail generation,
    and optional S3 upload/MediaPackage import.

    Args:
        config: The full configuration dictionary loaded from config.json.
        defaults: The 'defaults' section from the config.
        paths: The 'paths' section from the config.
        s3_config: The 's3' section from the config.
        esam_config: The 'Esam' section from the config.
        mediapackage_config: The 'mediapackage' section from the config defaults.
        default_input_video: Default path to the input video.
        default_output_dir: Default output directory name.
        default_subtitle_file: Default path to the subtitle file.
        default_subtitle_language: Default subtitle language.
        default_mp_packaging_group_id: Default MediaPackage Packaging Group ID.
        default_mp_vod_role_name: Default MediaPackage VOD IAM role name.
        default_region: Default AWS region.
        default_package_type: Default MediaPackage package type.
        input_video: Actual input video path after argument parsing.
        output_dir: Absolute path to the output directory.
        output_dir_name: Name of the output directory.
        subtitle_file: Actual subtitle file path after argument parsing.
        ffmpeg_executable: Path to the FFmpeg executable.
        ffprobe_executable: Path to the FFprobe executable.
        video_templates: A dictionary of video templates, defining resolutions and encoding settings.

    Returns:
        0 on successful completion of the workflow, 1 on failure.
    """
    if not Path(ffmpeg_executable).exists():
        logging.critical(f"FFmpeg executable not found at {ffmpeg_executable}. "
                         "Cannot proceed without FFmpeg.")
        return 1
    if not Path(ffprobe_executable).exists():
        logging.critical(f"FFprobe executable not found at {ffprobe_executable}. "
                         "Cannot proceed without FFprobe.")
        return 1
    if temp_dir is None:
        temp_dir = str(Path.cwd() / ".wz_temp")

    try:
        video_info = get_video_info(ffprobe_executable, input_video)
        video_fps = video_info["frame_rate"]
        original_video_duration = video_info["duration"]
        if original_video_duration <= 0:
            logging.critical(f"Invalid video duration detected: {original_video_duration}s. "
                             f"The input file '{input_video}' may be corrupted or empty.")
            return 1
        logging.info(f"Detected video properties: FPS={video_fps}, "
                     f"Duration={original_video_duration}s")
    except Exception as e:
        logging.error(f"Could not determine video properties: {e}", exc_info=True)
        return 1

    clippings = defaults.get("InputClippings", [])
    if not clippings:
        logging.info("InputClippings not provided in config. "
                     "Using full input video as a single clip.")
        duration_seconds = get_video_info(ffprobe_executable, input_video).get("duration", 0.0)
        if duration_seconds > 0:
            clippings = [{'StartTimecode': "00:00:00:00",
                          'EndTimecode': seconds_to_timecode(duration_seconds, str(video_fps))}]
        else:
            logging.error("Could not determine video duration. "
                          "Cannot proceed without InputClippings.")
            return 1

    hls_settings = defaults.get("hls_settings", {})
    temp_clipping_dir = None

    try:
        events = []
        asset_tags_map = {}
        master_playlist_path = output_dir / "channel.m3u8"
        _ = []
        merged_timeline_cue_points_str = ""
        hls_output_paths = []
        temp_subtitle_dir = None
        subtitle_input_path = None
        total_merged_duration = 0.0
        _ = {}
        effective_clippings = list(clippings)

        if duration is not None:
            try:
                effective_end_duration = min(float(duration), original_video_duration)
                frame_number = round(effective_end_duration * float(video_fps))
                quantized_effective_end_duration = float(frame_number / float(video_fps))
                logging.info(f"Quantized effective --duration: {effective_end_duration}s -> "
                             f"Frame {frame_number} -> "
                             f"{quantized_effective_end_duration:.6f}s")
                effective_clippings = [{
                    'StartTimecode': "00:00:00:00",
                    'EndTimecode': seconds_to_timecode(quantized_effective_end_duration,
                                                       str(video_fps)),
                    'Name': "FullDurationClip"}]
            except (FileNotFoundError, subprocess.CalledProcessError, ValueError) as e:
                logging.error(f"Failed to get video duration: {e}")
                if isinstance(e, subprocess.CalledProcessError):
                    try:
                        ffprobe_output_formatted = e.stdout.strip().replace("\n", "\n        ")
                        logging.error(
                            f'FFprobe command failed: '
                            f'{" ".join(shlex.quote(arg) for arg in e.cmd)}\n'
                            f'    FFprobe output:\n        {ffprobe_output_formatted}')
                    except Exception:
                        pass
                return 1

        try:
            validate_clippings(effective_clippings, video_fps)
        except ValueError as e:
            logging.error(f"Configuration Error: {e}")
            return 1

        clip_gop_offsets = []
        gop_size_seconds = hls_settings.get("hls_time", 6.0)
        gop_size_frames = int(round(gop_size_seconds * float(video_fps)))
        for clip in effective_clippings:
            start_tc = clip["StartTimecode"]
            absolute_start_frame = time_utils.timecode_to_frame(start_tc, str(video_fps))
            offset_frames = (gop_size_frames - absolute_start_frame % gop_size_frames) % gop_size_frames
            offset_seconds = float(offset_frames) / float(video_fps)
            clip_gop_offsets.append(offset_seconds)

        logging.debug(f"Clip GOP offsets (seconds): {clip_gop_offsets}")

        if esam:
            scc_xml = esam_config.get("SignalProcessingNotification", {}).get("SccXml")
            mcc_xml = esam_config.get("ManifestConfirmConditionNotification", {}).get("MccXml")
            if scc_xml:
                events = parse_esam_xml_string(scc_xml)
                unique_events = []
                seen_npts = set()
                for event in events:
                    if event.get("npt") is not None and event["npt"] not in seen_npts:
                        unique_events.append(event)
                        seen_npts.add(event["npt"])

                if len(events) != len(unique_events):
                    logging.info(f"De-duplicated ESAM events from {len(events)} to "
                                 f"{len(unique_events)} based on unique timestamps.")
                events = unique_events

                tolerance_frames = 10
                logging.info(f"Applying ad snapping logic with a tolerance of "
                             f"{tolerance_frames} frames.")
                clip_end_frames = [
                    math.floor(timecode_to_seconds(c.get("EndTimecode"), str(video_fps))
                               * float(video_fps))
                    for c in effective_clippings]

                for event in events:
                    if "npt" in event:
                        original_npt = event["npt"]
                        original_frame = math.floor(original_npt * float(video_fps))
                        if not clip_end_frames:
                            continue
                        nearest_clip_end_frame = min(
                            clip_end_frames, key=lambda cf: abs(cf - original_frame))

                    if abs(original_frame - nearest_clip_end_frame) <= tolerance_frames:
                        snapped_frame = nearest_clip_end_frame
                        snapped_npt = float(snapped_frame / float(video_fps))
                        logging.info(f"Snapping ESAM event at {original_npt:.3f}s to clip "
                                     f"boundary. New NPT: {snapped_npt:.3f}s.")
                        event["npt"] = snapped_npt
                    else:
                        event["npt"] = original_npt

                all_potential_force_times = {e["npt"] for e in events if "npt" in e}
                accumulated_duration = 0.0
                for i, clip in enumerate(effective_clippings):
                    duration_seconds = (
                        timecode_to_seconds(clip.get("EndTimecode"), str(video_fps))
                        - timecode_to_seconds(clip.get("StartTimecode"), str(video_fps)))
                    quantized_duration = float(
                        round(duration_seconds * float(video_fps)) / float(video_fps))
                    accumulated_duration += quantized_duration
                    if i < len(effective_clippings) - 1:
                        all_potential_force_times.add(accumulated_duration)

                final_hls_force_times = []
                if all_potential_force_times:
                    sorted_potential_times = sorted(list(all_potential_force_times))
                    min_distance = 5.0 / float(video_fps) if video_fps else 0.167
                    prev_t = sorted_potential_times[0]
                    final_hls_force_times.append(prev_t)
                    for t in sorted_potential_times[1:]:
                        if t - prev_t > min_distance:
                            final_hls_force_times.append(t)
                            prev_t = t
                        else:
                            logging.info(f"Debouncing/Skipping force time {t:.6f}s as it is "
                                         f"too close to {prev_t:.6f}s")

                merged_timeline_cue_points_str = ",".join(
                    [f"{t:.6f}" for t in final_hls_force_times])
                logging.info("Final HLS force times after snapping and debouncing: "
                             f"{merged_timeline_cue_points_str}")

            if mcc_xml:
                asset_tags_map = parse_mcc_xml_asset_tags(mcc_xml)

        if not esam:
            events = []
            asset_tags_map = {}
            logging.info("ESAM is disabled. Ensuring ESAM events and asset tags are cleared.")

        all_resolutions_data = video_templates.get(template_name)
        if not all_resolutions_data:
            logging.error(f"Template '{template_name}' not found.")
            return 1

        if resolution:
            selected_resolution_names = [r.strip() for r in resolution.split(",")]
            logging.info("Overriding resolutions from config, will process: "
                         f"{selected_resolution_names}")
        else:
            selected_resolution_names = [
                r.strip()
                for r in defaults.get("resolutions", "1080p,720p,540p,360p").split(",")
                if r.strip()]

        selected_resolutions_data = [res for res in all_resolutions_data
                                     if res["name"] in selected_resolution_names]
        found_resolution_names = {res["name"] for res in selected_resolutions_data}
        missing_resolutions = [name for name in selected_resolution_names
                               if name not in found_resolution_names]
        if missing_resolutions:
            logging.critical("Configuration Error: The following configured resolutions were "
                             f"not found in template '{template_name}': "
                             f'{", ".join(missing_resolutions)}. Exiting.')
            return 1
        if not selected_resolutions_data:
            logging.critical("Configuration Error: None of the specified resolutions "
                             f"({selected_resolution_names}) were found in the template "
                             f"'{template_name}'. Exiting.")
            return 1

        logging.info('Processing for the following resolutions: '
                     f'{[res["name"] for res in selected_resolutions_data]}')
        for res_data in selected_resolutions_data:
            if "frame_rate" not in res_data or not res_data["frame_rate"]:
                logging.info(f'Frame rate not configured for resolution {res_data["name"]}. '
                             f'Applying detected input video frame rate: {video_fps}')
                res_data["frame_rate"] = video_fps

        os.makedirs(output_dir, exist_ok=True)
        loudnorm_analysis_results = {}
        loudnorm_settings = config.get("audio_normalization", {}).get("loudnorm_settings", {})

        if audio_norm:
            logging.info("Running loudnorm analysis...")
            audio_analysis_input = os.path.abspath(input_video)
            loudnorm_analysis_duration = None
            if effective_clippings:
                first_clip = effective_clippings[0]
                start_seconds = timecode_to_seconds(first_clip.get("StartTimecode"),
                                                    str(video_fps))
                end_seconds = timecode_to_seconds(first_clip.get("EndTimecode"),
                                                  str(video_fps))
                loudnorm_analysis_duration = end_seconds - start_seconds
                logging.info("Performing loudnorm analysis on the first clipping for "
                             f"{float(loudnorm_analysis_duration):.3f} seconds.")
            else:
                if total_merged_duration == 0:
                    total_merged_duration = original_video_duration
                loudnorm_analysis_duration = total_merged_duration
                logging.info("Performing loudnorm analysis on the full video for "
                             f"{loudnorm_analysis_duration:.3f} seconds.")

            loudnorm_analysis_results = run_loudnorm_analysis(
                ffmpeg_executable, audio_analysis_input, cwd=temp_dir,
                duration=loudnorm_analysis_duration)
            if not loudnorm_analysis_results:
                logging.warning("Warning: Loudnorm analysis failed. "
                                "Audio normalization will be skipped.")
                audio_norm = False
            else:
                logging.info("Loudnorm analysis results:")
                for k, v in loudnorm_analysis_results.items():
                    logging.info(f"  {k}: {v}")

        merged_video_paths_by_resolution = {}
        merged_audio_path = None

        if effective_clippings:
            (merged_video_paths_by_resolution, merged_audio_path, total_merged_duration,
             temp_clipping_dir, remapped_esam_events_for_video) = \
                generate_clipped_transcoded_merged_mp4s(
                    input_video, ffmpeg_executable, ffprobe_executable, effective_clippings,
                    video_fps, selected_resolutions_data, output_dir, duration,
                    transcode_workers, Path(temp_dir), audio_norm, loudnorm_analysis_results,
                    loudnorm_settings, clip_gop_offsets=clip_gop_offsets,
                    esam_events=(events if esam else []))

            if esam and remapped_esam_events_for_video:
                events = remapped_esam_events_for_video
                logging.info("Updated ESAM events to remapped versions for merged timeline. "
                             f"Count: {len(events)}")

            if subtitle_file:
                merged_vtt_path_temp, temp_subtitle_dir_local, total_merged_duration = \
                    generate_merged_subtitle_file(
                        Path(subtitle_file).resolve(), ffmpeg_executable, effective_clippings,
                        video_fps, output_dir, Path(temp_dir))
                if merged_vtt_path_temp:
                    subtitle_input_path = Path(merged_vtt_path_temp)
                    temp_subtitle_dir = Path(temp_subtitle_dir_local)
                else:
                    logging.warning("No merged VTT file generated during initial processing.")
        else:
            for res_data in selected_resolutions_data:
                merged_video_paths_by_resolution[res_data["name"]] = os.path.abspath(input_video)
            total_merged_duration = original_video_duration

        if esam and events:
            logging.info("Using remapped ESAM cue points for merged timeline "
                         f"(for HLS segmentation): {merged_timeline_cue_points_str}")

        if merged_video_paths_by_resolution:
            for res_data in selected_resolutions_data:
                res_name = res_data["name"]
                merged_mp4_path = merged_video_paths_by_resolution.get(res_name)
                if merged_mp4_path:
                    hls_playlist_res_path = output_dir / f"channel_{res_name}.m3u8"
                    if _generate_hls_for_resolution(
                            res_data, merged_mp4_path, merged_audio_path, ffmpeg_executable,
                            total_merged_duration, output_dir, hls_settings,
                            merged_force_times_str=merged_timeline_cue_points_str,
                            frame_rate=res_data.get("frame_rate", "")):
                        hls_output_paths.append(hls_playlist_res_path)
        else:
            merged_audio_path = os.path.abspath(input_video)
            for res_data in selected_resolutions_data:
                res_name = res_data["name"]
                merged_mp4_path = merged_video_paths_by_resolution.get(
                    res_name, os.path.abspath(input_video))
                if merged_mp4_path:
                    hls_playlist_res_path = output_dir / f"channel_{res_name}.m3u8"
                    if _generate_hls_for_resolution(
                            res_data, merged_mp4_path, merged_audio_path, ffmpeg_executable,
                            total_merged_duration, output_dir, hls_settings,
                            merged_force_times_str=merged_timeline_cue_points_str,
                            frame_rate=res_data.get("frame_rate", "")):
                        hls_output_paths.append(hls_playlist_res_path)

        _ = []
        video_segments = []
        _ = None
        global_mpegts_start_val = None

        if hls_output_paths:
            reference_video_m3u8_path = None
            for res_path in hls_output_paths:
                if "1080p" in str(res_path):
                    reference_video_m3u8_path = res_path
                    break

            if not reference_video_m3u8_path and hls_output_paths:
                reference_video_m3u8_path = hls_output_paths[0]

            if reference_video_m3u8_path and reference_video_m3u8_path.exists():
                _, video_segments, _, _ = parse_variant_segments(reference_video_m3u8_path)
                if video_segments:
                    first_segment_filename = video_segments[0][2]
                    first_segment_path = Path(os.path.join(output_dir, first_segment_filename))
                    if first_segment_path.exists():
                        pts_seconds = get_ffprobe_first_pts(ffprobe_executable,
                                                            first_segment_path)
                        if pts_seconds is not None:
                            global_mpegts_start_val = int(pts_seconds * 90000)
                            logging.info(f"Detected start PTS from {first_segment_filename}: "
                                         f"{pts_seconds}s. Setting global VTT MPEGTS start to "
                                         f"{global_mpegts_start_val}.")
            else:
                logging.warning("Could not find any video HLS playlist to extract segment "
                                "times. VTT segmentation might not be perfectly aligned.")

        hls_sub_playlist_path = None
        if subtitle_file and subtitle_input_path and subtitle_input_path.exists():
            success = False
            hls_sub_playlist_path_local = None
            success, hls_sub_playlist_path_local = segment_vtt_for_hls(
                subtitle_input_path,
                output_dir,
                default_subtitle_language,
                total_merged_duration,
                video_segments=video_segments,
                global_stream_mpegts_start=global_mpegts_start_val)
            if not success:
                logging.error("Failed to segment VTT for HLS.")
                return 1
            if hls_sub_playlist_path_local:
                hls_sub_playlist_path = hls_sub_playlist_path_local
                hls_output_paths.append(hls_sub_playlist_path)
            if temp_subtitle_dir and temp_subtitle_dir.exists():
                shutil.rmtree(temp_subtitle_dir, ignore_errors=True)

        create_master_playlist(str(master_playlist_path), selected_resolutions_data,
                               merged_video_paths_by_resolution, ffprobe_executable,
                               float(video_fps), subtitle_file, hls_sub_playlist_path,
                               default_subtitle_language, _get_video_stream_details,
                               _get_h264_profile_idc)

        if thumbnails_enabled:
            first_mp4_for_thumbnails = next(iter(merged_video_paths_by_resolution.values()),
                                            None)
            if first_mp4_for_thumbnails:
                generate_thumbnails(ffmpeg_executable, first_mp4_for_thumbnails, output_dir)
            else:
                logging.warning("No mp4 source found for thumbnail generation.")

        if esam and events:
            for playlist_path in hls_output_paths:
                if playlist_path.is_file():
                    logging.info(f"Injecting ESAM markers into {playlist_path.name}")
                    process_playlist(str(playlist_path), events, asset_tags_map,
                                     video_segments=video_segments)

        s3_bucket_name_local = s3_config.get("bucket_name")
        s3_key_prefix_local = s3_config.get("key_prefix", "").strip("/")
        if not s3_bucket_name_local and (upload or import_to_mediapackage):
            logging.error("S3 bucket_name not specified in config. "
                          "Cannot perform S3 operations.")
            return 1

        if upload:
            s3_destination_key = (f"{s3_key_prefix_local}/{output_dir_name}"
                                  if s3_key_prefix_local else output_dir_name)
            s3_destination_path = f"s3://{s3_bucket_name_local}/{s3_destination_key}"
            run_aws_cli(['aws', 's3', 'rm', '--recursive', s3_destination_path],
                        check=True, log_output=True)
            run_aws_cli(["aws", "s3", "cp", "--recursive", str(output_dir),
                         s3_destination_path], check=True, log_output=True)

        if import_to_mediapackage:
            aws_account_id = get_aws_account_id(default_region, debug_aws)
            if not aws_account_id:
                logging.error("Could not retrieve AWS account ID. "
                              "Cannot perform MediaPackage import.")
                return 1
            vod_role_arn_to_use = (f"arn:aws:iam::{aws_account_id}:role/"
                                   f"{default_mp_vod_role_name}")
            s3_source_arn = (f'arn:aws:s3:::{s3_bucket_name_local}/{s3_key_prefix_local}'
                             f'{"/" if s3_key_prefix_local else ""}'
                             f'{output_dir_name}/channel.m3u8')
            mediapackage_results = manage_mediapackage_vod_asset(
                region=default_region,
                packaging_group_id=default_mp_packaging_group_id,
                vod_role_arn=vod_role_arn_to_use,
                s3_source_arn=s3_source_arn,
                package_type=default_package_type,
                asset_id=None,
                debug=debug_aws)
            if mediapackage_results and "error" in mediapackage_results:
                logging.error(f'MediaPackage integration failed: '
                              f'{mediapackage_results.get("error")} - '
                              f'{mediapackage_results.get("details")}')
                return 1
            logging.info("MediaPackage integration completed successfully.")
            if mediapackage_results and mediapackage_results.get("playback_urls"):
                logging.info("Detected MediaPackage Playback URLs:")
                for url in mediapackage_results["playback_urls"]:
                    logging.info(f"- {url}")
            else:
                logging.info("No specific MediaPackage Playback URLs detected in the output.")

    except KeyboardInterrupt:
        logging.warning("Keyboard interrupt. Exiting.")
        return 1
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}", exc_info=True)
        return 1
    finally:
        try:
            if temp_clipping_dir and temp_clipping_dir.exists():
                if not debug:
                    shutil.rmtree(temp_clipping_dir, ignore_errors=True)
                else:
                    logging.info("Debug enabled: Keeping temporary directory "
                                 f"{temp_clipping_dir}")
        except Exception as e:
            logging.warning(f"Error while cleaning temporary dirs: {e}")

    return 0
