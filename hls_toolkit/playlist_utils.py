import logging
import re
from pathlib import Path
from typing import List, Tuple, Dict, Any

from hls_toolkit.job_context import log


def is_master_playlist(lines: List[str]) -> bool:
    return any(ln.strip().startswith("#EXT-X-STREAM-INF") for ln in lines)


def find_variants(lines: List[str], basepath: Path) -> List[Tuple[str, Path]]:
    variants = []
    i = 0
    while i < len(lines):
        ln = lines[i].strip()
        if ln.startswith("#EXT-X-STREAM-INF"):
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j < len(lines):
                uri = lines[j].strip()
                variants.append((uri, (basepath / uri).resolve()))
            i = j + 1
        else:
            i += 1
    return variants


def parse_variant_segments(m3u8_path: Path) -> Tuple[List[str], List[Tuple[float, float, str, int]], float, float]:
    text = m3u8_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    segments = []
    total = 0.0
    max_duration = 0.0
    i = 0
    while i < len(lines):
        ln = lines[i].strip()
        if ln.startswith("#EXTINF:"):
            try:
                dur = float(ln.split(":", 1)[1].split(",", 1)[0])
            except ValueError as e:
                log().error(f"Error parsing EXTINF duration from line '{ln.strip()}': {e}")
                dur = 0.0
            if i + 1 < len(lines):
                segfile = lines[i + 1].strip()
                segments.append((total, total + dur, segfile, i + 1))
            total += dur
            max_duration = max(max_duration, dur)
            i += 2
        else:
            i += 1
    return (lines, segments, total, max_duration)


def bump_segment_lines(segments: List[Tuple], from_line: int, delta: int = 1):
    for k in range(len(segments)):
        sstart, send, sfile, sline = segments[k]
        if sline >= from_line:
            segments[k] = (sstart, send, sfile, sline + delta)


def normalize_float(x: float, prec: int = 3) -> float:
    return round(float(x), prec)


def has_marker_near(lines: List[str], insert_index: int, prefixes: List[str], window: int = 2) -> bool:
    start = max(0, insert_index - window)
    end = min(len(lines), insert_index + window + 1)
    for i in range(start, end):
        line = lines[i].strip()
        for p in prefixes:
            if line.startswith(p):
                return True
    return False


def create_master_playlist(master_playlist_path: str,
                           selected_resolutions_data: List[Dict[str, Any]],
                           merged_mp4_paths_by_resolution: Dict[str, str],
                           ffprobe_executable: str,
                           video_fps: float,
                           subtitle_file: str,
                           hls_sub_playlist_path: Path,
                           sub_lang: str,
                           _get_video_stream_details,
                           _get_h264_profile_idc):
    """Generates the HLS master playlist."""
    with open(master_playlist_path, "w") as master_pl_file:
        master_pl_file.write("#EXTM3U\n")
        master_pl_file.write("#EXT-X-VERSION:3\n")
        master_pl_file.write("#EXT-X-INDEPENDENT-SEGMENTS\n")
        has_subtitles = subtitle_file is not None and hls_sub_playlist_path is not None
        for res_data in selected_resolutions_data:
            res_name = res_data["name"]
            if res_name not in merged_mp4_paths_by_resolution:
                continue
            merged_mp4_path = merged_mp4_paths_by_resolution[res_name]
            stream_details = _get_video_stream_details(ffprobe_executable, merged_mp4_path)
            video_codec_str = None
            if stream_details:
                codec_name = stream_details.get("codec_name")
                if codec_name == "hevc":
                    video_codec_str = "hvc1.1.6.L93.B0"
                elif codec_name == "h264":
                    profile_raw = str(stream_details.get("profile", "Main"))
                    level = stream_details.get("level", 31)
                    profile_idc = _get_h264_profile_idc(profile_raw)
                    level_hex = f"{level:02x}"
                    constraint = "40" if profile_idc == "4d" else "00"
                    video_codec_str = f"avc1.{profile_idc}{constraint}{level_hex}"
            bandwidth = 0
            if "bitrate" in res_data and res_data["bitrate"]:
                bandwidth = int(res_data["bitrate"].replace("M", "000000").replace("k", "000"))
            elif "codec_params" in res_data:
                match = re.search("vbv-maxrate=(\\d+)", res_data["codec_params"])
                if match:
                    bandwidth = int(match.group(1)) * 1000
            if bandwidth == 0:
                bandwidth = 2500000
                log().warning(f"Could not determine bandwidth for resolution {res_name}. "
                                f"Defaulting to {bandwidth} bps.")
            avg_bandwidth = bandwidth
            frame_rate_val = stream_details.get("avg_frame_rate") if stream_details else video_fps
            frame_rate = f"{frame_rate_val:.3f}" if frame_rate_val else f"{video_fps:.3f}"
            stream_inf_parts = [
                f"BANDWIDTH={bandwidth}",
                f"AVERAGE-BANDWIDTH={avg_bandwidth}"]
            if video_codec_str:
                audio_codec_str = "mp4a.40.2"
                full_codec_string = f'"{video_codec_str},{audio_codec_str}"'
                stream_inf_parts.append(f"CODECS={full_codec_string}")
            else:
                log().warning(f"Could not determine video codec for {res_name}. "
                                "Omitting CODECS attribute.")
            stream_inf_parts.extend([
                f'RESOLUTION={res_data["width"]}x{res_data["height"]}',
                f"FRAME-RATE={frame_rate}"])
            stream_inf_line = f'#EXT-X-STREAM-INF:{",".join(stream_inf_parts)}'
            if has_subtitles:
                stream_inf_line += ',SUBTITLES="subs"'
            master_pl_file.write(stream_inf_line + "\n")
            master_pl_file.write(f"channel_{res_name}.m3u8\n")
        if has_subtitles:
            master_pl_file.write(
                '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",DEFAULT=YES,'
                'AUTOSELECT=YES,FORCED=NO,LANGUAGE="'
                f'{sub_lang}",URI="{hls_sub_playlist_path.name}"\n')
