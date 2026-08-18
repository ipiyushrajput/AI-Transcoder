import logging
import math
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple, Dict

from hls_toolkit.time_utils import timecode_to_seconds


@dataclass
class VttCue:
    start: float
    end: float
    text: str
    id: Optional[str] = None
    settings: Optional[str] = None

    def __str__(self):
        start_str = _format_time_string(self.start)
        end_str = _format_time_string(self.end)
        lines = list(filter(None, [self.id, f"{start_str} --> {end_str}", self.text]))
        return "\n".join(lines)


def _parse_time_string(time_str: str) -> float:
    match_ms = re.match("^(?:(\\d{2,})?:)?(\\d{2}):(\\d{2}\\.\\d{3})", time_str)
    if match_ms:
        h_str, m_str, s_ms_str = match_ms.groups()
        h = int(h_str) if h_str else 0
        m = int(m_str)
        s = float(s_ms_str)
        return float(h) * 3600 + float(m) * 60 + float(s)
    raise ValueError(f"Invalid time string format for VTT cue: {time_str}. "
                     "Expected (HH:)MM:SS.mmm")


def _format_time_string(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    total_milliseconds = int(round(seconds * 1000))
    hours = total_milliseconds // 3600000
    total_milliseconds %= 3600000
    minutes = total_milliseconds // 60000
    total_milliseconds %= 60000
    secs = total_milliseconds // 1000
    milli = total_milliseconds % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milli:03d}"


