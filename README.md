# AI-Transcoder

VOD HLS transcoding service built on a custom FFmpeg build whose H.264 / H.265
encoders were fine-tuned in house and ship as **`libwz264`** and **`libwz265`**.

It reads a source video (and optional WebVTT subtitles) **from S3 or local
disk**, clips it, transcodes it into an ABR ladder, packages it as HLS, injects
SCTE-35 ad markers from ESAM signalling, generates thumbnails, and **publishes
straight back to S3** — removing the local copy once every object is verified.

It runs two ways:

* **CLI** — `python app.py --config config.json`
* **HTTP service** — submit jobs over an API, poll progress, read per-job logs,
  and list job history from PostgreSQL

---

## Quick start

```bash
git clone <this-repo> && cd AI-Transcoder
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# The custom FFmpeg build (must provide libwz264 / libwz265)
mkdir -p bin
cp /path/to/ffmpeg bin/ffmpeg && cp /path/to/ffprobe bin/ffprobe
chmod +x bin/ffmpeg bin/ffprobe

# Edit inputs, ladder and S3 destination
$EDITOR config.json
```

**As a CLI:**

```bash
python app.py --config config.json
```

**As a service:**

```bash
cp .env.example .env && $EDITOR .env      # database credentials
set -a && . ./.env && set +a
python -m api.app                          # http://localhost:8000
```

**Check the setup before running anything:**

```bash
python app.py --check
```

That validates the Python packages, the FFmpeg build (including that
`libwz264`/`libwz265` are actually present), AWS credentials, that the input
objects are readable, that the output bucket is *writable*, and the database —
reporting each one with a specific remedy. Run it first on any new server.

Verify the install without the encoder build or AWS:

```bash
python tests/smoke_test.py       # pure-logic checks (no dependencies)
python tests/pipeline_test.py    # full S3 -> transcode -> S3 -> API -> DB run
```

AWS credentials come from the ambient chain — instance role, `~/.aws`, or the
standard environment variables. Nothing is read from `config.json`.

---

## Repository layout

```
app.py                        CLI entry point
wz_vod_hls.py                 CLI argument parsing and dispatch
config.json                   Working configuration
config.example.json           Reference copy

hls_toolkit/                  The pipeline
├── runner.py                 One entry point for a run (CLI and API share it)
├── hls_generator.py          Stage orchestration
├── ffmpeg_wrapper.py         FFmpeg invocation, parallel transcode, HLS packaging
├── s3_io.py                  S3 input fetch, output publish, verification
├── job_context.py            Per-channel logging, progress, cancellation
├── subtitle_processor.py     WebVTT parse, clip, merge, HLS segmentation
├── esam_parser.py            ESAM/MCC XML, SCTE-35 marker injection
├── playlist_utils.py         m3u8 parsing, master playlist
├── time_utils.py             SMPTE timecode maths (drop-frame aware)
├── aws_operations.py         --upload-only handler
└── logging_utils.py          Console logging setup

api/                          HTTP service
├── app.py                    Flask app factory
├── routes.py                 Endpoints
├── job_manager.py            Worker pool, progress sync, job queries
└── database.py               PostgreSQL models

docs/
├── API.md                    Endpoint reference with examples
├── POSTGRES_SETUP.md         Ubuntu database setup, start to finish
└── schema.sql                Explicit DDL (the API also creates it on boot)

deploy/ai-transcoder.service  systemd unit
tests/                        Offline verification
```

---

## Inputs: S3 or local

`input_video` and `subtitle_file` accept either form. An `s3://` value is
downloaded to a scratch directory before transcoding; a local path is used as-is.

```jsonc
"defaults": {
  "input_video":   "s3://dev-us-west-2-transcoder-bucket/Visionular/AETN_AmericanPickers_S10_E03_en.mp4",
  "subtitle_file": "s3://dev-us-west-2-transcoder-bucket/Visionular/AETN_AmericanPickers_S10_E03_en.vtt"
}
```

A missing object fails the job at `FETCHING_INPUT` with the URI named, rather
than surfacing later as a confusing FFmpeg error.

## Output: straight to S3

The package is staged in a scratch directory, uploaded to
`s3://<bucket>/<key_prefix>/<output_dir>/`, and **verified object by object**
(every key HEADed and its size compared) before the local directory is deleted.
A partial upload fails the job and leaves the local copy in place, so output is
never lost silently.

Correct content types are set on the way up (`application/vnd.apple.mpegurl`
for `.m3u8`, `video/mp2t` for `.ts`, `text/vtt` for `.vtt`) so players can read
the package directly from S3 or CloudFront.

