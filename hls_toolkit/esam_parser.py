import xml.etree.ElementTree as ET
import logging
import html
import re
import math
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path

from hls_toolkit.time_utils import timecode_to_seconds
from hls_toolkit.playlist_utils import (is_master_playlist, find_variants,
                                        parse_variant_segments, bump_segment_lines,
                                        normalize_float, has_marker_near)

ESAM_NS = "{urn:cablelabs:iptvservices:esam:xsd:signal:1}"
SIG_NS = "{urn:cablelabs:md:xsd:signaling:3.0}"
COMMON_NS = "{urn:cablelabs:iptvservices:esam:xsd:common:1}"
FLOAT_TOLERANCE = 0.05


def parse_esam_xml_string(xml_string):
    try:
        root = ET.fromstring(xml_string.strip())
        logging.debug(f"Parsed XML Root: {root.tag}, Attributes: {root.attrib}")
    except ET.ParseError as e:
        logging.error(f"Error parsing ESAM XML string: {e}. Returning empty event list.")
        return []
    except Exception as e:
        logging.error(f"Unexpected error during ESAM XML parsing: {e}. Returning empty event list.")
        return []

    events = []
    for rs in root.findall(f".//{ESAM_NS}ResponseSignal"):
        point = rs.find(f"{SIG_NS}NPTPoint")
        seginfo = rs.find(f"{SIG_NS}SCTE35PointDescriptor/{SIG_NS}SegmentationDescriptorInfo")
        if point is None:
            logging.warning("ESAM ResponseSignal missing NPTPoint. Skipping event.")
            continue
        try:
            npt = float(point.get("nptPoint"))
        except (ValueError, TypeError):
            logging.warning(f'Invalid NPTPoint value: {point.get("nptPoint")}. Skipping event.')
            continue

        ev = {'npt': npt,
              'duration': None,
              'segmentTypeId': None,
              'segmentEventId': None,
              'acquisitionSignalID': rs.get("acquisitionSignalID")}
        if seginfo is not None:
            dur = seginfo.get("duration") or seginfo.get("Duration")
            if dur and dur.startswith("PT") and dur.endswith("S"):
                try:
                    ev["duration"] = float(dur[2:-1])
                except ValueError:
                    ev["duration"] = None
            ev["segmentTypeId"] = seginfo.get("segmentTypeId")
            ev["segmentEventId"] = seginfo.get("segmentEventId")
        events.append(ev)

    events.sort(key=lambda x: x["npt"])
    logging.info(f"Parsed {len(events)} ESAM event(s) from XML string")
    return events


def parse_mcc_xml_asset_tags(xml_string):
    ns_map = {'ns2': "http://www.cablelabs.com/namespaces/metadata/xsd/confirmation/2",
              'ns3': "http://www.cablelabs.com/namespaces/metadata/xsd/signaling/2"}
    if not xml_string:
        logging.warning("MCC XML string is empty. Returning empty asset tags map.")
        return {}

    asset_tags_map = {}
    try:
        root = ET.fromstring(xml_string)
        for manifest_response in root.findall(".//ns2:ManifestResponse", ns_map):
            acquisition_signal_id = manifest_response.get("acquisitionSignalID")
            if not acquisition_signal_id:
                logging.warning("MCC ManifestResponse missing acquisitionSignalID. Skipping.")
                continue
            asset_tag = None
            for tag_element in manifest_response.findall(".//ns2:Tag", ns_map):
                value = tag_element.get("value")
                if value:
                    decoded_value = html.unescape(value)
                    asset_match = re.search("(#EXT-X-ASSET:.*)", decoded_value)
                    if asset_match:
                        asset_tag = asset_match.group(1).replace("-->", "").strip()
                        break
            if asset_tag:
                asset_tags_map[acquisition_signal_id] = asset_tag
            else:
                logging.warning(f"MCC ManifestResponse for acquisitionSignalID "
                                f"'{acquisition_signal_id}' missing #EXT-X-ASSET tag. Skipping.")
    except ET.ParseError as e:
        logging.error(f"Error parsing MCC XML string: {e}. Returning empty asset tags map.")
        return {}
    except Exception as e:
        logging.error(f"Unexpected error during MCC XML parsing: {e}. Returning empty asset tags map.")
        return {}

    logging.info(f"Parsed {len(asset_tags_map)} asset tag(s) from MCC XML string")
    return asset_tags_map


