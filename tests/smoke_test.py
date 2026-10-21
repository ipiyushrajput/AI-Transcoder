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
from hls_toolkit.playlist_utils import (calculate_bitrates_from_segments, create_master_playlist,
                                        parse_variant_segments)
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


def check_true(label, value, detail=""):
    check(label + (f" ({detail})" if detail and not value else ""), bool(value), True)


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

    # SDK v2.3 timecode rules.
    from hls_toolkit import time_utils as tu
    check("'29.97' is read as 30000/1001", parse_fps("29.97"), (Fraction(30000, 1001), False))
    check("'59.94 DF' is read as 60000/1001 drop-frame",
          parse_fps("59.94 DF"), (Fraction(60000, 1001), True))
    check("colon at 29.97 is drop-frame with allow_colon_df",
          timecode_to_frame("00:10:02:27", "30000/1001", allow_colon_df=True),
          timecode_to_frame("00:10:02;27", "30000/1001"))
    check("colon at 59.94 is drop-frame with allow_colon_df",
          timecode_to_frame("00:42:50:14", "60000/1001", allow_colon_df=True),
          timecode_to_frame("00:42:50;14", "60000/1001"))
    check("colon at 59.94 is non-drop without it",
          timecode_to_frame("00:01:00:00", "60000/1001"), 3596)
    check("25 fps is unaffected by allow_colon_df",
          timecode_to_frame("00:42:50:14", "25", allow_colon_df=True), 64264)
    for label, tc, fps, words in (
            ("a dropped label is rejected", "00:01:00;00", "30000/1001", "does not exist"),
            ("';' at a non-DF rate is rejected", "00:00:10;00", "25", "not valid for frame rate"),
            ("frames past the rate are rejected", "00:00:10:30", "30000/1001", "Invalid frames"),
            ("seconds past 59 are rejected", "00:00:60:00", "25", "Invalid seconds"),
            ("a malformed timecode is rejected", "0:0:10:00", "25", "Invalid timecode format")):
        try:
            timecode_to_seconds(tc, fps, allow_colon_df=True)
            check(label, "accepted", "rejected")
        except ValueError as e:
            check_true(label, words in str(e), str(e))
    # Frames -> timecode -> frames is exact in drop-frame.
    check_true("drop-frame frame/timecode round trip is exact",
               all(tu._df_tc_to_frames(*tu._df_frames_to_tc(n, Fraction(30000, 1001)),
                                       Fraction(30000, 1001)) == n
                   for n in range(0, 200000, 997)))
    # A 1001 rate that went through float() is still that exact rate, so
    # drop-frame timecodes keep working; a genuinely different rate is not changed.
    check("float(30000/1001) parses back to 30000/1001",
          parse_fps(str(float(Fraction(30000, 1001))))[0], Fraction(30000, 1001))
    check("float(60000/1001) as a Fraction is snapped too",
          parse_fps(Fraction(float(Fraction(60000, 1001))))[0], Fraction(60000, 1001))
    check("float(24000/1001) parses back to 24000/1001",
          parse_fps(str(float(Fraction(24000, 1001))))[0], Fraction(24000, 1001))
    check("2997/100 is left alone (a different rate, still non-drop)",
          parse_fps("2997/100")[0], Fraction(2997, 100))
    check("';' timecodes work with a float-rounded 29.97 rate",
          float(timecode_to_seconds("00:10:02;27", str(float(Fraction(30000, 1001))),
                                    allow_colon_df=True)),
          float(timecode_to_seconds("00:10:02;27", "30000/1001")))
    check("DF seconds_to_timecode uses ';'",
          seconds_to_timecode(Fraction(3600), "30000/1001", drop_frame_tc_format=True),
          "01:00:00;00")


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

    # Regression: a caption whose text is a number ("800" in the AVC test asset)
    # used to end its own cue empty and turn into the next cue's identifier.
    numeric = work / "numeric.vtt"
    numeric.write_text(
        "WEBVTT\n\n"
        "00:00:01.468 --> 00:00:02.469\n800\n\n"
        "00:00:02.602 --> 00:00:03.837\n800?\n\n"
        "00:00:03.970 --> 00:00:06.006\nHe's at full boat on the prices,\n"
        "and what we have to consider\n")
    cues = parse_vtt_file(numeric)
    check("a numeric caption keeps its text", [(c.text, c.id) for c in cues[:2]],
          [("800", None), ("800?", None)])
    num_out = work / "numeric_out"
    num_out.mkdir(exist_ok=True)
    merged, _, dur = generate_merged_subtitle_file(
        numeric, "ffmpeg", [{"StartTimecode": "00:00:00:00", "EndTimecode": "00:00:06:00"}],
        25.0, num_out, tmp)
    segment_vtt_for_hls(merged, num_out, "en", dur,
                        [(0.0, 3.0, "s0.ts", 1), (3.0, 6.0, "s1.ts", 3)],
                        global_stream_mpegts_start=126000, vtt_index=1)
    seg = (num_out / "channel_en-vtt-1_00001.vtt").read_text()
    check_true("the written segment shows 800 under its own timing line",
               "00:00:01.468 --> 00:00:02.469\n800\n" in seg, seg)
    check_true("…and not as the next cue's identifier", "800\n00:00:02.602" not in seg, seg)

    # Numbered cues with no blank line between them still split.
    packed = work / "packed.vtt"
    packed.write_text("WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\nFirst\n"
                      "2\n00:00:03.000 --> 00:00:04.000\nSecond\n")
    check("numbered cues without blank lines still split",
          [(c.id, c.text) for c in parse_vtt_file(packed)], [("1", "First"), ("2", "Second")])
    years = work / "years.vtt"
    years.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n911\n2024\n\n"
                     "00:00:03.000 --> 00:00:04.000\nIn 2024\n42\n")
    check("numbers inside multi-line captions stay text",
          [c.text for c in parse_vtt_file(years)], ["911\n2024", "In 2024\n42"])

    # SRT-style comma timestamps (the HEVC test asset) are no longer dropped.
    srt = work / "srt_style.vtt"
    srt.write_text("WEBVTT\n\n00:10:01,000 --> 00:10:11,000 line:90%\nComma times\n")
    cues = parse_vtt_file(srt)
    check("SRT-style comma timestamps parse",
          [(c.start, c.end, c.text, c.settings) for c in cues],
          [(601.0, 611.0, "Comma times", "line:90%")])


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

    # An HEVC rung must declare the profile and level it was actually encoded
    # at, not a fixed string.
    hevc_master = work / "channel_hevc.m3u8"
    create_master_playlist(
        str(hevc_master), [{"name": "1080p", "width": 1920, "height": 1080,
                            "bitrate": "6M", "codec": "H_265"}],
        {"1080p": "a.mp4"}, "ffprobe", 25.0, None, None, "en",
        lambda p, f: {"codec_name": "hevc", "profile": "Main 10", "level": 120,
                      "avg_frame_rate": 25.0},
        fw._get_h264_profile_idc)
    check("hevc codec string follows the probed stream",
          "hvc1.2.6.L120.B0" in hevc_master.read_text(), True)

    # BANDWIDTH measured from the real segments: peak x 1.1, plus the average.
    # Three 6 s segments of 750 kB, 1500 kB, 750 kB -> 1 / 2 / 1 Mbit/s.
    for i, size in ((1, 750_000), (2, 1_500_000), (3, 750_000)):
        (work / f"channel_1080p_{i:05d}.ts").write_bytes(b"\0" * size)
    measured_variant = work / "channel_1080p.m3u8"
    measured_variant.write_text(
        "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
        + "".join(f"#EXTINF:6.000000,\nchannel_1080p_{i:05d}.ts\n" for i in (1, 2, 3))
        + "#EXT-X-ENDLIST\n")
    measured = calculate_bitrates_from_segments(measured_variant)
    check("measured BANDWIDTH is the peak segment bitrate + 10%",
          measured["bandwidth"], 2_200_000)
    check("measured AVERAGE-BANDWIDTH is total bits / duration",
          measured["avg_bandwidth"], 1_333_333)
    check("a missing playlist measures as 0 (so the template value is used)",
          calculate_bitrates_from_segments(work / "nope.m3u8"),
          {"bandwidth": 0, "avg_bandwidth": 0})
    measured_master = work / "channel_measured.m3u8"
    create_master_playlist(
        str(measured_master), ladder, {"1080p": "a.mp4", "720p": "b.mp4"}, "ffprobe",
        25.0, None, None, "en",
        lambda p, f: {"codec_name": "h264", "profile": "High", "level": 40,
                      "avg_frame_rate": 25.0},
        fw._get_h264_profile_idc, bitrate_data_by_resolution={"1080p": measured})
    text = measured_master.read_text()
    check("the master uses the measured values",
          "BANDWIDTH=2200000,AVERAGE-BANDWIDTH=1333333" in text, True)
    check("a rung without a measurement falls back to the template",
          "BANDWIDTH=3500000,AVERAGE-BANDWIDTH=3500000" in text, True)
    variant.write_text(
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n#EXT-X-PLAYLIST-TYPE:VOD\n"
        + "".join(f"#EXTINF:6.000000,\nchannel_1080p_{i:05d}.ts\n" for i in (1, 2, 3, 4))
        + "#EXT-X-ENDLIST\n")
    return variant, segments


