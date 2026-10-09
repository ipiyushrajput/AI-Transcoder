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
  and list job history from MySQL

Several jobs can run at once — separate `app.py` runs, API jobs, or both — and
they share the server's cores through one CPU budget instead of each assuming it
owns the machine. See [Parallel jobs](#parallel-jobs).

---

## Quick start

```bash
git clone <this-repo> && cd AI-Transcoder
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# Edit inputs, ladder and S3 destination
$EDITOR config.json
```

The custom FFmpeg build ships in `bin/` — `ffmpeg` and `ffprobe` (with the
in-house `libwz264` / `libwz265` encoders) plus the Visionular license files
`wz_license.cnf` and `wz_license.key` — so a fresh clone runs as is. To use a
different build, replace those files or point `paths.ffmpeg_executable` and
`paths.ffprobe_executable` in the config at it.

**As a CLI:**

```bash
python app.py --config config.json
```

**As a service:**

```bash
cp .env.example .env && chmod 600 .env && $EDITOR .env   # set DB_PASSWORD
python -m api.app                                        # http://localhost:8000
```

`.env` is read automatically; nothing needs exporting.

**Check the setup before running anything:**

```bash
python app.py --check
```

That validates the Python packages, the FFmpeg build (including that
`libwz264`/`libwz265` are actually present), AWS credentials, that the input
objects are readable, that the output bucket is *writable*, and MySQL —
reporting each one with a specific remedy. Run it first on any new server.

Verify the install without the encoder build or AWS:

```bash
python tests/smoke_test.py       # pure-logic checks (no dependencies)
python tests/pipeline_test.py    # S3 -> transcode -> S3, parallel jobs, API, MySQL
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
├── cpu_budget.py             One CPU budget shared by every job on the server
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
└── database.py               MySQL models

docs/
├── API.md                    Endpoint reference with examples
├── MYSQL_SETUP.md            Ubuntu database setup, start to finish
└── schema.sql                MySQL DDL (the API also creates it on boot)

bin/                          Custom FFmpeg build and Visionular license
├── ffmpeg, ffprobe           With the in-house libwz264 / libwz265 encoders
└── wz_license.cnf, .key      License server address and license key

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

The package is staged in a scratch directory, **validated**, uploaded to
`s3://<bucket>/<key_prefix>/<output_dir>/`, and **verified object by object**
(every key HEADed and its size compared) before the local directory is deleted.

**Validation before upload.** The job checks what is actually on disk: the
master playlist exists and lists every requested rendition; every playlist it
references exists, ends with `#EXT-X-ENDLIST` and has segments; every segment
exists and is non-empty; each rendition's total duration matches what was
requested (within half a segment); thumbnails exist when requested. Any problem
fails the job at `VALIDATING_OUTPUT`, every problem is listed, and nothing is
published. A rendition that fails to package fails the job at `PACKAGING_HLS`.

**Republishing never takes the live output down.** Nothing is deleted first.
The upload goes in three phases — segments, subtitles and thumbnails, then the
variant playlists, then the master playlist last — so a player or CDN never sees
a playlist before the files it references. If a file fails to upload the job
stops at that phase boundary: no playlist is published over missing media, and
the previously published package stays live. Only after every new object is
verified are files the new package no longer has (a dropped rendition, surplus
old segments) removed.

**The output folder is a name, not a path.** `output_dir` / `--output`
(`AETN_S10_E03`, or nested like `shows/AETN_S10_E03`) names the folder in S3 and
for a local copy. Absolute paths, `..`, backslashes and drive letters are
refused — such a name used to point the staging folder outside scratch, where
it was uploaded and then deleted.

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

### Local copies

A local copy of the package is saved under `--local-output-dir` (default
`./hls_output`, or `defaults.local_output_dir`):

* with `--no-upload` — the package is saved there instead of uploaded;
* with `--keep-local` (or `"delete_local_output": false`) — uploaded *and* saved;
* when an upload fails — the finished package is saved, and the error names the
  folder and the `--upload-only` command that retries it without transcoding.

An existing folder is never overwritten or merged into: the copy goes to
`<name>_2`, `<name>_3` and so on. If the copy cannot be saved the job fails.

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
`logs/<channel>/`. The API always uses a per-job subdirectory.

**A log folder is never reused.** If `logs/AETN_AmericanPickers_S10_E03_en/`
already exists, the next run on that source logs to
`logs/AETN_AmericanPickers_S10_E03_en_2/`, then `_3`, and so on — so parallel or
repeated runs never append to each other's `job.log` or overwrite each other's
`job.json`. The first lines of `job.log` say which folder was used and why. With
`--job-id`, a repeated id becomes `<job_id>_2` in the same way. The folder is
claimed atomically, so jobs started at the same instant still each get their
own.

The service's own log is separate, at `logs/_server/api.log`.

---

## Parallel jobs

### How a job runs today

**The variants within a clip encode in parallel.** One FFmpeg process per clip
decodes the source once, splits it, and runs every selected rung's encoder at the
same time, each with its configured `threads`. This was already the case and is
unchanged.

**The clips within a job encode in parallel, as many as the server fits.** Each
clip's video encode reserves cores from the machine-wide CPU budget before it
starts — half its rungs' `threads`, added up; four rungs at `threads: 4` reserve 8.
On a 32-core server a lone four-rung job therefore encodes 4 clips at once.
Longer clips start first, so the job does not end with one long clip running
alone. Audio encodes reserve one core and fill in around the video.

**Separate jobs run in parallel and share the server.** Every `python app.py`
run, and every job the API runs, draws from the same budget. A job alone gets
the whole machine; three jobs split it. When jobs are waiting for cores they take
turns clip by clip, so they progress together instead of queueing whole job
behind whole job. A job that crashes or is killed — even with `kill -9` — gives
its cores back immediately.

### What changed, and why it was slow

The worker count used to be worked out from **every rung in the template**, not
just the ones being encoded. In `h265_standard` the unselected `2160p` rung has
`threads: 12`, which made a four-rung H.265 job size itself as needing 36 cores
per clip — so on any server under 96 cores it got **one worker and encoded its
clips one after another**. Separately, each job sized its pool as if it owned the
whole machine, so jobs started side by side asked for several machines' worth of
CPU and competed for it rather than sharing it.

None of this touched FFmpeg. The encoder settings — preset, CRF, `threads`,
`codec_params`, GOP — and the FFmpeg command lines themselves are byte-for-byte
the same as before; only *when* each encode starts is different.

### Sizing

The budget defaults to the server's CPU count. To set it:

```bash
python app.py --config config1.json --cpu-budget 32     # one run
export WZ_CPU_BUDGET=32                                  # every run in this shell
```

or `"parallelism": {"cpu_budget": 32}` in the config. Precedence is the flag,
then `WZ_CPU_BUDGET`, then the config, then the CPU count. **Use the same value
for every job on a server** — they share it.

`--transcode-workers N` still exists and caps how many clips one job encodes at
once; without it there is no per-job cap.

Each job's log states its plan, for example:

```
Transcoding 10 task(s) across 5 clip(s). CPU budget 32 core(s), shared with every
job on this server; each clip's video encode reserves 8 core(s), so up to 4 clip(s)
encode at once when this job runs alone.
```

The budget is a directory of lock files, `/tmp/ai-transcoder-cpu-slots` by
default (`WZ_CPU_SLOT_DIR` to move it). Every job on the server must be able to
write to it; if one cannot, it logs a warning and falls back to a budget for its
own process only.

Parallelism cannot create CPU. If one job alone already keeps every core busy,
three jobs take about three times as long in total — the budget makes them share
the machine evenly and stops them thrashing it, but the work is the same.

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
* **Packaging / validation** — a rendition that fails to package, or a package
  with a missing, empty, incomplete or short playlist or segment, fails the job
  before anything is published (see *Output*)
* **Upload** — per-file retries, then verification; a failure stops before the
  next publishing phase, leaves the previous package live, and saves the finished
  package locally for a retry
* **Hung FFmpeg / FFprobe** — an FFmpeg run whose position (`time=`) stops
  advancing for 10 minutes is stopped and the job fails, naming the step and
  where it stopped; its CPU cores are freed for other jobs. FFprobe has a
  5-minute limit. See *Configuration → Time limits*.
* **Server restart** — see *Deployment → Restarts*

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

### Time limits

| Setting | Default | Meaning |
| --- | --- | --- |
| `WZ_FFMPEG_STALL_SECONDS` / `defaults.ffmpeg_stall_timeout_seconds` | `600` | Stop an FFmpeg run whose output position has not advanced for this long |
| `WZ_FFMPEG_MAX_SECONDS` / `defaults.ffmpeg_max_seconds` | `0` (off) | Hard limit on any single FFmpeg command |
| `WZ_FFPROBE_TIMEOUT_SECONDS` / `defaults.ffprobe_timeout_seconds` | `300` | Limit on any FFprobe call |

The environment variable wins over the config value; `0` disables a limit.
Slow encodes keep advancing, so the stall limit only stops a genuine hang — it
never shortens a long encode.

### Scratch disk

Before downloading, each job reserves about *source size × factor* of space on
its scratch disk (`--work-dir` / `WORK_ROOT`, or the system temp). Reservations
are shared by every job on the machine, so parallel jobs cannot fill the disk
together: a job that fits starts at once, one that would fit after others
finish waits (logging why), and one that could never fit fails immediately at
`FETCHING_INPUT` with the sizes involved. A job that dies frees its
reservation automatically.

| Setting | Default | Meaning |
| --- | --- | --- |
| `WZ_DISK_SPACE_FACTOR` / `defaults.disk_space_factor` | `3` | Scratch needed per byte of source (one less for a local source, which is not copied) |
| `WZ_DISK_MIN_FREE_BYTES` | 1 GiB | Always left free for everything else |
| `WZ_DISK_WAIT_SECONDS` | `1800` | How long to wait for space before failing |

### Other sections

`paths` (FFmpeg locations), `thumbnail_generation`, `audio_normalization`,
`Esam` (`SccXml` and `MccXml` as inline strings), and the optional
`parallelism` (`cpu_budget`; see [Parallel jobs](#parallel-jobs)).

---

## CLI

```bash
# Full run from the config
python app.py --config config.json

# Override input and output for one run
python app.py --input s3://bucket/in.mp4 --output my_asset

# First 60 seconds, H.265 ladder, keep local output and scratch
python app.py --duration 60 --template h265_standard --keep-local --debug

# Package locally without uploading (saved to ./hls_output/my_asset)
python app.py --no-upload --output my_asset

# ...or somewhere else
python app.py --no-upload --output my_asset --local-output-dir /data/packages

# Upload an already-packaged directory
python app.py --upload-only --s3-upload-source-dir ./hls_out --output my_asset

# Several jobs at once: they share the server's cores (see Parallel jobs)
python app.py --config config1.json &
python app.py --config config2.json &
python app.py --config config3.json &
wait
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

MySQL stores every job with the exact configuration it ran under, its rendition
ladder and its clip list. Setup for Ubuntu, start to finish:
[`docs/MYSQL_SETUP.md`](docs/MYSQL_SETUP.md).

The connection comes from `.env` (copy `.env.example`):

| Setting | Default |
| --- | --- |
| `DB_HOST` | `localhost` |
| `DB_PORT` | `3306` |
| `DB_USER` | `root` |
| `DB_PASSWORD` | *(none — must be set)* |
| `DB_NAME` | `Visionular-Transcoder` |

```bash
cp .env.example .env && chmod 600 .env && $EDITOR .env    # set DB_PASSWORD
python app.py --check                                     # MySQL line should say ok
python -m api.app        # creates the database and tables on first start
```

Write the password as-is in `.env`; characters such as `@` need no escaping. The
database name contains a hyphen, so quote it with backticks in your own SQL:
``USE `Visionular-Transcoder`;``.

Tables: `jobs`, `job_variants`, `job_clips` (InnoDB, utf8mb4, native `JSON` for
the config snapshot). The API also runs without a database — transcodes still
work, only history and listings are unavailable, and `/health` reports
`"database": "unavailable"`.

The most common setup problem: Ubuntu creates MySQL's `root` with
`auth_socket`, which refuses every password. `--check` detects it and prints the
one-line fix; see step 2 of the setup guide.

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
worker count. Raising it is safe for CPU: jobs beyond what the CPU budget fits
wait for cores rather than oversubscribing them. Each running job still needs
its own scratch space.

`WORK_ROOT` needs free space of roughly three times the source file per
concurrent job.

### Restarts

The unit starts gunicorn through `deploy/gunicorn.conf.py`. On
`systemctl stop`, `restart` or a deploy:

* new submissions get `503` with `Retry-After`;
* running jobs are interrupted — FFmpeg is stopped and each job is recorded as
  `FAILED` with *"Interrupted: the server shut down while this job was running
  … Resubmit the job to run it again."* Nothing half-finished is published;
* queued jobs stay `PENDING`.

On the next start, jobs a previous process left behind are settled:

* `PENDING` jobs are queued again from the configuration stored with them
  (set `REQUEUE_PENDING_ON_START=0` to mark them failed instead);
* `RUNNING` jobs — left by a crash, `kill -9` or power loss — are marked
  `FAILED` as interrupted, with the stage and progress they reached.

Each queued or running job holds a lock file (under `WZ_CPU_SLOT_DIR`) for as
long as it is alive, and the kernel drops it when its process ends, so startup
only ever settles jobs whose process is gone — never one that is still running.

`graceful_timeout` (120s, `GRACEFUL_TIMEOUT`) is how long gunicorn waits for
this; keep systemd's `TimeoutStopSec` (300s) above it. Run gunicorn with
`-c deploy/gunicorn.conf.py` if you start it by hand, or the shutdown handling
does not run.

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
| `/health` shows `"database": "unavailable"` | Run `python app.py --check`; the MySQL line names the cause. See [`docs/MYSQL_SETUP.md`](docs/MYSQL_SETUP.md) |
| `Access denied for user 'root'@'localhost'` (1698) | MySQL's root uses `auth_socket`, which ignores passwords. Step 2 of [`docs/MYSQL_SETUP.md`](docs/MYSQL_SETUP.md) |
| Parallel jobs finish at very different times | Each job's log states its CPU budget and how many clips fit at once. Check every job uses the same `--cpu-budget` / `WZ_CPU_BUDGET` and can write to `/tmp/ai-transcoder-cpu-slots` (a job that cannot logs a warning) |
| `Falling back to a budget of N core(s) for this process only` | This job cannot write the shared budget directory, so it does not see other jobs' usage. Fix its permissions, or point every job at one writable directory with `WZ_CPU_SLOT_DIR` |
| `Running without persistence` in the log | Same — jobs still run, history does not persist |

Start with `logs/<channel>/<job_id>/error.log`, then `ffmpeg.log` for encoder
failures. `--debug` raises verbosity and keeps the scratch directory so
intermediate clips can be inspected.
