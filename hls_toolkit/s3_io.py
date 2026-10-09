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


def environment_problem(exc: BaseException) -> Optional[str]:
    """Describe `exc` if it is a broken-Python-install problem, else None.

    A misconfigured TLS stack surfaces from deep inside boto3 as an
    ``AttributeError`` that reads like nothing in particular. Reporting that
    verbatim against an S3 URI sends people hunting through bucket policies for
    a problem that is entirely local, so these are named explicitly.
    """
    text = f"{type(exc).__name__}: {exc}"

    # pyOpenSSL is imported optionally by botocore via urllib3.contrib.pyopenssl.
    # cryptography >= 42 dropped the X509_V_FLAG_* constants that older
    # pyOpenSSL reads at import time, so `import OpenSSL` raises AttributeError
    # while the module body runs. botocore only began catching that in 1.38.46.
    if "X509_V_FLAG" in text or ("module 'lib' has no attribute" in text):
        return (
            "Your Python TLS packages are mismatched: the installed 'pyOpenSSL' "
            "is too old for the installed 'cryptography' "
            f"(reported as {text}).\n"
            "        This is a local environment problem, not an S3 or "
            "permissions problem.\n"
            "        Fix it with either of:\n"
            "          pip install --upgrade 'boto3>=1.38.46' 'botocore>=1.38.46'\n"
            "            (newer botocore ignores the broken optional import)\n"
            "          pip install --upgrade 'pyOpenSSL>=24.0.0' 'cryptography>=42'\n"
            "            (repairs the pair itself)\n"
            "        Running inside a virtualenv avoids the apt/pip mix that "
            "usually causes this.")

    # pyOpenSSL being absent is not a problem — botocore then uses the stdlib
    # SSL context. Only a present-but-unimportable pyOpenSSL is worth reporting.
    if isinstance(exc, ModuleNotFoundError) and getattr(exc, "name", None) == "OpenSSL":
        return None
    if isinstance(exc, ImportError) and "OpenSSL" in text:
        return (f"A TLS dependency failed to import ({text}).\n"
                "        Try: pip install --upgrade 'pyOpenSSL>=24.0.0' "
                "'cryptography>=42'")
    return None


def _raise_environment_error(exc: BaseException, stage: str) -> None:
    """Re-raise `exc` as a TranscodeError if it is an environment problem."""
    hint = environment_problem(exc)
    if hint:
        raise TranscodeError(hint, stage=stage) from exc


