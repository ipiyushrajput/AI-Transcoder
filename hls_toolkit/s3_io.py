"""S3 input fetching and output publishing.

Inputs (``input_video`` / ``subtitle_file``) may be either a local path or an
``s3://bucket/key`` URI; :func:`resolve_input` normalises both to a local file,
downloading when needed. Outputs are pushed to S3 with :func:`upload_directory`,
which verifies every object landed before the local copy is removed.

Credentials come from the ambient boto3 chain (instance role, ``~/.aws``,
environment) — nothing is read from config.
"""
import mimetypes
import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from hls_toolkit.job_context import JobContext, TranscodeError, get_logger

S3_SCHEME = "s3://"

# Content types so players fetch HLS assets correctly straight from S3/CloudFront.
CONTENT_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
    ".vtt": "text/vtt",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}

_client_lock = threading.Lock()
_client_cache: Dict[Optional[str], object] = {}


def is_s3_uri(uri) -> bool:
    return isinstance(uri, str) and uri.strip().lower().startswith(S3_SCHEME)


def parse_s3_uri(uri: str) -> Tuple[str, str]:
    """``s3://bucket/a/b.mp4`` -> ``("bucket", "a/b.mp4")``."""
    if not is_s3_uri(uri):
        raise ValueError(f"Not an S3 URI: {uri!r}")
    parsed = urlparse(uri.strip())
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    if not bucket:
        raise ValueError(f"S3 URI is missing a bucket name: {uri!r}")
    return bucket, key


def get_s3_client(region: Optional[str] = None):
    """Cached boto3 S3 client. Raises TranscodeError when boto3 is missing."""
    try:
        import boto3
        from botocore.config import Config
    except ImportError as e:
        raise TranscodeError(
            "boto3 is required for S3 input/output. Install it with "
            "`pip install -r requirements.txt`.", stage="S3") from e

    with _client_lock:
        client = _client_cache.get(region)
        if client is None:
            client = boto3.client(
                "s3",
                region_name=region,
                config=Config(retries={"max_attempts": 5, "mode": "standard"},
                              max_pool_connections=32))
            _client_cache[region] = client
        return client


