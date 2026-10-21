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
import time
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
    if os.getenv("FAKE_HANG_FFPROBE"):
        time.sleep(3600)                     # test switch: a probe that never returns
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

    # Test switch: the encoder refuses its licence, as an expired one would.
    if os.getenv("FAKE_LICENSE_FAIL") and any(str(a).startswith("libwz") for a in argv):
        sys.stderr.write("[libwz264 @ 0x1] wz license check failed: license expired\n")
        return 1

    # HLS packaging: -f hls ... <segment pattern> <playlist>
    if "hls" in argv and arg_after(argv, "-f") == "hls":
        playlist = Path(argv[-1])
        # Test switches: fail packaging, or "succeed" with a truncated package.
        if os.getenv("FAKE_FAIL_HLS") and os.getenv("FAKE_FAIL_HLS") in playlist.name:
            sys.stderr.write("simulated HLS packaging failure\n")
            return 1
        pattern = arg_after(argv, "-hls_segment_filename")
        total = float(arg_after(argv, "-t") or DURATION)
        if os.getenv("FAKE_TRUNCATE_HLS") and os.getenv("FAKE_TRUNCATE_HLS") in playlist.name:
            total = total / 2
        # Like FFmpeg: full segments, then a shorter last one, summing to `total`.
        count = max(1, int(-(-total // SEGMENT_SECONDS)))
        durations = [SEGMENT_SECONDS] * (count - 1)
        durations.append(max(0.001, total - SEGMENT_SECONDS * (count - 1)))
        playlist.parent.mkdir(parents=True, exist_ok=True)
        lines = ["#EXTM3U\n", "#EXT-X-VERSION:3\n",
                 f"#EXT-X-TARGETDURATION:{int(SEGMENT_SECONDS)}\n",
                 "#EXT-X-MEDIA-SEQUENCE:1\n", "#EXT-X-PLAYLIST-TYPE:vod\n"]
        for i, seg_duration in enumerate(durations, 1):
            segment = Path(pattern % i) if pattern else playlist.with_suffix(f".{i}.ts")
            segment.parent.mkdir(parents=True, exist_ok=True)
            write_ts_segment(segment, pts_seconds=sum(durations[:i - 1]))
            lines.append(f"#EXTINF:{seg_duration:.6f},\n{segment.name}\n")
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
    started = time.time()
    kind = _encode_kind(argv)
    if kind and os.getenv("FAKE_SELF_KILL") == kind:
        import signal
        os.kill(os.getpid(), signal.SIGKILL)     # test switch: as the OOM killer would
    if kind and os.getenv("FAKE_HANG") == kind:
        # Test switch: report a little progress, then hang with no output.
        _emit_progress(1.0)
        time.sleep(3600)
    slow = float(os.getenv("FAKE_SLOW_PROGRESS_SECONDS", "0") or 0)
    if kind == "video" and slow > 0:
        # Test switch: slow but steady, one progress line per second.
        for second in range(1, int(slow) + 1):
            time.sleep(1)
            sys.stderr.write(f"frame= {second * 25:5d} fps= 25 q=28.0 size= 1kB "
                             f"time=00:00:{second:02d}.00 bitrate=1.0kbits/s speed=1x\n")
            sys.stderr.flush()
    _simulate_encode_time(argv)
    for path in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(os.urandom(4096))
    _emit_progress(float(arg_after(argv, "-t") or DURATION))
    _record_timeline(argv, started)
    return 0


def _crc32_mpeg2(data):
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else (crc << 1)
            crc &= 0xFFFFFFFF
    return crc


def _psi_packet(pid, section):
    section = section + _crc32_mpeg2(section).to_bytes(4, "big")
    body = bytes([0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10, 0x00]) + section
    return body + b"\xff" * (188 - len(body))


def write_ts_segment(path, pts_seconds=0.0, video_packets=10):
    """A small MPEG-TS segment shaped like FFmpeg's HLS output: PAT, then a PMT
    on PID 0x1000 listing H.264 on 0x100 and AAC on 0x101 (with an ISO 639
    'und' descriptor, as FFmpeg writes), then video PES packets with a PTS."""
    pat = bytes([0x00, 0xB0, 0x0D, 0x00, 0x01, 0xC1, 0x00, 0x00,
                 0x00, 0x01, 0xF0, 0x00])                        # program 1 -> PMT 0x1000
    es = (bytes([0x1B, 0xE1, 0x00, 0xF0, 0x00])
          + bytes([0x0F, 0xE1, 0x01, 0xF0, 0x06, 0x0A, 0x04]) + b"und" + b"\x00")
    body = bytes([0x00, 0x01, 0xC1, 0x00, 0x00, 0xE1, 0x00, 0xF0, 0x00]) + es
    length = len(body) + 4
    pmt = bytes([0x02, 0xB0 | (length >> 8), length & 0xFF]) + body
    packets = [_psi_packet(0x0000, pat), _psi_packet(0x1000, pmt)]
    pts = int(round((pts_seconds + 1.4) * 90000))
    pts_bytes = bytes([0x21 | ((pts >> 29) & 0x0E), (pts >> 22) & 0xFF,
                       0x01 | ((pts >> 14) & 0xFE), (pts >> 7) & 0xFF,
                       0x01 | ((pts << 1) & 0xFE)])
    pes = b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + pts_bytes
    for n in range(video_packets):
        head = bytes([0x47, (0x40 if n == 0 else 0x00) | 0x01, 0x00, 0x10 | (n & 0x0F)])
        payload = (pes if n == 0 else b"") + os.urandom(184)
        packets.append((head + payload)[:188])
    Path(path).write_bytes(b"".join(packets))


def _encode_kind(argv):
    """'video' / 'audio' for a clip encode, None for anything else."""
    if "-ss" not in argv:
        return None
    if any(str(a).startswith("libwz") for a in argv):
        return "video"
    if "aac" in argv:
        return "audio"
    return None


def _simulate_encode_time(argv):
    """Sleep in proportion to the clip length, so scheduling can be observed.

    Off unless FAKE_SECONDS_PER_MEDIA_MINUTE is set; it uses no CPU, so it
    measures when encodes run, not how fast they would run under contention.
    """
    rate = float(os.getenv("FAKE_SECONDS_PER_MEDIA_MINUTE", "0") or 0)
    kind = _encode_kind(argv)
    if rate <= 0 or kind is None:
        return
    seconds = float(arg_after(argv, "-t") or 0) / 60.0 * rate
    if kind == "audio":
        seconds *= 0.1                    # AAC is cheap next to the video encode
    time.sleep(seconds)


def _record_timeline(argv, started):
    """Append one JSON line per clip encode to FAKE_TIMELINE, when set."""
    path = os.getenv("FAKE_TIMELINE")
    kind = _encode_kind(argv)
    if not path or kind is None:
        return
    entry = {"pid": os.getpid(), "job": os.getenv("FAKE_JOB_LABEL", ""),
             "kind": kind, "start": started, "end": time.time(),
             "clip_seconds": float(arg_after(argv, "-t") or 0)}
    with open(path, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


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
