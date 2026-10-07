# API reference

Base URL: `http://<host>:8000`
All request and response bodies are JSON.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness, database state, queue depth |
| `POST` | `/api/v1/jobs` (alias `/api/v1/jobs/start`) | Queue a transcoding job |
| `GET` | `/api/v1/jobs` | List jobs (paginated, filterable) |
| `GET` | `/api/v1/jobs/<job_id>` | Full record: settings, ladder, clips, config snapshot |
| `GET` | `/api/v1/jobs/<job_id>/status` | Status and progress percentage |
| `GET` | `/api/v1/jobs/<job_id>/logs` | Tail `job.log`, `error.log` or `ffmpeg.log` |
| `POST` | `/api/v1/jobs/<job_id>/cancel` | Stop a running job |
| `DELETE` | `/api/v1/jobs/<job_id>` | Delete a finished job's rows |
| `GET` | `/api/v1/templates` | Configured encoding ladders |
| `GET` | `/api/v1/config` | The server's active configuration |

---

## POST /api/v1/jobs

Queues a job and returns immediately with `202`. Every field is optional —
anything omitted falls back to `config.json`, so an empty body `{}` runs the
server's configured defaults.

```jsonc
{
  "name": "American Pickers S10E03",           // label; defaults to the channel name

  "input_video":  "s3://bucket/path/asset.mp4", // local path or s3:// URI
  "subtitle_file":"s3://bucket/path/asset.vtt", // local path, s3:// URI, or null
  "subtitle_language": "en",

  "output_dir": "AETN_AmericanPickers_S10_E03_en",  // S3 folder under s3.key_prefix
  "template": "h264_standard",
  "resolutions": "1080p,720p",

  "esam": true,
  "audio_norm": true,
  "generate_thumbnails": true,
  "upload": true,
  "duration": 120,                              // transcode only the first N seconds
  "transcode_workers": 4,                       // optional cap on clips encoding at once;
                                                // omit to let the shared CPU budget decide

  "clippings": [                                // replaces defaults.InputClippings
    {"StartTimecode": "00:00:00:00", "EndTimecode": "00:10:02:27"}
  ],
  "hls_settings": {"hls_time": 6},

  "s3_bucket": "dev-us-west-2-transcoder-bucket", // override the configured bucket
  "s3_key_prefix": "Visionular/V3",

  "esam_scc_xml": "<?xml ...",                  // per-job ESAM, overrides config
  "esam_mcc_xml": "<?xml ...",

  "config": { "audio_normalization": { "loudnorm_settings": {"i": -23} } }
}
```

`202 Accepted`:

```json
{
  "message": "Transcoding job queued.",
  "job_id": "1ca7831b-c27c-469a-a0c1-41cb4b62a71d",
  "name": "American Pickers S10E03",
  "channel": "AETN_AmericanPickers_S10_E03_en",
  "status": "PENDING",
  "log_dir": "logs/AETN_AmericanPickers_S10_E03_en/1ca7831b-...",
  "status_url": "/api/v1/jobs/1ca7831b-.../status"
}
```

`400 Bad Request` when the request cannot run. Validation happens before a job
id is issued, so a rejected request never appears in the job list:

```json
{"error": "Resolutions not in template 'h264_standard': 4k. Available: 1080p, 360p, 540p, 720p"}
```

Rejected for: missing/unreadable input, unknown template, unknown resolution,
`upload` with no bucket configured, or a missing FFmpeg binary.

---

## GET /api/v1/jobs/&lt;job_id&gt;/status

Served from the live job while it runs, from MySQL afterwards (`source`
tells you which).

```json
{
  "job_id": "1ca7831b-...",
  "channel": "AETN_AmericanPickers_S10_E03_en",
  "status": "RUNNING",
  "stage": "TRANSCODING",
  "progress_pct": 41,
  "output_prefix": null,
  "playback_url": null,
  "uploaded_files": 0,
  "started_at": "2026-01-31T10:12:03.114+00:00",
  "finished_at": null,
  "elapsed_seconds": 512.4,
  "log_dir": "logs/AETN_AmericanPickers_S10_E03_en/1ca7831b-...",
  "source": "live"
}
```

