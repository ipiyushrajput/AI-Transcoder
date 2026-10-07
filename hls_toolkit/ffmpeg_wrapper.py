import fractions
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import tempfile
import math
import concurrent.futures
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, Tuple

from hls_toolkit.cpu_budget import BudgetAcquireAborted, get_shared_budget
from hls_toolkit.job_context import (JobCancelled, TranscodeError, bind_context,
                                     current_context, log)
from hls_toolkit.time_utils import timecode_to_seconds
from hls_toolkit.esam_parser import remap_esam_events_for_merged_clips

ACTIVE_PROCESSES = []
PROCESS_LOCK = threading.Lock()


def get_active_processes():
    """Returns a copy of the list of active processes."""
    with PROCESS_LOCK:
        return list(ACTIVE_PROCESSES)


def run_loudnorm_analysis(ffmpeg_executable: str,
                          input_video: Union[str, Path],
                          duration: Optional[float] = None,
                          cwd: Optional[Union[str, Path]] = None) -> Optional[Dict[str, Any]]:
    """Runs FFmpeg loudnorm analysis on an audio track."""
    ffmpeg_cmd = [ffmpeg_executable, "-y"]
    if duration is not None:
        ffmpeg_cmd.extend(["-t", str(float(duration))])
    if "ffconcat" in os.path.basename(str(input_video)):
        ffmpeg_cmd.extend(["-safe", "0"])
    ffmpeg_cmd.extend([
        "-i",
        str(input_video),
        "-vn",
        "-af",
        "loudnorm=print_format=json",
        "-f",
        "null",
        "-"])
    try:
        _, stderr_output = _run_ffmpeg_command_with_logging(
            ffmpeg_cmd, log_prefix="Loudnorm analysis", cwd=cwd, check_returncode=True)
        json_lines, in_json_block = [], False
        for line in stderr_output.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("{"):
                in_json_block = True
            if in_json_block:
                json_lines.append(line)
                if line.endswith("}"):
                    break
        if not json_lines:
            log().error("Could not find JSON output from loudnorm analysis.")
            return None
        json_str = "".join(json_lines)
        return json.loads(json_str)
    except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError) as e:
        log().error(f"Loudnorm analysis failed: {e}", exc_info=False)
        return None


def get_video_info(ffprobe_executable: str,
                   input_video: Union[str, Path]) -> Dict[str, Union[float, fractions.Fraction]]:
    """Gets video information (duration and frame rate) using a single ffprobe call."""
    ffprobe_cmd = [
        ffprobe_executable,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=avg_frame_rate",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(input_video)]
    try:
        result_json = _run_ffprobe_command(ffprobe_cmd, log_prefix="FFprobe video info")
        if not result_json:
            return {'duration': 0.0, 'frame_rate': fractions.Fraction(25, 1)}
        data = json.loads(result_json)
        duration_str = data.get("format", {}).get("duration")
        duration = float(duration_str) if duration_str else 0.0
        streams = data.get("streams", [])
        frame_rate_str = "25/1"
        if streams:
            frame_rate_str = streams[0].get("avg_frame_rate", "25/1")
        frame_rate = (fractions.Fraction(frame_rate_str)
                      if (frame_rate_str and frame_rate_str != "0/0")
                      else fractions.Fraction(25, 1))
        return {'duration': duration, 'frame_rate': frame_rate}
    except (subprocess.CalledProcessError, json.JSONDecodeError, ValueError,
            IndexError, TypeError) as e:
        log().warning(f"Could not get video info: {e}. Defaulting duration to 0 and FPS to 25/1.")
        return {'duration': 0.0, 'frame_rate': fractions.Fraction(25, 1)}


def _run_ffprobe_command(cmd_list: List[str],
                         log_prefix: str = 'FFprobe command',
                         check_returncode: bool = True) -> Optional[str]:
    """Runs an ffprobe command and captures its output."""
    try:
        process = subprocess.Popen(cmd_list,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT,
                                   universal_newlines=True,
                                   encoding="utf-8",
                                   errors="ignore")
        output, _ = process.communicate()
        if check_returncode and process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, cmd_list, output=output)
        return output.strip()
    except FileNotFoundError:
        log().error(f"Error: FFprobe executable not found for {log_prefix}.")
        raise
    except subprocess.CalledProcessError as e:
        log().error(f'{log_prefix} failed with exit code {e.returncode}. '
                      f'Command: {" ".join(shlex.quote(arg) for arg in cmd_list)}\n'
                      f'Output:\n{e.output}')
        raise


def get_ffprobe_first_pts(ffprobe_path: str, filepath: Union[str, Path]) -> Optional[float]:
    """Gets the PTS of the first video packet from a media file."""
    command = [
        ffprobe_path,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "packet=pts_time",
        "-of",
        "csv=p=0:s=N",
        str(filepath)]
    try:
        output = _run_ffprobe_command(
            command, log_prefix=f"FFprobe first PTS for {os.path.basename(str(filepath))}")
        if output:
            return float(output.splitlines()[0].strip().replace("N", ""))
        return None
    except (subprocess.CalledProcessError, ValueError, IndexError) as e:
        log().error(f"Error getting first PTS for {filepath}: {e}")
        return None


