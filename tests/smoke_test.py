#!/usr/bin/env python3
"""Offline self-check for AI-Transcoder.

Exercises every part of the pipeline that does not need the custom FFmpeg build:
timecode maths, WebVTT parsing/merging/segmentation, m3u8 parsing, master-playlist
generation, ESAM parsing and SCTE-35 marker injection, and the FFmpeg command that
would be issued for a transcode.

    python tests/smoke_test.py
"""
import os
import shutil
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hls_toolkit import ffmpeg_wrapper as fw
from hls_toolkit.esam_parser import (parse_esam_xml_string, parse_mcc_xml_asset_tags,
                                     process_playlist, remap_esam_events_for_merged_clips)
from hls_toolkit.playlist_utils import create_master_playlist, parse_variant_segments
from hls_toolkit.subtitle_processor import (generate_merged_subtitle_file, parse_vtt_file,
                                            segment_vtt_for_hls)
from hls_toolkit.time_utils import (parse_fps, seconds_to_timecode, timecode_to_frame,
                                    timecode_to_seconds)
from hls_toolkit import s3_io

FAILURES = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n       got:  {got!r}\n       want: {want!r}")
        FAILURES.append(label)


def test_time_utils():
    print("time_utils")
    check("parse_fps('30000/1001')", parse_fps("30000/1001"), (Fraction(30000, 1001), False))
    check("parse_fps('30000/1001DF')", parse_fps("30000/1001DF"), (Fraction(30000, 1001), True))
    check("timecode_to_frame 00:00:10:00 @25", timecode_to_frame("00:00:10:00", "25"), 250)
    check("timecode_to_seconds 00:00:10:00 @25",
          float(timecode_to_seconds("00:00:10:00", "25")), 10.0)
    check("seconds_to_timecode 10s @25", seconds_to_timecode(Fraction(10), "25"), "00:00:10:00")
    check("drop-frame 00:01:00;02 @29.97",
          timecode_to_frame("00:01:00;02", "30000/1001"), 1800)


def test_subtitles(work):
    print("subtitle_processor")
    src = work / "in.vtt"
    src.write_text(
        "WEBVTT\n\n"
        "1\n00:00:01.000 --> 00:00:04.000\nHello there.\n\n"
        "2\n00:00:05.500 --> 00:00:09.000 line:90%\nSecond caption.\n\n"
        "3\n00:00:12.000 --> 00:00:16.000\nThird caption.\n")
    cues = parse_vtt_file(src)
    check("parsed cue count", len(cues), 3)
    check("cue settings preserved", cues[1].settings, "line:90%")

    out = work / "out"
    out.mkdir(exist_ok=True)
    tmp = work / "tmp"
    tmp.mkdir(exist_ok=True)
    clippings = [{"StartTimecode": "00:00:00:00", "EndTimecode": "00:00:18:00"}]
    merged, _, dur = generate_merged_subtitle_file(src, "ffmpeg", clippings, 25.0, out, tmp)
    check("merged timeline duration", dur, 18.0)

    video_segments = [(0.0, 6.0, "s0.ts", 1), (6.0, 12.0, "s1.ts", 3), (12.0, 18.0, "s2.ts", 5)]
    ok, playlist = segment_vtt_for_hls(merged, out, "en", dur, video_segments,
                                       global_stream_mpegts_start=126000, vtt_index=1)
    check("segmentation succeeded", ok, True)
    check("vtt segment count", len(list(out.glob("channel_en-vtt-1_*.vtt"))), 3)
    body = playlist.read_text()
    check("playlist has ENDLIST", "#EXT-X-ENDLIST" in body, True)
    check("first segment carries MPEGTS map",
          "X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:126000"
          in (out / "channel_en-vtt-1_00001.vtt").read_text(), True)


def test_playlists(work):
    print("playlist_utils")
    variant = work / "channel_1080p.m3u8"
    variant.write_text(
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n#EXT-X-PLAYLIST-TYPE:VOD\n"
        + "".join(f"#EXTINF:6.000000,\nchannel_1080p_{i:05d}.ts\n" for i in (1, 2, 3, 4))
        + "#EXT-X-ENDLIST\n")
    _, segments, total, maxd = parse_variant_segments(variant)
    check("segment count", len(segments), 4)
    check("total duration", total, 24.0)

    master = work / "channel.m3u8"
    ladder = [{"name": "1080p", "width": 1920, "height": 1080, "bitrate": "6M"},
              {"name": "720p", "width": 1280, "height": 720,
               "codec_params": "vbv-maxrate=3500"}]
    create_master_playlist(
        str(master), ladder, {"1080p": "a.mp4", "720p": "b.mp4"}, "ffprobe", 25.0,
        None, None, "en",
        lambda p, f: {"codec_name": "h264", "profile": "High", "level": 40,
                      "avg_frame_rate": 25.0},
        fw._get_h264_profile_idc)
    text = master.read_text()
    check("master lists both rungs", text.count("#EXT-X-STREAM-INF"), 2)
    check("bitrate rung bandwidth", "BANDWIDTH=6000000" in text, True)
    check("codec_params rung bandwidth", "BANDWIDTH=3500000" in text, True)
    return variant, segments