`status` is one of `PENDING`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`.

On `COMPLETED`, `progress_pct` is 100 and `playback_url` points at the master
playlist in S3. On `FAILED`, the response also carries `error_message`,
`error_stage` and an `error_log_tail` with the last 40 lines of `error.log` — so
a failure is diagnosable from the status call alone.

### Progress model

`progress_pct` is stage-weighted, not linear in wall-clock time:

| Stage | Range | What is happening |
| --- | --- | --- |
| `QUEUED` | 0 | Waiting for a worker slot |
| `FETCHING_INPUT` | 0–5 | Downloading source (and subtitles) from S3 |
| `PROBING` | 5–8 | `ffprobe` for duration and frame rate |
| `ANALYZING_AUDIO` | 8–12 | loudnorm analysis pass |
| `TRANSCODING` | 12–60 | Per-clip, per-rendition encoding (the long one) |
| `MERGING` | 60–68 | Concat of clips into continuous tracks |
| `PACKAGING_HLS` | 68–80 | HLS segmentation per rendition |
| `SUBTITLES` | 80–84 | VTT clip, merge and segment |
| `MANIFEST` | 84–86 | Master playlist |
| `AD_MARKERS` | 86–88 | SCTE-35 / ESAM injection |
| `THUMBNAILS` | 88–90 | Thumbnail extraction |
| `UPLOADING` | 90–99 | Publish to S3 and verify |
| `CLEANUP` / `DONE` | 99–100 | Remove local scratch |

Inside `TRANSCODING` the figure advances from FFmpeg's own `time=` output, so it
keeps moving during a long encode rather than jumping per clip.

---

## GET /api/v1/jobs

```
/api/v1/jobs?page=1&per_page=20&status=RUNNING&channel=AETN_AmericanPickers_S10_E03_en
```

```json
{ "total": 42, "page": 1, "per_page": 20, "source": "database", "jobs": [ ... ] }
```

Newest first. Rows for running jobs carry live `stage` and `progress_pct`.

---

## GET /api/v1/jobs/&lt;job_id&gt;

The full record — every setting, plus `variants` (the ladder used), `clips`, and
`config_snapshot` / `request_payload`, which together are enough to reproduce the
job exactly.

---

## GET /api/v1/jobs/&lt;job_id&gt;/logs

```
/api/v1/jobs/<job_id>/logs?type=error&tail=200
```

`type` is `job` (default), `error` or `ffmpeg`; `tail` defaults to 200, max 5000.

```json
{
  "job_id": "1ca7831b-...",
  "type": "ffmpeg",
  "path": "logs/AETN_.../1ca7831b-.../ffmpeg.log",
  "lines_returned": 200,
  "total_lines": 4821,
  "content": "..."
}
```

---

## POST /api/v1/jobs/&lt;job_id&gt;/cancel

Signals the job; the current FFmpeg process group is terminated and the scratch
directory cleaned up. `200` when cancellation was accepted, `409` when the job
already finished or is not running on this process.

---

## Examples

```bash
# Queue a job
JOB=$(curl -sX POST localhost:8000/api/v1/jobs \
  -H 'Content-Type: application/json' \
  -d '{
        "input_video":  "s3://dev-us-west-2-transcoder-bucket/Visionular/AETN_AmericanPickers_S10_E03_en.mp4",
        "subtitle_file":"s3://dev-us-west-2-transcoder-bucket/Visionular/AETN_AmericanPickers_S10_E03_en.vtt",
        "output_dir":   "AETN_AmericanPickers_S10_E03_en",
        "template": "h264_standard",
        "resolutions": "1080p,720p,540p,360p",
        "esam": true, "audio_norm": true, "generate_thumbnails": true
      }' | python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')

# Watch it
watch -n5 "curl -s localhost:8000/api/v1/jobs/$JOB/status | python3 -m json.tool"

# Why did it fail?
curl -s "localhost:8000/api/v1/jobs/$JOB/logs?type=error&tail=100" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["content"])'

# Stop it
curl -sX POST localhost:8000/api/v1/jobs/$JOB/cancel
```

Python client:

```python
import requests, time

API = "http://localhost:8000/api/v1"
job = requests.post(f"{API}/jobs", json={
    "input_video": "s3://my-bucket/input/movie.mp4",
    "output_dir": "movie",
    "resolutions": "1080p,720p",
}).json()

while True:
    status = requests.get(f"{API}/jobs/{job['job_id']}/status").json()
    print(f"{status['progress_pct']:3d}%  {status['stage']}")
    if status["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
        break
    time.sleep(10)

if status["status"] == "FAILED":
    print(status["error_message"])
    print(status["error_log_tail"])
else:
    print("Output:", status["playback_url"])
```