def inject_elemental_markers(m3u8_path: Path,
                             esam_events: List[Dict[str, Any]],
                             asset_tags_map: Dict[str, str],
                             video_segments: Optional[List[Tuple[float, float, str, int]]] = None) -> int:
    lines, segments, total, _ = parse_variant_segments(m3u8_path)
    if not segments:
        logging.info(f"{m3u8_path.name}: No segments found, skipping")
        return 0

    plans = []
    reference_segments = video_segments if video_segments else segments
    for ev_original in esam_events:
        ev = ev_original.copy()
        npt = normalize_float(ev.get("npt", 0.0))
        matched = False
        npt_aligned = None
        for idx, (sstart, send, sfile, sline) in enumerate(reference_segments):
            sstart_n = normalize_float(sstart)
            send_n = normalize_float(send)
            if math.isclose(npt, sstart_n, abs_tol=FLOAT_TOLERANCE):
                if idx == 0:
                    plans.append({'seg_index': -1, 'event': ev, 'npt_aligned': sstart_n})
                else:
                    plans.append({'seg_index': idx - 1, 'event': ev, 'npt_aligned': sstart_n})
                matched = True
                break
            elif sstart_n < npt < send_n + FLOAT_TOLERANCE:
                npt_aligned = sstart_n
                plans.append({'seg_index': idx, 'event': ev, 'npt_aligned': npt_aligned})
                matched = True
                break
            elif npt < sstart_n and idx == 0 and math.isclose(npt, 0.0, abs_tol=FLOAT_TOLERANCE):
                npt_aligned = 0.0
                plans.append({'seg_index': -1, 'event': ev, 'npt_aligned': npt_aligned})
                matched = True
                break

        if not matched:
            last_start = normalize_float(reference_segments[-1][0])
            last_end = normalize_float(reference_segments[-1][1])
            if npt >= last_start and npt <= last_end + FLOAT_TOLERANCE:
                npt_aligned = last_start
                plans.append({'seg_index': len(reference_segments) - 1,
                              'event': ev,
                              'npt_aligned': npt_aligned})
                matched = True
            elif npt > last_end + FLOAT_TOLERANCE:
                npt_aligned = last_end
                plans.append({'seg_index': len(reference_segments) - 1,
                              'event': ev,
                              'npt_aligned': npt_aligned,
                              'insert_at_end': True})
                matched = True

        if not matched:
            logging.info(f"{m3u8_path.name}: Skipping signal {npt:.3f}s as it is "
                         "outside segment boundaries.")

    if not plans:
        return 0

    plans.sort(key=lambda p: (p["seg_index"], p["npt_aligned"], p["event"].get("npt", 0.0)),
               reverse=True)

    inserted_count = 0
    for plan in plans:
        idx = plan["seg_index"]
        ev = plan["event"]
        npt_to_log = plan.get("npt_aligned", ev.get("npt"))
        insert_at_end = plan.get("insert_at_end", False)

        if insert_at_end:
            try:
                endlist_index = len(lines) - 1 - next(
                    i for i, line in enumerate(reversed(lines))
                    if line.strip() == "#EXT-X-ENDLIST")
                insert_pos = endlist_index
            except StopIteration:
                insert_pos = len(lines)

            stype = ev.get("segmentTypeId")
            if str(stype) == "53":
                logging.info(f"{m3u8_path.name}: Signal {npt_to_log:.3f}s (Type 53) is beyond "
                             "total duration. Skipping explicit CUE-IN insertion.")
                continue

            cue_out_tag_duration = ev.get("duration") if ev.get("duration") is not None else 0.0
            ad_break_logic_duration = normalize_float(cue_out_tag_duration, 3)
            if ad_break_logic_duration < FLOAT_TOLERANCE:
                ad_break_logic_duration = 0.0
            duration_str = "0" if ad_break_logic_duration == 0.0 else f"{ad_break_logic_duration:.3f}"
            cueout_tag = f"#EXT-X-CUE-OUT:{duration_str}\n"
            lines_to_insert_at_end = [cueout_tag]
            acq_signal_id = ev.get("acquisitionSignalID")
            if acq_signal_id and acq_signal_id in asset_tags_map:
                asset_tag_line = f"{asset_tags_map[acq_signal_id]}\n"
                lines_to_insert_at_end.append(asset_tag_line)
            if ad_break_logic_duration == 0:
                lines_to_insert_at_end.append("#EXT-X-CUE-IN\n")
            for line_to_insert in reversed(lines_to_insert_at_end):
                lines.insert(insert_pos, line_to_insert)
            inserted_count += len(lines_to_insert_at_end)
            logging.info(f"{m3u8_path.name}: Signal {npt_to_log:.3f}s is beyond total duration. "
                         f"Inserting EXT-X-CUE-OUT at end of playlist (line {insert_pos}).")
            continue

        if idx == -1:
            if not segments:
                logging.warning(f"{m3u8_path.name}: No segments found, cannot insert at start.")
                continue
            sstart = 0.0
            sfile = "Start of Playlist"
            insert_pos = max(0, segments[0][3] - 1)
        else:
            sstart, send, sfile, sline = segments[idx]
            insert_pos = sline + 1

        marker_exists = has_marker_near(
            lines, insert_pos,
            ["#EXT-X-CUE-OUT", "#EXT-X-CUE-OUT-CONT", "#EXT-X-CUE-IN"], window=1)
        if marker_exists:
            if math.isclose(npt_to_log, normalize_float(sstart), abs_tol=FLOAT_TOLERANCE):
                logging.info(f"{m3u8_path.name}: Marker for {npt_to_log:.3f}s exists near "
                             f"{sfile}, skipping insertion.")
                continue
            else:
                logging.info(f"{m3u8_path.name}: Found existing marker near {sfile} but not "
                             f"aligned with NPT {npt_to_log:.3f}s. Inserting new marker.")

        stype = ev.get("segmentTypeId")
        cue_out_tag_duration = ev.get("duration") if ev.get("duration") is not None else 0.0
        ad_break_logic_duration = normalize_float(cue_out_tag_duration, 3)
        if ad_break_logic_duration < FLOAT_TOLERANCE:
            ad_break_logic_duration = 0.0

        if str(stype) == "53":
            if not (marker_exists and math.isclose(npt_to_log, normalize_float(sstart),
                                                   abs_tol=FLOAT_TOLERANCE)):
                lines.insert(insert_pos, "#EXT-X-CUE-IN\n")
                inserted_count += 1
                bump_segment_lines(segments, insert_pos)
                logging.info(f"{m3u8_path.name}: Inserting EXT-X-CUE-IN after {sfile} "
                             f"(signal {npt_to_log:.3f}s)")
            else:
                logging.info(f"{m3u8_path.name}: Skipping insertion of EXT-X-CUE-IN "
                             f"(signal {npt_to_log:.3f}s) due to nearby existing and aligned CUE-IN.")
            continue

        lines_to_insert = []
        asset_inserted = False
        duration_str = ("0" if cue_out_tag_duration == 0.0
                        else f"{normalize_float(cue_out_tag_duration, 3):.3f}")
        cueout_tag = f"#EXT-X-CUE-OUT:{duration_str}\n"
        lines_to_insert.append(cueout_tag)
        acq_signal_id = ev.get("acquisitionSignalID")
        if acq_signal_id and acq_signal_id in asset_tags_map:
            asset_tag_line = f"{asset_tags_map[acq_signal_id]}\n"
            lines_to_insert.append(asset_tag_line)
            asset_inserted = True
        if ad_break_logic_duration == 0:
            lines_to_insert.append("#EXT-X-CUE-IN\n")
            logging.info(f"{m3u8_path.name}: Inserting immediate EXT-X-CUE-IN for "
                         f"0-duration break after {sfile}.")

        current_insert_pos = insert_pos
        for i, line_to_insert in enumerate(lines_to_insert):
            lines.insert(current_insert_pos + i, line_to_insert)
        inserted_count += len(lines_to_insert)
        bump_segment_lines(segments, insert_pos, delta=len(lines_to_insert))
        logging.info(f"{m3u8_path.name}: Inserting EXT-X-CUE-OUT "
                     f"(duration={cue_out_tag_duration}) after {sfile} "
                     f"(signal {npt_to_log:.3f}s)")
        if asset_inserted:
            logging.info(f"{m3u8_path.name}: Inserting Asset Tag for signal "
                         f"{acq_signal_id} after {sfile}")
        if ad_break_logic_duration == 0:
            logging.info(f"{m3u8_path.name}: Inserting immediate EXT-X-CUE-IN for 0-duration break.")
            continue

        anchor_start = normalize_float(sstart, 3)
        j = idx + 1
        while j < len(segments):
            seg_start, seg_end, seg_file, seg_line = segments[j]
            seg_start_n = normalize_float(seg_start, 3)
            elapsed = normalize_float(seg_start_n - anchor_start, 3)
            insert_pos_cont = seg_line + 1
            if elapsed >= ad_break_logic_duration - FLOAT_TOLERANCE:
                if not has_marker_near(lines, insert_pos_cont, ["#EXT-X-CUE-IN"], window=1) \
                        or not math.isclose(elapsed, ad_break_logic_duration,
                                            abs_tol=FLOAT_TOLERANCE):
                    lines.insert(insert_pos_cont, "#EXT-X-CUE-IN\n")
                    inserted_count += 1
                    bump_segment_lines(segments, insert_pos_cont)
                logging.info(f"{m3u8_path.name}: Inserting EXT-X-CUE-IN after {seg_file} "
                             f"(elapsed {elapsed:.3f}s)")
                break
            full_dur_str = f"{ad_break_logic_duration:.3f}"
            cont_tag = f"#EXT-X-CUE-OUT-CONT:{elapsed:.3f}/{full_dur_str}\n"
            if not has_marker_near(lines, insert_pos_cont, ["#EXT-X-CUE-OUT-CONT"], window=1) \
                    or not math.isclose(elapsed, ad_break_logic_duration, abs_tol=FLOAT_TOLERANCE):
                lines.insert(insert_pos_cont, cont_tag)
                inserted_count += 1
                bump_segment_lines(segments, insert_pos_cont)
            logging.info(f"{m3u8_path.name}: Inserting EXT-X-CUE-OUT-CONT after {seg_file} "
                         f"(elapsed {elapsed:.3f}s)")
            j += 1
        else:
            last_line = segments[-1][3]
            insert_at = last_line + 1
            if not has_marker_near(lines, insert_at, ["#EXT-X-CUE-IN"], window=1):
                lines.insert(insert_at, "#EXT-X-CUE-IN\n")
                inserted_count += 1
                bump_segment_lines(segments, insert_at)
            logging.info(f"{m3u8_path.name}: Inserting EXT-X-CUE-IN at the end of the "
                         "playlist (ad ran to end)")

    m3u8_path.write_text("".join(lines), encoding="utf-8")
    return inserted_count


