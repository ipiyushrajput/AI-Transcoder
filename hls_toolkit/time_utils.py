"""SMPTE timecode arithmetic (ported from SDK v2.3).

Rules:

* A ``;`` before the frame field means drop-frame, and is only valid at the
  SMPTE drop-frame rates 30000/1001 and 60000/1001. A ``DF`` suffix on the
  frame rate means the same.
* A ``:`` means non-drop-frame, except that with ``allow_colon_df=True`` (which
  every caller in this pipeline passes, as the SDK does) a colon timecode at
  30000/1001 or 60000/1001 is read as drop-frame, to match how customer
  clipping lists are written.
* Timecodes are validated: minutes and seconds 0..59, frames below the nominal
  rate, and drop-frame labels that do not exist (frames 0-1 / 0-3 at the start
  of a minute not divisible by 10) are rejected instead of silently adjusted.
* Rates are compared as exact fractions; "29.97", "59.94", "23.976" and the
  like are read as their 1001-denominator rates, and so is a value that is a
  1001 rate passed through float() (e.g. "29.97002997002997"). A genuinely
  different rate such as 2997/100 is not changed.
"""
from fractions import Fraction
import re
from typing import Dict, Tuple

DROP_FRAME_RATES: Dict[Fraction, Tuple[int, int]] = {
    Fraction(60000, 1001): (60, 4),
    Fraction(30000, 1001): (30, 2),
}

CANONICAL_FPS_ALIASES: Dict[str, Fraction] = {
    "23.976": Fraction(24000, 1001),
    "23.98": Fraction(24000, 1001),
    "29.97": Fraction(30000, 1001),
    "59.94": Fraction(60000, 1001),
    "59.9401": Fraction(60000, 1001),
    "119.88": Fraction(120000, 1001),
}


# Exact 1001-denominator rates a float-rounded value is snapped back to.
_NTSC_RATES = (Fraction(24000, 1001), Fraction(30000, 1001), Fraction(60000, 1001),
               Fraction(120000, 1001))
# Far below any real rate difference (2997/100 is 3e-5 away from 30000/1001),
# far above float rounding (about 1e-15).
_FLOAT_ROUNDING_TOLERANCE = Fraction(1, 1000000)


def _undo_float_rounding(fps: Fraction) -> Fraction:
    """30000/1001 that went through float() comes back as
    2997002997002997/100000000000000; give the exact rate back so drop-frame
    detection, which compares exactly, still recognises it."""
    for rate in _NTSC_RATES:
        if fps != rate and abs(fps - rate) < _FLOAT_ROUNDING_TOLERANCE:
            return rate
    return fps


def parse_fps(fps_str):
    """Parse a frame rate and an optional ``DF`` marker.

    Accepts a Fraction, '29.97'-style aliases, '30000/1001'-style fractions,
    integers and floats, each optionally followed by ``DF``.
    Returns: (fps: Fraction, is_df: bool)
    """
    if isinstance(fps_str, Fraction):
        return (_undo_float_rounding(fps_str), False)
    df = False
    fps_clean = str(fps_str).strip()
    if fps_clean.upper().endswith("DF"):
        df = True
        fps_clean = fps_clean[:-2].strip()
    if fps_clean in CANONICAL_FPS_ALIASES:
        fps = CANONICAL_FPS_ALIASES[fps_clean]
    else:
        fps = _undo_float_rounding(Fraction(fps_clean))
    return (fps, df)


def parse_timecode(tc: str):
    """
    Parse timecode string HH:MM:SS:FF or HH:MM:SS;FF
    Returns: hh, mm, ss, ff as integers
    """
    m = re.match(r"^(\d+):(\d{2}):(\d{2})[;:](\d{2})$", str(tc).strip())
    if not m:
        raise ValueError(f"Invalid timecode format: '{tc}' "
                         f"(expected HH:MM:SS:FF or HH:MM:SS;FF)")
    return tuple(map(int, m.groups()))


def validate_timecode_components(hh: int, mm: int, ss: int, ff: int, nominal_fps: int) -> None:
    """Reject a timecode outside SMPTE bounds."""
    if hh < 0:
        raise ValueError(f"Invalid hours in timecode: {hh} (must be >= 0)")
    if not 0 <= mm < 60:
        raise ValueError(f"Invalid minutes in timecode: {mm} (must be 0..59)")
    if not 0 <= ss < 60:
        raise ValueError(f"Invalid seconds in timecode: {ss} (must be 0..59)")
    if not 0 <= ff < nominal_fps:
        raise ValueError(f"Invalid frames in timecode: {ff} (must be 0..{nominal_fps - 1})")


def _is_drop_frame_rate(fps: Fraction) -> bool:
    """True only for exactly 30000/1001 or 60000/1001 (no float tolerance)."""
    return fps in DROP_FRAME_RATES


is_drop_frame_rate = _is_drop_frame_rate


def get_df_params(fps: Fraction) -> Tuple[int, int]:
    """(nominal fps, frames dropped per minute) for a drop-frame rate."""
    if fps in DROP_FRAME_RATES:
        return DROP_FRAME_RATES[fps]
    raise ValueError(f"Frame rate {fps} is not a valid SMPTE Drop-Frame rate "
                     f"(expected 30000/1001 or 60000/1001)")


