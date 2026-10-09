"""A one-second test encode with each in-house encoder.

Listing ``-encoders`` only proves libwz264 / libwz265 are compiled in. Whether
they will actually encode depends on the licence (``bin/wz_license.cnf`` /
``wz_license.key``, or the ``WZ_LICENSE_*`` environment), and a missing or
expired licence would otherwise surface only when the first real job reaches
its encode. This runs a tiny synthetic encode (FFmpeg's own test pattern,
discarded with ``-f null``) — no input files, nothing written — and reports
exactly what the encoder said.

It is a check of the build and licence only: it does not use, and has no
effect on, the encoding settings of real jobs.

The last result is cached beside the CPU budget files, so ``/ready`` can report
it without starting FFmpeg on every probe.
"""
import json
import os
import subprocess
import time
from typing import Any, Dict, List, Optional

_TIMEOUT_SECONDS = 120
_CACHE_NAME = "encoder_check.json"
_LICENCE_WORDS = ("licen", "expire", "authoriz", "authoris", "activation", "wz_license")
CODECS = {"H_264": "libwz264", "H_265": "libwz265"}


def encoders_in_use(config: Dict[str, Any]) -> List[str]:
    """The in-house encoders the configured templates need."""
    wanted = set()
    for ladder in (config.get("video_templates") or {}).values():
        for rung in ladder or []:
            encoder = CODECS.get(str(rung.get("codec", "")).upper())
            if encoder:
                wanted.add(encoder)
    return sorted(wanted) or ["libwz264"]


def test_encode(ffmpeg: str, encoder: str) -> Dict[str, Any]:
    """Encode one second of test pattern with `encoder`. Returns ok/detail."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
           "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
           "-t", "1", "-pix_fmt", "yuv420p", "-c:v", encoder, "-f", "null", "-"]
    started = time.monotonic()
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return {"ok": False, "encoder": encoder,
                "detail": f"{encoder}: the test encode did not finish within "
                          f"{_TIMEOUT_SECONDS}s",
                "fix": "If the licence is checked online, this server needs outbound "
                       "HTTPS access (and any proxy configured)."}
    except OSError as e:
        return {"ok": False, "encoder": encoder, "detail": f"FFmpeg would not run: {e}",
                "fix": ""}
    seconds = time.monotonic() - started
    text = ((out.stderr or "") + (out.stdout or "")).strip()
    tail = " | ".join(text.splitlines()[-3:])[:500]
    if out.returncode == 0:
        return {"ok": True, "encoder": encoder,
                "detail": f"{encoder} encoded a test clip in {seconds:.1f}s", "fix": ""}
    if "lavfi" in text and ("Unknown input format" in text or "not found" in text):
        return {"ok": None, "encoder": encoder,
                "detail": "this FFmpeg build has no lavfi test source; test encode "
                          "skipped", "fix": ""}
    fix = ""
    if any(word in text.lower() for word in _LICENCE_WORDS):
        fix = ("The encoder rejected its licence. Check bin/wz_license.cnf and "
               "bin/wz_license.key (or WZ_LICENSE_PATH), that they match this "
               "server, and that they have not expired.")
    return {"ok": False, "encoder": encoder,
            "detail": f"{encoder} test encode failed (exit {out.returncode}): "
                      f"{tail or 'no output'}", "fix": fix}


def _cache_path():
    from hls_toolkit.coordination import coordination_dir
    return coordination_dir() / _CACHE_NAME


def _binary_stamp(ffmpeg: str) -> Optional[float]:
    try:
        return os.stat(ffmpeg).st_mtime
    except OSError:
        return None


def run_and_cache(ffmpeg: str, encoders: List[str]) -> List[Dict[str, Any]]:
    """Test every encoder in `encoders` and remember the outcome."""
    results = [test_encode(ffmpeg, encoder) for encoder in encoders]
    record = {"at": time.time(), "ffmpeg": os.path.abspath(ffmpeg),
              "ffmpeg_mtime": _binary_stamp(ffmpeg), "results": results}
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(record))
        os.replace(tmp, path)
    except OSError:
        pass
    return results


def cached_result(ffmpeg: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The last test encode as {ok, detail}, or None if there is none to trust."""
    try:
        record = json.loads(_cache_path().read_text())
    except (OSError, ValueError):
        return None
    if ffmpeg and record.get("ffmpeg") != os.path.abspath(ffmpeg):
        return None
    if record.get("ffmpeg_mtime") != _binary_stamp(record.get("ffmpeg", "")):
        return None                      # the binary was replaced since
    results = record.get("results") or []
    failed = [r for r in results if r.get("ok") is False]
    age_min = (time.time() - float(record.get("at", 0))) / 60
    if failed:
        return {"ok": False, "detail": "; ".join(r["detail"] for r in failed)
                + f" (checked {age_min:.0f} min ago)"}
    return {"ok": True, "detail": "; ".join(r["detail"] for r in results)
            + f" (checked {age_min:.0f} min ago)"}


__all__ = ["encoders_in_use", "test_encode", "run_and_cache", "cached_result"]