def test_hevc_codec_string():
    """RFC 6381 HEVC codec strings, derived rather than hardcoded."""
    print("hevc codec strings")
    from hls_toolkit.playlist_utils import hevc_codec_string
    # ffprobe reports the HEVC level as general_level_idc: 4.0 -> 120, 3.1 -> 93.
    check("main at level 4.0", hevc_codec_string("Main", 120), "hvc1.1.6.L120.B0")
    check("main 10 at level 5.0", hevc_codec_string("Main 10", 150),
          "hvc1.2.6.L150.B0")
    check("main still picture", hevc_codec_string("Main Still Picture", 93),
          "hvc1.3.6.L93.B0")
    check("unknown profile falls back to main",
          hevc_codec_string("Something Else", 120), "hvc1.1.6.L120.B0")
    check("missing level falls back to 3.1", hevc_codec_string("Main", None),
          "hvc1.1.6.L93.B0")
    check("nonsense level falls back to 3.1", hevc_codec_string("Main", "n/a"),
          "hvc1.1.6.L93.B0")


def test_duplicate_rung_guard():
    """Two selected rungs sharing a name would overwrite each other's output."""
    print("duplicate rendition guard")
    from hls_toolkit.hls_generator import validate_unique_rung_names
    from hls_toolkit.job_context import TranscodeError

    single_codec = [{"name": "1080p", "codec": "H_264"},
                    {"name": "720p", "codec": "H_264"}]
    try:
        validate_unique_rung_names("h264_standard", single_codec)
        check("a normal ladder is accepted", "accepted", "accepted")
    except TranscodeError as e:
        check("a normal ladder is accepted", f"refused: {e}", "accepted")

    # 720p listed under both codecs: the shape that silently collapsed two
    # renditions into one set of files.
    mixed = [{"name": "720p", "codec": "H_264"},
             {"name": "360p", "codec": "H_264"},
             {"name": "720p", "codec": "H_265"}]
    try:
        validate_unique_rung_names("mixed", mixed)
        check("duplicate rungs refused", "accepted", "TranscodeError")
    except TranscodeError as e:
        check("duplicate rungs refused at validation", e.stage, "VALIDATION")
        check("error names only the duplicated rung",
              "720p" in str(e) and "360p" not in str(e), True)
        check("error explains the overwrite", "overwrite" in str(e).lower(), True)


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

    # Regression: ESAM remap at 29.97 with drop-frame clips, given the exact
    # rate or a float-rounded one, must remap every event (it used to skip
    # every clip with "Drop-frame notation is not valid for frame rate").
    ev = [{"npt": 80.0, "duration": 10.0, "segmentTypeId": "52",
           "acquisitionSignalID": "1"}]
    df_clips = [{"StartTimecode": "00:00:00;00", "EndTimecode": "00:00:20;00"},
                {"StartTimecode": "00:01:10;00", "EndTimecode": "00:01:50;00"}]
    for label, fps in (("exact 30000/1001", Fraction(30000, 1001)),
                       ("float-rounded 29.97", float(Fraction(30000, 1001)))):
        out = remap_esam_events_for_merged_clips(ev, df_clips, fps)
        # 20;00 = 600 frames = 20.020 s, plus 80 - 70.003 = 9.997 s into clip 2.
        check(f"ESAM remap at {label} keeps the event", len(out), 1)
        check(f"ESAM remap at {label} puts it at the right time",
              out[0]["npt"] if out else None, 30.017)

    # Two signals past the end of a 24 s playlist: one CUE-OUT at the end, not two.
    late = work / "channel_late.m3u8"
    late.write_text(
        "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
        + "".join(f"#EXTINF:6.000000,\nlate_{i:05d}.ts\n" for i in (1, 2, 3, 4))
        + "#EXT-X-ENDLIST\n")
    late_events = parse_esam_xml_string(
        '<SignalProcessingNotification '
        'xmlns="urn:cablelabs:iptvservices:esam:xsd:signal:1" '
        'xmlns:sig="urn:cablelabs:md:xsd:signaling:3.0">'
        + "".join(
            f'<ResponseSignal acquisitionSignalID="late-{n}">'
            f'<sig:NPTPoint nptPoint="{n}"/><sig:SCTE35PointDescriptor>'
            '<sig:SegmentationDescriptorInfo segmentTypeId="52" duration="PT10.0S"/>'
            '</sig:SCTE35PointDescriptor></ResponseSignal>' for n in ("30.0", "40.0"))
        + '</SignalProcessingNotification>')
    check("two late signals parsed", len(late_events), 2)
    process_playlist(str(late), late_events, {})
    late_body = late.read_text()
    check("only one CUE-OUT is added at the end for signals past the end",
          late_body.count("#EXT-X-CUE-OUT:"), 1)
    check_true("…before EXT-X-ENDLIST",
               late_body.rstrip().endswith("#EXT-X-CUE-OUT:10.000\n#EXT-X-ENDLIST"),
               late_body)


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
    check("color range is declared", "-pix_fmt yuv420p -color_range tv" in cmd, True)

    # Frame-rate snapping (SDK v2.3): standard rates pass through unchanged,
    # high-denominator measurements snap to the broadcast rate.
    from fractions import Fraction
    n = fw.normalize_encoder_framerate
    check("30000/1001 is unchanged", n(Fraction(30000, 1001)), Fraction(30000, 1001))
    check("25 is unchanged", n(Fraction(25)), Fraction(25))
    check("a 29.97 float snaps to 30000/1001", n(29.97), Fraction(30000, 1001))
    check("a high-denominator 59.94 snaps to 60000/1001",
          n(Fraction(5994006, 100000)), Fraction(60000, 1001))
    check("an odd high-denominator rate is reduced",
          n(Fraction(12345, 10007)), Fraction(12345, 10007).limit_denominator(1001))
    check("a malformed or zero rate becomes 25", (n("abc"), n("0/1"), n(None)),
          (Fraction(25), Fraction(25), Fraction(25)))

    for label, source_fps, expected in (
            ("an NTSC source keeps fps=30000/1001", Fraction(30000, 1001), "fps=30000/1001"),
            ("a high-denominator source is snapped in the filter",
             Fraction(2997003, 100000), "fps=30000/1001")):
        captured.clear()
        fw._run_ffmpeg_command_with_logging = (
            lambda cmd, **kw: (captured.append(cmd), ("", ""))[1])
        try:
            fw._transcode_clip_with_single_command(
                0, [{"name": "720p", "width": 1280, "height": 720, "codec": "H_264",
                     "bitrate": "3M", "GopSize": 6.0}],
                "/in/movie.mp4", "/tmp", "ffmpeg", 0.0, 12.0, False, {}, {},
                [], 0.0, 12.0, source_fps, 0.0, "video")
        finally:
            fw._run_ffmpeg_command_with_logging = original
        check_true(label, expected in " ".join(str(a) for a in captured[0]))


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

    # Credential failures say which credential sources were found, masked.
    class NoCredentialsError(Exception):
        pass
    check("missing credentials are an auth error",
          s3_io.is_aws_auth_error(NoCredentialsError("Unable to locate credentials")), True)
    check("AccessDenied is an auth error", s3_io.is_aws_auth_error(_Denied()), True)
    check("a 404 is not an auth error",
          s3_io.is_aws_auth_error(type("E", (Exception,), {
              "response": {"Error": {"Code": "404"}}})("not found")), False)
    check("non-auth errors add nothing", s3_io.auth_diagnosis(ValueError("boom")), "")
    saved = {k: os.environ.get(k) for k in ("AWS_ACCESS_KEY_ID", "AWS_PROFILE",
                                            "AWS_SECRET_ACCESS_KEY")}
    os.environ.update(AWS_ACCESS_KEY_ID="AKIAEXAMPLEKEY1234", AWS_PROFILE="transcoder",
                      AWS_SECRET_ACCESS_KEY="never-shown-secret")
    try:
        diagnosis = s3_io.auth_diagnosis(_Denied())
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    check_true("the diagnosis lists the access key masked",
               "AWS_ACCESS_KEY_ID (AKIA...1234)" in diagnosis, diagnosis)
    check_true("…and the profile", "AWS_PROFILE ('transcoder')" in diagnosis)
    check_true("…and never the full key or the secret",
               "AKIAEXAMPLEKEY1234" not in diagnosis
               and "never-shown-secret" not in diagnosis)


