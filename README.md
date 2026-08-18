# AI-Transcoder

VOD HLS packaging pipeline built on a custom FFmpeg build whose H.264 / H.265 encoders
were fine-tuned in house and shipped as **`libwz264`** and **`libwz265`**.

The pipeline takes a source video (plus an optional WebVTT subtitle track and an ESAM
signalling document), clips it, transcodes it into an ABR ladder, packages it as HLS,
injects SCTE-35 ad markers, and can optionally upload the result to S3 and register it
as an AWS MediaPackage VOD asset.

This repository is the plain-Python source of the pipeline. It was recovered from the
`wz_vod_hls` single-file distribution and reorganised into an importable package.

---

## Quick start

```bash
git clone <this-repo> && cd AI-Transcoder

# 1. Put the custom FFmpeg build in place
mkdir -p bin
cp /path/to/your/ffmpeg  bin/ffmpeg
cp /path/to/your/ffprobe bin/ffprobe
chmod +x bin/ffmpeg bin/ffprobe

# 2. Point the config at your input and encoding ladder
cp config.example.json config.json   # already present; edit in place
$EDITOR config.json

# 3. Run
python app.py --config config.json --input assets/input.mp4 --output hls_output
```

`python app.py --help` lists every flag.

There is nothing to `pip install` — the toolkit is standard library only and needs
**Python 3.8+** (3.11 recommended). `awscli` v2 is required only for the S3 /
MediaPackage flags.

Verify the pure-Python half of the pipeline without any FFmpeg build:

```bash
python tests/smoke_test.py
```

---

## Repository layout

```
app.py                        Entry point — run this
wz_vod_hls.py                 CLI: argument parsing, config loading, dispatch
config.example.json           Annotated configuration template
config.json                   Working config (edit this)
tests/smoke_test.py           Offline self-check, no FFmpeg needed

hls_toolkit/
├── hls_generator.py          The workflow — orchestrates every stage below
├── ffmpeg_wrapper.py         All FFmpeg/FFprobe invocation, parallel transcode, HLS packaging
├── subtitle_processor.py     WebVTT parsing, clipping, merging, HLS VTT segmentation
├── esam_parser.py            ESAM/MCC XML parsing, SCTE-35 marker injection, cue remapping
├── playlist_utils.py         m3u8 parsing and master-playlist generation
├── time_utils.py             SMPTE timecode ↔ seconds ↔ frames (drop-frame aware)
├── aws_handler.py            AWS CLI wrapper, MediaPackage VOD asset lifecycle
├── aws_operations.py         --upload-only / --import-only handlers
├── logging_utils.py          Console + file logging setup
└── version.py                Build version string
```

---

## How a run flows

1. **Probe** — `ffprobe` reports duration and frame rate of the input.
2. **Clip plan** — `defaults.InputClippings` defines the segments to keep. With no
   clippings the whole file is used as a single clip. `--duration N` overrides this
   with a single clip of the first N seconds, quantised to a frame boundary.
3. **GOP alignment** — for each clip, an offset is computed so IDR frames land on a
   consistent cadence across clip boundaries after stitching.
4. **ESAM (optional, `--esam`)** — the SCC XML is parsed into cue points, de-duplicated,
   snapped to nearby clip boundaries (10-frame tolerance), debounced, and remapped onto
   the compacted output timeline.
5. **Transcode** — every clip is encoded to every ladder rung in parallel
   (`--transcode-workers`, auto-sized from CPU count and per-rung thread counts). One
   FFmpeg process per clip emits all resolutions via a `split` filter graph. Audio is
   extracted in a separate pass, with `loudnorm` applied when `--audio-norm` is set.
6. **Merge** — per-resolution clips are stitched with the concat demuxer, no re-encode.
7. **Package** — each merged rung is segmented into HLS, forcing segment boundaries at
   the ESAM cue points so ad breaks land exactly on a segment edge.
8. **Subtitles** — the source VTT is clipped to match, merged onto the output timeline,
   then split into per-segment VTT files aligned to the video segments, each carrying an
   `X-TIMESTAMP-MAP` derived from the first video segment's PTS.
9. **Master playlist** — `channel.m3u8` with CODECS, RESOLUTION, FRAME-RATE and the
   subtitle rendition group.
10. **Marker injection** — `#EXT-X-CUE-OUT` / `-CONT` / `-IN` (plus `#EXT-X-ASSET` tags
    from the MCC XML) are written into every variant playlist.
11. **Thumbnails (optional)** — one JPEG every 10 seconds into `thumbnails/`.
12. **Publish (optional)** — `--upload` syncs to S3; `--import` registers the asset with
    MediaPackage VOD and polls until it is `PLAYABLE`, then prints the playback URLs.

Temporary work lives in `--temp-dir` (default `.wz_temp/`) and is deleted on exit
unless `--debug` is passed. `SIGINT`/`SIGTERM` terminate the whole FFmpeg process group.