```
s3://dev-us-west-2-transcoder-bucket/Visionular/V3/AETN_AmericanPickers_S10_E03_en/
├── channel.m3u8                  master playlist
├── channel_1080p.m3u8            variant playlists
├── channel_1080p_00001.ts        segments
├── channel_en-vtt-1.m3u8         subtitle playlist
├── channel_en-vtt-1_00001.vtt    subtitle segments
└── thumbnails/thumb_0001.jpg
```

Pass `--keep-local` (or `"delete_local_output": false`) to retain the local copy.

---

## Per-channel logging

Every run logs under a folder named after the source file — for
`AETN_AmericanPickers_S10_E03_en.mp4` that is:

```
logs/AETN_AmericanPickers_S10_E03_en/<job_id>/
├── job.log      every step: stages, decisions, FFmpeg commands, S3 transfers
├── error.log    warnings and errors only — read this first when a job fails
├── ffmpeg.log   raw FFmpeg/FFprobe output, verbatim, every command
└── job.json     live status, stage, progress, timings, output location
```

Under the CLI without `--job-id` the files sit directly in
`logs/<channel>/`. The API always uses a per-job subdirectory so concurrent runs
on the same channel never interleave.

The service's own log is separate, at `logs/_server/api.log`.

---

## Error handling

Failures are typed and carry the stage that broke, so a caller never has to
guess. Every stage boundary is a checkpoint:

* **Input** — missing object, access denied, wrong bucket, short download
* **Validation** — unknown template or resolution, bad timecodes, missing FFmpeg
* **Probing** — unreadable or zero-duration source
* **Transcode** — FFmpeg exit code, with the last 40 lines of its output in the
  message and the full stream in `ffmpeg.log`; the pool cancels remaining work
  on the first failure
* **Upload** — per-file retries, then verification; a mismatch fails the job and
  keeps the local output

A failed job's `GET /status` includes `error_stage`, `error_message` and the
tail of `error.log` inline. Cancellation terminates the FFmpeg process group and
cleans up scratch space.

---

## Configuration

`config.json` drives everything; CLI flags and API payload fields override it.

### `defaults`

| Key | Meaning |
| --- | --- |
| `input_video`, `subtitle_file` | Local paths or `s3://` URIs |
| `output_dir` | S3 folder name under `s3.key_prefix` |
| `template` | Which `video_templates` ladder to use |
| `resolutions` | Comma-separated rung names from that ladder |
| `esam`, `audio_norm`, `generate_thumbnails`, `upload` | Feature switches |
| `InputClippings` | `[{"StartTimecode": "HH:MM:SS:FF", "EndTimecode": "..."}]` |
| `hls_settings` | `hls_time`, `hls_playlist_type`, `hls_flags`, `hls_segment_type` |

### `s3`

| Key | Meaning |
| --- | --- |
| `bucket_name` | Destination bucket |
| `key_prefix` | Prefix under which `output_dir` is created |
| `region` | Optional; defaults to the instance's region |

### `video_templates`

A map of template name to ABR ladder. Per rung: `name`, `width`, `height`,
`codec` (`H_264` → `libwz264`, `H_265` → `libwz265`), `codec_params` (passed as
`-wz264-params` / `-wz265-params`), `bitrate` or `crf`, `preset`, `caeopts`,
`threads`, `GopSize`, `interlace_mode`, `video_format`, `frame_rate`.

### Other sections

`paths` (FFmpeg locations), `thumbnail_generation`, `audio_normalization`,
`Esam` (`SccXml` and `MccXml` as inline strings).

---

## CLI

```bash
# Full run from the config
python app.py --config config.json

# Override input and output for one run
python app.py --input s3://bucket/in.mp4 --output my_asset

# First 60 seconds, H.265 ladder, keep local output and scratch
python app.py --duration 60 --template h265_standard --keep-local --debug

# Package locally without uploading
python app.py --no-upload --output ./local_out

# Upload an already-packaged directory
python app.py --upload-only --s3-upload-source-dir ./hls_out --output my_asset
```

`python app.py --help` lists every flag.

---

## HTTP API

Full reference with examples: [`docs/API.md`](docs/API.md).

