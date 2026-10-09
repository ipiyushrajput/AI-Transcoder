#!/usr/bin/env python3
"""End-to-end test of the S3 -> transcode -> S3 pipeline and the HTTP API.

Runs the real workflow, per-channel logging, progress tracking, S3 fetch/publish,
parallel jobs sharing the CPU budget, and (when MySQL is reachable) the database
layer — substituting a stub FFmpeg and a filesystem-backed S3 so it needs neither
the custom encoder build nor AWS credentials.

    python tests/pipeline_test.py

The database checks need MySQL and the DB_* settings (see .env.example); without
them those checks fail and the rest still run.
"""
import json
import os
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.fakes import fake_s3  # noqa: E402

FAILURES = []
BUCKET = "dev-us-west-2-transcoder-bucket"
CHANNEL = "AETN_AmericanPickers_S10_E03_en"


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n       got:  {got!r}\n       want: {want!r}")
        FAILURES.append(label)


def check_true(label, value, detail=""):
    check(label + (f" ({detail})" if detail and not value else ""), bool(value), True)


def make_fake_binaries(work: Path):
    """Install fake ffmpeg/ffprobe executables and return their paths."""
    bin_dir = work / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    source = ROOT / "tests" / "fakes" / "fake_ffmpeg.py"
    paths = {}
    for role in ("ffmpeg", "ffprobe"):
        target = bin_dir / role
        target.write_text(f"#!/usr/bin/env python3\n"
                          f"import os, runpy\n"
                          f"os.environ['FAKE_ROLE'] = {role!r}\n"
                          f"runpy.run_path({str(source)!r}, run_name='__main__')\n")
        target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        paths[role] = str(target)
    return paths


def build_config(work: Path, binaries: dict) -> dict:
    """The shipped config, retargeted at the fakes and a short clip list."""
    config = json.loads((ROOT / "config.json").read_text())
    config["paths"] = {"ffmpeg_executable": binaries["ffmpeg"],
                       "ffprobe_executable": binaries["ffprobe"]}
    config["defaults"]["InputClippings"] = [
        {"StartTimecode": "00:00:00:00", "EndTimecode": "00:00:20:00"},
        {"StartTimecode": "00:00:20:01", "EndTimecode": "00:00:40:00"},
    ]
    config["defaults"]["resolutions"] = "1080p,720p"
    config["defaults"]["output_dir"] = CHANNEL
    return config


def test_pipeline(work: Path, binaries: dict):
    """The full runner path: S3 in, transcode, S3 out, local cleanup."""
    print("pipeline (s3 -> transcode -> s3)")
    from hls_toolkit import s3_io
    from hls_toolkit.runner import run_transcode_job

    client = fake_s3.install(s3_io, work / "s3")
    video_uri = client.put(BUCKET, f"Visionular/{CHANNEL}.mp4", os.urandom(4096))
    client.put(BUCKET, f"Visionular/{CHANNEL}.vtt",
               b"WEBVTT\n\n1\n00:00:01.000 --> 00:00:04.000\nHello.\n")

    config = build_config(work, binaries)
    log_root = work / "logs"
    result = run_transcode_job(
        config,
        overrides={"input_video": video_uri, "esam": True, "audio_norm": True,
                   "thumbnails_enabled": True, "upload": True},
        job_id="test-job-0001",
        log_root=str(log_root),
        work_root=str(work / "scratch"))

    check("status", result["status"], "COMPLETED")
    check("channel derived from S3 key", result["channel"], CHANNEL)
    check("progress reaches 100", result["progress_pct"], 100)
    check_true("input downloaded from S3", client.downloads)

    keys = client.keys_under(BUCKET, f"Visionular/V3/{CHANNEL}")
    check_true("master playlist uploaded",
               any(k.endswith("channel.m3u8") for k in keys))
    check_true("1080p variant uploaded",
               any(k.endswith("channel_1080p.m3u8") for k in keys))
    check_true("720p variant uploaded",
               any(k.endswith("channel_720p.m3u8") for k in keys))
    check_true("segments uploaded", any(k.endswith(".ts") for k in keys))
    check_true("subtitle segments uploaded", any(k.endswith(".vtt") for k in keys))
    check_true("thumbnails uploaded", any("thumbnails/" in k for k in keys))
    check("playback url points at S3",
          result["metadata"]["playback_url"],
          f"s3://{BUCKET}/Visionular/V3/{CHANNEL}/channel.m3u8")

    content_types = {u["key"].rsplit(".", 1)[-1]: u["content_type"]
                     for u in client.uploads}
    check("m3u8 content type", content_types.get("m3u8"),
          "application/vnd.apple.mpegurl")
    check("ts content type", content_types.get("ts"), "video/mp2t")
    check("vtt content type", content_types.get("vtt"), "text/vtt")

    # Nothing should be left on local disk after a verified upload.
    scratch = Path(result["metadata"]["work_dir"])
    check("scratch directory removed", scratch.exists(), False)

    log_dir = Path(result["log_dir"])
    check("log dir is logs/<channel>/<job_id>",
          log_dir.relative_to(log_root).as_posix(), f"{CHANNEL}/test-job-0001")
    for name in ("job.log", "error.log", "ffmpeg.log", "job.json"):
        check_true(f"{name} written", (log_dir / name).exists())
    ffmpeg_log = (log_dir / "ffmpeg.log").read_text()
    check_true("ffmpeg.log captured encoder commands", "libwz264" in ffmpeg_log)
    check_true("ffmpeg.log captured progress lines", "bitrate=" in ffmpeg_log)
    meta = json.loads((log_dir / "job.json").read_text())
    check("job.json records completion", meta["status"], "COMPLETED")
    return client


