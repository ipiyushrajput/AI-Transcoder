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


def main():
    work = Path(tempfile.mkdtemp(prefix="aitranscoder_smoke_"))
    try:
        test_time_utils()
        test_subtitles(work)
        variant, segments = test_playlists(work)
        test_esam(work, variant, segments)
        test_ffmpeg_command()
        test_hevc_codec_string()
        test_duplicate_rung_guard()
        test_cpu_budget(work)
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