```bash
curl -sX POST localhost:8000/api/v1/jobs -H 'Content-Type: application/json' -d '{
  "input_video": "s3://dev-us-west-2-transcoder-bucket/Visionular/AETN_AmericanPickers_S10_E03_en.mp4",
  "output_dir": "AETN_AmericanPickers_S10_E03_en",
  "resolutions": "1080p,720p,540p,360p",
  "esam": true, "audio_norm": true, "generate_thumbnails": true
}'

curl -s localhost:8000/api/v1/jobs/<job_id>/status
curl -s "localhost:8000/api/v1/jobs/<job_id>/logs?type=error"
curl -s "localhost:8000/api/v1/jobs?status=RUNNING"
```

| Method | Path |
| --- | --- |
| `POST` | `/api/v1/jobs` — queue a job (`202` + `job_id`) |
| `GET` | `/api/v1/jobs` — list, paginated and filterable |
| `GET` | `/api/v1/jobs/<id>` — full record, ladder, clips, config snapshot |
| `GET` | `/api/v1/jobs/<id>/status` — status + progress percentage |
| `GET` | `/api/v1/jobs/<id>/logs?type=job\|error\|ffmpeg` |
| `POST` | `/api/v1/jobs/<id>/cancel` |
| `DELETE` | `/api/v1/jobs/<id>` |
| `GET` | `/api/v1/templates`, `/api/v1/config`, `/health` |

---

## Database

PostgreSQL stores every job with the exact configuration it ran under, its
rendition ladder and its clip list. Setup for Ubuntu, start to finish:
[`docs/POSTGRES_SETUP.md`](docs/POSTGRES_SETUP.md).

```bash
sudo apt install -y postgresql postgresql-contrib
sudo -u postgres psql -c "CREATE ROLE transcoder LOGIN PASSWORD 'secret' CREATEDB;" \
                    -c "CREATE DATABASE ai_transcoder OWNER transcoder;"
cp .env.example .env && $EDITOR .env
python -m api.app          # tables are created on first start
```

Tables: `jobs`, `job_variants`, `job_clips`. The API also runs without a
database — transcodes still work, only history and listings are unavailable, and
`/health` reports `"database": "unavailable"`.

---

## Deployment

```bash
sudo cp deploy/ai-transcoder.service /etc/systemd/system/
sudo cp .env.example /etc/ai-transcoder.env && sudo chmod 600 /etc/ai-transcoder.env
sudo $EDITOR /etc/ai-transcoder.env
sudo systemctl daemon-reload && sudo systemctl enable --now ai-transcoder
curl -s localhost:8000/health
```

The unit runs gunicorn with **one worker and multiple threads**. That is
deliberate: the in-process job registry that serves live progress is per-process,
so multiple gunicorn workers would answer status requests for jobs they are not
running. Scale by raising `MAX_CONCURRENT_JOBS` (and the thread count), not the
worker count.

`WORK_ROOT` needs free space of roughly three times the source file per
concurrent job.

---

## Troubleshooting

Run `python app.py --check` first — it identifies most of the table below
directly, with the fix for each.

| Symptom | Cause |
| --- | --- |
| `module 'lib' has no attribute 'X509_V_FLAG_NOTIFY_POLICY'` | Installed `pyOpenSSL` is older than the installed `cryptography` (which dropped those constants). botocore imports pyOpenSSL optionally and versions before 1.38.46 did not catch this. Fix with `pip install --upgrade 'boto3>=1.38.46' 'botocore>=1.38.46'`, or repair the pair: `pip install --upgrade 'pyOpenSSL>=24.0.0' 'cryptography>=42'`. Installing into a virtualenv avoids the apt/pip mix that causes it. |
| `FFmpeg executable not found at ...` | `paths.ffmpeg_executable` is wrong, or `bin/ffmpeg` is not executable |
| `Unknown encoder 'libwz264'` | The binary in `bin/` is a stock FFmpeg without the in-house encoders |
| `S3 object not found: s3://...` | Wrong key, or the instance role cannot see that bucket |
| `Access denied reading s3://...` | The instance role lacks `s3:GetObject` on that prefix |
| `upload is enabled but s3.bucket_name is not configured` | Set `s3.bucket_name`, or pass `"upload": false` |
| `Upload verification failed for N object(s)` | Object size mismatch in S3 — the local output is kept; check the bucket and retry |
| Job stuck at `FETCHING_INPUT` | Large source download; watch `job.log` for percentage lines |
| `/health` shows `"database": "unavailable"` | See [`docs/POSTGRES_SETUP.md`](docs/POSTGRES_SETUP.md) |
| `Running without persistence` in the log | Same — jobs still run, history does not persist |

Start with `logs/<channel>/<job_id>/error.log`, then `ffmpeg.log` for encoder
failures. `--debug` raises verbosity and keeps the scratch directory so
intermediate clips can be inspected.
