#!/usr/bin/env python3
"""End-to-end test of the S3 -> transcode -> S3 pipeline and the HTTP API.

Runs the real workflow, per-channel logging, progress tracking, S3 fetch/publish
and (when PostgreSQL is reachable) the database layer — substituting a stub
FFmpeg and a filesystem-backed S3 so it needs neither the custom encoder build
nor AWS credentials.

    python tests/pipeline_test.py
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

    logs = http.get(f"/api/v1/jobs/{job_id}/logs?type=ffmpeg&tail=50").get_json()
    check_true("ffmpeg logs retrievable", "libwz" in logs.get("content", ""))
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


def main():
    work = Path(tempfile.mkdtemp(prefix="aitx_pipeline_test_"))
    print(f"workspace: {work}\n")
    binaries = make_fake_binaries(work)
    try:
        test_pipeline(work, binaries)
        print()
        test_failure_reporting(work, binaries)
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