_PROGRESS_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")

# Per-clip encode fractions, so overall transcode progress moves smoothly while
# long clips are still in flight rather than jumping only as tasks finish.
_CLIP_PROGRESS: Dict[str, float] = {}
_CLIP_PROGRESS_LOCK = threading.Lock()


def _clip_progress_key(ctx, clip_index: int, stream_type: str) -> str:
    # Keyed per job: the API runs several jobs in one process, and every job has
    # a clip 0, so a bare clip number would mix their progress figures.
    return f"{id(ctx)}:{clip_index}:{stream_type}"


def _clear_clip_progress(ctx) -> None:
    """Drop one job's clip progress, leaving other jobs' entries alone."""
    prefix = f"{id(ctx)}:"
    with _CLIP_PROGRESS_LOCK:
        for key in [k for k in _CLIP_PROGRESS if k.startswith(prefix)]:
            del _CLIP_PROGRESS[key]


def _clip_progress_cb(clip_index: int, stream_type: str):
    """Callback that folds one clip's encode fraction into the job's progress."""
    ctx = current_context()
    if ctx is None or stream_type != "video":
        return None
    key = _clip_progress_key(ctx, clip_index, stream_type)
    prefix = f"{id(ctx)}:"

    def _report(fraction: float):
        with _CLIP_PROGRESS_LOCK:
            _CLIP_PROGRESS[key] = fraction
            mine = [v for k, v in _CLIP_PROGRESS.items() if k.startswith(prefix)]
            total = ctx.metadata.get("video_clip_count") or len(mine)
            done = sum(mine)
        if total:
            ctx.advance_within_stage(min(1.0, done / total))

    return _report