def test_failure_reporting(work: Path, binaries: dict):
    """A broken input must fail loudly, with the stage and a readable message."""
    print("failure handling")
    from hls_toolkit import s3_io
    from hls_toolkit.runner import run_transcode_job

    fake_s3.install(s3_io, work / "s3")
    config = build_config(work, binaries)
    result = run_transcode_job(
        config,
        overrides={"input_video": f"s3://{BUCKET}/Visionular/missing-asset.mp4",
                   "upload": False},
        job_id="test-job-fail",
        log_root=str(work / "logs"),
        work_root=str(work / "scratch"))

    check("failed status", result["status"], "FAILED")
    check("stage recorded", result["stage"], "FETCHING_INPUT")
    check_true("error names the missing object",
               "not found" in (result["error_message"] or "").lower())
    error_log = Path(result["log_dir"]) / "error.log"
    check_true("error.log has the failure", "FAILED" in error_log.read_text())


def test_api(work: Path, binaries: dict):
    """Submit through HTTP, poll status, then read back detail, logs and listing."""
    print("api")
    from hls_toolkit import s3_io
    client = fake_s3.install(s3_io, work / "s3")
    video_uri = client.put(BUCKET, f"Visionular/{CHANNEL}.mp4", os.urandom(4096))

    config = build_config(work, binaries)
    config_path = work / "api-config.json"
    config_path.write_text(json.dumps(config))

    os.environ["LOG_ROOT"] = str(work / "api-logs")
    os.environ["WORK_ROOT"] = str(work / "api-scratch")
    os.environ["PROGRESS_SYNC_SECONDS"] = "0.5"
    Path(os.environ["WORK_ROOT"]).mkdir(parents=True, exist_ok=True)

    from api.app import create_app
    app = create_app(str(config_path))
    http = app.test_client()

    check("health ok", http.get("/health").get_json()["status"], "ok")

    bad = http.post("/api/v1/jobs", json={"template": "does_not_exist"})
    check("unknown template rejected", bad.status_code, 400)
    check_true("rejection explains why",
               "Unknown template" in bad.get_json()["error"])

    bad_input = http.post("/api/v1/jobs", json={"input_video": "/no/such/file.mp4"})
    check("missing local input rejected", bad_input.status_code, 400)

    bad_output = http.post("/api/v1/jobs", json={"input_video": video_uri,
                                                 "output_dir": "/home/ubuntu"})
    check("an output path is rejected with 400", bad_output.status_code, 400)

    response = http.post("/api/v1/jobs", json={
        "name": "American Pickers S10E03",
        "input_video": video_uri,
        "subtitle_file": None,
        "resolutions": "720p",
        "esam": False,
        "audio_norm": False,
        "generate_thumbnails": False,
        "upload": True,
    })
    check("job accepted", response.status_code, 202)
    body = response.get_json()
    job_id = body["job_id"]
    check("channel in response", body["channel"], CHANNEL)

    deadline = time.time() + 120
    status = {}
    while time.time() < deadline:
        status = http.get(f"/api/v1/jobs/{job_id}/status").get_json()
        if status["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        time.sleep(0.5)

    check("job completed via API", status["status"], "COMPLETED")
    check("progress is 100", status["progress_pct"], 100)
    check_true("status carries the S3 output", str(status.get("output_prefix", "")).startswith("s3://"))

    detail = http.get(f"/api/v1/jobs/{job_id}").get_json()
    check("detail returns the job", detail["job_id"], job_id)
    check("detail lists clips", len(detail.get("clips", [])), 2)
    check("detail lists the selected rung", [v["name"] for v in detail.get("variants", [])],
          ["720p"])

    listing = http.get("/api/v1/jobs?per_page=10").get_json()
    check_true("listing includes the job",
               any(j["job_id"] == job_id for j in listing["jobs"]))
    check("listing is served from the database", listing.get("source"), "database")

    filtered = http.get("/api/v1/jobs?status=COMPLETED").get_json()
    check_true("status filter works",
               all(j["status"] == "COMPLETED" for j in filtered["jobs"]))

    # The whole log, so the check does not hinge on where in it the encoder
    # lines fall — that depends on the order clips happen to be scheduled.
    logs = http.get(f"/api/v1/jobs/{job_id}/logs?type=ffmpeg&tail=5000").get_json()
    check_true("ffmpeg logs retrievable", "libwz" in logs.get("content", ""))
    short = http.get(f"/api/v1/jobs/{job_id}/logs?type=ffmpeg&tail=5").get_json()
    check_true("tail limits the lines returned",
               0 < len(short.get("content", "").splitlines()) <= 5)
    job_logs = http.get(f"/api/v1/jobs/{job_id}/logs?type=job").get_json()
    check_true("job logs retrievable", "[stage]" in job_logs.get("content", ""))
    bad_log = http.get(f"/api/v1/jobs/{job_id}/logs?type=nope")
    check("unknown log type rejected", bad_log.status_code, 400)

    check("status 404 for unknown job",
          http.get("/api/v1/jobs/nope/status").status_code, 404)
    check("delete removes the job",
          http.delete(f"/api/v1/jobs/{job_id}").get_json()["deleted"], True)
    check("deleted job is gone",
          http.get(f"/api/v1/jobs/{job_id}").status_code, 404)


def test_parallel_cli_jobs(work: Path, binaries: dict):
    """Three `python app.py --config ...` runs started together, on one source.

    They must share one CPU budget (never reserving more cores than it holds),
    still encode several clips at once each, and log to separate folders.
    """
    print("parallel jobs (3 x app.py at once)")
    import subprocess
    import time

    budget, video_cost = 16, 8               # 4 H.265 rungs at threads=4 -> 8 cores
    media = work / "parallel"
    media.mkdir(parents=True, exist_ok=True)
    source = media / f"{CHANNEL}.mp4"
    source.write_bytes(os.urandom(4096))
    timeline = media / "timeline.jsonl"
    log_root = media / "logs"

    # Hold every core first, so all three jobs are already waiting when the
    # first one is let in. Otherwise whichever process starts fastest is alone
    # in the queue for its first clips, and turn-taking cannot be observed.
    from hls_toolkit.cpu_budget import CpuBudget
    gate = CpuBudget(budget, str(media / "slots"))
    blocker = gate.acquire(budget)

    procs = []
    for k in (1, 2, 3):
        config = build_config(work, binaries)
        config["defaults"].update({
            "input_video": str(source), "subtitle_file": None, "template": "h265_standard",
            "resolutions": "1080p,720p,540p,360p", "output_dir": f"job{k}",
            "InputClippings": [
                {"StartTimecode": "00:00:00:00", "EndTimecode": "00:00:30:00"},
                {"StartTimecode": "00:00:30:01", "EndTimecode": "00:01:00:00"},
                {"StartTimecode": "00:01:00:01", "EndTimecode": "00:01:30:00"}]})
        for rung in config["video_templates"]["h265_standard"]:
            rung["threads"] = 4
        path = media / f"config{k}.json"
        path.write_text(json.dumps(config))
        env = dict(os.environ, FAKE_MEDIA_DURATION="100", FAKE_TIMELINE=str(timeline),
                   FAKE_SECONDS_PER_MEDIA_MINUTE="3", FAKE_JOB_LABEL=f"job{k}",
                   WZ_CPU_BUDGET=str(budget), WZ_CPU_SLOT_DIR=str(media / "slots"))
        procs.append(subprocess.Popen(
            [sys.executable, "app.py", "--config", str(path), "--no-upload",
             "--no-esam", "--no-audio-norm", "--no-generate-thumbnails",
             "--work-dir", str(media / "scratch"), "--log-dir", str(log_root)],
            cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL))
    queue_dir = media / "slots" / "queue"
    deadline = time.time() + 120
    while time.time() < deadline:
        if len([n for n in os.listdir(queue_dir) if not n.startswith(".")]) >= 3:
            break
        time.sleep(0.1)
    check_true("all three jobs queued for cores while they were taken",
               len([n for n in os.listdir(queue_dir) if not n.startswith(".")]) >= 3)
    blocker.release()

    codes = [p.wait(timeout=600) for p in procs]
    check("all three jobs succeed", codes, [0, 0, 0])

    events = [json.loads(line) for line in timeline.read_text().splitlines()]
    cost = {"video": video_cost, "audio": 1}

    def peak(selected):
        return max(sum(cost[e["kind"]] for e in selected if e["start"] <= t < e["end"])
                   for t in {e["start"] for e in selected})

    check_true("the shared budget is never over-committed",
               peak(events) <= budget, f"peak {peak(events)} of {budget}")
    by_job = {}
    for event in events:
        if event["kind"] == "video":
            by_job.setdefault(event["job"], []).append(event)
    check("every job encoded all its clips", sorted(len(v) for v in by_job.values()),
          [3, 3, 3])
    # Jobs waiting together must take turns: in the order clips started, every
    # run of three holds one clip from each job. Serving one job's clips back
    # to back — what made three parallel jobs finish at very different times —
    # would put the same job twice in a group.
    order = [e["job"] for e in sorted(
        (e for e in events if e["kind"] == "video"), key=lambda e: e["start"])]
    groups = [order[i:i + 3] for i in range(0, len(order), 3)]
    check_true("waiting jobs take turns clip by clip",
               all(len(set(g)) == 3 for g in groups), f"start order {order}")

    folders = sorted(p.parent.name for p in log_root.rglob("job.log"))
    check("three log folders for three runs of one source", folders,
          [CHANNEL, f"{CHANNEL}_2", f"{CHANNEL}_3"])
    outputs = sorted(json.loads((log_root / f / "job.json").read_text())["metadata"]
                     ["output_dir_name"] for f in folders)
    check("each folder holds a different job", outputs, ["job1", "job2", "job3"])
    headers = [(log_root / f / "job.log").read_text().count("Channel :") for f in folders]
    check("no folder mixes two jobs' logs", headers, [1, 1, 1])


def test_single_job_uses_the_budget(work: Path, binaries: dict):
    """A lone job overlaps its clips when the budget has room (it used to run
    an H.265 ladder one clip at a time)."""
    print("single job overlaps its clips")
    from hls_toolkit import cpu_budget
    from hls_toolkit.runner import run_transcode_job

    media = work / "single"
    media.mkdir(parents=True, exist_ok=True)
    source = media / f"{CHANNEL}.mp4"
    source.write_bytes(os.urandom(4096))
    timeline = media / "timeline.jsonl"
    config = build_config(work, binaries)
    config["defaults"].update({"subtitle_file": None, "template": "h265_standard",
                               "resolutions": "1080p,720p,540p,360p"})
    for rung in config["video_templates"]["h265_standard"]:
        rung["threads"] = 4                  # 4 rungs x 4 threads / 2 -> 8 cores
    previous = {k: os.environ.get(k) for k in
                ("FAKE_TIMELINE", "FAKE_SECONDS_PER_MEDIA_MINUTE", "FAKE_MEDIA_DURATION")}
    os.environ.update(FAKE_TIMELINE=str(timeline), FAKE_SECONDS_PER_MEDIA_MINUTE="3",
                      FAKE_MEDIA_DURATION="100")
    cpu_budget.reset_shared_budget()
    cpu_budget.get_shared_budget(32, slot_dir=str(media / "slots"))
    try:
        result = run_transcode_job(
            config, overrides={"input_video": str(source), "upload": False,
                               "esam": False, "audio_norm": False,
                               "thumbnails_enabled": False},
            job_id="single", log_root=str(media / "logs"),
            work_root=str(media / "scratch"))
    finally:
        cpu_budget.reset_shared_budget()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    check("single job completes", result["status"], "COMPLETED")
    videos = [json.loads(l) for l in timeline.read_text().splitlines()
              if json.loads(l)["kind"] == "video"]
    overlap = max(sum(v["start"] <= t < v["end"] for v in videos)
                  for t in {v["start"] for v in videos})
    check("both clips encode at once on a 32-core budget", overlap, 2)


def _quick_overrides(source, **extra):
    """Overrides for a fast job: no ESAM, loudnorm or thumbnails."""
    overrides = {"input_video": str(source), "esam": False, "audio_norm": False,
                 "thumbnails_enabled": False}
    overrides.update(extra)
    return overrides


def test_output_folder_safety(work: Path, binaries: dict):
    """An output name that is a path must never reach outside scratch."""
    print("output folder safety")
    from hls_toolkit import s3_io
    from hls_toolkit.job_context import TranscodeError
    from hls_toolkit.runner import run_transcode_job

    client = fake_s3.install(s3_io, work / "s3_safety")
    media = work / "safety"
    media.mkdir(parents=True, exist_ok=True)
    source = media / f"{CHANNEL}.mp4"
    source.write_bytes(os.urandom(4096))
    victim = media / "unrelated_folder"
    victim.mkdir()
    (victim / "contract.pdf").write_text("was here before the job")
    config = build_config(work, binaries)
    config["defaults"]["subtitle_file"] = None

    for label, name in (("absolute path", str(victim)),
                        ("'..' path", f"../../../{victim.name}"),
                        ("hidden '..' segment", f"ok/../{victim.name}")):
        try:
            run_transcode_job(config, overrides=_quick_overrides(
                source, upload=True, output_dir=name),
                log_root=str(media / "logs"), work_root=str(media / "scratch"))
            check(f"{label} refused", "accepted", "refused")
        except TranscodeError as e:
            check(f"{label} refused at validation", e.stage, "VALIDATION")
    check_true("the unrelated folder is untouched", (victim / "contract.pdf").exists())
    check_true("nothing was uploaded", not any(
        k.endswith("contract.pdf") for k in client.keys_under(BUCKET)))


def test_local_copies(work: Path, binaries: dict):
    """--no-upload, --keep-local and a failed upload all leave a usable package."""
    print("local copies of the package")
    from hls_toolkit import s3_io
    from hls_toolkit.runner import run_transcode_job

    client = fake_s3.install(s3_io, work / "s3_local")
    media = work / "local"
    media.mkdir(parents=True, exist_ok=True)
    source = media / f"{CHANNEL}.mp4"
    source.write_bytes(os.urandom(4096))
    local_root = media / "hls_output"
    config = build_config(work, binaries)
    config["defaults"]["subtitle_file"] = None

    def run(**extra):
        return run_transcode_job(
            config, overrides=_quick_overrides(source, local_output_dir=str(local_root),
                                               **extra),
            log_root=str(media / "logs"), work_root=str(media / "scratch"))

    result = run(upload=False, output_dir="show")
    saved = Path(result["output_prefix"])
    check("--no-upload completes", result["status"], "COMPLETED")
    check("saved under the local output folder", saved, local_root / "show")
    check_true("the package survives the job", (saved / "channel.m3u8").exists())
    check("playback points at the saved copy", result["metadata"]["playback_url"],
          str(saved / "channel.m3u8"))

    again = run(upload=False, output_dir="show")
    check("a second run never overwrites the first", Path(again["output_prefix"]).name,
          "show_2")
    check_true("the first copy is still intact", (saved / "channel.m3u8").exists())

    kept = run(upload=True, delete_local_output=False, output_dir="kept")
    check("--keep-local completes", kept["status"], "COMPLETED")
    check_true("--keep-local uploads to S3",
               any(k.endswith("kept/channel.m3u8") for k in client.keys_under(BUCKET)))
    check_true("--keep-local also keeps a local copy",
               (local_root / "kept" / "channel.m3u8").exists())

    original_upload = client.upload_file

    def flaky_upload(filename, bucket, key, ExtraArgs=None, Config=None):
        if key.endswith(".ts"):
            raise OSError("simulated network failure")
        return original_upload(filename, bucket, key, ExtraArgs=ExtraArgs, Config=Config)

    client.upload_file = flaky_upload
    try:
        failed = run(upload=True, output_dir="retry_me")
    finally:
        client.upload_file = original_upload
    check("a failed upload fails the job", failed["status"], "FAILED")
    check_true("the finished package is saved for a retry",
               (local_root / "retry_me" / "channel.m3u8").exists())
    check_true("the error says where it is and how to retry",
               "--upload-only" in (failed["error_message"] or "")
               and str(local_root / "retry_me") in failed["error_message"])


def test_safe_publish(work: Path, binaries: dict):
    """Republishing never takes the live output down, and the master goes last."""
    print("safe publish")
    from hls_toolkit import s3_io
    from hls_toolkit.runner import run_transcode_job

    client = fake_s3.install(s3_io, work / "s3_publish")
    media = work / "publish"
    media.mkdir(parents=True, exist_ok=True)
    source = media / f"{CHANNEL}.mp4"
    source.write_bytes(os.urandom(4096))
    config = build_config(work, binaries)
    config["defaults"]["subtitle_file"] = None
    prefix = "Visionular/V3/live_show"

    # A previous run's package is live, including a rendition this run drops.
    for name in ("channel.m3u8", "channel_1080p.m3u8", "channel_1080p_00001.ts",
                 "channel_2160p.m3u8", "channel_2160p_00001.ts"):
        client.put(BUCKET, f"{prefix}/{name}", b"previous run")

    live_during_upload = []
    original_upload = client.upload_file

    def watching_upload(filename, bucket, key, ExtraArgs=None, Config=None):
        live_during_upload.append(
            (client._path(bucket, f"{prefix}/channel.m3u8")).is_file())
        return original_upload(filename, bucket, key, ExtraArgs=ExtraArgs, Config=Config)

    client.upload_file = watching_upload
    client.undeletable = {f"{prefix}/channel_2160p_00001.ts"}
    try:
        result = run_transcode_job(
            config, overrides=_quick_overrides(source, upload=True, output_dir="live_show",
                                               resolution="1080p,720p"),
            log_root=str(media / "logs"), work_root=str(media / "scratch"))
    finally:
        client.upload_file = original_upload
        client.undeletable = set()

    check("republish completes", result["status"], "COMPLETED")
    check_true("the old master stayed live for the whole upload",
               live_during_upload and all(live_during_upload))
    order = [u["key"].rsplit("/", 1)[-1] for u in client.uploads
             if u["key"].startswith(prefix + "/")]
    first_playlist = min(i for i, k in enumerate(order) if k.endswith(".m3u8"))
    check_true("every segment is uploaded before any playlist",
               all(not k.endswith(".m3u8") for k in order[:first_playlist])
               and all(k.endswith(".m3u8") for k in order[first_playlist:]))
    check("the master playlist is uploaded last", order[-1], "channel.m3u8")
    keys = {k.rsplit("/", 1)[-1] for k in client.keys_under(BUCKET, prefix)}
    check_true("the dropped rendition's playlist is removed afterwards",
               "channel_2160p.m3u8" not in keys)
    check_true("a stale object S3 refused to delete is reported, not hidden",
               result["metadata"].get("stale_objects_not_removed") == 1)

    # An upload that fails part-way leaves the previous package fully live.
    client.put(BUCKET, f"{prefix}/channel.m3u8", b"previous run")

    def failing_upload(filename, bucket, key, ExtraArgs=None, Config=None):
        if key.endswith("_00002.ts"):
            raise OSError("simulated network failure")
        return original_upload(filename, bucket, key, ExtraArgs=ExtraArgs, Config=Config)

    client.upload_file = failing_upload
    try:
        failed = run_transcode_job(
            config, overrides=_quick_overrides(source, upload=True, output_dir="live_show",
                                               resolution="1080p,720p",
                                               local_output_dir=str(media / "saved")),
            log_root=str(media / "logs"), work_root=str(media / "scratch"))
    finally:
        client.upload_file = original_upload
    check("a failed republish fails the job", failed["status"], "FAILED")
    check("the live master is still the previous one",
          client._path(BUCKET, f"{prefix}/channel.m3u8").read_bytes(), b"previous run")


def main():
    work = Path(tempfile.mkdtemp(prefix="aitx_pipeline_test_"))
    print(f"workspace: {work}\n")
    binaries = make_fake_binaries(work)
    try:
        test_pipeline(work, binaries)
        print()
        test_failure_reporting(work, binaries)
        print()
        test_output_folder_safety(work, binaries)
        print()
        test_local_copies(work, binaries)
        print()
        test_safe_publish(work, binaries)
        print()
        test_single_job_uses_the_budget(work, binaries)
        print()
        test_parallel_cli_jobs(work, binaries)
        print()
        test_api(work, binaries)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