def parse_vtt_file(vtt_path: Path, video_fps: float = 29.97002997002997) -> List[VttCue]:
    """
    Parses a VTT file and returns a list of VttCue objects.
    Robustly handles cue IDs and settings on the time line.
    """
    cues = []
    if not vtt_path.exists():
        logging.warning(f"VTT file not found: {vtt_path}")
        return cues

    with open(vtt_path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    if not lines or not lines[0].strip().startswith("WEBVTT"):
        logging.error(f"Invalid VTT file header for {vtt_path}. Expected 'WEBVTT'.")
        return cues

    current_cue_id = None
    current_cue_time_line = None
    current_cue_text_lines = []

    def flush_current_cue():
        nonlocal current_cue_id
        nonlocal current_cue_text_lines
        nonlocal current_cue_time_line
        if not current_cue_time_line:
            return
        try:
            try:
                parts = current_cue_time_line.split("-->")
                start_str = parts[0].strip()
                end_str_and_settings = parts[1].strip()
                settings = None
                match_settings = re.match("([\\d:.]+)\\s*(.*)", end_str_and_settings)
                if match_settings:
                    end_str = match_settings.group(1)
                    settings = match_settings.group(2) if match_settings.group(2) else None
                else:
                    end_str = end_str_and_settings
                start_sec = _parse_time_string(start_str)
                end_sec = _parse_time_string(end_str)
                cues.append(VttCue(id=current_cue_id,
                                   start=start_sec,
                                   end=end_sec,
                                   text="\n".join(current_cue_text_lines),
                                   settings=settings))
            except Exception as e:
                logging.error(f"Error parsing cue from {vtt_path}: "
                              f"{current_cue_time_line} - {e}. Skipping cue.")
        finally:
            current_cue_id = None
            current_cue_time_line = None
            current_cue_text_lines = []

    for line in lines[1:]:
        line = line.strip()
        if not line:
            flush_current_cue()
            continue
        if "-->" in line:
            flush_current_cue()
            current_cue_time_line = line
        elif (not current_cue_time_line
              and not line.startswith("NOTE")
              and "X-TIMESTAMP-MAP" not in line):
            current_cue_id = line
        elif line.startswith("NOTE") or "X-TIMESTAMP-MAP" in line:
            continue
        elif line.isdigit() and current_cue_time_line:
            flush_current_cue()
            current_cue_id = line
        else:
            current_cue_text_lines.append(line)

    flush_current_cue()
    return cues


def clip_and_offset_vtt_cues(all_cues: List[VttCue],
                             clip_start_abs: float,
                             clip_end_abs: float) -> List[VttCue]:
    """
    Clips a list of VttCues to a specific absolute time range and offsets their timestamps
    to be relative to the start of that clipping (i.e., new start time is 0 for the clip).

    Args:
        all_cues: A list of all VttCue objects from the original source.
        clip_start_abs: The absolute start time (in seconds) of the desired clip.
        clip_end_abs: The absolute end time (in seconds) of the desired clip.

    Returns:
        A list of VttCue objects, clipped to the range and with timestamps relative to `clip_start_abs`.
    """
    clipped_cues = []
    for cue in all_cues:
        overlap_start = max(cue.start, clip_start_abs)
        overlap_end = min(cue.end, clip_end_abs)
        if overlap_start < overlap_end:
            clipped_cues.append(VttCue(id=cue.id,
                                       start=overlap_start,
                                       end=overlap_end,
                                       text=cue.text,
                                       settings=cue.settings))
    return clipped_cues


def merge_contiguous_cues(cues: List[VttCue], time_tolerance: float = 0.05) -> List[VttCue]:
    """
    Merges contiguous VttCues in a sorted list, mimicking MediaConvert's aggressive merging.
    Contiguous means the end time of one cue is very close to the start time of the next,
    or there's a slight overlap.
    """
    if not cues:
        return []

    merged_cues = []
    current_cue = cues[0]
    for next_cue in cues[1:]:
        if next_cue.start - current_cue.end <= time_tolerance:
            current_cue.end = max(current_cue.end, next_cue.end)
            current_cue.text += "\n" + next_cue.text
        else:
            merged_cues.append(current_cue)
            current_cue = next_cue
    merged_cues.append(current_cue)
    return merged_cues


def generate_merged_subtitle_file(input_subtitle_path: Path,
                                  ffmpeg_executable,
                                  clippings: List[Dict],
                                  video_fps: float,
                                  output_dir_base: Path,
                                  temp_dir: Path) -> Tuple[Optional[Path], Optional[Path], float]:
    """
    Generates a single, globally merged VTT file from the source VTT and clippings,
    applying MediaConvert-like cue merging.
    Returns the path to the merged VTT, the temp dir used, and the total duration.
    """
    temp_subtitle_dir = None
    merged_subtitle_path = None
    cumulative_timeline_offset = 0.0
    if not input_subtitle_path or not input_subtitle_path.exists() or not clippings:
        logging.info("No input subtitle file, or no clippings provided for subtitle merging.")
        return (None, None, 0.0)
    try:
        Path(temp_dir).mkdir(parents=True, exist_ok=True)
        temp_subtitle_dir = Path(tempfile.mkdtemp(prefix="hls_sub_clips_", dir=temp_dir))
        logging.info(f"Created temporary directory for subtitle clips: {temp_subtitle_dir}")
        all_original_cues = parse_vtt_file(input_subtitle_path, video_fps=video_fps)
        if not all_original_cues:
            logging.warning(f"No cues found in original subtitle file: {input_subtitle_path}. "
                            "Skipping subtitle processing.")
            return (None, None, 0.0)

        global_timeline_cues = []
        for i, clip in enumerate(clippings):
            start_timecode_str = clip.get("StartTimecode")
            end_timecode_str = clip.get("EndTimecode")
            if not start_timecode_str or not end_timecode_str:
                logging.warning(f"Warning: Clipping {i + 1} is missing StartTimecode or "
                                "EndTimecode. Skipping subtitle part.")
                continue
            clip_start_abs = timecode_to_seconds(start_timecode_str, str(video_fps))
            clip_end_abs = timecode_to_seconds(end_timecode_str, str(video_fps))
            clip_duration = clip_end_abs - clip_start_abs
            if clip_duration <= 0:
                logging.warning(f"Warning: Clipping {i + 1} has non-positive duration "
                                f"({clip_duration}s). Skipping subtitle part.")
                continue
            logging.info(f"Processing subtitle clipping: Clip {i + 1} - Original "
                         f"start={start_timecode_str}, end={end_timecode_str}, "
                         f"duration={float(clip_duration):.3f}s")
            clipped_cues_for_this_segment = clip_and_offset_vtt_cues(
                all_original_cues, clip_start_abs, clip_end_abs)
            for clipped_cue in clipped_cues_for_this_segment:
                final_start = clipped_cue.start - clip_start_abs + cumulative_timeline_offset
                final_end = clipped_cue.end - clip_start_abs + cumulative_timeline_offset
                global_timeline_cues.append(VttCue(id=clipped_cue.id,
                                                   start=final_start,
                                                   end=final_end,
                                                   text=clipped_cue.text,
                                                   settings=clipped_cue.settings))
            cumulative_timeline_offset += clip_duration

        if not global_timeline_cues:
            logging.info("No valid subtitle cues generated after clipping. "
                         "Creating an empty merged VTT.")
            merged_subtitle_path = temp_subtitle_dir / "merged_subtitles.vtt"
            with open(merged_subtitle_path, "w", encoding="utf-8") as f:
                f.write("WEBVTT\n")
            return (merged_subtitle_path, temp_subtitle_dir, 0.0)

        global_timeline_cues.sort(key=lambda c: c.start)
        final_merged_cues_after_processing = merge_contiguous_cues(global_timeline_cues)
        merged_subtitle_path = temp_subtitle_dir / "merged_subtitles.vtt"
        with open(merged_subtitle_path, "w", encoding="utf-8") as f:
            f.write("WEBVTT\n\n")
            for cue in final_merged_cues_after_processing:
                f.write(str(cue) + "\n\n")
        logging.info("Globally merged subtitle file (with MediaConvert-like merging) "
                     f"created: {merged_subtitle_path}")
        return (merged_subtitle_path, temp_subtitle_dir, cumulative_timeline_offset)
    except Exception as e:
        logging.error(f"An error occurred during subtitle clipping or merging: {e}",
                      exc_info=True)
        if temp_subtitle_dir and temp_subtitle_dir.exists():
            shutil.rmtree(temp_subtitle_dir)
        return (None, None, 0.0)


def segment_vtt_for_hls(merged_vtt_path: Path,
                        output_dir: Path,
                        sub_lang: str,
                        total_merged_duration: float,
                        video_segments: List[Tuple[float, float, str, int]],
                        global_stream_mpegts_start: Optional[int] = None,
                        vtt_index: int = 1) -> Tuple[bool, Optional[Path]]:
    """
    Segments a globally merged VTT file into multiple HLS-compatible VTT segments,
    mimicking MediaConvert's behavior, aligning precisely with video segments.
    """
    hls_playlist_entries = []

    if not merged_vtt_path or not merged_vtt_path.exists():
        logging.error(f"Merged VTT file not found for segmentation: {merged_vtt_path}")
        return (False, None)

    global_timeline_cues = parse_vtt_file(merged_vtt_path)

    if not global_timeline_cues:
        logging.warning(f"No cues found in merged VTT file {merged_vtt_path}. "
                        "Generating empty HLS playlist for VTT.")
        hls_playlist_path = output_dir / f"channel_{sub_lang}-vtt-{vtt_index}.m3u8"
        with open(hls_playlist_path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            f.write("#EXT-X-VERSION:3\n")
            target_duration_val = 6.0
            if video_segments:
                target_duration_val = max([s[1] for s in video_segments])
            f.write(f"#EXT-X-TARGETDURATION:{math.ceil(target_duration_val)}\n")
            f.write("#EXT-X-PLAYLIST-TYPE:VOD\n")
            f.write("#EXT-X-ENDLIST\n")
        return (True, hls_playlist_path)

    segment_boundaries = [s[0] for s in video_segments]

    if not segment_boundaries or len(segment_boundaries) < 2:
        logging.error("Invalid video_segments provided for VTT segmentation. Cannot proceed.")
        return (False, None)

    logging.debug(f"Final VTT segment boundaries based on video: {segment_boundaries}")

    segment_index = 0
    hls_sub_playlist_path = output_dir / f"channel_{sub_lang}-vtt-{vtt_index}.m3u8"

    MIN_OVERLAP_FOR_INCLUSION = 0.1

    for i in range(len(video_segments)):
        current_video_segment_start_time = video_segments[i][0]
        video_segment_end_time = video_segments[i][1]
        actual_segment_duration = video_segment_end_time - current_video_segment_start_time

        if actual_segment_duration <= 1e-06:
            continue

        segment_vtt_filename = f"channel_{sub_lang}-vtt-{vtt_index}_{segment_index + 1:05d}.vtt"
        segment_vtt_filepath = output_dir / segment_vtt_filename

        segment_cues = []
        for cue in global_timeline_cues:
            overlap_start = max(cue.start, current_video_segment_start_time)
            overlap_end = min(cue.end, video_segment_end_time)

            if overlap_end - overlap_start >= MIN_OVERLAP_FOR_INCLUSION:
                segment_cues.append(VttCue(
                    id=cue.id,
                    start=overlap_start - current_video_segment_start_time,
                    end=overlap_end - current_video_segment_start_time,
                    text=cue.text,
                    settings=cue.settings))

        segment_cues.sort(key=lambda c: c.start)

        with open(segment_vtt_filepath, "w", encoding="utf-8") as f:
            f.write("WEBVTT\n")

            if global_stream_mpegts_start is not None:
                segment_mpegts_start = global_stream_mpegts_start + int(
                    current_video_segment_start_time * 90000)
            else:
                segment_mpegts_start = int(current_video_segment_start_time * 90000)

            f.write(f"X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:{segment_mpegts_start}\n\n")
            for cue in segment_cues:
                f.write(str(cue) + "\n\n")

        hls_playlist_entries.append(f"#EXTINF:{actual_segment_duration:.6f},\n")
        hls_playlist_entries.append(f"{segment_vtt_filename}\n")

        segment_index += 1

    target_duration_val = 6.0
    if video_segments:
        target_duration_val = max([s[1] - s[0] for s in video_segments])

    with open(hls_sub_playlist_path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        f.write("#EXT-X-VERSION:3\n")
        f.write(f"#EXT-X-TARGETDURATION:{math.ceil(target_duration_val)}\n")
        f.write("#EXT-X-MEDIA-SEQUENCE:1\n")
        f.write("#EXT-X-PLAYLIST-TYPE:VOD\n")
        f.writelines(hls_playlist_entries)
        f.write("#EXT-X-ENDLIST\n")

    logging.info(f"VTT HLS playlist generated: {hls_sub_playlist_path}")
    return (True, hls_sub_playlist_path)