def _parse_progress_seconds(line: str) -> Optional[float]:
    """Seconds encoded in an FFmpeg `time=HH:MM:SS.ms` progress line."""
    match = _PROGRESS_TIME_RE.search(line)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _run_ffmpeg_command_with_logging(cmd_list: List[str],
                                     log_prefix: str,
                                     cwd: Optional[Union[str, Path]] = None,
                                     check_returncode: bool = True,
                                     expected_seconds: Optional[float] = None,
                                     progress_cb=None) -> Tuple[str, str]:
    """Run an FFmpeg command, mirroring its output into the job's ffmpeg.log.

    Every raw stdout/stderr line is written verbatim to ``ffmpeg.log`` for the
    active job (see :mod:`hls_toolkit.job_context`), while a filtered view goes
    to ``job.log``. When ``expected_seconds`` is given, ``time=`` lines drive
    ``progress_cb(fraction)`` so the status API can report a percentage.

    Raises :class:`JobCancelled` if the job is cancelled mid-encode — the whole
    process group is terminated first.
    """
    ctx = current_context()
    full_cmd_str = " ".join(shlex.quote(str(arg)) for arg in cmd_list)
    log().info(f"Executing {log_prefix}. Command: {full_cmd_str}")
    if ctx is not None:
        ctx.ffmpeg_command(log_prefix, full_cmd_str)

    try:
        process = subprocess.Popen(cmd_list,
                                   stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,
                                   universal_newlines=True,
                                   cwd=cwd,
                                   encoding="utf-8",
                                   errors="ignore",
                                   start_new_session=True)
    except FileNotFoundError as e:
        raise TranscodeError(
            f"FFmpeg executable not found while running {log_prefix}: {cmd_list[0]}",
            stage="TRANSCODING") from e
    except OSError as e:
        raise TranscodeError(f"Could not start {log_prefix}: {e}",
                             stage="TRANSCODING") from e

    with PROCESS_LOCK:
        ACTIVE_PROCESSES.append(process)
    try:
        stdout_buffer, stderr_buffer = [], []

        def read_stream(stream, buffer, stream_name):
            progress_keys = ['frame=', 'fps=', 'size=', 'time=', 'bitrate=', 'speed=']
            for line in stream:
                buffer.append(line)
                line_stripped = line.strip()
                if ctx is not None and line_stripped:
                    ctx.ffmpeg_line(line_stripped)
                if not line_stripped:
                    continue
                if stream_name == "stderr":
                    if all(k in line_stripped for k in progress_keys):
                        log().debug(f"FFmpeg {log_prefix} progress: {line_stripped}")
                        if progress_cb and expected_seconds:
                            elapsed = _parse_progress_seconds(line_stripped)
                            if elapsed is not None:
                                progress_cb(max(0.0, min(1.0, elapsed / expected_seconds)))
                    elif "warning" in line_stripped.lower():
                        log().warning(f"FFmpeg {log_prefix} stderr: {line_stripped}")
                    else:
                        log().debug(f"FFmpeg {log_prefix} stderr: {line_stripped}")
                else:
                    log().info(f"FFmpeg {log_prefix} {stream_name}: {line_stripped}")

        stdout_thread = threading.Thread(target=read_stream,
                                         args=(process.stdout, stdout_buffer, "stdout"),
                                         daemon=True)
        stderr_thread = threading.Thread(target=read_stream,
                                         args=(process.stderr, stderr_buffer, "stderr"),
                                         daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        cancelled = False
        try:
            while process.poll() is None:
                if ctx is not None and ctx.cancelled:
                    cancelled = True
                    log().warning(f"Cancellation requested — terminating {log_prefix}")
                    _terminate_process(process)
                    break
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    continue
            stdout_thread.join(timeout=10)
            stderr_thread.join(timeout=10)
            process.wait()
        except BaseException:
            _terminate_process(process)
            raise

        stdout_output = "".join(stdout_buffer)
        stderr_output = "".join(stderr_buffer)
        if cancelled:
            raise JobCancelled(f"{log_prefix} cancelled")

        if process.returncode != 0:
            tail = _tail_text(stderr_output or stdout_output, 40)
            message = (f"{log_prefix} failed with exit code {process.returncode}.\n"
                       f"Command: {full_cmd_str}\n"
                       f"Last FFmpeg output:\n{tail}")
            if check_returncode:
                log().error(message)
                raise subprocess.CalledProcessError(process.returncode, cmd_list,
                                                    output=stdout_output,
                                                    stderr=stderr_output)
            log().warning(message)
        return (stdout_output, stderr_output)
    finally:
        with PROCESS_LOCK:
            if process in ACTIVE_PROCESSES:
                ACTIVE_PROCESSES.remove(process)


def _terminate_process(process) -> None:
    """Terminate an FFmpeg process group, escalating to SIGKILL if needed."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except Exception:
        try:
            process.terminate()
        except Exception:
            return
    try:
        process.wait(timeout=10)
    except Exception:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


def _tail_text(text: str, lines: int) -> str:
    rows = [r for r in (text or "").splitlines() if r.strip()]
    return "\n".join(rows[-lines:]) or "(no output captured)"


def _merge_transcoded_clips(ffmpeg_executable: str,
                            individual_transcoded_clips_per_resolution: Dict[str, List[Path]],
                            temp_clipping_dir: Path) -> Dict[str, str]:
    """
    Merges individual transcoded segments into single continuous MP4 files per resolution.

    Uses the FFmpeg concat demuxer (via a temporary .txt file list) to stitch clips together
    without re-encoding (copy codec).

    Args:
        ffmpeg_executable: Path to the FFmpeg binary.
        individual_transcoded_clips_per_resolution: Dictionary mapping resolution names to lists of clip paths.
        temp_clipping_dir: Directory where the concat list files and merged MP4s will be stored.

    Returns:
        Dict[str, str]: A map of resolution name to the absolute path of the merged MP4 file.
    """
    merged_mp4_paths_by_resolution = {}
    for res_name, clip_paths in individual_transcoded_clips_per_resolution.items():
        if not clip_paths:
            continue
        ffconcat_file_path = temp_clipping_dir / f"ffconcat_{res_name}.txt"
        with open(ffconcat_file_path, "w") as f:
            f.write("ffconcat version 1.0\n")
            for path in sorted(clip_paths, key=lambda p: p.name):
                f.write(f"file '{os.path.abspath(path)}'\n")

        merged_output_path = temp_clipping_dir / f"merged_{res_name}.mp4"
        merge_cmd = [
            ffmpeg_executable,
            "-y",
            "-safe",
            "0",
            "-i",
            str(ffconcat_file_path),
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            str(merged_output_path)]
        _run_ffmpeg_command_with_logging(merge_cmd, log_prefix=f"Merge clips for {res_name}")
        merged_mp4_paths_by_resolution[res_name] = str(merged_output_path)

    return merged_mp4_paths_by_resolution


def _transcode_clip_with_single_command(clip_index,
                                        video_templates,
                                        abs_input_video,
                                        temp_clipping_dir,
                                        ffmpeg_executable,
                                        original_clip_start_seconds,
                                        original_clip_end_seconds,
                                        audio_normalization_enabled,
                                        loudnorm_analysis_results,
                                        loudnorm_settings,
                                        esam_events,
                                        current_clip_start_compacted,
                                        current_clip_end_compacted,
                                        video_fps,
                                        current_clip_gop_offset_seconds: float = 0.0,
                                        stream_type='video'):
    """
    Transcodes a single time slice of the input video into multiple outputs.

    For video streams:
    - Splits the input into multiple branches (one per resolution).
    - Applies scaling and frame rate filters.
    - Encodes using specified codecs (libwz264/265) and parameters (bitrate/CRF).
    - Inserts SCTE-35 cue points if ESAM events fall within this clip's time window.
    - Forces keyframes based on GOP alignment rules to ensure smooth stitching.

    For audio streams:
    - Extracts the audio track.
    - Applies loudnorm filter if enabled.
    - Encodes to AAC.

    Args:
        clip_index: Index of the current clip (for naming).
        video_templates: List of output resolution configurations.
        abs_input_video: Absolute path to the source video.
        temp_clipping_dir: Directory for output files.
        ffmpeg_executable: Path to FFmpeg.
        original_clip_start_seconds: Start time in the source video.
        original_clip_end_seconds: End time in the source video.
        audio_normalization_enabled: Whether to apply audio normalization.
        loudnorm_analysis_results: Data from pre-analysis for linear normalization.
        loudnorm_settings: Settings for the loudnorm filter.
        esam_events: List of SCTE-35 events to potentially insert.
        current_clip_start_compacted: Start time of this clip in the final timeline.
        current_clip_end_compacted: End time of this clip in the final timeline.
        video_fps: Frame rate of the video.
        current_clip_gop_offset_seconds: Time offset for the GOP structure.
        stream_type: 'video' or 'audio'.

    Returns:
        Dict: Information about the generated outputs (type, paths).
    """
    clip_duration = original_clip_end_seconds - original_clip_start_seconds
    ffmpeg_cmd = [
        ffmpeg_executable,
        "-y",
        "-ss",
        f"{float(original_clip_start_seconds):.3f}",
        "-t",
        f"{float(clip_duration):.3f}",
        "-i",
        str(abs_input_video)]
    if stream_type == "video":
        split_labels = "".join([f"[v{i}]" for i in range(len(video_templates))])
        filter_complex_parts = [f"[0:v]split={len(video_templates)}{split_labels}"]
        for i, res_data in enumerate(video_templates):
            res_name = res_data["name"]
            video_filters = []
            if res_data.get("interlace_mode") == "PROGRESSIVE":
                video_filters.append("yadif=mode=0:parity=auto:deint=1")
            video_filters.extend([
                f'scale=w={res_data["width"]}:h={res_data["height"]}',
                f'fps={str(res_data.get("frame_rate", video_fps))}'])
            filter_complex_parts.append(
                f'[v{i}]{",".join(video_filters)}[v_out_{res_name}]')

        ffmpeg_cmd.extend(["-filter_complex", ";".join(filter_complex_parts)])
        results_this_clip = {}
        for res_data in video_templates:
            res_name = res_data["name"]
            output_clip_path = Path(temp_clipping_dir) / f"clip_{clip_index:03d}_{res_name}_video.mp4"
            results_this_clip[res_name] = output_clip_path
            ffmpeg_cmd.extend(["-map", f"[v_out_{res_name}]", "-an"])
            ffmpeg_cmd.extend(["-c:d", "none"])
            codec_name = "libwz264"
            if res_data.get("codec") == "H_265":
                codec_name = "libwz265"
                ffmpeg_cmd.extend(["-tag:v", "hvc1"])
            ffmpeg_cmd.extend(["-c:v", codec_name])
            if res_data.get("codec_params"):
                param_key = (f"-{codec_name[3:]}-params" if codec_name.startswith("libwz")
                             else f"-{codec_name}-params")
                ffmpeg_cmd.extend([param_key, res_data["codec_params"]])
            if res_data.get("crf") is not None:
                ffmpeg_cmd.extend(["-crf", str(res_data["crf"])])
            elif res_data.get("bitrate"):
                ffmpeg_cmd.extend(["-b:v", res_data["bitrate"]])
            ffmpeg_cmd.extend(["-a53cc", "0"])
            if res_data.get("preset"):
                ffmpeg_cmd.extend(["-preset", res_data["preset"]])
            if res_data.get("caeopts") is not None:
                ffmpeg_cmd.extend(["-caeopts", str(res_data["caeopts"])])
            if res_data.get("threads"):
                ffmpeg_cmd.extend(["-threads:v", str(res_data["threads"])])
            video_format = res_data.get("video_format", "yuv420p")
            if video_format == "yuv420p":
                ffmpeg_cmd.extend(["-pix_fmt", "yuv420p", "-color_range", "tv"])
            elif video_format == "yuvj420p":
                ffmpeg_cmd.extend(["-pix_fmt", "yuvj420p", "-color_range", "pc"])
            elif video_format == "yuv420p10":
                ffmpeg_cmd.extend(["-pix_fmt", "yuv420p10le", "-color_range", "tv"])
            elif video_format == "yuvj420p10":
                ffmpeg_cmd.extend(["-pix_fmt", "yuv420p10le", "-color_range", "pc"])
            else:
                ffmpeg_cmd.extend(["-pix_fmt", "yuv420p", "-color_range", "tv"])

            scte35_times_unfiltered = [
                round(e["npt"] - current_clip_start_compacted, 3)
                for e in esam_events
                if current_clip_start_compacted <= e.get("npt", -1) < current_clip_end_compacted]
            scte35_times = sorted([t for t in scte35_times_unfiltered if t > 0.1])
            for t in scte35_times_unfiltered:
                if t <= 0.1:
                    log().info(f"Ignoring cue point at {t:.3f}s as it is effectively zero.")
            if scte35_times:
                ffmpeg_cmd.extend([
                    "-scte35_cue_points", ",".join([f"{t:.3f}" for t in scte35_times])])

            gop_size_seconds_target = float(res_data.get("GopSize", 6.0))
            try:
                fps_fraction = fractions.Fraction(video_fps)
                gop_size_frames = int(round(gop_size_seconds_target * fps_fraction))
                precise_gop_duration = float(gop_size_frames / fps_fraction)
                log().info(f'Precise GOP for {res_data["name"]}: '
                             f'target={gop_size_seconds_target}s, fps={fps_fraction}, '
                             f'frames={gop_size_frames}, '
                             f'precise_duration={precise_gop_duration:.3f}s')
            except (ValueError, ZeroDivisionError):
                precise_gop_duration = gop_size_seconds_target
                log().warning(f"Could not parse video_fps '{video_fps}'. Falling back to "
                                f"target GOP duration {precise_gop_duration}s.")

            force_key_frames_expr = f"expr:gte(t,n_forced*{precise_gop_duration:.3f})"
            if current_clip_gop_offset_seconds > 0.001:
                force_key_frames_expr = (f"expr:gte(t,{current_clip_gop_offset_seconds:.3f} "
                                         f"+ n_forced*{precise_gop_duration:.3f})")
            ffmpeg_cmd.extend(["-force_key_frames", force_key_frames_expr])
            ffmpeg_cmd.extend(["-avoid_negative_ts", "make_zero"])
            ffmpeg_cmd.append(str(output_clip_path))

    elif stream_type == "audio":
        output_clip_path = Path(temp_clipping_dir) / f"clip_{clip_index:03d}_audio.mp4"
        ffmpeg_cmd.extend(["-vn"])
        if audio_normalization_enabled and loudnorm_analysis_results:
            s = loudnorm_settings
            r = loudnorm_analysis_results
            l_filter = (f'loudnorm=I={s.get("i", -23.0)}:LRA={s.get("lra", 7.0)}'
                        f':TP={s.get("tp", -2.0)}:measured_I={r.get("input_i")}'
                        f':measured_LRA={r.get("input_lra")}:measured_TP={r.get("input_tp")}'
                        f':offset={r.get("target_offset")}:linear=true:print_format=summary')
            ffmpeg_cmd.extend(["-af", l_filter])
        ffmpeg_cmd.extend(["-c:a", "aac", "-ar", "48000", "-b:a", "192k",
                           "-avoid_negative_ts", "make_zero", str(output_clip_path)])
    else:
        raise ValueError(f"Unsupported stream_type: {stream_type}")

    _run_ffmpeg_command_with_logging(
        ffmpeg_cmd,
        log_prefix=f"Transcode clip {clip_index:03d} ({stream_type})",
        expected_seconds=float(clip_duration) if clip_duration else None,
        progress_cb=_clip_progress_cb(clip_index, stream_type))
    if stream_type == "video":
        return {'type': "video",
                'outputs': {k: str(v) for k, v in results_this_clip.items()}}
    else:
        return {'type': "audio", 'output': str(output_clip_path)}


# What an audio-only clip encode reserves from the CPU budget. AAC is cheap next
# to the video encoders and should not hold a whole video encode's share.
AUDIO_TASK_CORES = 1


def estimate_video_task_cores(rungs: List[Dict[str, Any]]) -> int:
    """Cores one clip's video encode is expected to keep busy.

    One FFmpeg process encodes every rung of a clip at once, each encoder with
    its configured ``threads``. Encoders rarely keep all their threads busy, so
    this follows the vendor's sizing rule of half the configured threads: the
    sum of ``threads / 2`` over the rungs being encoded. A rung without a
    ``threads`` value counts as one thread, as the vendor rule did.

    Only the rungs actually selected for the job count. The previous sizing
    looked at every rung in the template, so an unselected 2160p rung with
    ``threads: 12`` made a four-rung H.265 job reserve 36 cores per clip — on
    any server under 96 cores that left room for one clip at a time.
    """
    if not rungs:
        return 1
    threads = sum(max(1, int(rung.get("threads") or 0)) for rung in rungs)
    return max(1, math.ceil(threads / 2))


def _schedule_transcode_tasks(tasks: List[tuple], video_cost: int) -> List[Dict[str, Any]]:
    """Order clip tasks for release into the CPU budget.

    Longest clip first: when clips differ in length, starting the long ones
    early keeps the last minutes of the job from being one long clip running
    alone on an otherwise idle machine. Video before audio, because the audio
    encodes are short and fill whatever cores the video leaves free.
    """
    plan = []
    for order, task in enumerate(tasks):
        stream_type = task[-1]
        duration = float(task[7]) - float(task[6])
        plan.append({"task": task, "order": order, "stream_type": stream_type,
                     "duration": duration,
                     "cost": video_cost if stream_type == "video" else AUDIO_TASK_CORES})
    plan.sort(key=lambda item: (item["stream_type"] != "video", -item["duration"],
                                item["order"]))
    return plan


def _dispatch_within_budget(executor, plan: List[Dict[str, Any]], budget, ctx,
                            transcode_workers: Optional[int]) -> Dict[Any, tuple]:
    """Start each task as soon as the shared CPU budget has room for it.

    Returns the futures, keyed to their task, for the caller to collect. Waiting
    stops early if the job is cancelled or a task already started has failed;
    the caller then sees that failure (or the cancellation) when collecting.
    """
    local_cap = (threading.BoundedSemaphore(transcode_workers)
                 if transcode_workers else None)
    failed = threading.Event()
    futures: Dict[Any, tuple] = {}

    def should_stop() -> bool:
        return failed.is_set() or (ctx is not None and ctx.cancelled)

    def run(item, lease):
        try:
            task = item["task"]
            return task[0](*task[1:])
        finally:
            lease.release()
            if local_cap is not None:
                local_cap.release()

    def on_done(future):
        if future.cancelled() or future.exception() is not None:
            failed.set()

    for item in plan:
        try:
            if local_cap is not None:
                while not local_cap.acquire(timeout=0.25):
                    if should_stop():
                        raise BudgetAcquireAborted("stopped waiting for a worker")
            try:
                lease = budget.acquire(item["cost"], should_abort=should_stop)
            except BudgetAcquireAborted:
                if local_cap is not None:
                    local_cap.release()
                raise
        except BudgetAcquireAborted:
            break
        future = executor.submit(run, item, lease)
        future.add_done_callback(on_done)
        futures[future] = item["task"]

    if ctx is not None and ctx.cancelled:
        raise JobCancelled("Transcode cancelled while waiting for CPU budget")
    return futures


def generate_clipped_transcoded_merged_mp4s(
        input_video: Union[str, Path],
        ffmpeg_executable: str,
        ffprobe_executable: str,
        clippings: List[Dict[str, Any]],
        video_fps: fractions.Fraction,
        video_templates: List[Dict[str, Any]],
        output_dir_base: Path,
        args_duration: Optional[int],
        transcode_workers: Optional[int],
        temp_dir: Path,
        audio_norm: bool,
        loudnorm_analysis_results: Dict[str, Any],
        loudnorm_settings: Dict[str, Any],
        clip_gop_offsets: List[float] = [],
        esam_events: List[Dict[str, Any]] = []
) -> Tuple[Dict[str, str], Optional[str], float, Optional[Path], List[Dict[str, Any]]]:
    """
    Orchestrates the parallel transcoding and merging of video clips.

    This function breaks down the input video into specified clippings, transcodes each clip
    into multiple resolutions (and an audio stream) in parallel using a ThreadPoolExecutor,
    and then merges the transcoded segments back into continuous tracks.

    Key features:
    - Parallel Processing: Clips encode concurrently, as many at once as the
      machine-wide CPU budget allows (see :mod:`hls_toolkit.cpu_budget`).
    - Multi-Resolution: Generates video streams for all resolutions defined in `video_templates`.
    - Audio Handling: Extracts and optionally normalizes audio (loudnorm) in a separate pass.
    - ESAM Remapping: Adjusts ESAM SCTE-35 cue points to align with the new compacted timeline.
    - GOP Alignment: Applies GOP offsets to ensure consistent IDR frame cadence across clips.

    Args:
        input_video: Path to the source video file.
        ffmpeg_executable: Path to the FFmpeg binary.
        ffprobe_executable: Path to the FFprobe binary.
        clippings: List of dicts defining start/end timecodes for each clip.
        video_fps: Frame rate of the input video (as a Fraction).
        video_templates: List of dicts defining output resolutions and encoding parameters.
        output_dir_base: Base directory for outputs (used for reference, not direct writing here).
        args_duration: Optional limit on the total duration (not directly used here but passed for context).
        transcode_workers: Optional cap on how many clip encodes this job runs at
            once. ``None`` leaves concurrency entirely to the CPU budget.
        temp_dir: Directory for temporary files (transcoded clips).
        audio_norm: Boolean indicating if audio normalization should be applied.
        loudnorm_analysis_results: Results from a prior loudnorm analysis pass.
        loudnorm_settings: Configuration settings for audio normalization.
        clip_gop_offsets: List of time offsets to shift GOP boundaries for each clip.
        esam_events: List of ESAM events to be remapped to the output timeline.

    Returns:
        A tuple containing:
        - merged_video_paths (Dict[str, str]): Map of resolution name to path of the merged video MP4.
        - merged_audio_path (Optional[str]): Path to the merged audio AAC file.
        - final_duration (float): Total duration of the merged content in seconds.
        - temp_clipping_dir (Optional[Path]): Path to the temporary directory created.
        - remapped_esam_events (List[Dict[str, Any]]): ESAM events adjusted for the merged timeline.
    """
    ctx0 = current_context()
    if ctx0 is not None:
        ctx0.metadata["video_clip_count"] = len(clippings)
        ctx0.set_stage("TRANSCODING", 0.0)
    temp_clipping_dir = Path(tempfile.mkdtemp(prefix="hls_clips_transcoded_", dir=temp_dir))
    log().info(f"Created temporary directory for transcoded clips: {temp_clipping_dir}")
    abs_input_video = Path(os.path.abspath(str(input_video)))
    remapped_esam_events = remap_esam_events_for_merged_clips(esam_events, clippings,
                                                              float(video_fps))
    tasks = []
    total_merged_duration = 0.0
    for i, clip in enumerate(clippings):
        start_seconds = timecode_to_seconds(clip["StartTimecode"], str(video_fps))
        end_seconds = timecode_to_seconds(clip["EndTimecode"], str(video_fps))
        clip_duration = float(end_seconds) - float(start_seconds)
        clip_start_compacted = total_merged_duration
        clip_end_compacted = total_merged_duration + clip_duration
        tasks.append((
            _transcode_clip_with_single_command,
            i,
            video_templates,
            abs_input_video,
            temp_clipping_dir,
            ffmpeg_executable,
            start_seconds,
            end_seconds,
            audio_norm,
            loudnorm_analysis_results,
            loudnorm_settings,
            remapped_esam_events,
            clip_start_compacted,
            clip_end_compacted,
            video_fps,
            clip_gop_offsets[i] if i < len(clip_gop_offsets) else 0.0,
            "video"))
        tasks.append((
            _transcode_clip_with_single_command,
            i, [],
            abs_input_video,
            temp_clipping_dir,
            ffmpeg_executable,
            start_seconds,
            end_seconds,
            audio_norm,
            loudnorm_analysis_results,
            loudnorm_settings, [],
            0,
            0,
            video_fps,
            0.0,
            "audio"))
        total_merged_duration = clip_end_compacted

    video_clips_per_res = {res["name"]: [] for res in video_templates}
    audio_clips = []
    ctx = current_context()
    completed = 0
    total_tasks = len(tasks)

    budget = get_shared_budget()
    video_cost = budget.clamp(estimate_video_task_cores(video_templates))
    plan = _schedule_transcode_tasks(tasks, video_cost)
    clips_at_once = max(1, budget.budget // video_cost)
    log().info(
        f"Transcoding {total_tasks} task(s) across {len(clippings)} clip(s). "
        f"CPU budget {budget.budget} core(s), shared with every job on this "
        f"server; each clip's video encode reserves {video_cost} core(s), so up "
        f"to {clips_at_once} clip(s) encode at once when this job runs alone"
        + (f", capped at {transcode_workers} by --transcode-workers"
           if transcode_workers else "") + ".")

    # Every task gets its own thread so the pool never limits concurrency — the
    # CPU budget does. Tasks are released into it longest clip first.
    # The pool's threads need the job bound too, so their FFmpeg output lands in
    # this job's ffmpeg.log rather than the root logger.
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, total_tasks),
            initializer=bind_context, initargs=(ctx,)) as executor:
        futures = _dispatch_within_budget(executor, plan, budget, ctx,
                                          transcode_workers)
        try:
            for future in concurrent.futures.as_completed(futures):
                try:
                    result = future.result()
                except JobCancelled:
                    log().warning("Transcode cancelled — stopping remaining tasks")
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise
                except subprocess.CalledProcessError as exc:
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise TranscodeError(
                        f"FFmpeg failed during transcode (exit {exc.returncode}). "
                        f"See ffmpeg.log for the full output.",
                        stage="TRANSCODING") from exc
                except Exception as exc:
                    log().error(f"A transcoding task generated an exception: {exc}",
                                exc_info=True)
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise TranscodeError(f"Transcode task failed: {exc}",
                                         stage="TRANSCODING") from exc

                if result["type"] == "video":
                    for res_name, path in result["outputs"].items():
                        video_clips_per_res[res_name].append(Path(path))
                elif result["type"] == "audio":
                    audio_clips.append(Path(result["output"]))

                completed += 1
                if ctx is not None:
                    ctx.set_stage("TRANSCODING", completed / total_tasks)
                log().info(f"Transcode progress: {completed}/{total_tasks} task(s) done")
        finally:
            _clear_clip_progress(ctx)

    missing = [name for name, clips in video_clips_per_res.items() if not clips]
    if missing:
        raise TranscodeError(
            f"No transcoded clips were produced for: {', '.join(missing)}",
            stage="TRANSCODING")

    if ctx is not None:
        ctx.set_stage("MERGING", 0.0)
    merged_video_paths = _merge_transcoded_clips(ffmpeg_executable, video_clips_per_res,
                                                 temp_clipping_dir)
    audio_clips_for_merge = {"audio": audio_clips}
    merged_audio_paths = _merge_transcoded_clips(ffmpeg_executable, audio_clips_for_merge,
                                                 temp_clipping_dir)
    merged_audio_path = merged_audio_paths.get("audio")
    final_duration = 0.0
    if merged_video_paths:
        ref_path = next(iter(merged_video_paths.values()))
        final_duration = get_video_info(ffprobe_executable, ref_path)["duration"]
    return (merged_video_paths,
            merged_audio_path,
            final_duration,
            temp_clipping_dir,
            remapped_esam_events)


def get_actual_duration_frames(ffprobe_executable: str,
                               video_file: str,
                               fps: Union[float, fractions.Fraction]) -> int:
    """Gets the precise duration of a video file in seconds and converts it to a frame count."""
    cmd = [
        ffprobe_executable,
        '-v',
        'error',
        '-show_entries',
        'format=duration',
        '-of',
        'default=noprint_wrappers=1:nokey=1',
        video_file]
    duration_str = _run_ffprobe_command(
        cmd, log_prefix=f"Get duration for {os.path.basename(video_file)}")
    if not duration_str:
        return 0
    try:
        return math.ceil(float(duration_str) * float(fps))
    except (ValueError, TypeError):
        return 0


def validate_clippings(clippings: List[Dict[str, Any]], video_fps: fractions.Fraction) -> None:
    """Validates a list of clipping configurations to ensure they have positive durations."""
    for i, clip in enumerate(clippings):
        try:
            s_check = timecode_to_seconds(clip["StartTimecode"], str(video_fps))
            e_check = timecode_to_seconds(clip["EndTimecode"], str(video_fps))
            if e_check <= s_check:
                raise ValueError("Duration must be positive.")
        except Exception as e:
            raise ValueError(f"Invalid timecode format in clipping {i}: {e}") from e


def _get_h264_profile_idc(profile_name: str) -> str:
    """Maps an H.264 profile name to its hexadecimal profile_idc value."""
    profile_name = str(profile_name).lower()
    if profile_name in ('100', 'high'):
        return "64"
    if profile_name in ('77', 'main'):
        return "4d"
    return "42"


def _get_video_stream_details(ffprobe_path: str, file_path: Union[str, Path]) -> Dict[str, Any]:
    """Uses ffprobe to get detailed video stream information."""
    cmd = [
        ffprobe_path,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=profile,level,avg_frame_rate,codec_name,codec_tag_string",
        "-of",
        "json",
        str(file_path)]
    try:
        result_json = _run_ffprobe_command(cmd, log_prefix="FFprobe video stream details")
        if not result_json:
            return {}
        stream_data = json.loads(result_json).get("streams", [{}])[0]
        if "avg_frame_rate" in stream_data:
            try:
                stream_data["avg_frame_rate"] = float(
                    fractions.Fraction(stream_data["avg_frame_rate"]))
            except (ValueError, ZeroDivisionError):
                stream_data["avg_frame_rate"] = 25.0
        return stream_data
    except (subprocess.CalledProcessError, json.JSONDecodeError, IndexError) as e:
        log().warning(f"Could not get video stream details for {file_path}: {e}")
        return {}


def _generate_hls_for_resolution(res_data: Dict[str, Any],
                                 merged_video_path: Union[str, Path],
                                 merged_audio_path: Optional[Union[str, Path]],
                                 ffmpeg_executable: str,
                                 total_merged_duration: float,
                                 output_dir: Path,
                                 hls_settings: Dict[str, Any],
                                 merged_force_times_str: str = '',
                                 frame_rate: Union[str, float, fractions.Fraction] = '') -> bool:
    """Generates HLS playlists and segments for a single video resolution."""
    if not merged_video_path or not Path(merged_video_path).exists():
        return False

    cmd = [ffmpeg_executable, "-y", "-thread_queue_size", "1024"]
    if total_merged_duration > 0:
        cmd.extend(["-t", str(total_merged_duration)])
    cmd.extend(["-i", str(merged_video_path)])
    if merged_audio_path and Path(merged_audio_path).exists():
        cmd.extend(["-i", str(merged_audio_path)])
        cmd.extend(['-map', '0:v:0', '-c:v', 'copy', '-map', '1:a:0', '-c:a', 'copy',
                    '-c:d', 'none'])
    else:
        cmd.extend(['-map', '0:v:0', '-c:v', 'copy', '-an', '-c:d', 'none'])

    hls_args = [
        "-f",
        "hls",
        "-hls_time",
        str(hls_settings.get("hls_time", 6)),
        "-start_number",
        "1",
        "-hls_playlist_type",
        hls_settings.get("hls_playlist_type", "vod"),
        "-hls_flags",
        hls_settings.get("hls_flags", "independent_segments"),
        "-hls_segment_type",
        hls_settings.get("hls_segment_type", "mpegts"),
        "-hls_segment_filename",
        str(output_dir / f'channel_{res_data["name"]}_%05d.ts'),
        str(output_dir / f'channel_{res_data["name"]}.m3u8')]
    if merged_force_times_str:
        hls_args.insert(4, "-hls_force_times")
        hls_args.insert(5, merged_force_times_str)
    cmd.extend(hls_args)
    try:
        _run_ffmpeg_command_with_logging(
            cmd, log_prefix=f'HLS generation for {res_data["name"]}')
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        log().error(f'HLS generation failed for {res_data["name"]}: {e}')
        return False


def generate_thumbnails(ffmpeg_executable: str,
                        input_video: Union[str, Path],
                        output_dir: Path) -> None:
    """Generates thumbnails from the input video."""
    thumbnails_output_path = output_dir / "thumbnails"
    os.makedirs(thumbnails_output_path, exist_ok=True)
    thumbnail_cmd = [
        ffmpeg_executable,
        "-y",
        "-i",
        str(input_video),
        "-vf",
        "fps=1/10,scale=320:180",
        "-q:v",
        "80",
        str(thumbnails_output_path / "thumb_%04d.jpg")]
    _run_ffmpeg_command_with_logging(thumbnail_cmd, log_prefix="Thumbnail generation")