def process_playlist(m3u8_arg: str,
                     esam_events: List[Dict[str, Any]],
                     asset_tags_map: Dict[str, str],
                     video_segments: Optional[List[Tuple[float, float, str, int]]] = None):
    m3u8_path = Path(m3u8_arg).resolve()
    if not m3u8_path.exists():
        logging.error(f"Playlist not found: {m3u8_path}")
        return

    content = m3u8_path.read_text(encoding="utf-8").splitlines(keepends=True)
    if is_master_playlist(content):
        variants = find_variants(content, m3u8_path.parent)
        logging.info(f"Master playlist detected, variant count: {len(variants)}")
        total_injected = 0
        for uri, abs_path in variants:
            if not abs_path.exists():
                logging.warning(f"Variant file not found, skipping: {uri} -> {abs_path}")
                continue
            logging.info(f"Processing variant: {uri}")
            cnt = inject_elemental_markers(abs_path, esam_events, asset_tags_map,
                                           video_segments=video_segments)
            logging.info(f"  Injected {cnt} markers into {uri}")
            total_injected += cnt
        logging.info(f"Total injected markers across all variants: {total_injected}")
    else:
        cnt = inject_elemental_markers(m3u8_path, esam_events, asset_tags_map,
                                       video_segments=video_segments)
        logging.info(f"Injected {cnt} markers into {m3u8_path.name}")


