from fractions import Fraction
import re


def parse_fps(fps_str: str):
    """
    Parse FPS string and detect Drop Frame.
    Supports:
      - integer fps: '24', '25'
      - fractional fps: '2997/125', '30000/1001', '60000/1001'
      - optional 'DF' suffix
    Returns: (fps: Fraction, is_df: bool)
    """
    df = False
    fps_clean = fps_str.strip()
    if fps_clean.upper().endswith("DF"):
        df = True
        fps_clean = fps_clean[:-2].strip()
    if "/" in fps_clean:
        fps = Fraction(fps_clean)
    else:
        fps = Fraction(fps_clean)
    return (fps, df)


def parse_timecode(tc: str):
    """
    Parse timecode string HH:MM:SS:FF or HH:MM:SS;FF
    Returns: hh, mm, ss, ff as integers
    """
    m = re.match("(\\d+):(\\d+):(\\d+)[;:](\\d+)", tc)
    if not m:
        raise ValueError(f"Invalid timecode: {tc}")
    return tuple(map(int, m.groups()))


def _ndf_tc_to_frames(hh, mm, ss, ff, fps: Fraction) -> Fraction:
    """
    Non-Drop Frame timecode -> total frames
    Returns Fraction to avoid rounding errors
    """
    total_frames = (hh * 3600 + mm * 60 + ss) * fps + ff
    return total_frames


def _df_tc_to_frames(hh, mm, ss, ff, fps: Fraction) -> int:
    """
    Drop Frame timecode -> total frames (int)
    Uses SMPTE drop-frame rules for 29.97 and 59.94 fps
    """
    fps_float = float(fps)
    if abs(fps_float - 29.97) < 0.01 or abs(fps_float - 29.97002997002997) < 0.01:
        nominal_fps = 30
        drop = 2
    elif abs(fps_float - 59.94) < 0.01 or abs(fps_float - 59.94005994005994) < 0.01:
        nominal_fps = 60
        drop = 4
    else:
        raise ValueError(f"Unsupported DF fps={fps}")
    if ss == 0 and ff < drop and mm % 10 != 0:
        ff = nominal_fps - 1
        ss = 59
        if mm == 0:
            mm = 59
            hh -= 1
        else:
            mm -= 1
    total_minutes = hh * 60 + mm
    total_frames = (total_minutes * 60 + ss) * nominal_fps + ff
    num_drops = total_minutes - total_minutes // 10
    total_frames -= num_drops * drop
    return total_frames


def timecode_to_frame(tc: str, fps_str: str) -> int:
    """
    Convert timecode HH:MM:SS:FF or HH:MM:SS;FF -> total frames (int)
    Supports DF and NDF.
    """
    fps, df = parse_fps(fps_str)
    hh, mm, ss, ff = parse_timecode(tc)
    is_2997_family = abs(float(fps) - 29.97) < 0.01
    if df or is_2997_family:
        return _df_tc_to_frames(hh, mm, ss, ff, fps)
    else:
        return int(round(_ndf_tc_to_frames(hh, mm, ss, ff, fps)))


def timecode_to_seconds(tc: str, fps_str: str) -> Fraction:
    """
    Convert timecode -> seconds
    Supports DF and NDF
    """
    fps, df = parse_fps(fps_str)
    hh, mm, ss, ff = parse_timecode(tc)
    is_2997_family = abs(float(fps) - 29.97) < 0.01
    if df or is_2997_family:
        frames = _df_tc_to_frames(hh, mm, ss, ff, fps)
        return Fraction(frames, fps)
    else:
        frames = _ndf_tc_to_frames(hh, mm, ss, ff, fps)
        return frames / fps


def seconds_to_timecode(seconds: Fraction, fps_str: str, drop_frame_tc_format=False) -> str:
    """
    Convert seconds -> timecode string
    DF uses SMPTE drop-frame rules.
    """
    fps, df_from_str = parse_fps(fps_str)
    df = drop_frame_tc_format or df_from_str
    total_frames = int(round(seconds * fps))
    if df:
        fps_float = float(fps)
        if abs(fps_float - 29.97) < 0.01 or abs(fps_float - 29.97002997002997) < 0.01:
            nominal_fps = 30
            drop = 2
        elif abs(fps_float - 59.94) < 0.01 or abs(fps_float - 59.94005994005994) < 0.01:
            nominal_fps = 60
            drop = 4
        else:
            raise ValueError(f"Unsupported DF fps={fps}")
        frames = total_frames
        d = drop
        R = nominal_fps
        estimated_total_minutes = frames // (R * 60)
        frames_compensated = frames + d * (estimated_total_minutes - estimated_total_minutes // 10)
        hh = frames_compensated // (R * 3600)
        frames_compensated %= R * 3600
        mm = frames_compensated // (R * 60)
        frames_compensated %= R * 60
        ss = frames_compensated // R
        ff = frames_compensated % R
        total_minutes_derived = hh * 60 + mm
        recalculated_dropped_frames = d * (total_minutes_derived - total_minutes_derived // 10)
        frames_final_ndf_equivalent = frames + recalculated_dropped_frames
        hh = frames_final_ndf_equivalent // (R * 3600)
        frames_final_ndf_equivalent %= R * 3600
        mm = frames_final_ndf_equivalent // (R * 60)
        frames_final_ndf_equivalent %= R * 60
        ss = frames_final_ndf_equivalent // R
        ff = frames_final_ndf_equivalent % R
        return f"{hh:02d}:{mm:02d}:{ss:02d};{ff:02d}"
    else:
        seconds_exact = Fraction(total_frames, fps)
        hh = int(seconds_exact // 3600)
        remaining_seconds_frac = seconds_exact % 3600
        mm = int(remaining_seconds_frac // 60)
        remaining_seconds_frac %= 60
        ss = int(remaining_seconds_frac // 1)
        ff = int(remaining_seconds_frac % 1 * fps)
        nominal_fps_int = int(round(float(fps)))
        if ff >= nominal_fps_int:
            ff = 0
            ss += 1
            if ss >= 60:
                ss = 0
                mm += 1
                if mm >= 60:
                    mm = 0
                    hh += 1
        return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"