def _df_tc_to_frames(hh: int, mm: int, ss: int, ff: int, fps: Fraction) -> int:
    """Drop-frame timecode -> total frames, rejecting labels that do not exist."""
    nominal_fps, drop = get_df_params(fps)
    validate_timecode_components(hh, mm, ss, ff, nominal_fps)
    if ss == 0 and mm % 10 != 0 and ff < drop:
        raise ValueError(f"Invalid drop-frame timecode: frame {ff:02d} does not exist at "
                         f"minute boundary {mm:02d}:00 (frames 0..{drop - 1} dropped for "
                         f"rate {fps})")
    total_minutes = hh * 60 + mm
    drop_minutes = total_minutes - total_minutes // 10
    return (hh * 3600 + mm * 60 + ss) * nominal_fps + ff - drop_minutes * drop


def _df_frames_to_tc(total_frames: int, fps: Fraction) -> Tuple[int, int, int, int]:
    """Total frames -> (hh, mm, ss, ff) in drop-frame, by 10-minute blocks."""
    nominal_fps, drop = get_df_params(fps)
    if total_frames < 0:
        raise ValueError(f"Negative frame count {total_frames} cannot be converted to timecode")
    f10 = 600 * nominal_fps - 9 * drop
    f1 = 60 * nominal_fps - drop
    d10, m10 = divmod(total_frames, f10)
    if m10 < 60 * nominal_fps:
        m1 = 0
        rem = m10
    else:
        m_rem = m10 - 60 * nominal_fps
        m1 = 1 + m_rem // f1
        rem = m_rem % f1 + drop
    total_minutes = d10 * 10 + m1
    hh, mm = divmod(total_minutes, 60)
    ss, ff = divmod(rem, nominal_fps)
    return (hh, mm, ss, ff)


def nominal_fps_from_fps(fps: Fraction) -> int:
    """Integer nominal rate (for the frame-field bound only, not DF detection)."""
    return (fps.numerator + fps.denominator // 2) // fps.denominator


def _ndf_tc_to_frames(hh: int, mm: int, ss: int, ff: int, fps: Fraction) -> Fraction:
    """Non-drop-frame timecode -> total frames, as an exact Fraction."""
    nominal_fps = nominal_fps_from_fps(fps)
    validate_timecode_components(hh, mm, ss, ff, nominal_fps)
    return (hh * 3600 + mm * 60 + ss) * fps + ff


def _ndf_frames_to_tc(total_frames: int, fps: Fraction) -> Tuple[int, int, int, int]:
    """Total frames -> (hh, mm, ss, ff) in non-drop-frame."""
    if total_frames < 0:
        raise ValueError(f"Negative frame count {total_frames} cannot be converted to timecode")
    seconds_exact = Fraction(total_frames, fps)
    hh = int(seconds_exact // 3600)
    rem_sec = seconds_exact % 3600
    mm = int(rem_sec // 60)
    rem_sec %= 60
    ss = int(rem_sec // 1)
    ff = int(round((rem_sec % 1) * fps))
    nominal_fps = nominal_fps_from_fps(fps)
    if ff >= nominal_fps:
        ff = 0
        ss += 1
        if ss >= 60:
            ss = 0
            mm += 1
            if mm >= 60:
                mm = 0
                hh += 1
    return (hh, mm, ss, ff)


def resolve_drop_frame(tc: str, fps: Fraction, explicit_df: bool = False,
                       allow_colon_df: bool = False) -> bool:
    """Whether `tc` is drop-frame: ';' or a DF rate marker (only valid at DF
    rates), or a colon at a DF rate when `allow_colon_df` is set."""
    if ";" in tc or explicit_df:
        if not _is_drop_frame_rate(fps):
            raise ValueError(f"Drop-frame notation is not valid for frame rate: {fps} "
                             f"(Drop-frame is only valid for SMPTE rates 30000/1001 "
                             f"or 60000/1001)")
        return True
    if allow_colon_df and _is_drop_frame_rate(fps):
        return True
    return False


def timecode_to_frame(tc: str, fps_str, allow_colon_df: bool = False) -> int:
    """Timecode -> total frames (int), DF or NDF."""
    fps, explicit_df = parse_fps(fps_str)
    hh, mm, ss, ff = parse_timecode(tc)
    if resolve_drop_frame(tc, fps, explicit_df=explicit_df, allow_colon_df=allow_colon_df):
        return _df_tc_to_frames(hh, mm, ss, ff, fps)
    return int(round(_ndf_tc_to_frames(hh, mm, ss, ff, fps)))


def timecode_to_seconds(tc: str, fps_str, allow_colon_df: bool = False) -> Fraction:
    """Timecode -> seconds as an exact Fraction, DF or NDF."""
    fps, explicit_df = parse_fps(fps_str)
    hh, mm, ss, ff = parse_timecode(tc)
    if resolve_drop_frame(tc, fps, explicit_df=explicit_df, allow_colon_df=allow_colon_df):
        return Fraction(_df_tc_to_frames(hh, mm, ss, ff, fps), fps)
    return _ndf_tc_to_frames(hh, mm, ss, ff, fps) / fps


def seconds_to_timecode(seconds, fps_str, drop_frame_tc_format: bool = False) -> str:
    """Seconds -> timecode string (``;`` before the frames when drop-frame)."""
    if seconds < 0:
        raise ValueError(f"Negative seconds {seconds} cannot be converted to timecode")
    fps, df_from_str = parse_fps(fps_str)
    if drop_frame_tc_format or df_from_str:
        if not _is_drop_frame_rate(fps):
            raise ValueError(f"Drop-frame timecode format is not supported for frame rate: {fps}")
        total_frames = int(round(seconds * fps))
        hh, mm, ss, ff = _df_frames_to_tc(total_frames, fps)
        return f"{hh:02d}:{mm:02d}:{ss:02d};{ff:02d}"
    total_frames = int(round(seconds * fps))
    hh, mm, ss, ff = _ndf_frames_to_tc(total_frames, fps)
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"