def get_s3_client(region: Optional[str] = None):
    """Cached boto3 S3 client. Raises TranscodeError when boto3 is unusable."""
    try:
        import boto3
        from botocore.config import Config
    except ImportError as e:
        _raise_environment_error(e, "S3")
        raise TranscodeError(
            "boto3 is required for S3 input/output. Install it with "
            "`pip install -r requirements.txt`.", stage="S3") from e
    except Exception as e:
        _raise_environment_error(e, "S3")
        raise TranscodeError(f"Could not load boto3: {e}", stage="S3") from e

    with _client_lock:
        client = _client_cache.get(region)
        if client is None:
            try:
                client = boto3.client(
                    "s3",
                    region_name=region,
                    config=Config(retries={"max_attempts": 5, "mode": "standard"},
                                  max_pool_connections=32))
            except Exception as e:
                _raise_environment_error(e, "S3")
                raise TranscodeError(f"Could not create an S3 client: {e}",
                                     stage="S3") from e
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
        _raise_environment_error(e, "FETCHING_INPUT")
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
        _raise_environment_error(e, "FETCHING_INPUT")
        raise TranscodeError(f"Failed to download {uri}: {e}",
                             stage="FETCHING_INPUT") from e

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
    phases = publish_phases(p for p in local_dir.rglob("*") if p.is_file())
    files = [path for group in phases for path in group]
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

    index = 0
    for phase_name, group in zip(PUBLISH_PHASES, phases):
        for path in group:
            index += 1
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
                _raise_environment_error(e, "UPLOADING")
                log.error(f"Upload failed for {rel}: {e}")
                failures.append(rel)
                continue

            sent_bytes += path.stat().st_size
            if ctx is not None:
                ctx.advance_within_stage(index / len(files))
            if index % 50 == 0 or index == len(files):
                log.info(f"  uploaded {index}/{len(files)} "
                         f"({_human(sent_bytes)} / {_human(total_bytes)})")

        # Stop at the phase boundary: publishing playlists (above all the
        # master) over media that did not arrive would point viewers at
        # missing files. The previously published playlists stay live instead.
        if failures:
            remaining = [name for name in PUBLISH_PHASES[PUBLISH_PHASES.index(phase_name) + 1:]]
            raise TranscodeError(
                f"{len(failures)} file(s) failed to upload to s3://{bucket}/{prefix}: "
                f"{', '.join(failures[:10])}" + (" ..." if len(failures) > 10 else "")
                + (f". The {' and '.join(remaining)} were not uploaded, so the "
                   f"previously published package (if any) is still the live one."
                   if remaining else ""),
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


def _is_master_playlist(path: Path) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "#EXT-X-STREAM-INF" in f.read()
    except OSError:
        return False


PUBLISH_PHASES = ("media", "playlists", "master playlist")


def publish_phases(files) -> List[List[Path]]:
    """Split a package into the groups it must be published in, in order.

    Segments, subtitles and thumbnails first, then the variant and subtitle
    playlists, and the master playlist last. Whatever moment a player or CDN
    fetches a playlist, everything it references is already in S3 — uploading
    alphabetically put ``channel.m3u8`` first, pointing at files not yet there,
    and a CDN could cache those 404s.
    """
    phases: List[List[Path]] = [[], [], []]
    for path in files:
        if path.suffix.lower() != ".m3u8":
            phases[0].append(path)
        else:
            phases[2 if _is_master_playlist(path) else 1].append(path)
    return [sorted(group, key=lambda p: p.as_posix()) for group in phases]


def publish_order(files) -> List[Path]:
    """Every file of the package, in publishing order."""
    return [path for group in publish_phases(files) for path in group]


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


def remove_stale_objects(bucket: str, prefix: str, keep_keys,
                         ctx: Optional[JobContext] = None,
                         region: Optional[str] = None) -> Dict[str, int]:
    """Delete objects under ``prefix/`` that are not part of the new package.

    Called only after every new object is uploaded and verified, so the live
    output is never missing: an earlier run's files stay until the new package
    has fully replaced them, and only what the new package no longer contains
    (a dropped rendition, extra old segments) is removed.

    Returns ``{"deleted": n, "failed": n}``. A failure here does not fail the
    job — the new package is complete and correct — but it is logged as a
    warning with the keys that remain.
    """
    log = get_logger(ctx)
    prefix = (prefix or "").strip("/")
    if not prefix:
        raise TranscodeError("Refusing to clean an empty S3 prefix "
                             "(that would target the whole bucket).", stage="UPLOADING")
    keep = set(keep_keys)
    client = get_s3_client(region)
    stale: List[str] = []
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/"):
            stale.extend(o["Key"] for o in page.get("Contents", []) if o["Key"] not in keep)
    except Exception as e:
        log.warning(f"Could not list s3://{bucket}/{prefix} to remove files left over "
                    f"from an earlier run: {e}. The new package is complete; stale "
                    f"objects, if any, remain.")
        return {"deleted": 0, "failed": 0}

    deleted = failed = 0
    for i in range(0, len(stale), 1000):
        batch = stale[i:i + 1000]
        try:
            response = client.delete_objects(
                Bucket=bucket, Delete={"Objects": [{"Key": k} for k in batch]})
        except Exception as e:
            log.warning(f"Could not delete {len(batch)} stale object(s) under "
                        f"s3://{bucket}/{prefix}: {e}")
            failed += len(batch)
            continue
        # delete_objects reports per-key failures in the response, not by raising.
        errors = (response or {}).get("Errors", []) if isinstance(response, dict) else []
        failed += len(errors)
        deleted += len(batch) - len(errors)
        for err in errors[:10]:
            log.warning(f"Could not delete stale s3://{bucket}/{err.get('Key')}: "
                        f"{err.get('Code')} {err.get('Message')}")
    if deleted:
        log.info(f"Removed {deleted} object(s) left over from an earlier run under "
                 f"s3://{bucket}/{prefix}")
    if failed:
        log.warning(f"{failed} stale object(s) could not be removed from "
                    f"s3://{bucket}/{prefix}; the new package is complete.")
    return {"deleted": deleted, "failed": failed}


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
