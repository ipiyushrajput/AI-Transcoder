"""Check a finished HLS package before anything is published.

A job used to report COMPLETED as long as no step raised, so a rendition whose
packaging failed simply went missing while the master playlist still pointed at
it, and a truncated encode published as if it were whole. This module looks at
what is actually on disk and fails the job, naming every problem, before a
broken package can reach S3.
"""
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from hls_toolkit.job_context import TranscodeError

MASTER_PLAYLIST = "channel.m3u8"
_URI_ATTRIBUTE = re.compile(r'URI="([^"]+)"')


def _read_lines(path: Path) -> List[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _master_references(master: Path):
    """(variant playlist URIs, media playlist URIs) listed in a master playlist."""
    variants, media = [], []
    lines = _read_lines(master)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#EXT-X-STREAM-INF"):
            for nxt in lines[i + 1:]:
                if nxt.strip() and not nxt.strip().startswith("#"):
                    variants.append(nxt.strip())
                    break
        elif stripped.startswith("#EXT-X-MEDIA"):
            match = _URI_ATTRIBUTE.search(stripped)
            if match:
                media.append(match.group(1))
    return variants, media


def _check_media_playlist(path: Path, problems: List[str]) -> Optional[Dict[str, Any]]:
    """Check one media playlist and its segments. Returns its totals, or None."""
    lines = _read_lines(path)
    name = path.name
    if not lines or lines[0].strip() != "#EXTM3U":
        problems.append(f"{name}: not an HLS playlist (no #EXTM3U header)")
        return None
    if not any(line.strip() == "#EXT-X-ENDLIST" for line in lines):
        problems.append(f"{name}: incomplete — no #EXT-X-ENDLIST")

    total = 0.0
    segments = 0
    missing: List[str] = []
    empty: List[str] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("#EXTINF:"):
            continue
        try:
            total += float(stripped.split(":", 1)[1].split(",", 1)[0])
        except ValueError:
            problems.append(f"{name}: unreadable segment duration on line {i + 1}")
        uri = next((n.strip() for n in lines[i + 1:]
                    if n.strip() and not n.strip().startswith("#")), None)
        if uri is None:
            problems.append(f"{name}: segment entry on line {i + 1} has no file")
            continue
        segments += 1
        segment = path.parent / uri
        if not segment.is_file():
            missing.append(uri)
        elif segment.stat().st_size == 0:
            empty.append(uri)
    if segments == 0:
        problems.append(f"{name}: contains no segments")
    if missing:
        problems.append(f"{name}: {len(missing)} segment(s) missing, e.g. "
                        f"{', '.join(missing[:3])}")
    if empty:
        problems.append(f"{name}: {len(empty)} segment(s) are empty, e.g. "
                        f"{', '.join(empty[:3])}")
    return {"segments": segments, "duration": total}


def validate_package(output_dir: Path,
                     expected_renditions: List[str],
                     expected_duration: Optional[float] = None,
                     hls_time: float = 6.0,
                     expect_thumbnails: bool = False) -> Dict[str, Any]:
    """Raise TranscodeError(stage VALIDATING_OUTPUT) unless the package is whole.

    Checks that the master playlist exists and lists every expected rendition;
    that every playlist it references exists, is complete and has all its
    segments, none of them empty; and that each rendition's total duration
    matches `expected_duration` to within half a segment (at least one second).

    Returns a summary for the job metadata.
    """
    output_dir = Path(output_dir)
    problems: List[str] = []
    master = output_dir / MASTER_PLAYLIST
    if not master.is_file():
        raise TranscodeError(f"The package has no master playlist ({MASTER_PLAYLIST}).",
                             stage="VALIDATING_OUTPUT")

    variants, media = _master_references(master)
    if not variants:
        problems.append(f"{MASTER_PLAYLIST} lists no renditions")
    for name in expected_renditions:
        playlist = f"channel_{name}.m3u8"
        if playlist not in variants:
            problems.append(f"{MASTER_PLAYLIST} does not list rendition {name} ({playlist})")

    tolerance = max(1.0, 0.5 * float(hls_time or 6.0))
    summary: Dict[str, Any] = {"renditions": {}, "media_playlists": {}}
    for uri in variants + media:
        path = output_dir / uri
        if not path.is_file():
            problems.append(f"{MASTER_PLAYLIST} references {uri}, which does not exist")
            continue
        totals = _check_media_playlist(path, problems)
        if totals is None:
            continue
        (summary["renditions"] if uri in variants else summary["media_playlists"])[uri] = totals
        if expected_duration and expected_duration > 0:
            gap = abs(totals["duration"] - expected_duration)
            if gap > tolerance:
                problems.append(
                    f"{uri}: runs {totals['duration']:.3f}s but {expected_duration:.3f}s "
                    f"was expected (off by {gap:.3f}s; tolerance {tolerance:.1f}s)")

    if expect_thumbnails and not any((output_dir / "thumbnails").glob("*.jpg")):
        problems.append("thumbnails were requested but none were produced")

    if problems:
        shown = problems[:20]
        more = f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else ""
        raise TranscodeError(
            "The package failed validation, so it was not published:\n  "
            + "\n  ".join(shown) + more, stage="VALIDATING_OUTPUT")
    return summary


__all__ = ["validate_package", "MASTER_PLAYLIST"]