def test_cpu_budget(work):
    """The machine-wide budget: never over-committed, never leaked."""
    print("cpu budget")
    import subprocess
    import threading
    import time
    from hls_toolkit.cpu_budget import BudgetAcquireAborted, CpuBudget

    budget = CpuBudget(8, str(work / "slots_basic"))
    check("a request larger than the budget is clamped", budget.clamp(20), 8)
    check("a zero request still takes one core", budget.clamp(0), 1)
    lease = budget.acquire(5)
    check("cores in use after taking 5", budget.in_use(), 5)
    lease.release()
    lease.release()                                    # idempotent
    check("released cores return to the pool", budget.in_use(), 0)

    # Many threads competing: the total held must never exceed the budget.
    peak = [0]
    held = [0]
    guard = threading.Lock()

    def worker(cost):
        lease = budget.acquire(cost)
        with guard:
            held[0] += cost
            peak[0] = max(peak[0], held[0])
        time.sleep(0.05)
        with guard:
            held[0] -= cost
        lease.release()

    threads = [threading.Thread(target=worker, args=(c,)) for c in (3, 5, 2, 4, 1, 6, 3, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check_true("threads never held more than the budget", peak[0] <= 8, f"peak {peak[0]}")
    check("every thread got its cores and gave them back", budget.in_use(), 0)

    # Waiting stops as soon as the job is cancelled.
    hog = budget.acquire(8)
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    started = time.time()
    try:
        budget.acquire(1, should_abort=stop.is_set)
        check("a cancelled wait is abandoned", "acquired", "aborted")
    except BudgetAcquireAborted:
        check("a cancelled wait is abandoned", "aborted", "aborted")
    check_true("…promptly", time.time() - started < 2.0)
    hog.release()

    # A job killed while holding cores must not leak them: the kernel drops a
    # dead process's flocks.
    slots = work / "slots_crash"
    holder = subprocess.Popen([sys.executable, "-c", (
        "import sys, time; sys.path.insert(0, %r)\n"
        "from hls_toolkit.cpu_budget import CpuBudget\n"
        "lease = CpuBudget(8, %r).acquire(6)\n"
        "print('held', flush=True); time.sleep(60)\n")
        % (os.path.dirname(os.path.dirname(os.path.abspath(__file__))), str(slots))],
        stdout=subprocess.PIPE, text=True)
    check("another process holds 6 cores", holder.stdout.readline().strip(), "held")
    shared = CpuBudget(8, str(slots))
    check("the hold is visible across processes", shared.in_use(), 6)
    holder.kill()
    holder.wait()
    check("kill -9 releases the dead job's cores", shared.in_use(), 0)

    # A job killed while *waiting in line* leaves its queue entry behind; the
    # next request must clear it rather than wait behind it forever.
    hog = shared.acquire(8)
    waiter = subprocess.Popen([sys.executable, "-c", (
        "import sys; sys.path.insert(0, %r)\n"
        "from hls_toolkit.cpu_budget import CpuBudget\n"
        "CpuBudget(8, %r).acquire(2)\n")
        % (os.path.dirname(os.path.dirname(os.path.abspath(__file__))), str(slots))])
    queue = slots / "queue"
    deadline = time.time() + 10
    while time.time() < deadline and not [n for n in os.listdir(queue)
                                          if not n.startswith(".")]:
        time.sleep(0.05)
    check_true("the other process is waiting in line",
               [n for n in os.listdir(queue) if not n.startswith(".")])
    waiter.kill()
    waiter.wait()
    hog.release()
    started = time.time()
    lease = shared.acquire(2)
    check_true("a dead waiter's entry does not block the line",
               time.time() - started < 2.0)
    lease.release()
    check("its stale entry is cleared", [n for n in os.listdir(queue)
                                         if not n.startswith(".")], [])


def test_disk_budget(work):
    """Scratch reservations: shared, waited for, refused when hopeless, never leaked."""
    print("disk budget")
    import subprocess
    import threading
    import time
    from hls_toolkit import disk_budget
    from hls_toolkit.job_context import JobCancelled, TranscodeError

    GB = 1024 ** 3
    root = work / "scratch"
    root.mkdir()
    saved_env = os.environ.get("WZ_CPU_SLOT_DIR")
    saved_free = disk_budget._free_bytes
    os.environ["WZ_CPU_SLOT_DIR"] = str(work / "coord_disk")
    disk_budget._free_bytes = lambda path: 10 * GB      # pretend 10 GB is free
    try:
        first = disk_budget.reserve(root, root / "a", 6 * GB, min_free_bytes=GB,
                                    max_wait_seconds=0)
        check("a job that fits is reserved", first.nbytes, 6 * GB)

        # 10 free - 6 held - 1 spare = 3 available: 4 GB must wait (and here, give up).
        try:
            disk_budget.reserve(root, root / "b", 4 * GB, min_free_bytes=GB,
                                max_wait_seconds=0)
            check("a job that would overcommit waits", "reserved", "waited")
        except TranscodeError as e:
            check_true("a job that would overcommit waits", "Waited" in str(e), str(e))

        # More than the disk could ever give is refused at once, with numbers.
        started = time.time()
        try:
            disk_budget.reserve(root, root / "c", 50 * GB, min_free_bytes=GB,
                                max_wait_seconds=60)
            check("an impossible job is refused", "reserved", "refused")
        except TranscodeError as e:
            check_true("an impossible job is refused", "Not enough scratch space" in str(e)
                       and e.stage == "FETCHING_INPUT", str(e))
        check_true("…without waiting", time.time() - started < 2.0)

        # A waiting job starts as soon as the space is released.
        result = {}

        def waiter():
            result["r"] = disk_budget.reserve(root, root / "d", 4 * GB, min_free_bytes=GB,
                                              max_wait_seconds=30)
        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.3)
        check_true("the second job is still waiting", thread.is_alive())
        first.release()
        first.release()                                   # idempotent
        thread.join(15)
        check_true("it starts once the space is released", "r" in result)
        result["r"].release()

        # Space other jobs have already written counts as used, not as held twice.
        (root / "e").mkdir()
        held = disk_budget.reserve(root, root / "e", 6 * GB, min_free_bytes=GB,
                                   max_wait_seconds=0)
        (root / "e" / "blob").write_bytes(b"x" * 1024)
        disk_budget._free_bytes = lambda path: 10 * GB - 1024
        again = disk_budget.reserve(root, root / "f", 3 * GB - 2048, min_free_bytes=GB,
                                    max_wait_seconds=0)
        check_true("written bytes are not counted twice", again.nbytes > 0)
        again.release()
        held.release()

        # Cancelling a waiting job stops the wait.
        hog = disk_budget.reserve(root, root / "g", 8 * GB, min_free_bytes=GB,
                                  max_wait_seconds=0)
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        try:
            disk_budget.reserve(root, root / "h", 4 * GB, min_free_bytes=GB,
                                should_abort=stop.is_set, max_wait_seconds=30)
            check("a cancelled wait is abandoned", "reserved", "cancelled")
        except JobCancelled:
            check("a cancelled wait is abandoned", "cancelled", "cancelled")
        hog.release()

        # A job killed while holding space must not keep it.
        holder = subprocess.Popen([sys.executable, "-c", (
            "import sys, time; sys.path.insert(0, %r)\n"
            "from hls_toolkit import disk_budget\n"
            "disk_budget._free_bytes = lambda path: 10 * 1024 ** 3\n"
            "r = disk_budget.reserve(%r, %r, 8 * 1024 ** 3, min_free_bytes=1024 ** 3)\n"
            "print('held', flush=True); time.sleep(60)\n")
            % (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
               str(root), str(root / "dead"))],
            stdout=subprocess.PIPE, text=True, env=dict(os.environ))
        check("another process holds 8 GB", holder.stdout.readline().strip(), "held")
        try:
            disk_budget.reserve(root, root / "i", 4 * GB, min_free_bytes=GB,
                                max_wait_seconds=0)
            check("the hold is visible across processes", "reserved", "blocked")
        except TranscodeError:
            check("the hold is visible across processes", "blocked", "blocked")
        holder.kill()
        holder.wait()
        after = disk_budget.reserve(root, root / "i", 4 * GB, min_free_bytes=GB,
                                    max_wait_seconds=0)
        check("kill -9 releases the dead job's space", after.nbytes, 4 * GB)
        after.release()
    finally:
        disk_budget._free_bytes = saved_free
        if saved_env is None:
            os.environ.pop("WZ_CPU_SLOT_DIR", None)
        else:
            os.environ["WZ_CPU_SLOT_DIR"] = saved_env


def test_housekeeping(work):
    """Dead jobs' scratch folders and old logs go; anything alive or kept stays."""
    print("housekeeping")
    import subprocess
    import time
    from hls_toolkit import housekeeping as hk

    root = work / "janitor"
    root.mkdir()
    old = time.time() - 3600
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def scratch(name, owner=True, aged=True):
        folder = root / f"aitx_{name}"
        (folder / "input").mkdir(parents=True)
        (folder / "input" / "source.mp4").write_bytes(b"x" * 100)
        if owner:
            hk.ScratchOwner(folder).release()
            if aged:
                os.utime(folder / hk.OWNER_FILE, (old, old))
        return folder

    dead = scratch("dead")
    young = scratch("young", aged=False)
    foreign = scratch("foreign", owner=False)
    kept = scratch("kept")
    (kept / hk.KEEP_FILE).write_text("debug")
    unrelated = root / "something_else"
    unrelated.mkdir()

    live_here = root / "aitx_live_here"
    live_here.mkdir()
    holder_here = hk.ScratchOwner(live_here)
    os.utime(live_here / hk.OWNER_FILE, (old, old))

    live_there = root / "aitx_live_there"
    live_there.mkdir()
    holder = subprocess.Popen([sys.executable, "-c", (
        "import os, sys, time; sys.path.insert(0, %r)\n"
        "from hls_toolkit.housekeeping import ScratchOwner, OWNER_FILE\n"
        "o = ScratchOwner(%r); t = time.time() - 3600\n"
        "os.utime(os.path.join(%r, OWNER_FILE), (t, t))\n"
        "print('held', flush=True); time.sleep(60)\n")
        % (repo, str(live_there), str(live_there))], stdout=subprocess.PIPE, text=True)
    check("another process owns a folder", holder.stdout.readline().strip(), "held")

    removed = hk.sweep_scratch(root)
    check("only the dead job's folder is removed", removed, 1)
    check_true("…it is gone", not dead.exists())
    for folder, why in ((young, "a brand-new folder"), (foreign, "a folder not made by a job"),
                        (kept, "a --debug folder"), (unrelated, "an unrelated folder"),
                        (live_here, "a folder this process's job owns"),
                        (live_there, "a folder another live process owns")):
        check_true(f"{why} is left alone", folder.exists())

    holder.kill()
    holder.wait()
    holder_here.release()
    check("once their jobs die, their folders go too", hk.sweep_scratch(root), 2)

    # Log retention.
    logs = work / "janitor_logs"
    stale_cli = logs / "ShowA_2"
    stale_api = logs / "ShowB" / "job-old"
    fresh_api = logs / "ShowB" / "job-new"
    lonely = logs / "ShowC" / "job-only"
    for folder in (stale_cli, stale_api, fresh_api, lonely):
        folder.mkdir(parents=True)
        (folder / "job.json").write_text("{}")
        (folder / "job.log").write_text("log")
    ancient = time.time() - 40 * 86400
    for folder in (stale_cli, stale_api, lonely):
        for path in [folder, *folder.iterdir()]:
            os.utime(path, (ancient, ancient))
    os.utime(fresh_api / "job.log", (ancient, ancient))   # job.json still fresh
    check("retention is off by default", hk.sweep_logs(logs), 0)
    check("old job folders are removed after LOG_RETENTION_DAYS",
          hk.sweep_logs(logs, days=30), 3)
    check_true("a folder with any recent file is kept", fresh_api.exists())
    check_true("an emptied channel folder is removed", not (logs / "ShowC").exists())
    check_true("a channel with jobs left is kept", (logs / "ShowB").exists())

    # The periodic sweep runs once per interval across processes.
    saved = os.environ.get("WZ_CPU_SLOT_DIR")
    os.environ["WZ_CPU_SLOT_DIR"] = str(work / "coord_janitor")
    try:
        first = scratch("dead_again")
        hk.run_periodic(root)
        check_true("the first periodic sweep runs", not first.exists())
        second = scratch("dead_later")
        hk.run_periodic(root)
        check_true("a second sweep within the interval is skipped", second.exists())
        hk.run_periodic(root, force=True)
        check_true("…unless forced (API start-up)", not second.exists())
    finally:
        if saved is None:
            os.environ.pop("WZ_CPU_SLOT_DIR", None)
        else:
            os.environ["WZ_CPU_SLOT_DIR"] = saved


# The clip lists from the HEVC and AVC test jobs whose ad markers drifted.
HEVC_CLIPS = [("00:00:00;00", "00:10:00;14"), ("00:10:00;15", "00:20:00;14"),
              ("00:20:00;15", "00:30:00;14"), ("00:30:00;15", "00:40:00;14"),
              ("00:40:00;15", "00:50:00;14"), ("00:50:00;15", "00:59:59;28")]
AVC_CLIPS = [("00:00:00;00", "00:10:02;27"), ("00:10:02;28", "00:19:12;26"),
             ("00:19:12;27", "00:26:57;05"), ("00:26:57;06", "00:35:00;07"),
             ("00:35:00;08", "00:42:50;14")]


def _ffmpeg_frames(ss, t, frames_cap, fps):
    """What FFmpeg keeps for -ss/-t on a CFR source, as the real logs show:
    frames from the first with pts >= ss, while the frame ends within t
    (floor(t x fps) of them), capped by -frames:v."""
    import math
    from fractions import Fraction
    first = math.ceil(Fraction(ss) * fps)
    kept = math.floor(Fraction(t) * fps)
    if frames_cap is not None:
        kept = min(kept, frames_cap)
    return first, kept


def test_clip_cuts_frame_exact():
    """Regression: every clip encode gets exactly its planned frames.

    The cut used to be sent as -ss/-t rounded to milliseconds; a length that
    rounded down (599.966033 -> 599.966) lost the clip's last frame, so the real
    ad split ran one frame earlier per clip than the ESAM marker, and from the
    third break on the marker landed a whole segment late.
    """
    print("frame-exact clip cuts")
    from fractions import Fraction
    from hls_toolkit.time_utils import timecode_to_seconds
    fps = Fraction(30000, 1001)
    captured = []
    original = fw._run_ffmpeg_command_with_logging
    fw._run_ffmpeg_command_with_logging = lambda cmd, **kw: (captured.append(cmd), ("", ""))[1]

    def arg(cmd, flag):
        return cmd[cmd.index(flag) + 1] if flag in cmd else None

    try:
        for name, clips in (("HEVC", HEVC_CLIPS), ("AVC", AVC_CLIPS)):
            planned_total = actual_total = 0
            drift = []
            for i, (start_tc, end_tc) in enumerate(clips):
                start = timecode_to_seconds(start_tc, fps, allow_colon_df=True)
                end = timecode_to_seconds(end_tc, fps, allow_colon_df=True)
                start_frame, planned = int(start * fps), int((end - start) * fps)
                rung = [{"name": "720p", "width": 1280, "height": 720, "codec": "H_265",
                         "crf": 26, "GopSize": 6.0}]
                for kind in ("video", "audio"):
                    captured.clear()
                    fw._transcode_clip_with_single_command(
                        i, rung, "/in/src.mp4", "/tmp", "ffmpeg", start, end, False, {}, {},
                        [], 0.0, float(end - start), fps, 0.0, kind)
                    cmd = [str(a) for a in captured[0]]
                    ss, t = arg(cmd, "-ss"), arg(cmd, "-t")
                    if kind == "video":
                        cap = arg(cmd, "-frames:v")
                        cap = int(cap) if cap else None
                        first, kept = _ffmpeg_frames(ss, t, cap, fps)
                        check(f"{name} clip {i}: video starts on its first frame",
                              first, start_frame)
                        check(f"{name} clip {i}: video has exactly the planned frames",
                              kept, planned)
                    else:
                        check_true(f"{name} clip {i}: audio starts with the video",
                                   abs(Fraction(ss) - Fraction(start_frame) / fps)
                                   < Fraction(1, 10 ** 6))
                        check_true(f"{name} clip {i}: audio is exactly as long",
                                   abs(Fraction(t) - Fraction(planned) / fps)
                                   < Fraction(1, 10 ** 6))
                planned_total += planned
                actual_total += kept
                drift.append(planned_total - actual_total)
            check(f"{name}: no drift at any clip boundary", drift, [0] * len(clips))
    finally:
        fw._run_ffmpeg_command_with_logging = original


def test_markers_follow_real_splits(work):
    """CUE-OUT goes on the segment boundary where the encoder split, even
    when the signal time is a few frames off it (it used to go one segment late
    once the gap passed 0.05 s)."""
    print("ad markers on real segment boundaries")
    from hls_toolkit.esam_parser import inject_elemental_markers

    def playlist(path, durations):
        path.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:7\n" + "".join(
            f"#EXTINF:{d:.6f},\nseg_{i:05d}.ts\n" for i, d in enumerate(durations, 1))
            + "#EXT-X-ENDLIST\n")
        return path

    def segment_after_cue(text):
        lines = text.splitlines()
        i = next(n for n, l in enumerate(lines) if l.startswith("#EXT-X-CUE-OUT:"))
        return next(l for l in lines[i:] if l.endswith(".ts"))

    # HEVC break 3: the encoder split at 1800.332, the signal says 1800.399.
    durs = [6.006] * 299 + [1800.332 - 6.006 * 299, 1801.633 - 1800.332, 6.006, 6.006]
    p = playlist(work / "drift.m3u8", durs)
    inject_elemental_markers(p, [{"npt": 1800.399, "duration": 0.0}], {})
    check("a signal 2 frames after the split goes on the split",
          segment_after_cue(p.read_text()), "seg_00301.ts")

    p = playlist(work / "exact.m3u8", durs)
    inject_elemental_markers(p, [{"npt": 1800.332, "duration": 0.0}], {})
    check("a signal on the split is unchanged", segment_after_cue(p.read_text()),
          "seg_00301.ts")

    p = playlist(work / "far.m3u8", [6.0] * 10)
    inject_elemental_markers(p, [{"npt": 15.0, "duration": 0.0}], {})
    check("a signal far from any boundary keeps the old placement (after its segment)",
          segment_after_cue(p.read_text()), "seg_00004.ts")

    # A signal at the very end of the content still goes at the end, not
    # before a short last segment.
    p = playlist(work / "end.m3u8", [6.0] * 5 + [0.3])
    inject_elemental_markers(p, [{"npt": 30.3, "duration": 0.0}], {})
    text = p.read_text()
    check_true("a signal at the end of the content stays at the end",
               text.index("#EXT-X-CUE-OUT:") > text.index("seg_00006.ts"), text)