def remap_esam_events_for_merged_clips(original_events: List[Dict[str, Any]],
                                       clippings: List[Dict[str, Any]],
                                       video_fps: float) -> List[Dict[str, Any]]:
    if not original_events:
        return []
    if not clippings:
        logging.warning("No clippings provided for ESAM remapping. "
                        "Returning original events without remapping.")
        return original_events

    remapped_events_final = []
    processed_clippings = []
    for clip in clippings:
        start_timecode = clip.get("StartTimecode")
        end_timecode = clip.get("EndTimecode")
        if not start_timecode or not end_timecode:
            logging.warning(f"Clipping missing StartTimecode or EndTimecode. Skipping: {clip}")
            continue
        try:
            clip_start_orig = timecode_to_seconds(start_timecode, str(video_fps))
            clip_end_orig = timecode_to_seconds(end_timecode, str(video_fps))
            if clip_end_orig <= clip_start_orig:
                logging.warning(f"Clipping has non-positive duration. Skipping: {clip}")
                continue
            processed_clippings.append({**{'start_orig': clip_start_orig,
                                           'end_orig': clip_end_orig,
                                           'duration': clip_end_orig - clip_start_orig},
                                        **clip})
        except ValueError as e:
            logging.error(f"Error parsing timecode in clipping {clip}: {e}. Skipping.")
            continue

    processed_clippings.sort(key=lambda x: x["start_orig"])
    if not processed_clippings:
        logging.warning("No valid clippings after processing. Cannot remap ESAM events.")
        return []

    for original_event in original_events:
        original_npt = original_event["npt"]
        current_offset = 0.0
        event_remapped = False
        for i, clip in enumerate(processed_clippings):
            clip_start_orig = clip["start_orig"]
            clip_end_orig = clip["end_orig"]
            clip_duration = clip["duration"]
            if original_npt < clip_start_orig - FLOAT_TOLERANCE:
                if i == 0:
                    remapped_npt = 0.0
                else:
                    remapped_npt = current_offset
                remapped_event = original_event.copy()
                remapped_event["npt"] = round(remapped_npt, 3)
                remapped_events_final.append(remapped_event)
                event_remapped = True
                break
            if original_npt >= clip_start_orig - FLOAT_TOLERANCE \
                    and original_npt < clip_end_orig + FLOAT_TOLERANCE:
                remapped_npt = original_npt - clip_start_orig + current_offset
                logging.debug(f"Remapping event {float(original_npt):.6f}: Within clip {i} "
                              f"({float(clip_start_orig):.6f}-{float(clip_end_orig):.6f}). "
                              f"Offset {float(current_offset):.6f}. New: {float(remapped_npt):.6f}")
                remapped_event = original_event.copy()
                remapped_event["npt"] = round(remapped_npt, 3)
                remapped_events_final.append(remapped_event)
                event_remapped = True
                break
            logging.debug(f"Remapping event {float(original_npt):.6f}: After clip {i} "
                          f"({float(clip_start_orig):.6f}-{float(clip_end_orig):.6f}).")
            current_offset += clip_duration
        else:
            remapped_npt = current_offset
            remapped_event = original_event.copy()
            remapped_event["npt"] = round(remapped_npt, 3)
            remapped_events_final.append(remapped_event)
            event_remapped = True

        if not event_remapped:
            logging.debug(f"ESAM event (Original NPT: {original_npt:.2f}s) was not remapped")

    remapped_events_final.sort(key=lambda x: x["npt"])
    logging.info(f"Remapped {len(remapped_events_final)} ESAM events for compacted timeline. "
                 f"Total original events: {len(original_events)}.")
    return remapped_events_final
