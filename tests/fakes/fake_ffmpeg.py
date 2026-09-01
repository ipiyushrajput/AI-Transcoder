#!/usr/bin/env python3
"""A stand-in for the custom FFmpeg/FFprobe build, for tests only.

It parses the same command lines the pipeline emits and writes plausible output
files, so the whole workflow can be exercised on a machine that does not have
the libwz264/libwz265 build. It encodes nothing.

Invoked as ``fake_ffmpeg.py`` (ffmpeg role) or ``fake_ffprobe.py`` (ffprobe role).
"""
import json
import os
import random
import sys
from pathlib import Path

DURATION = float(os.getenv("FAKE_MEDIA_DURATION", "60"))
FPS = os.getenv("FAKE_MEDIA_FPS", "30000/1001")
SEGMENT_SECONDS = 6.0


def arg_after(argv, flag):
    if flag in argv:
        index = argv.index(flag)
        if index + 1 < len(argv):
            return argv[index + 1]
    return None


def run_ffprobe(argv):
    entries = " ".join(argv)
    if "packet=pts_time" in entries:
        print("1.400000")
        return 0
    if "stream=profile,level" in entries:
        print(json.dumps({"streams": [{"profile": "High", "level": 40,
                                       "avg_frame_rate": FPS,
                                       "codec_name": "h264",
                                       "codec_tag_string": "avc1"}]}))
        return 0
    if "format=duration" in entries and "-of" in argv and arg_after(argv, "-of") == "json":
        print(json.dumps({"streams": [{"avg_frame_rate": FPS}],
                          "format": {"duration": f"{DURATION:.6f}"}}))
        return 0
    if "format=duration" in entries:
        print(f"{DURATION:.6f}")
        return 0
    print(json.dumps({"streams": [], "format": {"duration": f"{DURATION:.6f}"}}))
    return 0


def run_ffmpeg(argv):
    # Loudnorm analysis: the pipeline scrapes a JSON block from stderr.
    if any("loudnorm=print_format=json" in a for a in argv):
        sys.stderr.write("[Parsed_loudnorm_0 @ 0x1] \n")
        sys.stderr.write(json.dumps({
            "input_i": "-19.51", "input_tp": "-1.20", "input_lra": "6.30",
            "input_thresh": "-30.10", "output_i": "-23.00", "target_offset": "0.42",
        }, indent=2) + "\n")
        return 0

    # HLS packaging: -f hls ... <segment pattern> <playlist>
    if "hls" in argv and arg_after(argv, "-f") == "hls":
        playlist = Path(argv[-1])
        pattern = arg_after(argv, "-hls_segment_filename")
        total = float(arg_after(argv, "-t") or DURATION)
        count = max(1, int(total // SEGMENT_SECONDS))
        playlist.parent.mkdir(parents=True, exist_ok=True)
        lines = ["#EXTM3U\n", "#EXT-X-VERSION:3\n",
                 f"#EXT-X-TARGETDURATION:{int(SEGMENT_SECONDS)}\n",
                 "#EXT-X-MEDIA-SEQUENCE:1\n", "#EXT-X-PLAYLIST-TYPE:vod\n"]
        for i in range(1, count + 1):
            segment = Path(pattern % i) if pattern else playlist.with_suffix(f".{i}.ts")
            segment.parent.mkdir(parents=True, exist_ok=True)
            segment.write_bytes(os.urandom(2048))
            lines.append(f"#EXTINF:{SEGMENT_SECONDS:.6f},\n{segment.name}\n")
        lines.append("#EXT-X-ENDLIST\n")
        playlist.write_text("".join(lines))
        _emit_progress(total)
        return 0

    # Thumbnails: -vf fps=... <dir>/thumb_%04d.jpg
    if any("thumb_" in str(a) for a in argv):
        pattern = argv[-1]
        for i in range(1, 4):
            path = Path(pattern % i)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(os.urandom(512))
        return 0

    # Everything else writes one or more output files: clip transcode, concat merge.
    outputs = [Path(a) for a in argv
               if str(a).endswith((".mp4", ".m4s")) and not _is_input_value(argv, a)]
    for path in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(os.urandom(4096))
    _emit_progress(float(arg_after(argv, "-t") or DURATION))
    return 0


def _is_input_value(argv, value):
    """True when `value` is the argument of a -i flag (an input, not an output)."""
    for i, a in enumerate(argv):
        if a == "-i" and i + 1 < len(argv) and argv[i + 1] == value:
            return True
    return False


def _emit_progress(total_seconds: float):
    """Emit FFmpeg-style progress lines so the progress parser has real input."""
    steps = 4
    for i in range(1, steps + 1):
        elapsed = total_seconds * i / steps
        hours, rem = divmod(elapsed, 3600)
        minutes, seconds = divmod(rem, 60)
        sys.stderr.write(
            f"frame= {int(elapsed * 25):5d} fps= 50 q=28.0 size= {i * 512}kB "
            f"time={int(hours):02d}:{int(minutes):02d}:{seconds:05.2f} "
            f"bitrate=1500.0kbits/s speed=2.0x\n")
        sys.stderr.flush()


def main():
    argv = sys.argv[1:]
    # runpy rewrites sys.argv[0], so the launcher passes the role explicitly.
    role = os.getenv("FAKE_ROLE") or (
        "ffprobe" if "ffprobe" in Path(sys.argv[0]).name else "ffmpeg")
    try:
        return run_ffprobe(argv) if role == "ffprobe" else run_ffmpeg(argv)
    except Exception as e:
        sys.stderr.write(f"fake {role} error: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