def test_transcode_scheduling():
    """Cost estimates and the order clips are released into the budget."""
    print("transcode scheduling")
    selected = [{"name": n, "threads": 4} for n in ("1080p", "720p", "540p", "360p")]
    check("four rungs at threads=4 reserve 8 cores",
          fw.estimate_video_task_cores(selected), 8)
    full_hevc = [{"name": "2160p", "threads": 12}] + [
        {"name": n, "threads": 4} for n in ("1440p", "1080p", "720p", "540p", "360p")]
    check("the full H.265 ladder reserves 16, not the old 36",
          fw.estimate_video_task_cores(full_hevc), 16)
    check("a rung without threads counts as one thread",
          fw.estimate_video_task_cores([{"name": "720p"}, {"name": "360p"}]), 1)

    def task(index, start, end, kind):
        return (None, index, [], None, None, None, start, end) + (None,) * 8 + (kind,)

    # Clip lengths as in the 5-clip, 42:50 example: 10:02, 9:09, 7:44, 8:02, 7:49.
    tasks = []
    for i, (s, e) in enumerate([(0, 602), (603, 1152), (1153, 1617), (1617, 2100),
                                (2100, 2570)]):
        tasks += [task(i, s, e, "video"), task(i, s, e, "audio")]
    plan = fw._schedule_transcode_tasks(tasks, video_cost=8)
    check("video before audio", [p["stream_type"] for p in plan],
          ["video"] * 5 + ["audio"] * 5)
    check("longest clip first", [p["task"][1] for p in plan[:5]], [0, 1, 3, 4, 2])
    check("audio reserves one core", {p["cost"] for p in plan[5:]}, {1})


