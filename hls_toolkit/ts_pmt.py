"""Declare a SCTE-35 PID in the PMT of finished MPEG-TS segments.

MediaConvert's HLS segments list a SCTE-35 elementary stream (stream_type 0x86,
PID 0x1F4) in every segment's PMT, with a ``CUEI`` registration descriptor in
the program info, which is what lets players, packagers and ffprobe see
``Data: scte_35``. It sends no SCTE-35 packets on that PID; the declaration is
the whole difference. FFmpeg's mpegts muxer writes no such entry, so this adds
it after packaging.

The rewrite is done in place, inside the PMT's own 188-byte packets: every
other packet (video, audio, PCR, PAT) is left byte-for-byte as FFmpeg wrote
it, and the segment's size does not change. It is idempotent, and a file that
is not a plain MPEG-TS, or a PMT that would not fit its packet, is left alone.
"""
import os
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from hls_toolkit.job_context import get_logger

TS_PACKET = 188
SCTE35_STREAM_TYPE = 0x86
DEFAULT_SCTE35_PID = 0x1F4
CUEI_REGISTRATION = bytes([0x05, 0x04]) + b"CUEI"


def _crc32_mpeg2(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else (crc << 1)
            crc &= 0xFFFFFFFF
    return crc


def _pid(packet: bytes) -> int:
    return ((packet[1] & 0x1F) << 8) | packet[2]


def _payload_offset(packet: bytes) -> Optional[int]:
    """Where the payload starts, or None when the packet carries none."""
    control = (packet[3] >> 4) & 0x3
    if not control & 0x1:
        return None
    offset = 4
    if control & 0x2:
        offset += 1 + packet[4]
    return offset if offset < TS_PACKET else None


def _section_at(packet: bytes) -> Optional[Tuple[int, bytes]]:
    """(start offset, section) of a section that starts in this packet."""
    if not packet[1] & 0x40:                       # payload_unit_start_indicator
        return None
    offset = _payload_offset(packet)
    if offset is None:
        return None
    start = offset + 1 + packet[offset]            # skip pointer_field
    if start + 3 > TS_PACKET:
        return None
    length = ((packet[start + 1] & 0x0F) << 8) | packet[start + 2]
    end = start + 3 + length
    if end > TS_PACKET:
        return None                                # spans packets: not handled
    return start, packet[start:end]


def _descriptors(blob: bytes) -> Iterable[Tuple[int, bytes]]:
    i = 0
    while i + 2 <= len(blob):
        tag, size = blob[i], blob[i + 1]
        yield tag, blob[i + 2:i + 2 + size]
        i += 2 + size


def parse_pmt(section: bytes) -> dict:
    """Program info and elementary streams of a PMT section."""
    program_info_length = ((section[10] & 0x0F) << 8) | section[11]
    program_info = section[12:12 + program_info_length]
    streams = []
    i = 12 + program_info_length
    end = len(section) - 4
    while i + 5 <= end:
        stream_type = section[i]
        pid = ((section[i + 1] & 0x1F) << 8) | section[i + 2]
        info_length = ((section[i + 3] & 0x0F) << 8) | section[i + 4]
        streams.append((stream_type, pid, section[i + 5:i + 5 + info_length]))
        i += 5 + info_length
    return {"program_info": program_info, "streams": streams,
            "crc_ok": _crc32_mpeg2(section[:-4]) == int.from_bytes(section[-4:], "big")}


def _with_scte35(section: bytes, pid: int) -> bytes:
    """The PMT section with the CUEI registration and the SCTE-35 stream added."""
    info = parse_pmt(section)
    program_info = info["program_info"]
    if not any(tag == 0x05 and body == b"CUEI" for tag, body in _descriptors(program_info)):
        program_info = CUEI_REGISTRATION + program_info
    es_loop = bytearray()
    for stream_type, es_pid, es_info in info["streams"]:
        es_loop += bytes([stream_type, 0xE0 | (es_pid >> 8), es_pid & 0xFF,
                          0xF0 | (len(es_info) >> 8), len(es_info) & 0xFF]) + es_info
    if not any(t == SCTE35_STREAM_TYPE and p == pid for t, p, _ in info["streams"]):
        es_loop += bytes([SCTE35_STREAM_TYPE, 0xE0 | (pid >> 8), pid & 0xFF, 0xF0, 0x00])
    body = (section[3:10]                                   # program number .. PCR PID
            + bytes([0xF0 | (len(program_info) >> 8), len(program_info) & 0xFF])
            + program_info + bytes(es_loop))
    length = len(body) + 4                                  # + CRC
    head = bytes([section[0], (section[1] & 0xF0) | (length >> 8), length & 0xFF])
    without_crc = head + body
    return without_crc + _crc32_mpeg2(without_crc).to_bytes(4, "big")


def declares_scte35(path, pid: int = DEFAULT_SCTE35_PID) -> bool:
    """True when the segment's first PMT lists `pid` as a SCTE-35 stream.

    Reads only the start of the file: the PAT and PMT open every segment.
    """
    with open(path, "rb") as f:
        data = f.read(TS_PACKET * 1024)
    pmt_pid = None
    for i in range(0, len(data) - TS_PACKET + 1, TS_PACKET):
        packet = data[i:i + TS_PACKET]
        if packet[0] != 0x47:
            return False
        found = _section_at(packet)
        if found is None:
            continue
        section = found[1]
        if _pid(packet) == 0 and section[0] == 0x00:
            pmt_pid = ((section[10] & 0x1F) << 8) | section[11]
        elif pmt_pid is not None and _pid(packet) == pmt_pid and section[0] == 0x02:
            info = parse_pmt(section)
            return info["crc_ok"] and any(t == SCTE35_STREAM_TYPE and p == pid
                                          for t, p, _ in info["streams"])
    return False


def add_scte35_pid(path, pid: int = DEFAULT_SCTE35_PID) -> str:
    """Declare `pid` as a SCTE-35 stream in every PMT of one segment.

    Returns "added", "present", or "skipped: <reason>" (the file is untouched).
    """
    path = Path(path)
    data = bytearray(path.read_bytes())
    if not data or len(data) % TS_PACKET or any(
            data[i] != 0x47 for i in range(0, len(data), TS_PACKET)):
        return "skipped: not an MPEG-TS file"

    pmt_pid = None
    used_pids = set()
    pmt_packets: List[int] = []
    for i in range(0, len(data), TS_PACKET):
        packet = bytes(data[i:i + TS_PACKET])
        packet_pid = _pid(packet)
        used_pids.add(packet_pid)
        found = _section_at(packet)
        if found is None:
            continue
        section = found[1]
        if packet_pid == 0 and section[0] == 0x00 and pmt_pid is None:
            pmt_pid = ((section[10] & 0x1F) << 8) | section[11]
        elif pmt_pid is not None and packet_pid == pmt_pid and section[0] == 0x02:
            pmt_packets.append(i)
    if pmt_pid is None or not pmt_packets:
        return "skipped: no PAT/PMT found"

    changed = False
    for i in pmt_packets:
        packet = bytes(data[i:i + TS_PACKET])
        start, section = _section_at(packet)
        info = parse_pmt(section)
        if not info["crc_ok"]:
            return "skipped: PMT CRC does not match"
        stream_pids = {p for _, p, _ in info["streams"]}
        if pid in stream_pids and not any(t == SCTE35_STREAM_TYPE and p == pid
                                          for t, p, _ in info["streams"]):
            return f"skipped: PID {pid:#x} is already used by another stream"
        if pid in used_pids and pid not in stream_pids:
            return f"skipped: PID {pid:#x} carries other data in this segment"
        new_section = _with_scte35(section, pid)
        if new_section == section:
            continue
        if start + len(new_section) > TS_PACKET:
            return "skipped: the PMT would not fit in its packet"
        rewritten = (packet[:start] + new_section
                     + b"\xff" * (TS_PACKET - start - len(new_section)))
        data[i:i + TS_PACKET] = rewritten
        changed = True
    if not changed:
        return "present"
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(bytes(data))
    os.replace(tmp, path)
    return "added"


def parse_pid(value, default: int = DEFAULT_SCTE35_PID) -> int:
    """A PID from config: 500, "500" or "0x1F4". Must be 0x20-0x1FFE."""
    if value in (None, ""):
        return default
    pid = int(value, 0) if isinstance(value, str) else int(value)
    if not 0x20 <= pid <= 0x1FFE:
        raise ValueError(f"SCTE-35 PID {value!r} is outside 0x20-0x1FFE")
    return pid


def declare_in_segments(segments: Iterable[Path], pid: int = DEFAULT_SCTE35_PID,
                        ctx=None) -> dict:
    """Run :func:`add_scte35_pid` over segments; returns counts per outcome."""
    log = get_logger(ctx)
    counts = {"added": 0, "present": 0, "skipped": 0}
    first_skip = None
    for segment in segments:
        try:
            outcome = add_scte35_pid(segment, pid)
        except OSError as e:
            outcome = f"skipped: {e}"
        key = outcome.split(":")[0]
        counts[key] = counts.get(key, 0) + 1
        if key == "skipped" and first_skip is None:
            first_skip = f"{Path(segment).name}: {outcome}"
    if counts["skipped"]:
        log.warning(f"SCTE-35 PID {pid:#x} not declared in {counts['skipped']} segment(s), "
                    f"e.g. {first_skip}")
    return counts


__all__ = ["add_scte35_pid", "declares_scte35", "declare_in_segments", "parse_pmt",
           "parse_pid", "DEFAULT_SCTE35_PID"]
