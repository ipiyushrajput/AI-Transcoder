"""Environment checks that run before a transcode is attempted.

``python app.py --check`` validates everything a job depends on — Python
packages, the FFmpeg build, AWS credentials, the input objects, write access to
the output bucket, and the database — and reports each one individually. The
point is to fail on a server misconfiguration at setup time, with a specific
remedy, rather than several minutes into a job.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hls_toolkit import s3_io
from hls_toolkit.runner import build_run_settings

OK = "ok"
WARN = "warn"
FAIL = "fail"

# botocore below this mishandles a broken optional pyOpenSSL import; see
# requirements.txt for the full story.
MIN_BOTOCORE = (1, 38, 46)


class Result:
    def __init__(self, name: str, status: str, detail: str = "", fix: str = ""):
        self.name = name
        self.status = status
        self.detail = detail
        self.fix = fix

    @property
    def failed(self) -> bool:
        return self.status == FAIL


def _version_tuple(text: str) -> Tuple[int, ...]:
    parts = []
    for chunk in str(text).split(".")[:3]:
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def check_python() -> Result:
    major, minor = sys.version_info[:2]
    if (major, minor) < (3, 8):
        return Result("Python version", FAIL,
                      f"{sys.version.split()[0]} — 3.8 or newer is required",
                      "Install Python 3.11 and recreate the virtualenv.")
    inside_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    detail = f"{sys.version.split()[0]} ({'virtualenv' if inside_venv else 'system Python'})"
    if not inside_venv:
        return Result("Python version", WARN, detail,
                      "Using the system Python mixes apt and pip packages, which "
                      "is the usual cause of TLS version mismatches. Prefer: "
                      "python3 -m venv .venv && . .venv/bin/activate && "
                      "pip install -r requirements.txt")
    return Result("Python version", OK, detail)


def check_tls_stack() -> Result:
    """The failure mode that breaks every S3 call on a mismatched install."""
    try:
        import OpenSSL  # noqa: F401
    except ModuleNotFoundError:
        return Result("TLS packages (pyOpenSSL/cryptography)", OK,
                      "pyOpenSSL not installed — botocore uses the stdlib SSL context")
    except Exception as e:
        hint = s3_io.environment_problem(e)
        return Result("TLS packages (pyOpenSSL/cryptography)", FAIL,
                      f"{type(e).__name__}: {e}",
                      hint or "pip install --upgrade 'pyOpenSSL>=24.0.0' "
                              "'cryptography>=42'")

    try:
        import cryptography
        return Result("TLS packages (pyOpenSSL/cryptography)", OK,
                      f"pyOpenSSL {OpenSSL.__version__}, "
                      f"cryptography {cryptography.__version__}")
    except Exception:
        return Result("TLS packages (pyOpenSSL/cryptography)", OK, "pyOpenSSL imports cleanly")


def check_boto3() -> Result:
    try:
        import boto3
        import botocore
    except Exception as e:
        hint = s3_io.environment_problem(e)
        return Result("boto3 / botocore", FAIL, f"{type(e).__name__}: {e}",
                      hint or "pip install -r requirements.txt")

    version = _version_tuple(botocore.__version__)
    detail = f"boto3 {boto3.__version__}, botocore {botocore.__version__}"
    if version < MIN_BOTOCORE:
        return Result("boto3 / botocore", WARN, detail,
                      f"botocore < {'.'.join(map(str, MIN_BOTOCORE))} crashes on hosts "
                      f"with a mismatched pyOpenSSL. "
                      f"pip install --upgrade 'boto3>=1.38.46' 'botocore>=1.38.46'")

    # Import the module that actually performs the optional pyOpenSSL import.
    try:
        import botocore.httpsession  # noqa: F401
    except Exception as e:
        hint = s3_io.environment_problem(e)
        return Result("boto3 / botocore", FAIL,
                      f"botocore.httpsession failed to import: {e}",
                      hint or "pip install -r requirements.txt")
    return Result("boto3 / botocore", OK, detail)


def check_ffmpeg(settings: Dict[str, Any]) -> List[Result]:
    results = []
    for key, label in (("ffmpeg_executable", "FFmpeg"),
                       ("ffprobe_executable", "FFprobe")):
        path = settings[key]
        if not os.path.exists(path):
            results.append(Result(label, FAIL, f"not found at {path}",
                                  f"Place the custom build there, or correct "
                                  f"paths.{key} in the config."))
            continue
        if not os.access(path, os.X_OK):
            results.append(Result(label, FAIL, f"{path} is not executable",
                                  f"chmod +x {path}"))
            continue
        try:
            out = subprocess.run([path, "-version"], capture_output=True, text=True,
                                 timeout=30)
            first = (out.stdout or out.stderr).splitlines()
            results.append(Result(label, OK, first[0] if first else path))
        except Exception as e:
            results.append(Result(label, FAIL, f"{path} would not run: {e}",
                                  "Check the binary's architecture and its shared "
                                  "library dependencies (ldd)."))
    results.append(_check_encoders(settings["ffmpeg_executable"]))
    return results


def _check_encoders(ffmpeg: str) -> Result:
    """Confirm the in-house encoders are present in this FFmpeg build."""
    if not (os.path.exists(ffmpeg) and os.access(ffmpeg, os.X_OK)):
        return Result("libwz264 / libwz265 encoders", WARN,
                      "skipped — FFmpeg is not runnable")
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=60)
        text = (out.stdout or "") + (out.stderr or "")
    except Exception as e:
        return Result("libwz264 / libwz265 encoders", WARN,
                      f"could not list encoders: {e}")

    found = [name for name in ("libwz264", "libwz265") if name in text]
    if not found:
        return Result("libwz264 / libwz265 encoders", FAIL,
                      "neither encoder is present in this FFmpeg build",
                      "bin/ffmpeg is a stock build. Install the in-house build "
                      "that provides libwz264 / libwz265.")
    if len(found) == 1:
        return Result("libwz264 / libwz265 encoders", WARN,
                      f"only {found[0]} is available",
                      "Templates using the other codec will fail.")
    return Result("libwz264 / libwz265 encoders", OK, "libwz264, libwz265")


def check_aws_identity(region: Optional[str]) -> Result:
    try:
        import boto3
        sts = boto3.client("sts", region_name=region)
        identity = sts.get_caller_identity()
        return Result("AWS credentials", OK,
                      f"account {identity.get('Account')} as "
                      f"{identity.get('Arn', '').rsplit('/', 1)[-1]}")
    except Exception as e:
        hint = s3_io.environment_problem(e)
        return Result("AWS credentials", FAIL, f"{type(e).__name__}: {e}",
                      hint or "Attach an instance role, or configure credentials "
                              "with `aws configure`.")


def check_input(uri: Optional[str], label: str, region: Optional[str]) -> Result:
    if not uri:
        return Result(label, OK, "not configured")
    if not s3_io.is_s3_uri(uri):
        if os.path.exists(uri):
            size = os.path.getsize(uri)
            return Result(label, OK, f"local file, {_human(size)}")
        return Result(label, FAIL, f"local file not found: {uri}",
                      "Correct the path, or use an s3:// URI.")
    try:
        meta = s3_io.head_object(uri, region=region)
        return Result(label, OK, f"{uri} ({_human(int(meta.get('ContentLength', 0)))})")
    except Exception as e:
        return Result(label, FAIL, str(e).splitlines()[0],
                      "Check the URI and that this server's role can read it.")


def check_output_bucket(bucket: Optional[str], prefix: str,
                        region: Optional[str]) -> Result:
    """Write and delete a probe object — read access does not imply write."""
    if not bucket:
        return Result("S3 output bucket", FAIL, "s3.bucket_name is not set",
                      "Set s3.bucket_name in the config, or disable upload.")
    key = f"{prefix.strip('/')}/.preflight-{uuid.uuid4().hex[:8]}" if prefix \
        else f".preflight-{uuid.uuid4().hex[:8]}"
    try:
        client = s3_io.get_s3_client(region)
        client.put_object(Bucket=bucket, Key=key, Body=b"ok")
        client.delete_object(Bucket=bucket, Key=key)
        return Result("S3 output bucket", OK, f"s3://{bucket}/{prefix} is writable")
    except Exception as e:
        hint = s3_io.environment_problem(e)
        code = s3_io._error_code(e)
        if code in ("403", "AccessDenied"):
            return Result("S3 output bucket", FAIL,
                          f"write denied on s3://{bucket}/{prefix}",
                          "Grant s3:PutObject and s3:DeleteObject on that prefix.")
        return Result("S3 output bucket", FAIL, f"{type(e).__name__}: {e}",
                      hint or "Check the bucket name and region.")


def check_directories(log_root: str, work_root: Optional[str]) -> List[Result]:
    results = []
    for path, label in ((log_root, "Log directory"),
                        (work_root or tempfile.gettempdir(), "Work directory")):
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
            probe = Path(path) / f".preflight-{uuid.uuid4().hex[:8]}"
            probe.write_text("ok")
            probe.unlink()
            free = shutil.disk_usage(path).free
            status = WARN if free < 20 * 1024 ** 3 else OK
            fix = ("Less than 20 GB free. A job needs roughly 3x the source file "
                   "size." if status == WARN else "")
            results.append(Result(label, status, f"{path} ({_human(free)} free)", fix))
        except Exception as e:
            results.append(Result(label, FAIL, f"{path}: {e}",
                                  f"Create it and grant write access to this user."))
    return results


def check_database() -> Result:
    try:
        from api import database as db
    except Exception as e:
        return Result("PostgreSQL", WARN, f"API dependencies unavailable: {e}",
                      "Only needed for the HTTP service. pip install -r requirements.txt")
    if db.init_db():
        return Result("PostgreSQL", OK, db._safe_url())
    return Result("PostgreSQL", WARN, f"cannot connect to {db._safe_url()}",
                  "Only needed for the HTTP service — transcodes run without it. "
                  "See docs/POSTGRES_SETUP.md")


def run_all(config: Dict[str, Any], log_root: str = "logs",
            work_root: Optional[str] = None,
            include_database: bool = True) -> List[Result]:
    """Run every check and return the results in report order."""
    settings = build_run_settings(config, {})
    region = settings["s3_region"]
    s3_config = settings["s3_config"]

    results = [check_python(), check_tls_stack(), check_boto3()]
    results.extend(check_ffmpeg(settings))
    results.extend(check_directories(log_root, work_root))

    if settings["upload"] or s3_io.is_s3_uri(settings["input_video"] or ""):
        results.append(check_aws_identity(region))
    results.append(check_input(settings["input_video"], "Input video", region))
    results.append(check_input(settings["subtitle_file"], "Subtitle file", region))
    if settings["upload"]:
        results.append(check_output_bucket(s3_config.get("bucket_name"),
                                           s3_config.get("key_prefix", ""), region))
    if include_database:
        results.append(check_database())
    return results


def format_report(results: List[Result]) -> str:
    symbols = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[FAIL]"}
    width = max(len(r.name) for r in results)
    lines = ["", "Preflight checks", "=" * 72]
    for r in results:
        lines.append(f"{symbols[r.status]}  {r.name.ljust(width)}  {r.detail}")
        if r.fix:
            for fix_line in r.fix.splitlines():
                lines.append(f"        -> {fix_line.strip()}")
    lines.append("=" * 72)

    failures = [r for r in results if r.status == FAIL]
    warnings = [r for r in results if r.status == WARN]
    if failures:
        lines.append(f"{len(failures)} check(s) FAILED — fix these before running a job.")
    elif warnings:
        lines.append(f"All required checks passed ({len(warnings)} warning(s)).")
    else:
        lines.append("All checks passed.")
    lines.append("")
    return "\n".join(lines)


def _human(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} PB"