def _transfer_config():
    from boto3.s3.transfer import TransferConfig
    return TransferConfig(multipart_threshold=64 * 1024 * 1024,
                          multipart_chunksize=32 * 1024 * 1024,
                          max_concurrency=8,
                          use_threads=True)


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------
def _error_code(exc: Exception) -> str:
    """AWS error code carried by a botocore exception, if there is one."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code", ""))
    return ""


def head_object(uri: str, region: Optional[str] = None) -> Dict:
    """Metadata for an S3 object. Raises TranscodeError if it is not reachable."""
    bucket, key = parse_s3_uri(uri)
    try:
        return get_s3_client(region).head_object(Bucket=bucket, Key=key)
    except TranscodeError:
        raise
    except Exception as e:
        code = _error_code(e)
        if code in ("404", "NoSuchKey", "NotFound"):
            raise TranscodeError(f"S3 object not found: {uri}",
                                 stage="FETCHING_INPUT") from e
        if code in ("403", "AccessDenied"):
            raise TranscodeError(
                f"Access denied reading {uri}. Check the instance role or AWS "
                f"credentials on this server.", stage="FETCHING_INPUT") from e
        if code in ("NoSuchBucket",):
            raise TranscodeError(f"S3 bucket does not exist: {bucket}",
                                 stage="FETCHING_INPUT") from e
        raise TranscodeError(f"Could not read {uri}: {e}",
                             stage="FETCHING_INPUT") from e


def download_file(uri: str, dest_dir: Path, ctx: Optional[JobContext] = None,
                  region: Optional[str] = None) -> str:
    """Download an S3 object into `dest_dir`. Returns the local path."""
    log = get_logger(ctx)
    bucket, key = parse_s3_uri(uri)
    if not key or key.endswith("/"):
        raise TranscodeError(f"S3 input must point at an object, not a folder: {uri}",
                             stage="FETCHING_INPUT")

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    local_path = dest_dir / Path(key).name

    meta = head_object(uri, region=region)
    total = int(meta.get("ContentLength", 0))
    log.info(f"Downloading {uri} ({_human(total)}) -> {local_path}")

    downloaded = {"n": 0}
    last_report = {"t": 0.0}

    def _progress(chunk: int):
        downloaded["n"] += chunk
        if ctx is not None:
            if ctx.cancelled:
                raise TranscodeError("Cancelled during S3 download", stage="FETCHING_INPUT")
            if total:
                ctx.advance_within_stage(downloaded["n"] / total)
            now = time.time()
            if now - last_report["t"] > 10:
                last_report["t"] = now
                pct = (downloaded["n"] / total * 100) if total else 0
                log.info(f"  download {pct:5.1f}%  "
                         f"({_human(downloaded['n'])} / {_human(total)})")

    started = time.time()
    try:
        get_s3_client(region).download_file(
            bucket, key, str(local_path),
            Config=_transfer_config(), Callback=_progress)
    except TranscodeError:
        raise
    except Exception as e:
        raise TranscodeError(f"Failed to download {uri}: {e}", stage="FETCHING_INPUT") from e

    size = local_path.stat().st_size
    if total and size != total:
        raise TranscodeError(
            f"Short download for {uri}: got {size} bytes, expected {total}",
            stage="FETCHING_INPUT")
    log.info(f"Downloaded {local_path.name} ({_human(size)}) in "
             f"{time.time() - started:.1f}s")
    return str(local_path)


def resolve_input(uri: Optional[str], work_dir: Path, label: str,
                  ctx: Optional[JobContext] = None,
                  region: Optional[str] = None) -> Optional[str]:
    """Return a local path for `uri`, downloading it first if it is an S3 URI.

    Local paths are returned unchanged after an existence check. ``None`` and
    empty values pass straight through so optional inputs stay optional.
    """
    if not uri:
        return None
    log = get_logger(ctx)
    if is_s3_uri(uri):
        log.info(f"{label}: S3 input detected ({uri})")
        return download_file(uri, work_dir / "input", ctx=ctx, region=region)

    local = os.path.abspath(os.path.expanduser(str(uri)))
    if not os.path.exists(local):
        raise TranscodeError(f"{label} not found: {local}", stage="FETCHING_INPUT")
    log.info(f"{label}: local input {local}")
    return local


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def upload_directory(local_dir, bucket: str, prefix: str,
                     ctx: Optional[JobContext] = None,
                     region: Optional[str] = None,
                     delete_local: bool = True,
                     extra_args_for: Optional[Callable[[Path], Dict]] = None) -> Dict:
    """Upload every file under `local_dir` to ``s3://bucket/prefix``.

    Each object is verified with a HEAD (size match) after upload. The local
    directory is removed only when every file has been confirmed present in S3,
    so a partial upload never silently loses the output.

    Returns ``{"uploaded": int, "bytes": int, "prefix": str, "keys": [...]}``.
    """
    import shutil

    log = get_logger(ctx)
    local_dir = Path(local_dir)
    if not local_dir.is_dir():
        raise TranscodeError(f"Output directory does not exist: {local_dir}",
                             stage="UPLOADING")
    if not bucket:
        raise TranscodeError("s3.bucket_name is not set in the configuration; "
                             "cannot upload output.", stage="UPLOADING")

    prefix = (prefix or "").strip("/")
    files = sorted(p for p in local_dir.rglob("*") if p.is_file())
    if not files:
        raise TranscodeError(f"No output files were produced in {local_dir}",
                             stage="UPLOADING")

    total_bytes = sum(p.stat().st_size for p in files)
    log.info(f"Uploading {len(files)} file(s), {_human(total_bytes)} -> "
             f"s3://{bucket}/{prefix}")

    client = get_s3_client(region)
    config = _transfer_config()
    sent_bytes = 0
    keys: List[str] = []
    failures: List[str] = []

    for index, path in enumerate(files, 1):
        if ctx is not None:
            ctx.raise_if_cancelled()
        rel = path.relative_to(local_dir).as_posix()
        key = f"{prefix}/{rel}" if prefix else rel
        extra = {"ContentType": _content_type(path)}
        if extra_args_for:
            extra.update(extra_args_for(path) or {})
        try:
            client.upload_file(str(path), bucket, key, ExtraArgs=extra, Config=config)
            keys.append(key)
        except Exception as e:
            log.error(f"Upload failed for {rel}: {e}")
            failures.append(rel)
            continue

        sent_bytes += path.stat().st_size
        if ctx is not None:
            ctx.advance_within_stage(index / len(files))
        if index % 50 == 0 or index == len(files):
            log.info(f"  uploaded {index}/{len(files)} "
                     f"({_human(sent_bytes)} / {_human(total_bytes)})")

    if failures:
        raise TranscodeError(
            f"{len(failures)} file(s) failed to upload to s3://{bucket}/{prefix}: "
            f"{', '.join(failures[:10])}"
            + (" ..." if len(failures) > 10 else ""),
            stage="UPLOADING")

    log.info("Verifying uploaded objects...")
    missing = _verify_uploads(client, bucket, local_dir, keys, prefix, log)
    if missing:
        raise TranscodeError(
            f"Upload verification failed for {len(missing)} object(s): "
            f"{', '.join(missing[:10])}" + (" ..." if len(missing) > 10 else ""),
            stage="UPLOADING")
    log.info(f"All {len(keys)} object(s) verified in s3://{bucket}/{prefix}")

    if delete_local:
        try:
            shutil.rmtree(local_dir, ignore_errors=False)
            log.info(f"Removed local output directory {local_dir}")
        except Exception as e:
            # The data is safely in S3 — a failed cleanup must not fail the job.
            log.warning(f"Could not remove local output directory {local_dir}: {e}")

    return {"uploaded": len(keys), "bytes": sent_bytes,
            "prefix": f"s3://{bucket}/{prefix}" if prefix else f"s3://{bucket}",
            "keys": keys}


def _verify_uploads(client, bucket: str, local_dir: Path, keys: List[str],
                    prefix: str, log) -> List[str]:
    """HEAD every uploaded key and compare sizes. Returns the failing keys."""
    missing = []
    for key in keys:
        rel = key[len(prefix) + 1:] if prefix else key
        local = local_dir / rel
        try:
            head = client.head_object(Bucket=bucket, Key=key)
            if int(head.get("ContentLength", -1)) != local.stat().st_size:
                log.error(f"Size mismatch for s3://{bucket}/{key}")
                missing.append(key)
        except Exception as e:
            log.error(f"Verification failed for s3://{bucket}/{key}: {e}")
            missing.append(key)
    return missing


def build_output_prefix(key_prefix: str, output_dir_name: str) -> str:
    key_prefix = (key_prefix or "").strip("/")
    name = (output_dir_name or "").strip("/")
    return f"{key_prefix}/{name}" if key_prefix else name


def delete_prefix(bucket: str, prefix: str, ctx: Optional[JobContext] = None,
                  region: Optional[str] = None) -> int:
    """Delete everything under a prefix (clears a previous run's output)."""
    log = get_logger(ctx)
    prefix = (prefix or "").strip("/")
    if not prefix:
        raise TranscodeError("Refusing to delete an empty S3 prefix "
                             "(that would target the whole bucket).", stage="UPLOADING")
    client = get_s3_client(region)
    deleted = 0
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/"):
            objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if not objects:
                continue
            for i in range(0, len(objects), 1000):
                client.delete_objects(Bucket=bucket,
                                      Delete={"Objects": objects[i:i + 1000]})
            deleted += len(objects)
    except Exception as e:
        log.warning(f"Could not clear s3://{bucket}/{prefix}: {e}")
        return deleted
    if deleted:
        log.info(f"Cleared {deleted} existing object(s) under s3://{bucket}/{prefix}")
    return deleted


def _content_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in CONTENT_TYPES:
        return CONTENT_TYPES[ext]
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _human(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} PB"