---

## Configuration

`config.json` drives everything; CLI flags override it.

### `paths`

| Key | Meaning |
| --- | --- |
| `ffmpeg_executable` | Path to the custom FFmpeg (must expose `libwz264` / `libwz265`) |
| `ffprobe_executable` | Path to the matching FFprobe |

### `defaults`

| Key | Meaning |
| --- | --- |
| `input_video`, `output_dir` | Defaults for `--input` / `--output` |
| `subtitle_file`, `subtitle_language` | Defaults for `--subtitle` / `--sub-lang` |
| `template` | Which entry of `video_templates` to use |
| `resolutions` | Comma-separated rung names to render from that template |
| `esam`, `audio_norm`, `upload`, `import` | Booleans matching the corresponding flags |
| `log_file` | Also write logs to this file |
| `hls_settings` | `hls_time`, `hls_playlist_type`, `hls_flags`, `hls_segment_type` |
| `InputClippings` | `[{ "StartTimecode": "HH:MM:SS:FF", "EndTimecode": "..." }, ...]` |
| `mediapackage` | `packaging_group_id`, `vod_role_name`, `vod_role_arn`, `region`, `package_type`, `debug_aws` |

`vod_role_arn` supports a `{{RoleArn}}` placeholder, substituted at runtime with the
AWS account ID resolved from `sts get-caller-identity`.

### `video_templates`

A map of template name to an ABR ladder. Each rung:

| Key | Meaning |
| --- | --- |
| `name` | Rung name — must match entries in `defaults.resolutions` |
| `width`, `height` | Output dimensions |
| `codec` | `H_264` → `libwz264`, `H_265` → `libwz265` (with `hvc1` tag) |
| `codec_params` | Passed as `-wz264-params` / `-wz265-params` |
| `bitrate` / `crf` | Rate control — `crf` wins if both are set |
| `preset`, `caeopts`, `threads` | Encoder tuning |
| `video_format` | `yuv420p`, `yuvj420p`, `yuv420p10`, `yuvj420p10` |
| `interlace_mode` | `PROGRESSIVE` inserts a `yadif` deinterlace filter |
| `frame_rate` | Output fps; empty means inherit from the source |
| `GopSize` | GOP length in seconds, quantised to whole frames |

### Other sections

- **`s3`** — `bucket_name`, `key_prefix`.
- **`thumbnail_generation`** — `enabled`.
- **`audio_normalization.loudnorm_settings`** — `i`, `lra`, `tp` targets.
- **`Esam`** — `SignalProcessingNotification.SccXml` and
  `ManifestConfirmConditionNotification.MccXml` as inline XML strings.

---

## Common invocations

```bash
# Straight HLS package
python app.py --input in.mp4 --output hls_out

# First 60 seconds only, H.265 ladder, two rungs, keep temp files
python app.py --input in.mp4 --output hls_out \
              --duration 60 --template h265_standard \
              --resolution 1080p,720p --debug

# With subtitles, audio normalisation, ad markers and thumbnails
python app.py --input in.mp4 --subtitle in.vtt --sub-lang en \
              --audio-norm --esam --generate-thumbnails

# Package, upload to S3 and register with MediaPackage
python app.py --input in.mp4 --output my_asset --upload --import

# Upload an already-packaged directory
python app.py --upload-only --s3-upload-source-dir ./hls_out --output my_asset

# Register something already in S3 with MediaPackage
python app.py --import-only --s3-import-folder-name my_asset --output my_asset_id
```

---

## Output

```
hls_output/
├── channel.m3u8                    master playlist
├── channel_1080p.m3u8              variant playlists (one per rung)
├── channel_1080p_00001.ts          MPEG-TS segments
├── channel_en-vtt-1.m3u8           subtitle playlist
├── channel_en-vtt-1_00001.vtt      subtitle segments
└── thumbnails/thumb_0001.jpg       when --generate-thumbnails is set
```

---

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `FFmpeg executable not found at ...` | `paths.ffmpeg_executable` is wrong, or `bin/ffmpeg` is missing / not executable |
| `Unknown encoder 'libwz264'` | The FFmpeg build in `bin/` is a stock build without the in-house encoders |
| `Template '<name>' not found` | `defaults.template` does not match any key in `video_templates` |
| `... resolutions were not found in template` | A name in `defaults.resolutions` has no matching rung |
| `Invalid video duration detected: 0.0s` | Input is unreadable or has no video stream |
| `AWS CLI not found` | Install awscli v2 — only needed for the S3 / MediaPackage flags |
| `Timeout waiting for Asset ...` | MediaPackage packaging is slow or failing; re-run with `--debug-aws` |

`--debug` turns on verbose logging and keeps `.wz_temp/` so intermediate clips can be
inspected.