def test_unique_log_dirs(work):
    """A log folder is never reused, even by runs that start at the same time."""
    print("unique log folders")
    import threading
    from hls_toolkit.job_context import claim_unique_dir

    root = work / "logs_unique"
    names = [claim_unique_dir(root, "AETN_AmericanPickers_S10_E03_en").name for _ in range(3)]
    check("repeat runs get _2 and _3", names,
          ["AETN_AmericanPickers_S10_E03_en", "AETN_AmericanPickers_S10_E03_en_2",
           "AETN_AmericanPickers_S10_E03_en_3"])

    (root / "AETN_AmericanPickers_S10_E03_en_2").rmdir()
    check("numbering keeps going after an old folder is deleted",
          claim_unique_dir(root, "AETN_AmericanPickers_S10_E03_en").name,
          "AETN_AmericanPickers_S10_E03_en_4")
    claim_unique_dir(root, "Show_S01_E02")
    check("a name already ending in digits is suffixed, not confused",
          claim_unique_dir(root, "Show_S01_E02").name, "Show_S01_E02_2")

    claimed = []
    barrier = threading.Barrier(12)

    def race():
        barrier.wait()
        claimed.append(claim_unique_dir(root, "Raced").name)

    threads = [threading.Thread(target=race) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("12 simultaneous runs get 12 different folders", len(set(claimed)), 12)


def test_job_progress_isolation(work):
    """Two jobs in one process must not mix their per-clip progress."""
    print("per-job progress")
    from hls_toolkit.job_context import JobContext, bind_context

    jobs = [JobContext("s3://b/Same_Source.mp4", job_id=f"p{i}",
                       log_root=str(work / "logs_progress")) for i in (1, 2)]
    for ctx in jobs:
        ctx.metadata["video_clip_count"] = 2
        ctx.set_stage("TRANSCODING", 0.0)
    bind_context(jobs[0])
    fw._clip_progress_cb(0, "video")(1.0)               # job 1: clip 0 finished
    bind_context(jobs[1])
    fw._clip_progress_cb(1, "video")(0.5)               # job 2: clip 1 half done
    bind_context(None)
    # TRANSCODING spans 12-60%. Job 1: 1 of 2 clips -> 12 + 48*0.5 = 36.
    check("job 1 shows its own progress", jobs[0].progress_pct, 36)
    # Job 2: half of 1 of 2 clips -> 12 + 48*0.25 = 24. A shared table would
    # also count job 1's finished clip and report 48.
    check("job 2 is not credited with job 1's work", jobs[1].progress_pct, 24)
    fw._clear_clip_progress(jobs[1])
    with fw._CLIP_PROGRESS_LOCK:
        left = [k for k in fw._CLIP_PROGRESS if k.startswith(f"{id(jobs[0])}:")]
    check("finishing one job keeps the other's progress", len(left), 1)
    fw._clear_clip_progress(jobs[0])


def test_output_dir_names():
    """Output folder names must be names, never paths out of scratch."""
    print("output folder names")
    from hls_toolkit.runner import validate_output_dir_name
    for good in ("AETN_AmericanPickers_S10_E03_en", "shows/AETN_S10_E03",
                 "Show: Part 1", "  padded  "):
        try:
            check(f"accepts {good!r}", validate_output_dir_name(good), good.strip())
        except ValueError as e:
            check(f"accepts {good!r}", f"rejected: {e}", "accepted")
    for bad in ("/home/ubuntu", "../etc", "a/../b", "a//b", "./x", "~/x",
                "C:\\data", "C:/data", "a\\b", "", "   ", "tab\there", None, 42):
        try:
            validate_output_dir_name(bad)
            check(f"rejects {bad!r}", "accepted", "rejected")
        except ValueError:
            check(f"rejects {bad!r}", "rejected", "rejected")


def test_package_validator(work):
    """Every kind of broken package is caught; a whole one passes."""
    print("package validator")
    from hls_toolkit.job_context import TranscodeError
    from hls_toolkit.package_validator import validate_package

    def build(root, renditions=("1080p", "720p"), segments=3, duration=6.0):
        root.mkdir(parents=True)
        master = ["#EXTM3U", "#EXT-X-VERSION:3"]
        for name in renditions:
            master += [f"#EXT-X-STREAM-INF:BANDWIDTH=1", f"channel_{name}.m3u8"]
            lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:6"]
            for i in range(1, segments + 1):
                seg = f"channel_{name}_{i:05d}.ts"
                (root / seg).write_bytes(b"x" * 188)
                lines += [f"#EXTINF:{duration:.6f},", seg]
            lines.append("#EXT-X-ENDLIST")
            (root / f"channel_{name}.m3u8").write_text("\n".join(lines) + "\n")
        (root / "channel.m3u8").write_text("\n".join(master) + "\n")
        return root

    def problem(label, root, expected_text, **kw):
        try:
            validate_package(root, kw.pop("renditions", ["1080p", "720p"]),
                             expected_duration=kw.pop("expected", 18.0), **kw)
            check(label, "passed", "caught")
        except TranscodeError as e:
            check_true(label, e.stage == "VALIDATING_OUTPUT" and expected_text in str(e),
                       str(e))

    good = build(work / "pkg_good")
    try:
        summary = validate_package(good, ["1080p", "720p"], expected_duration=18.0)
        check("a whole package passes", len(summary["renditions"]), 2)
    except TranscodeError as e:
        check("a whole package passes", f"failed: {e}", "passed")

    root = build(work / "pkg_missing_seg")
    (root / "channel_720p_00002.ts").unlink()
    problem("a missing segment is caught", root, "1 segment(s) missing")

    root = build(work / "pkg_empty_seg")
    (root / "channel_1080p_00003.ts").write_bytes(b"")
    problem("an empty segment is caught", root, "segment(s) are empty")

    root = build(work / "pkg_no_endlist")
    text = (root / "channel_720p.m3u8").read_text().replace("#EXT-X-ENDLIST", "")
    (root / "channel_720p.m3u8").write_text(text)
    problem("a playlist without ENDLIST is caught", root, "no #EXT-X-ENDLIST")

    root = build(work / "pkg_missing_playlist")
    (root / "channel_720p.m3u8").unlink()
    problem("a master pointing at a missing playlist is caught", root, "does not exist")

    root = build(work / "pkg_missing_rung", renditions=("1080p",))
    problem("a requested rendition missing from the master is caught", root,
            "does not list rendition 720p")

    root = build(work / "pkg_short")
    problem("a rendition shorter than expected is caught", root, "runs 18.000s",
            expected=60.0)

    root = build(work / "pkg_no_thumbs")
    problem("missing thumbnails are caught when requested", root, "thumbnails",
            expect_thumbnails=True)


def main():
    work = Path(tempfile.mkdtemp(prefix="aitranscoder_smoke_"))
    try:
        test_time_utils()
        test_subtitles(work)
        variant, segments = test_playlists(work)
        test_esam(work, variant, segments)
        test_ffmpeg_command()
        test_clip_cuts_frame_exact()
        test_markers_follow_real_splits(work)
        test_hevc_codec_string()
        test_duplicate_rung_guard()
        test_output_dir_names()
        test_package_validator(work)
        test_cpu_budget(work)
        test_disk_budget(work)
        test_housekeeping(work)
        test_transcode_scheduling()
        test_unique_log_dirs(work)
        test_job_progress_isolation(work)
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