def test_esam(work, variant, segments):
    print("esam_parser")
    scc = ('<SignalProcessingNotification '
           'xmlns="urn:cablelabs:iptvservices:esam:xsd:signal:1" '
           'xmlns:sig="urn:cablelabs:md:xsd:signaling:3.0">'
           '<ResponseSignal acquisitionSignalID="sig-a">'
           '<sig:NPTPoint nptPoint="6.0"/><sig:SCTE35PointDescriptor>'
           '<sig:SegmentationDescriptorInfo segmentTypeId="52" duration="PT12.0S"/>'
           '</sig:SCTE35PointDescriptor></ResponseSignal>'
           '</SignalProcessingNotification>')
    events = parse_esam_xml_string(scc)
    check("event count", len(events), 1)
    check("event duration", events[0]["duration"], 12.0)

    mcc = ('<Notification xmlns:ns2='
           '"http://www.cablelabs.com/namespaces/metadata/xsd/confirmation/2">'
           '<ns2:ManifestResponse acquisitionSignalID="sig-a">'
           '<ns2:Tag value="&lt;!-- #EXT-X-ASSET:CAID=0x1234 --&gt;"/>'
           '</ns2:ManifestResponse></Notification>')
    tags = parse_mcc_xml_asset_tags(mcc)
    check("asset tag", tags.get("sig-a"), "#EXT-X-ASSET:CAID=0x1234")

    remapped = remap_esam_events_for_merged_clips(
        events,
        [{"StartTimecode": "00:00:00:00", "EndTimecode": "00:00:12:00"},
         {"StartTimecode": "00:00:30:00", "EndTimecode": "00:00:42:00"}],
        25.0)
    check("remapped npt", remapped[0]["npt"], 6.0)

    process_playlist(str(variant), events, tags, video_segments=segments)
    body = variant.read_text()
    check("CUE-OUT injected", "#EXT-X-CUE-OUT:12.000" in body, True)
    check("asset tag injected", "#EXT-X-ASSET:CAID=0x1234" in body, True)
    check("CUE-OUT-CONT injected", "#EXT-X-CUE-OUT-CONT:6.000/12.000" in body, True)
    check("CUE-IN injected", "#EXT-X-CUE-IN" in body, True)


def test_ffmpeg_command():
    print("ffmpeg_wrapper")
    captured = []
    original = fw._run_ffmpeg_command_with_logging
    fw._run_ffmpeg_command_with_logging = lambda cmd, **kw: (captured.append(cmd), ("", ""))[1]
    try:
        ladder = [
            {"name": "1080p", "width": 1920, "height": 1080, "codec": "H_264",
             "codec_params": "vbv-maxrate=6000", "bitrate": "6M", "threads": 8,
             "video_format": "yuv420p", "frame_rate": "25", "GopSize": 6.0},
            {"name": "720p", "width": 1280, "height": 720, "codec": "H_265",
             "crf": 23, "threads": 4, "video_format": "yuv420p10",
             "frame_rate": "25", "GopSize": 6.0}]
        result = fw._transcode_clip_with_single_command(
            0, ladder, "/in/movie.mp4", "/tmp", "ffmpeg", 0.0, 12.0, False, {}, {},
            [{"npt": 6.5}], 0.0, 12.0, 25, 0.0, "video")
    finally:
        fw._run_ffmpeg_command_with_logging = original

    cmd = " ".join(str(a) for a in captured[0])
    check("split filter labels", "[0:v]split=2[v0][v1]" in cmd, True)
    check("H.264 encoder", "libwz264" in cmd, True)
    check("H.265 encoder + tag", "libwz265" in cmd and "hvc1" in cmd, True)
    check("codec params flag", "-wz264-params" in cmd, True)
    check("10-bit pixel format", "yuv420p10le" in cmd, True)
    check("scte35 cue point", "-scte35_cue_points 6.500" in cmd, True)
    check("outputs mapped", sorted(result["outputs"]), ["1080p", "720p"])


def test_environment_diagnostics():
    """A broken TLS stack must be named as such, not blamed on S3."""
    print("environment diagnostics")

    # pyOpenSSL too old for the installed cryptography: OpenSSL/crypto.py reads
    # X509_V_FLAG_NOTIFY_POLICY at import time and cryptography >= 42 removed it.
    broken = AttributeError("module 'lib' has no attribute 'X509_V_FLAG_NOTIFY_POLICY'")
    hint = s3_io.environment_problem(broken)
    check("TLS mismatch is recognised", hint is not None, True)
    check("hint says it is not an S3 problem",
          bool(hint) and "not an S3 or permissions problem" in hint, True)
    check("hint gives the botocore remedy",
          bool(hint) and "boto3>=1.38.46" in hint, True)
    check("hint gives the pyOpenSSL remedy",
          bool(hint) and "pyOpenSSL>=24.0.0" in hint, True)

    # A missing pyOpenSSL is normal — botocore falls back to the stdlib context.
    absent = ModuleNotFoundError("No module named 'OpenSSL'")
    absent.name = "OpenSSL"
    check("absent pyOpenSSL is not an error", s3_io.environment_problem(absent), None)

    # Ordinary AWS errors must not be misread as environment problems.
    class _Denied(Exception):
        response = {"Error": {"Code": "AccessDenied"}}
    check("AWS errors stay AWS errors", s3_io.environment_problem(_Denied()), None)
    check("error code extraction", s3_io._error_code(_Denied()), "AccessDenied")


def main():
    work = Path(tempfile.mkdtemp(prefix="aitranscoder_smoke_"))
    try:
        test_time_utils()
        test_subtitles(work)
        variant, segments = test_playlists(work)
        test_esam(work, variant, segments)
        test_ffmpeg_command()
        test_environment_diagnostics()
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {', '.join(FAILURES)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
