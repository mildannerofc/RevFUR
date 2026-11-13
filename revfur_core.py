#!/usr/bin/env python3
"""RevFUR sequence disassembler/editor for Furnace Tracker modules.

The module data model and parser are shared with fur2mid 0.1.1. Sequence edits
are applied by appending replacement pattern blocks and patching only the
relevant pattern pointers and order bytes. Instrument/sample/wavetable blocks
are never rewritten.
"""
import hashlib
import json
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import furparser as fur2mid

LOG = Callable[[str], None]
FORMAT_TAG = "revfur-sequence"
FORMAT_VERSION = 1


def _log(log: Optional[LOG], message: str) -> None:
    if log:
        log(message)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_cstring(r: fur2mid.Reader) -> str:
    return r.cstring()


def _block_end(data: bytes, ptr: int) -> int:
    if ptr < 0 or ptr + 8 > len(data):
        raise fur2mid.FurFormatError("Invalid asset block pointer 0x%X" % ptr)
    size = struct.unpack_from("<I", data, ptr + 4)[0]
    end = ptr + 8 + size
    if end > len(data):
        raise fur2mid.FurFormatError("Asset block at 0x%X extends past end of module" % ptr)
    return end


@dataclass
class FurLayout:
    raw_file: bytes
    data: bytes
    compressed: bool
    version: int
    info_pos: int
    song: fur2mid.FurSong
    orders_offset: int
    pattern_pointer_table_offset: int
    pattern_ptrs: List[int]
    pattern_pointer_indices: Dict[Tuple[int, int, int], int]
    pattern_blocks: Dict[Tuple[int, int, int], Tuple[int, bytes]]
    instrument_ptrs: List[int]
    wavetable_ptrs: List[int]
    sample_ptrs: List[int]
    jump_treatment: int = 0
    ignore_jump_at_end: bool = False


def _parse_old_layout(data: bytes, info_pos: int, version: int, song: fur2mid.FurSong) -> Tuple[int, int, List[int], List[int], List[int], List[int]]:
    r = fur2mid.Reader(data, info_pos)
    if r.read(4) != b"INFO":
        raise fur2mid.FurFormatError("Invalid INFO header")
    r.u32()
    time_base = r.u8()
    r.read(3)  # speed1, speed2, arpeggio speed
    r.read(4)  # tick rate
    pat_len = r.u16()
    orders_len = r.u16()
    r.read(2)  # highlight A/B
    ins_count = r.u16()
    wave_count = r.u16()
    sample_count = r.u16()
    pattern_count = r.u32()
    if not (1 <= pat_len <= 256 and 1 <= orders_len <= 256):
        raise fur2mid.FurFormatError("Invalid legacy INFO dimensions")
    r.read(32 + 32 + 32)  # systems, legacy volume, legacy panning
    r.read(32 * 4)  # system flags pointers
    r.cstring()
    r.cstring()
    if version >= 33:
        r.read(4)  # tuning
    else:
        r.read(4)
    r.read(20)  # compatibility flags

    instrument_ptrs = [r.u32() for _ in range(ins_count)]
    wavetable_ptrs = [r.u32() for _ in range(wave_count)]
    sample_ptrs = [r.u32() for _ in range(sample_count)]
    pattern_pointer_table_offset = r.pos
    pattern_ptrs = [r.u32() for _ in range(pattern_count)]
    orders_offset = r.pos

    # The rest of the INFO block is not needed here; fur2mid.load_fur has
    # already validated and decoded it. Keeping this parser focused makes the
    # patch locations explicit and auditable.
    return (orders_offset, pattern_pointer_table_offset, pattern_ptrs,
            instrument_ptrs, wavetable_ptrs, sample_ptrs)


def _parse_new_layout(data: bytes, info_pos: int, version: int, song: fur2mid.FurSong) -> Tuple[int, int, List[int], List[int], List[int], List[int]]:
    r = fur2mid.Reader(data, info_pos)
    if r.read(4) != b"INF2":
        raise fur2mid.FurFormatError("Invalid INF2 header")
    r.u32()
    for _ in range(8):
        r.cstring()
    r.read(5)  # tuning + automatic-system flag
    r.read(4)  # master volume
    channel_count = r.u16()
    chip_count = r.u16()
    r.read(chip_count * 16)  # chip id/count + volume/pan/front balance
    connection_count = r.u32()
    if connection_count > 100000:
        raise fur2mid.FurFormatError("Implausible patchbay connection count")
    r.read(connection_count * 4)
    r.u8()  # automatic patchbay

    element_ptrs: Dict[int, List[int]] = {}
    element_pointer_offsets: Dict[int, int] = {}
    for _ in range(32):
        element_type = r.u8()
        if element_type == 0:
            break
        count = r.u32()
        element_pointer_offsets[element_type] = r.pos
        if count > 100000:
            raise fur2mid.FurFormatError("Implausible element pointer count")
        element_ptrs[element_type] = [r.u32() for _ in range(count)]

    sub_ptrs = element_ptrs.get(1, [])
    pattern_ptrs = element_ptrs.get(7, [])
    if not sub_ptrs:
        raise fur2mid.FurFormatError("INF2 has no subsong pointer")
    sr = fur2mid.Reader(data, sub_ptrs[0])
    if sr.read(4) != b"SNG2":
        raise fur2mid.FurFormatError("Invalid SNG2 block")
    sr.u32()
    sr.read(4)  # tick rate
    sr.read(2)  # arpeggio speed + effect divider
    pattern_length = sr.u16()
    orders_length = sr.u16()
    sr.read(2)  # highlight A/B
    sr.read(4)  # virtual tempo numerator/denominator
    speed_len = sr.u8()
    if speed_len < 1 or speed_len > 16:
        raise fur2mid.FurFormatError("Invalid SNG2 speed pattern length")
    sr.read(32)  # 16 signed shorts
    sr.cstring()
    sr.cstring()
    orders_offset = sr.pos
    if channel_count != song.channel_count or orders_length != song.subsong.orders_length:
        raise fur2mid.FurFormatError("SNG2 dimensions disagree with the parsed song")

    ins_ptrs = element_ptrs.get(4, [])
    wave_ptrs = element_ptrs.get(5, [])
    sample_ptrs = element_ptrs.get(6, [])
    pattern_table = element_pointer_offsets.get(7)
    if pattern_table is None:
        raise fur2mid.FurFormatError("INF2 has no pattern pointer table")
    return orders_offset, pattern_table, pattern_ptrs, ins_ptrs, wave_ptrs, sample_ptrs


def _extract_flow_compat(data: bytes, info_pos: int, version: int,
                         channel_count: int) -> Tuple[int, bool]:
    """Read only the two compatibility flags that affect order-flow decoding.

    The order jump rules are part of Furnace compatibility state.  Older INFO
    files store them in the extended flag byte array; INF2 files may carry a
    CFLG key/value block.  Defaults match Furnace's DivCompatFlags defaults.
    """
    jump_treatment = 0
    ignore_jump_at_end = version < 71
    if version < 240:
        r = fur2mid.Reader(data, info_pos)
        if r.read(4) != b"INFO":
            raise fur2mid.FurFormatError("Invalid INFO block while reading playback flags")
        r.u32()
        r.read(1 + 3 + 4 + 2 + 2 + 2 + 2 + 2 + 2 + 4)
        r.read(32 + 32 + 32 + 32 * 4)
        r.cstring(); r.cstring()
        r.read(4)  # tuning
        r.read(20) # original compatibility flags
        # Instrument, wavetable, sample and pattern pointer arrays.
        ins_count = struct.unpack_from("<H", data, info_pos + 8 + 1 + 3 + 4 + 2 + 2 + 2)[0]
        wave_count = struct.unpack_from("<H", data, info_pos + 8 + 1 + 3 + 4 + 2 + 2 + 2 + 2)[0]
        sample_count = struct.unpack_from("<H", data, info_pos + 8 + 1 + 3 + 4 + 2 + 2 + 2 + 2 + 2)[0]
        pattern_count = struct.unpack_from("<I", data, info_pos + 8 + 1 + 3 + 4 + 2 + 2 + 2 + 2 + 2 + 2)[0]
        r.read((ins_count + wave_count + sample_count + pattern_count) * 4)
        # The order/effect tables follow the pattern pointer table.
        orders_len = struct.unpack_from("<H", data, info_pos + 8 + 1 + 3 + 4 + 2)[0]
        r.read(channel_count * orders_len)
        r.read(channel_count)  # effect columns
        if version >= 39:
            r.read(channel_count * 2)  # visible/collapsed
            for _ in range(channel_count * 2 + 1):
                r.cstring()  # channel names, short names and song comments
        if version >= 59:
            r.read(4)  # master volume
        if version >= 70:
            flags = r.read(28)
            if version >= 71:
                ignore_jump_at_end = bool(flags[3])
            if version >= 113:
                jump_treatment = min(2, int(flags[23]))
        return jump_treatment, ignore_jump_at_end

    # INF2: find the optional unique CFLG element and read its textual config.
    r = fur2mid.Reader(data, info_pos)
    if r.read(4) != b"INF2":
        raise fur2mid.FurFormatError("Invalid INF2 block while reading playback flags")
    r.u32()
    for _ in range(8):
        r.cstring()
    r.read(5 + 4)  # tuning/auto-system and master volume
    _channels = r.u16()
    chips = r.u16()
    r.read(chips * 16)
    connections = r.u32()
    r.read(connections * 4)
    r.u8()
    flags_ptr = 0
    for _ in range(32):
        kind = r.u8()
        if kind == 0:
            break
        count = r.u32()
        ptrs = [r.u32() for _ in range(count)]
        if kind == 8 and ptrs:
            flags_ptr = ptrs[0]
    if flags_ptr:
        fr = fur2mid.Reader(data, flags_ptr)
        if fr.read(4) == b"CFLG":
            fr.u32()
            config = fr.cstring()
            for line in config.replace("\r", "\n").splitlines():
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip().lower()
                value = value.strip().lower()
                if key == "jumptreatment":
                    try:
                        jump_treatment = max(0, min(2, int(value, 0)))
                    except ValueError:
                        pass
                elif key == "ignorejump atend" or key == "ignorejumpatend":
                    ignore_jump_at_end = value in ("1", "true", "yes", "on")
    return jump_treatment, ignore_jump_at_end


def inspect_fur(path: Path, log: Optional[LOG] = None) -> FurLayout:
    path = Path(path)
    raw = path.read_bytes()
    compressed = not raw.startswith(fur2mid.MAGIC)
    data = fur2mid._decompress_fur(raw)
    if len(data) < 32 or data[:16] != fur2mid.MAGIC:
        raise fur2mid.FurFormatError("Invalid Furnace module header")
    hr = fur2mid.Reader(data, 16)
    version = hr.u16()
    hr.u16()
    info_pos = hr.u32()
    if not 0 < info_pos < len(data):
        raise fur2mid.FurFormatError("Invalid INFO/INF2 pointer")

    song = fur2mid.load_fur(path)
    if version >= 240:
        offsets = _parse_new_layout(data, info_pos, version, song)
    else:
        offsets = _parse_old_layout(data, info_pos, version, song)
    (orders_offset, ptr_table_offset, pattern_ptrs,
     instrument_ptrs, wavetable_ptrs, sample_ptrs) = offsets

    if orders_offset + song.channel_count * song.subsong.orders_length > len(data):
        raise fur2mid.FurFormatError("Order table extends past end of module")
    if ptr_table_offset + len(pattern_ptrs) * 4 > len(data):
        raise fur2mid.FurFormatError("Pattern pointer table extends past end of module")

    pattern_pointer_indices: Dict[Tuple[int, int, int], int] = {}
    pattern_blocks: Dict[Tuple[int, int, int], Tuple[int, bytes]] = {}
    for pointer_index, ptr in enumerate(pattern_ptrs):
        pat = fur2mid._parse_pattern(
            data, ptr, version,
            effect_columns=song.subsong.effect_columns or 1,
            pattern_length=song.subsong.pattern_length,
        )
        key = (pat.subsong, pat.channel, pat.index)
        if key in pattern_pointer_indices:
            raise fur2mid.FurFormatError("Duplicate pattern pointer for %s" % (key,))
        end = _block_end(data, ptr)
        pattern_pointer_indices[key] = pointer_index
        pattern_blocks[key] = (ptr, data[ptr:end])

    if not pattern_blocks:
        raise fur2mid.FurFormatError("No pattern blocks were found")

    _log(log, "Read Furnace v%d module: %s" % (version, song.name or path.name))
    _log(log, "Systems: %s" % (", ".join(s[1] for s in song.systems)))
    _log(log, "Channels: %d | Orders: %d | Pattern rows: %d" % (
        song.channel_count, song.subsong.orders_length, song.subsong.pattern_length))
    _log(log, "Pattern blocks: %d | Instruments: %d | Wavetables: %d | Samples: %d" % (
        len(pattern_blocks), len(instrument_ptrs), len(wavetable_ptrs), len(sample_ptrs)))
    jump_treatment, ignore_jump_at_end = _extract_flow_compat(
        data, info_pos, version, song.channel_count
    )
    _log(log, "Playback compatibility: jump treatment=%d | ignore 0D on last order=%s" % (
        jump_treatment, "yes" if ignore_jump_at_end else "no"))
    return FurLayout(
        raw_file=raw, data=data, compressed=compressed, version=version,
        info_pos=info_pos, song=song, orders_offset=orders_offset,
        pattern_pointer_table_offset=ptr_table_offset,
        pattern_ptrs=pattern_ptrs,
        pattern_pointer_indices=pattern_pointer_indices,
        pattern_blocks=pattern_blocks,
        instrument_ptrs=instrument_ptrs, wavetable_ptrs=wavetable_ptrs,
        sample_ptrs=sample_ptrs, jump_treatment=jump_treatment,
        ignore_jump_at_end=ignore_jump_at_end,
    )


def _row_to_json(cell: fur2mid.Cell) -> Dict[str, Any]:
    row: Dict[str, Any] = {}
    if cell.note is not None:
        row["note"] = int(cell.note)
    if cell.instrument is not None:
        row["instrument"] = int(cell.instrument)
    if cell.volume is not None:
        row["volume"] = int(cell.volume)
    if cell.raw_frequency is not None:
        row["raw_frequency"] = int(cell.raw_frequency)
    if cell.effects:
        row["effects"] = [
            {"command": ("%04X" if int(code) > 0xFF else "%02X") % int(code),
             "value": ("%04X" if int(value) > 0xFF else "%02X") % int(value)}
            for code, value in cell.effects
        ]
    return row


def _parse_pattern_for_export(data: bytes, ptr: int, version: int,
                              effect_columns: List[int], pattern_length: int) -> fur2mid.Pattern:
    # PATN is decoded by the shared Furnace parser. PATR needs an explicit
    # note-off mapping because the legacy note codes 100/101/102 are special.
    r = fur2mid.Reader(data, ptr)
    magic = r.read(4)
    if magic != b"PATR":
        return fur2mid._parse_pattern(data, ptr, version,
                                      effect_columns=effect_columns or 1,
                                      pattern_length=pattern_length)
    r.u32()
    channel = r.u16()
    index = r.u16()
    subsong = r.u16() if version >= 95 else 0
    r.u16()
    pat = fur2mid.Pattern(subsong, channel, index, "")
    cols = effect_columns[channel] if channel < len(effect_columns) else 1
    for row_index in range(pattern_length):
        note_code = r.i16()
        octave = r.i16()
        ins = r.i16()
        vol = r.i16()
        effects = []
        for _ in range(max(1, min(8, cols))):
            fx = r.i16()
            value = r.i16()
            if fx >= 0:
                effects.append((fx & 0xFFFF, value & 0xFFFF if value >= 0 else 0))
        cell = fur2mid.Cell()
        if note_code in (100, 101, 102):
            cell.note = {100: 180, 101: 181, 102: 182}[note_code]
        elif not (note_code == 0 and octave == 0):
            if note_code == 0 and octave != 0:
                note_code = 12
                octave -= 1
            cell.note = ((octave + 5) * 12 + (note_code % 12))
        if ins >= 0:
            cell.instrument = ins & 0xFF
        if vol >= 0:
            cell.volume = vol & 0xFF
        cell.effects = effects
        if cell.note is not None or cell.instrument is not None or cell.volume is not None or effects:
            pat.rows[row_index] = cell
    if version >= 51 and r.pos < len(data):
        pat.name = r.cstring()
    return pat


def _pattern_as_document(layout: FurLayout, key: Tuple[int, int, int], ptr: int,
                         block: bytes) -> Dict[str, Any]:
    _subsong, channel, index = key
    pattern = _parse_pattern_for_export(
        layout.data, ptr, layout.version,
        layout.song.subsong.effect_columns,
        layout.song.subsong.pattern_length,
    )
    rows = []
    for row_index in sorted(pattern.rows):
        item = {"row": row_index}
        item.update(_row_to_json(pattern.rows[row_index]))
        rows.append(item)
    return {
        "subsong": key[0], "channel": channel, "index": index,
        "name": pattern.name,
        "source_block_sha256": _sha(block),
        "rows": rows,
    }


def _orders_from_bytes(data: bytes, offset: int, channels: int, orders: int) -> List[List[int]]:
    out = []
    pos = offset
    for _ in range(channels):
        out.append(list(data[pos:pos + orders]))
        pos += orders
    return out


def export_sequence(input_path: Path, output_path: Optional[Path] = None,
                    log: Optional[LOG] = None) -> Tuple[Path, Path]:
    input_path = Path(input_path)
    layout = inspect_fur(input_path, log)
    song = layout.song
    order_count = song.subsong.orders_length
    channel_count = song.channel_count
    orders = _orders_from_bytes(layout.data, layout.orders_offset, channel_count, order_count)

    patterns = []
    for key in sorted(layout.pattern_blocks):
        if key[0] != 0:
            continue
        ptr, block = layout.pattern_blocks[key]
        patterns.append(_pattern_as_document(layout, key, ptr, block))

    document = {
        "format": FORMAT_TAG,
        "format_version": FORMAT_VERSION,
        "description": "Editable Furnace sequence data. Instrument/sample/wavetable data is not included.",
        "source": {
            "filename": input_path.name,
            "sha256": _sha(layout.raw_file),
            "fur_version": layout.version,
            "song": song.name,
            "author": song.author,
            "system": song.system_name or ", ".join(item[1] for item in song.systems),
            "compressed": layout.compressed,
        },
        "sequence": {
            "subsong": 0,
            "pattern_length": song.subsong.pattern_length,
            "orders_length": order_count,
            "channel_count": channel_count,
            "channel_names": [x["name"] for x in song.channel_meta()],
            "orders": orders,
            "effect_columns": song.subsong.effect_columns,
            "patterns": patterns,
        },
        "assets": {
            "instruments": len(layout.instrument_ptrs),
            "samples": len(layout.sample_ptrs),
            "wavetables": len(layout.wavetable_ptrs),
            "policy": "Do not edit or replace asset blocks; RevFUR only patches order and pattern sequence data.",
        },
    }
    if output_path is None:
        output_path = input_path.with_suffix(".furseq.json")
    output_path = Path(output_path)
    if output_path.resolve() == input_path.resolve():
        raise fur2mid.FurFormatError("Output sequence path must not overwrite the source .fur module")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    text_path = output_path.with_suffix(".txt")
    text_path.write_text(render_sequence_text(document), encoding="utf-8")
    _log(log, "Exported sequence JSON: %s" % output_path)
    _log(log, "Exported readable dump: %s" % text_path)
    _log(log, "No instrument, sample, or wavetable data was written to the sequence file.")
    return output_path, text_path


def _format_note(note: int) -> str:
    if note == 180:
        return "OFF"
    if note == 181:
        return "REL"
    if note == 182:
        return "MACRO-REL"
    if note < 0 or note > 179:
        return "RAW(%d)" % note
    names = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
    octave = note // 12 - 5
    return "%s%d" % (names[note % 12], octave)


def render_sequence_text(document: Dict[str, Any]) -> str:
    src = document["source"]
    seq = document["sequence"]
    lines = [
        "RevFUR - Furnace Sequence Disassembly",
        "=" * 48,
        "Song: %s" % (src.get("song") or src.get("filename", "Unknown")),
        "Author: %s" % (src.get("author") or "Unknown"),
        "Systems: %s" % (src.get("system") or "Unknown"),
        "Furnace format: %s" % src.get("fur_version"),
        "Channels: %s | Orders: %s | Pattern rows: %s" % (
            seq["channel_count"], seq["orders_length"], seq["pattern_length"]),
        "",
        "ORDER TABLE (channel-major)",
        "-" * 48,
    ]
    for ch, order_row in enumerate(seq["orders"]):
        name = seq.get("channel_names", [])[ch] if ch < len(seq.get("channel_names", [])) else "Channel %d" % (ch + 1)
        lines.append("CH%02d %-24s : %s" % (ch + 1, name, " ".join("%02X" % p for p in order_row)))
    lines.extend(["", "PATTERNS", "-" * 48])
    for pat in seq["patterns"]:
        ch = int(pat["channel"])
        chname = seq.get("channel_names", [])[ch] if ch < len(seq.get("channel_names", [])) else "Channel %d" % (ch + 1)
        lines.append("\n[Subsong %d | %s | Pattern %02X] %s" % (
            pat.get("subsong", 0), chname, pat["index"], pat.get("name", "")))
        if not pat["rows"]:
            lines.append("  <empty>")
            continue
        for row in pat["rows"]:
            columns = ["%03d" % row["row"]]
            columns.append(_format_note(int(row["note"])) if "note" in row else "---")
            columns.append("I%02X" % row["instrument"] if "instrument" in row else "--")
            columns.append("V%02X" % row["volume"] if "volume" in row else "--")
            effects = row.get("effects", [])
            fx_text = " ".join("%s%s" % (x["command"].upper(), x["value"].upper()) for x in effects) or "----"
            columns.append(fx_text)
            lines.append("  " + "  ".join(columns))
    lines.extend(["", "NOTE: Instrument, sample and wavetable asset blocks are intentionally excluded.",
                  "Effects remain in Furnace command/value form; sequence jumps such as 0Bxx are preserved.", ""])
    return "\n".join(lines)


def _parse_hex_value(value: Any, field: str, maximum: int) -> int:
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        try:
            result = int(value.strip().replace("$", "").replace("0x", ""), 16)
        except ValueError as exc:
            raise fur2mid.FurFormatError("Invalid hexadecimal %s: %r" % (field, value)) from exc
    else:
        raise fur2mid.FurFormatError("Invalid %s value" % field)
    if not 0 <= result <= maximum:
        raise fur2mid.FurFormatError("%s is outside the supported range" % field)
    return result


def _normalise_rows(pattern_doc: Dict[str, Any], pattern_length: int, version: int) -> Dict[int, fur2mid.Cell]:
    if not isinstance(pattern_doc.get("rows"), list):
        raise fur2mid.FurFormatError("Pattern rows must be a list")
    rows: Dict[int, fur2mid.Cell] = {}
    seen = set()
    for item in pattern_doc["rows"]:
        if not isinstance(item, dict):
            raise fur2mid.FurFormatError("Every row must be an object")
        row_index = int(item.get("row", -1))
        if not 0 <= row_index < pattern_length or row_index in seen:
            raise fur2mid.FurFormatError("Invalid or duplicate row index: %s" % row_index)
        seen.add(row_index)
        cell = fur2mid.Cell()
        if "note" in item:
            cell.note = int(item["note"])
            if not (0 <= cell.note <= 183):
                raise fur2mid.FurFormatError("Furnace note code must be 0..183")
        if "raw_frequency" in item:
            cell.raw_frequency = int(item["raw_frequency"])
            if not 0 <= cell.raw_frequency <= 0xFFFFFFFF:
                raise fur2mid.FurFormatError("Raw frequency must be a 32-bit unsigned value")
            if cell.note != 183:
                cell.note = 183
        if "instrument" in item:
            cell.instrument = int(item["instrument"])
            if not 0 <= cell.instrument <= 255:
                raise fur2mid.FurFormatError("Instrument index must be 0..255")
        if "volume" in item:
            cell.volume = int(item["volume"])
            if not 0 <= cell.volume <= 255:
                raise fur2mid.FurFormatError("Volume must be 0..255")
        effects = item.get("effects", [])
        if not isinstance(effects, list) or len(effects) > 8:
            raise fur2mid.FurFormatError("Each pattern row may contain at most 8 effects")
        effect_max = 0xFF if version >= 157 else 0x7FFF
        for effect in effects:
            code = _parse_hex_value(effect.get("command"), "effect command", effect_max)
            value = _parse_hex_value(effect.get("value", 0), "effect value", effect_max)
            cell.effects.append((code, value))
        if cell.note is not None or cell.instrument is not None or cell.volume is not None or cell.raw_frequency is not None or cell.effects:
            rows[row_index] = cell
    return rows


def _encode_patn(subsong: int, channel: int, index: int, name: str,
                 rows: Dict[int, fur2mid.Cell], pattern_length: int, version: int) -> bytes:
    if version < 157:
        raise fur2mid.FurFormatError("PATN blocks are not valid before Furnace format v157")
    name_bytes = name.encode("utf-8") + b"\0"
    payload = bytearray()
    current = 0
    for row_index in sorted(rows):
        gap = row_index - current
        while gap > 0:
            if gap == 1:
                payload.append(0)
                gap -= 1
                current += 1
            else:
                step = min(gap, 129)
                payload.append(0x80 | (step - 2))
                gap -= step
                current += step
        cell = rows[row_index]
        effects = cell.effects[:8]
        flags = 0
        if cell.note is not None: flags |= 0x01
        if cell.instrument is not None: flags |= 0x02
        if cell.volume is not None: flags |= 0x04
        effect_mask = 0
        for fx_index, (code, value) in enumerate(effects):
            effect_mask |= 1 << (fx_index * 2)
            effect_mask |= 1 << (fx_index * 2 + 1)
        if effect_mask & 0x0001: flags |= 0x08
        if effect_mask & 0x0002: flags |= 0x10
        if effect_mask & 0x00FC: flags |= 0x20
        if effect_mask & 0xFF00: flags |= 0x40
        payload.append(flags)
        if flags & 0x20:
            payload.append((effect_mask >> 0) & 0xFF)
        if flags & 0x40:
            payload.append((effect_mask >> 8) & 0xFF)
        if cell.note is not None:
            payload.append(cell.note)
            if cell.note == 183:
                if cell.raw_frequency is None:
                    raise fur2mid.FurFormatError("Raw-frequency note 183 requires raw_frequency")
                payload.extend(struct.pack("<I", cell.raw_frequency))
        if cell.instrument is not None:
            payload.append(cell.instrument)
        if cell.volume is not None:
            payload.append(cell.volume)
        for code, value in effects:
            payload.extend((code & 0xFF, value & 0xFF))
        current = row_index + 1
    payload.append(0xFF)
    channel_bytes = struct.pack("<H", channel) if version >= 240 else bytes((channel & 0xFF,))
    header = b"PATN" + b"\0\0\0\0" + bytes((subsong & 0xFF,)) + channel_bytes + struct.pack("<H", index) + name_bytes
    block = bytearray(header + payload)
    struct.pack_into("<I", block, 4, len(block) - 8)
    return bytes(block)


def _encode_patr(subsong: int, channel: int, index: int, name: str, rows: Dict[int, fur2mid.Cell],
                 pattern_length: int, effect_columns: int, version: int) -> bytes:
    if version >= 157:
        raise fur2mid.FurFormatError("PATR blocks are not valid from Furnace format v157 onward")
    payload = bytearray(b"PATR\0\0\0\0")
    payload.extend(struct.pack("<HHHH", channel, index, subsong if version >= 95 else 0, 0))
    cols = max(1, min(8, effect_columns))
    for row_index in range(pattern_length):
        cell = rows.get(row_index, fur2mid.Cell())
        # In legacy PATR, (note=0, octave=0) is the empty-note sentinel.
        # -1 is not valid here: old trackers decode it as a real pitch.
        note_value = 0
        octave_value = 0
        if cell.note is not None:
            if cell.note in (180, 181, 182):
                note_value = {180: 100, 181: 101, 182: 102}[cell.note]
            elif cell.note == 183:
                raise fur2mid.FurFormatError("Raw-frequency notes cannot be represented by legacy PATR blocks")
            elif 0 <= cell.note <= 179:
                note_value = cell.note % 12
                if note_value == 0:
                    note_value = 12
                octave_value = cell.note // 12 - 5
            else:
                raise fur2mid.FurFormatError("Unsupported legacy note code: %s" % cell.note)
        ins = cell.instrument if cell.instrument is not None else -1
        vol = cell.volume if cell.volume is not None else -1
        payload.extend(struct.pack("<hhhh", note_value, octave_value, ins, vol))
        if len(cell.effects) > cols:
            raise fur2mid.FurFormatError("PATR row has more effects than the channel's configured effect columns")
        fx = list(cell.effects)
        for fx_index in range(cols):
            if fx_index < len(fx):
                payload.extend(struct.pack("<hh", fx[fx_index][0], fx[fx_index][1]))
            else:
                payload.extend(struct.pack("<hh", -1, -1))
    if version >= 51:
        payload.extend(name.encode("utf-8") + b"\0")
    struct.pack_into("<I", payload, 4, len(payload) - 8)
    return bytes(payload)


def _asset_signatures(data: bytes, pointers: List[int]) -> List[Tuple[int, bytes]]:
    blocks = []
    for ptr in pointers:
        if ptr == 0:
            continue
        end = _block_end(data, ptr)
        blocks.append((ptr, data[ptr:end]))
    return blocks


def apply_sequence(input_path: Path, sequence_path: Path, output_path: Optional[Path] = None,
                   log: Optional[LOG] = None) -> Dict[str, Any]:
    input_path = Path(input_path)
    sequence_path = Path(sequence_path)
    try:
        document = json.loads(sequence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise fur2mid.FurFormatError("Could not read sequence JSON: %s" % exc) from exc
    if document.get("format") != FORMAT_TAG or document.get("format_version") != FORMAT_VERSION:
        raise fur2mid.FurFormatError("Unsupported RevFUR sequence file")

    layout = inspect_fur(input_path, log)
    expected_hash = document.get("source", {}).get("sha256")
    if expected_hash != _sha(layout.raw_file):
        raise fur2mid.FurFormatError(
            "This sequence file was exported from a different .fur file. "
            "Select the exact original module used for extraction."
        )
    if int(document.get("source", {}).get("fur_version", -1)) != layout.version:
        raise fur2mid.FurFormatError("Furnace format version does not match the source module")

    seq = document.get("sequence", {})
    song = layout.song
    expected_dims = (song.channel_count, song.subsong.orders_length, song.subsong.pattern_length)
    actual_dims = (int(seq.get("channel_count", -1)), int(seq.get("orders_length", -1)), int(seq.get("pattern_length", -1)))
    if actual_dims != expected_dims:
        raise fur2mid.FurFormatError("Sequence dimensions changed; RevFUR currently edits existing orders/pattern rows only")

    orders = seq.get("orders")
    if not isinstance(orders, list) or len(orders) != song.channel_count:
        raise fur2mid.FurFormatError("Order table channel count is invalid")
    normal_orders = []
    for channel_orders in orders:
        if not isinstance(channel_orders, list) or len(channel_orders) != song.subsong.orders_length:
            raise fur2mid.FurFormatError("Every channel must have exactly %d order entries" % song.subsong.orders_length)
        row = []
        for value in channel_orders:
            value = int(value)
            if not 0 <= value <= 255:
                raise fur2mid.FurFormatError("Order entries must be byte values 00..FF")
            row.append(value)
        normal_orders.append(row)

    new_data = bytearray(layout.data)
    old_orders = _orders_from_bytes(layout.data, layout.orders_offset, song.channel_count, song.subsong.orders_length)
    order_changed = normal_orders != old_orders
    if order_changed:
        flat = bytes(value for channel in normal_orders for value in channel)
        new_data[layout.orders_offset:layout.orders_offset + len(flat)] = flat
        _log(log, "Patched order table (%d channel rows)." % song.channel_count)

    patterns_doc = seq.get("patterns", [])
    if not isinstance(patterns_doc, list):
        raise fur2mid.FurFormatError("Patterns must be a list")
    seen_keys = set()
    changed_patterns = 0
    added_patterns = 0
    new_pattern_ptrs: List[int] = []
    append_cursor = len(new_data)
    for pattern_doc in patterns_doc:
        if not isinstance(pattern_doc, dict):
            raise fur2mid.FurFormatError("Pattern entry must be an object")
        key = (int(pattern_doc.get("subsong", 0)), int(pattern_doc.get("channel", -1)), int(pattern_doc.get("index", -1)))
        if key[0] != 0:
            raise fur2mid.FurFormatError("RevFUR currently edits the first subsong only")
        if not 0 <= key[1] < song.channel_count or not 0 <= key[2] <= 255:
            raise fur2mid.FurFormatError("Pattern channel/index is outside Furnace limits")
        if key in seen_keys:
            raise fur2mid.FurFormatError("Duplicate pattern entry: %s" % (key,))
        seen_keys.add(key)

        is_new_pattern = key not in layout.pattern_pointer_indices
        if is_new_pattern:
            # Reverse/repack may need a fresh pattern slot. New blocks are
            # appended and the INFO/INF2 pointer list is extended through a
            # relocated header, so all existing absolute asset pointers stay
            # valid and no instrument/sample/wavetable block is moved.
            rows = _normalise_rows(pattern_doc, song.subsong.pattern_length, layout.version)
            max_effects = (song.subsong.effect_columns[key[1]]
                           if key[1] < len(song.subsong.effect_columns) else 1)
            if any(len(cell.effects) > max_effects for cell in rows.values()):
                raise fur2mid.FurFormatError(
                    "Pattern %s exceeds its channel effect-column limit (%d)" % (key, max_effects)
                )
            if layout.version >= 157:
                replacement = _encode_patn(key[0], key[1], key[2], str(pattern_doc.get("name", "")),
                                            rows, song.subsong.pattern_length, layout.version)
            else:
                cols = song.subsong.effect_columns[key[1]] if key[1] < len(song.subsong.effect_columns) else 1
                replacement = _encode_patr(key[0], key[1], key[2], str(pattern_doc.get("name", "")), rows,
                                            song.subsong.pattern_length, cols, layout.version)
            new_ptr = append_cursor
            new_data.extend(replacement)
            append_cursor += len(replacement)
            new_pattern_ptrs.append(new_ptr)
            added_patterns += 1
            _log(log, "Created channel %d pattern %02X (new block at 0x%X)." % (
                key[1] + 1, key[2], new_ptr))
            continue

        ptr_index = layout.pattern_pointer_indices[key]
        old_ptr, old_block = layout.pattern_blocks[key]
        if pattern_doc.get("source_block_sha256") != _sha(old_block):
            raise fur2mid.FurFormatError("Pattern %s was not exported from the original module or its source checksum was modified" % (key,))
        source_document = _pattern_as_document(layout, key, old_ptr, old_block)
        if (pattern_doc.get("name", "") == source_document.get("name", "")
                and pattern_doc.get("rows", []) == source_document.get("rows", [])):
            continue
        rows = _normalise_rows(pattern_doc, song.subsong.pattern_length, layout.version)
        max_effects = (song.subsong.effect_columns[key[1]]
                       if key[1] < len(song.subsong.effect_columns) else 1)
        if any(len(cell.effects) > max_effects for cell in rows.values()):
            raise fur2mid.FurFormatError(
                "Pattern %s exceeds its channel effect-column limit (%d)" % (key, max_effects)
            )
        if layout.version >= 157:
            replacement = _encode_patn(key[0], key[1], key[2], str(pattern_doc.get("name", "")),
                                        rows, song.subsong.pattern_length, layout.version)
        else:
            cols = song.subsong.effect_columns[key[1]] if key[1] < len(song.subsong.effect_columns) else 1
            replacement = _encode_patr(key[0], key[1], key[2], str(pattern_doc.get("name", "")), rows,
                                        song.subsong.pattern_length, cols, layout.version)
        if _sha(replacement) == _sha(old_block):
            continue
        # Append replacement and update exactly one pointer. Original pattern
        # blocks remain in place for a non-destructive, auditable edit.
        new_ptr = append_cursor
        new_data.extend(replacement)
        struct.pack_into("<I", new_data, layout.pattern_pointer_table_offset + ptr_index * 4, new_ptr)
        append_cursor += len(replacement)
        changed_patterns += 1
        _log(log, "Updated channel %d pattern %02X (new block at 0x%X)." % (key[1] + 1, key[2], new_ptr))

    # Export includes every original first-subsong pattern. Reverse/repack may
    # additionally supply newly allocated patterns; it may never silently drop
    # an original pointer entry.
    first_subsong_keys = {key for key in layout.pattern_pointer_indices if key[0] == 0}
    exported_keys = {(int(p.get("subsong", 0)), int(p.get("channel", -1)), int(p.get("index", -1))) for p in patterns_doc}
    if not first_subsong_keys.issubset(exported_keys):
        raise fur2mid.FurFormatError("Sequence JSON must retain every original first-subsong pattern entry")

    if new_pattern_ptrs:
        info_size = struct.unpack_from("<I", new_data, layout.info_pos + 4)[0]
        info_end = layout.info_pos + 8 + info_size
        if info_end > len(new_data):
            raise fur2mid.FurFormatError("INFO/INF2 block extends beyond the source module")
        info_block = bytearray(new_data[layout.info_pos:info_end])
        relative_ptr_table = layout.pattern_pointer_table_offset - layout.info_pos
        if layout.version >= 240:
            count_offset = relative_ptr_table - 4
        else:
            # Legacy INFO stores the global pattern count in its fixed header,
            # before the system definitions and the later pointer arrays.
            count_offset = 28
        if count_offset < 8 or count_offset + 4 > len(info_block):
            raise fur2mid.FurFormatError("Could not locate the Furnace pattern-pointer count")
        old_count = struct.unpack_from("<I", info_block, count_offset)[0]
        if old_count != len(layout.pattern_ptrs):
            raise fur2mid.FurFormatError("Furnace pattern-pointer count changed unexpectedly")
        insert_at = relative_ptr_table + old_count * 4
        pointer_bytes = b"".join(struct.pack("<I", ptr) for ptr in new_pattern_ptrs)
        if insert_at < 8 or insert_at > len(info_block):
            raise fur2mid.FurFormatError("Pattern-pointer table is outside the INFO/INF2 block")
        info_block[insert_at:insert_at] = pointer_bytes
        struct.pack_into("<I", info_block, count_offset, old_count + len(new_pattern_ptrs))
        struct.pack_into("<I", info_block, 4, len(info_block) - 8)
        new_info_pos = len(new_data)
        new_data.extend(info_block)
        # The header's song-info pointer is at byte offset 20. The relocated
        # header continues to reference every original asset/subsong block at
        # its untouched absolute offset.
        struct.pack_into("<I", new_data, 20, new_info_pos)
        _log(log, "Extended Furnace pattern table with %d new pattern block(s)." % len(new_pattern_ptrs))

    changed_patterns += added_patterns

    # Asset blocks are checked byte-for-byte before writing. Only the order
    # table and pattern pointers are patched; original assets remain untouched.
    before_assets = {
        "instruments": _asset_signatures(layout.data, layout.instrument_ptrs),
        "wavetables": _asset_signatures(layout.data, layout.wavetable_ptrs),
        "samples": _asset_signatures(layout.data, layout.sample_ptrs),
    }
    after_assets = {
        "instruments": _asset_signatures(bytes(new_data), layout.instrument_ptrs),
        "wavetables": _asset_signatures(bytes(new_data), layout.wavetable_ptrs),
        "samples": _asset_signatures(bytes(new_data), layout.sample_ptrs),
    }
    assets_unchanged = all(before_assets[k] == after_assets[k] for k in before_assets)
    if not assets_unchanged:
        raise fur2mid.FurFormatError("Internal safety check failed: an instrument/sample/wavetable block changed")

    result_data = bytes(new_data)
    result_raw = zlib.compress(result_data, 9) if layout.compressed else result_data
    if output_path is None:
        output_path = input_path.with_name(input_path.stem + "_revfur.fur")
    output_path = Path(output_path)
    if output_path.resolve() == input_path.resolve():
        raise fur2mid.FurFormatError("Output .fur path must be different from the original module")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(result_raw)

    # Re-open the result through the same parser before considering conversion successful.
    parsed = fur2mid.load_fur(output_path)
    if parsed.channel_count != song.channel_count or parsed.subsong.orders_length != song.subsong.orders_length:
        output_path.unlink(missing_ok=True)
        raise fur2mid.FurFormatError("The generated module failed post-write structural validation")

    report = {
        "tool": "RevFUR",
        "input": str(input_path),
        "output": str(output_path),
        "fur_version": layout.version,
        "orders_changed": order_changed,
        "patterns_replaced": changed_patterns,
        "patterns_added": added_patterns,
        "instruments_unchanged": True,
        "wavetables_unchanged": True,
        "samples_unchanged": True,
        "instrument_blocks_checked": len(before_assets["instruments"]),
        "wavetable_blocks_checked": len(before_assets["wavetables"]),
        "sample_blocks_checked": len(before_assets["samples"]),
        "assets_policy": "Original asset blocks were compared byte-for-byte in the decompressed FUR data.",
    }
    report_path = output_path.with_suffix(".revfur-report.json")
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    _log(log, "Sequence applied: %s" % output_path)
    _log(log, "Replaced patterns: %d | Orders changed: %s" % (changed_patterns, "yes" if order_changed else "no"))
    _log(log, "Instrument blocks: unchanged (%d checked)" % report["instrument_blocks_checked"])
    _log(log, "Sample blocks: unchanged (%d checked)" % report["sample_blocks_checked"])
    _log(log, "Wavetable blocks: unchanged (%d checked)" % report["wavetable_blocks_checked"])
    _log(log, "Validation report: %s" % report_path)
    return report



@dataclass
class PlaybackAnalysis:
    termination: str
    states: List[Tuple[int, int]]
    segments: List[Dict[str, int]]
    loop_start: Optional[Tuple[int, int]]
    loop_end: Optional[Tuple[int, int]]
    end_state: Optional[Tuple[int, int]]
    flow_counts: Dict[str, int]
    flow_events: List[Dict[str, int]]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "termination": self.termination,
            "loop_detected": self.loop_start is not None,
            "loop_start": ({"order": self.loop_start[0], "row": self.loop_start[1]}
                           if self.loop_start is not None else None),
            "loop_end": ({"order": self.loop_end[0], "row": self.loop_end[1]}
                         if self.loop_end is not None else None),
            "end_state": ({"order": self.end_state[0], "row": self.end_state[1]}
                          if self.end_state is not None else None),
            "song_end": ({"type": self.termination, "order": self.end_state[0], "row": self.end_state[1]}
                         if self.end_state is not None else None),
            "played_rows": len(self.states),
            "segments": [dict(item) for item in self.segments],
            "flow_commands": dict(self.flow_counts),
            "flow_events": [dict(item) for item in self.flow_events],
        }


def analyze_playback(layout: FurLayout, log: Optional[LOG] = None,
                     max_steps: Optional[int] = None) -> PlaybackAnalysis:
    """Follow Furnace order/row control flow until a loop or song end is found.

    This models global 0Bxx/0Dxx/FFxx handling across channels and honors the
    module's jumpTreatment and ignoreJumpAtEnd compatibility flags.  Natural
    order-table completion is reported as the end of one song pass; Furnace
    may restart that pass according to its loop modality.
    """
    song = layout.song
    orders = song.subsong.orders
    order_count = song.subsong.orders_length
    pattern_length = song.subsong.pattern_length
    channel_count = song.channel_count
    if not orders or any(len(channel_orders) != order_count for channel_orders in orders):
        raise fur2mid.FurFormatError("Cannot analyze playback: malformed order table")
    if max_steps is None:
        max_steps = max(1, order_count * pattern_length + 1)

    states: List[Tuple[int, int]] = []
    segments: List[Dict[str, int]] = []
    seen: Dict[Tuple[int, int], int] = {}
    flow_counts = {"0B": 0, "0D": 0, "FF": 0}
    flow_events: List[Dict[str, int]] = []
    order, row = 0, 0
    termination = "step_limit"
    loop_start = None
    loop_end = None
    end_state = None

    while len(states) < max_steps:
        if order < 0 or order >= order_count or row < 0 or row >= pattern_length:
            termination = "invalid_flow_target"
            end_state = states[-1] if states else None
            break
        state = (order, row)
        if state in seen:
            termination = "loop"
            loop_start = state
            loop_end = states[-1] if states else state
            break
        seen[state] = len(states)
        states.append(state)
        if (not segments or segments[-1]["order"] != order
                or row != segments[-1]["end_row"] + 1):
            segments.append({"order": order, "start_row": row, "end_row": row})
        else:
            segments[-1]["end_row"] = row

        # changeOrd=-1 means no order jump; -2 means next order.
        change_order: Optional[int] = None
        change_row = 0
        stop_scheduled = False
        for channel in range(channel_count):
            pattern_index = int(orders[channel][order])
            pattern = song.subsong.patterns.get((channel, pattern_index))
            cell = pattern.rows.get(row) if pattern is not None else None
            if cell is None:
                continue
            for command, value in cell.effects:
                code = int(command) & 0xFF
                value = int(value) & 0xFF
                if code == 0x0B:
                    flow_counts["0B"] += 1
                    flow_events.append({"command": code, "value": value,
                                        "channel": channel, "order": order, "row": row})
                    if change_order is None or layout.jump_treatment == 0:
                        change_order = value
                        if layout.jump_treatment in (1, 2):
                            change_row = 0
                elif code == 0x0D:
                    flow_counts["0D"] += 1
                    flow_events.append({"command": code, "value": value,
                                        "channel": channel, "order": order, "row": row})
                    if order < order_count - 1 or not layout.ignore_jump_at_end:
                        if layout.jump_treatment == 2:
                            change_order = -2
                            change_row = value
                        elif layout.jump_treatment == 1:
                            if change_order is None:
                                change_order = -2
                                change_row = value
                        else:
                            if change_order is None:
                                change_order = -2
                            change_row = value
                elif code == 0xFF:
                    flow_counts["FF"] += 1
                    flow_events.append({"command": code, "value": value,
                                        "channel": channel, "order": order, "row": row})
                    stop_scheduled = True

        if stop_scheduled:
            termination = "explicit_stop"
            end_state = state
            break

        if change_order is not None:
            if change_order == -2:
                next_order = order + 1
            else:
                next_order = change_order
            if next_order >= order_count:
                # Furnace wraps an out-of-range order jump back to order zero.
                # Treat this as the end of the current finite pass.
                termination = "order_wrap_end"
                end_state = state
                break
            order, row = next_order, change_row
            continue

        if row + 1 >= pattern_length:
            if order + 1 >= order_count:
                termination = "natural_end"
                end_state = state
                break
            order += 1
            row = 0
        else:
            row += 1

    if termination == "step_limit":
        raise fur2mid.FurFormatError(
            "Playback-flow analysis exceeded its safety limit; a malformed or unsupported control-flow pattern may be present."
        )

    result = PlaybackAnalysis(
        termination=termination, states=states, segments=segments,
        loop_start=loop_start, loop_end=loop_end, end_state=end_state,
        flow_counts=flow_counts, flow_events=flow_events,
    )
    if termination == "loop":
        _log(log, "Playback analysis: loop detected from order %02X row %02X to order %02X row %02X." % (
            loop_start[0], loop_start[1], loop_end[0], loop_end[1]))
        _log(log, "No finite song end exists: Furnace repeats this loop indefinitely. One complete intro/loop pass (%d rows) will be reversed." % len(states))
    elif termination == "explicit_stop":
        _log(log, "Playback analysis: explicit FFxx stop at order %02X row %02X." % end_state)
    elif termination == "natural_end":
        _log(log, "Playback analysis: natural song end at order %02X row %02X (end of order table)." % end_state)
    elif termination == "order_wrap_end":
        _log(log, "Playback analysis: order jump leaves the order table at order %02X row %02X; this ends the current pass." % end_state)
    _log(log, "Reachable sequence: %d rows across %d playback segment(s); detected commands 0B=%d, 0D=%d, FF=%d." % (
        len(states), len(segments), flow_counts["0B"], flow_counts["0D"], flow_counts["FF"]))
    return result


def analyze_fur_file(input_path: Path, log: Optional[LOG] = None) -> Dict[str, Any]:
    """Public entry point used by the GUI and command line analysis mode."""
    layout = inspect_fur(Path(input_path), log)
    analysis = analyze_playback(layout, log)
    report = analysis.as_dict()
    report.update({
        "tool": "RevFUR",
        "input": str(input_path),
        "fur_version": layout.version,
        "channel_count": layout.song.channel_count,
        "order_table_length": layout.song.subsong.orders_length,
        "pattern_length": layout.song.subsong.pattern_length,
        "jump_treatment": layout.jump_treatment,
        "ignore_jump_at_end": layout.ignore_jump_at_end,
    })
    return report


def _is_note_event(note: Any) -> bool:
    return note is not None and (0 <= int(note) < 180 or int(note) == 183)


def count_leading_navigation_ticks(patterns_by_key: Dict[Tuple[int, int], Dict[str, Any]],
                                  original_orders: List[List[int]], channel_count: int,
                                  order_index: int, start_row: int, end_row: int) -> int:
    """Count consecutive empty/navigation-only source ticks at a path boundary.

    A reversed arrangement begins at the source path's final played row. If
    this row (and possibly earlier adjacent rows) contains only flow commands
    or no cell data, those ticks would appear as leading silence after their
    navigation effects are removed. Count the entire safe contiguous run while
    retaining at least one row in the segment.
    """
    trim_count = 0
    for source_row in range(end_row, start_row, -1):
        row_is_safe = True
        for channel in range(channel_count):
            pattern_index = original_orders[channel][order_index]
            pattern_doc = patterns_by_key.get((channel, pattern_index))
            row_item = next((item for item in pattern_doc.get("rows", [])
                             if int(item["row"]) == source_row), None) if pattern_doc else None
            if row_item is None:
                continue
            if any(field in row_item for field in ("note", "instrument", "volume", "raw_frequency")):
                row_is_safe = False
                break
            for effect in row_item.get("effects", []):
                command = _parse_hex_value(effect.get("command"), "effect command", 0xFFFF)
                if (command & 0xFF) not in (0x0B, 0x0D, 0xFF):
                    row_is_safe = False
                    break
            if not row_is_safe:
                break
        if not row_is_safe:
            break
        trim_count += 1
    return trim_count


def _row_document_has_playback_data(row_item: Optional[Dict[str, Any]]) -> bool:
    """Return whether a row contains anything except removable navigation.

    Notes (including OFF/REL), instrument and volume changes, raw frequencies,
    and all non-flow effects count as real sequence data. 0B/0D/FF alone do not:
    source navigation is removed when the reversed stream is repacked.
    """
    if row_item is None:
        return False
    if any(field in row_item for field in ("note", "instrument", "volume", "raw_frequency")):
        return True
    for effect in row_item.get("effects", []):
        command = _parse_hex_value(effect.get("command"), "effect command", 0xFFFF)
        if (command & 0xFF) not in (0x0B, 0x0D, 0xFF):
            return True
    return False


def find_isolated_empty_ticks(states: List[Tuple[int, int]],
                              patterns_by_key: Dict[Tuple[int, int], Dict[str, Any]],
                              original_orders: List[List[int]],
                              channel_count: int) -> set:
    """Find globally empty playback ticks whose empty run is exactly one row.

    This works on the simulated, reachable playback stream, not on raw pattern
    storage order. It therefore stays synchronized across every channel and
    handles loops, order jumps and pattern breaks consistently.
    """
    empty_flags = []
    for order_index, row_index in states:
        has_data = False
        for channel in range(channel_count):
            pattern_index = original_orders[channel][order_index]
            pattern_doc = patterns_by_key.get((channel, pattern_index))
            row_item = next((item for item in pattern_doc.get("rows", [])
                             if int(item["row"]) == row_index), None) if pattern_doc else None
            if _row_document_has_playback_data(row_item):
                has_data = True
                break
        empty_flags.append(not has_data)

    isolated = set()
    pos = 0
    while pos < len(empty_flags):
        if not empty_flags[pos]:
            pos += 1
            continue
        run_end = pos + 1
        while run_end < len(empty_flags) and empty_flags[run_end]:
            run_end += 1
        if run_end - pos == 1:
            isolated.add(states[pos])
        pos = run_end
    return isolated


def count_isolated_empty_ticks(layout: FurLayout,
                               states: Optional[List[Tuple[int, int]]] = None) -> int:
    """Count isolated globally empty ticks in a parsed module's playback path."""
    analysis = analyze_playback(layout) if states is None else None
    playback_states = analysis.states if analysis is not None else states
    song = layout.song
    flags = []
    terminal_state = playback_states[-1] if playback_states else None
    for state_index, (order_index, row_index) in enumerate(playback_states):
        has_data = False
        for channel in range(song.channel_count):
            pattern_index = song.subsong.orders[channel][order_index]
            pattern = song.subsong.patterns.get((channel, pattern_index))
            cell = pattern.rows.get(row_index) if pattern is not None else None
            if cell is None:
                continue
            if cell.note is not None or cell.instrument is not None or cell.volume is not None:
                has_data = True
                break
            if any((int(command) & 0xFF) not in (0x0B, 0x0D, 0xFF)
                   for command, _value in cell.effects):
                has_data = True
                break
            # A terminal FFxx/0Bxx is a structural end marker, not an empty
            # musical tick. Do not report it as a one-tick silence gap.
            if (state_index == len(playback_states) - 1 and
                    any((int(command) & 0xFF) in (0x0B, 0xFF)
                        for command, _value in cell.effects)):
                has_data = True
                break
        flags.append(not has_data)
    count = 0
    pos = 0
    while pos < len(flags):
        if not flags[pos]:
            pos += 1
            continue
        end = pos + 1
        while end < len(flags) and flags[end]:
            end += 1
        if end - pos == 1:
            count += 1
        pos = end
    return count


def _note_stream(path: Path) -> List[Tuple[Any, ...]]:
    """Return the note values of every playback tick, in playback order.

    Used to verify a reversed module against the reversal of its source. Only
    note values are compared: instrument and volume values may be legitimately
    filled in by reverse mode to restore inherited state.
    """
    import tempfile
    json_path = Path(tempfile.mkdtemp(prefix="revfur_verify_")) / "verify.furseq.json"
    export_sequence(Path(path), json_path, lambda *a, **k: None)
    doc = json.loads(json_path.read_text(encoding="utf-8"))
    seq = doc["sequence"]
    patterns = {(p["channel"], p["index"]): {int(r["row"]): r for r in p["rows"]} for p in seq["patterns"]}
    analysis = analyze_playback(inspect_fur(Path(path)))
    stream = []
    for order_index, row_index in analysis.states:
        tick = []
        for channel in range(seq["channel_count"]):
            pattern_index = seq["orders"][channel][order_index]
            cell = patterns.get((channel, pattern_index), {}).get(row_index)
            tick.append(None if cell is None else cell.get("note"))
        stream.append(tuple(tick))
    return stream


def reverse_entire_sequence(input_path: Path, output_path: Optional[Path] = None,
                            log: Optional[LOG] = None,
                            loop_back_to_start: bool = False,
                            compact_empty_ticks: bool = False,
                            trim_boundary_empty_ticks: bool = True) -> Dict[str, Any]:
    """Reverse the complete reachable playback stream and repack it into orders.

    By default the musical timing is preserved exactly: every reachable tick,
    including one-tick empty rows, is kept, so the reversed module has the same
    length as the playback path of the source. Pass compact_empty_ticks=True to
    additionally remove isolated one-tick empty rows (this shortens the song and
    changes the rhythm, so it is opt-in).

    trim_boundary_empty_ticks (default True) only removes empty or
    navigation-only ticks at the start of the reversed arrangement, which is
    the source's final played tick. Interior ticks are never touched by it.

    Unlike the earlier segment-preserving implementation, this version does
    not insert 0Dxx between partially filled source orders. It flattens the
    reachable row stream, reverses it, removes only isolated one-tick gaps,
    and repacks the result into full-length patterns. This avoids introducing
    pattern-break commands into songs that never used them and also handles
    source songs that do use 0Dxx.
    """
    import tempfile

    input_path = Path(input_path)
    layout = inspect_fur(input_path, log)
    song = layout.song
    analysis = analyze_playback(layout, log)
    if not analysis.states:
        raise fur2mid.FurFormatError("No playable sequence rows were found")

    seq_len = song.subsong.orders_length
    pattern_len = song.subsong.pattern_length
    original_orders = [list(row) for row in song.subsong.orders]
    _log(log, "Reversing %d reachable ticks across %d channels." % (
        len(analysis.states), song.channel_count))

    with tempfile.TemporaryDirectory(prefix="revfur_reverse_") as temp_dir:
        sequence_path = Path(temp_dir) / "reverse_input.furseq.json"
        exported, _dump = export_sequence(input_path, sequence_path, log)
        document = json.loads(exported.read_text(encoding="utf-8"))
        seq = document["sequence"]
        original_pattern_docs = seq["patterns"]
        patterns_by_key = {
            (int(item.get("channel", -1)), int(item.get("index", -1))): item
            for item in original_pattern_docs
        }
        row_lookup_by_key = {
            key: {int(item["row"]): item for item in pattern_doc.get("rows", [])}
            for key, pattern_doc in patterns_by_key.items()
        }

        # Capture stateful instrument/volume values while walking the original
        # song forward. A reversed note may no longer have its original setup
        # row before it, so only the required references are materialised.
        desired_state: Dict[Tuple[int, int, int], Tuple[Optional[int], Optional[int]]] = {}
        active_by_channel: List[Tuple[Optional[int], Optional[int]]] = [
            (None, None) for _ in range(song.channel_count)
        ]
        for order, source_row in analysis.states:
            for channel in range(song.channel_count):
                pattern_index = original_orders[channel][order]
                row_item = row_lookup_by_key.get((channel, pattern_index), {}).get(source_row)
                active_instrument, active_volume = active_by_channel[channel]
                if row_item is not None:
                    if "instrument" in row_item:
                        active_instrument = int(row_item["instrument"])
                    if "volume" in row_item:
                        active_volume = int(row_item["volume"])
                    if _is_note_event(row_item.get("note")):
                        key = (channel, pattern_index, source_row)
                        state = (active_instrument, active_volume)
                        previous = desired_state.get(key)
                        if previous is not None and previous != state:
                            raise fur2mid.FurFormatError(
                                "Cannot safely reverse channel %d pattern %02X row %02X: this row is reached "
                                "with conflicting instrument/volume states. Split that pattern in Furnace first." %
                                (channel + 1, pattern_index, source_row)
                            )
                        desired_state[key] = state
                active_by_channel[channel] = (active_instrument, active_volume)

        # The reverse starts at the final played source tick. Remove any
        # trailing empty/navigation-only run there, then remove globally
        # isolated one-tick gaps throughout the path. All channels are treated
        # as a synchronized tick: a tick is kept if any channel has real data.
        reversed_source_segments = list(reversed(analysis.segments))
        first_segment = reversed_source_segments[0]
        first_order = int(first_segment["order"])
        first_start_row = int(first_segment["start_row"])
        first_end_row = int(first_segment["end_row"])
        boundary_trim_rows = count_leading_navigation_ticks(
            patterns_by_key, original_orders, song.channel_count,
            first_order, first_start_row, first_end_row,
        )
        first_segment_has_data = any(
            _row_document_has_playback_data(
                row_lookup_by_key.get((channel, original_orders[channel][first_order]), {}).get(source_row)
            )
            for source_row in range(first_start_row, first_end_row + 1)
            for channel in range(song.channel_count)
        )
        if not first_segment_has_data:
            boundary_trim_rows = 0

        if compact_empty_ticks:
            isolated_empty_states = find_isolated_empty_ticks(
                analysis.states, patterns_by_key, original_orders, song.channel_count
            )
        else:
            isolated_empty_states = set()
        if not trim_boundary_empty_ticks:
            boundary_trim_rows = 0
        boundary_empty_states = {
            (first_order, source_row)
            for source_row in range(first_end_row - boundary_trim_rows + 1, first_end_row + 1)
        } if boundary_trim_rows else set()
        removed_empty_states = isolated_empty_states | boundary_empty_states
        reversed_states = list(reversed(analysis.states))
        output_source_states = [state for state in reversed_states if state not in removed_empty_states]
        if not output_source_states:
            raise fur2mid.FurFormatError("The sequence became empty after removing isolated one-tick gaps")

        dropped_empty_ticks = len(set(reversed_states) & removed_empty_states)
        single_tick_gaps_removed = len(isolated_empty_states)
        _log(log, "Removed %d isolated one-tick gap(s); preserved multi-tick rests." % single_tick_gaps_removed)
        if boundary_trim_rows:
            _log(log, "Trimmed %d empty/navigation-only tick(s) from the reversed beginning." % boundary_trim_rows)

        # Repack the flattened reversed stream into ordinary full-length
        # pattern blocks. This is the key fix: there are no partial source-order
        # boundaries left that would require generated 0Dxx interrupts.
        output_order_count = (len(output_source_states) + pattern_len - 1) // pattern_len
        if output_order_count > seq_len:
            raise fur2mid.FurFormatError(
                "The reversed sequence requires %d orders but the Furnace module has only %d." %
                (output_order_count, seq_len)
            )
        _log(log, "Repacked %d ticks into %d order(s) of %d rows; no automatic 0Dxx breaks are required." % (
            len(output_source_states), output_order_count, pattern_len))

        # Reuse existing first-subsong pattern indexes where possible, then
        # allocate free IDs. New IDs are appended to the Furnace pointer table
        # by apply_sequence(); this works even when a song reuses one pattern
        # index for many orders.
        chosen_indices: List[List[int]] = []
        for channel in range(song.channel_count):
            candidates: List[int] = []
            seen_indices = set()
            for segment in reversed_source_segments:
                source_order = int(segment["order"])
                candidate = original_orders[channel][source_order]
                if (channel, candidate) in patterns_by_key and candidate not in seen_indices:
                    candidates.append(candidate)
                    seen_indices.add(candidate)
            for (pattern_channel, candidate) in sorted(patterns_by_key):
                if pattern_channel == channel and candidate not in seen_indices:
                    candidates.append(candidate)
                    seen_indices.add(candidate)
            for candidate in range(256):
                if len(candidates) >= output_order_count:
                    break
                if candidate not in seen_indices:
                    candidates.append(candidate)
                    seen_indices.add(candidate)
            if len(candidates) < output_order_count:
                raise fur2mid.FurFormatError(
                    "Channel %d has no available Furnace pattern IDs for repacking %d output orders." %
                    (channel + 1, output_order_count)
                )
            chosen_indices.append(candidates[:output_order_count])

        # Preserve original pattern docs so source checksums and unused patterns
        # remain available for audit. Selected IDs are replaced by repacked
        # sequence blocks; missing selected IDs are added as new patterns.
        pattern_doc_map = dict(patterns_by_key)
        for channel in range(song.channel_count):
            for chunk_index in range(output_order_count):
                target_index = chosen_indices[channel][chunk_index]
                key = (channel, target_index)
                existing = pattern_doc_map.get(key)
                if existing is None:
                    new_doc = {
                        "subsong": 0,
                        "channel": channel,
                        "index": target_index,
                        "name": "RevFUR Reversed %02X" % chunk_index,
                        "rows": [],
                    }
                    original_pattern_docs.append(new_doc)
                    pattern_doc_map[key] = new_doc
                else:
                    existing["name"] = "RevFUR Reversed %02X" % chunk_index
                    existing["rows"] = []

        # Every source flow command is discarded from the reversed pass and
        # replaced only by a final stop/loop marker when one is required.
        removed_flow = {key: int(analysis.flow_counts.get(key, 0)) for key in ("0B", "0D", "FF")}
        for tick_index, (source_order, source_row) in enumerate(output_source_states):
            chunk_index, target_row = divmod(tick_index, pattern_len)
            for channel in range(song.channel_count):
                source_pattern_index = original_orders[channel][source_order]
                source_item = row_lookup_by_key.get((channel, source_pattern_index), {}).get(source_row)
                if source_item is None:
                    continue
                new_item = dict(source_item)
                new_item["row"] = target_row
                kept_effects = []
                for effect in source_item.get("effects", []):
                    command = _parse_hex_value(effect.get("command"), "effect command", 0xFFFF)
                    low = command & 0xFF
                    if low in (0x0B, 0x0D, 0xFF):
                        continue
                    else:
                        kept_effects.append(dict(effect))
                if kept_effects:
                    new_item["effects"] = kept_effects
                else:
                    new_item.pop("effects", None)
                meaningful = any(field in new_item for field in (
                    "note", "instrument", "volume", "raw_frequency", "effects"
                ))
                if meaningful:
                    target_key = (channel, chosen_indices[channel][chunk_index])
                    pattern_doc_map[target_key]["rows"].append(new_item)

        # Restore active instrument/volume state in the actual reversed
        # playback order, after all source rows have been mapped into new blocks.
        inserted_instruments = 0
        inserted_volumes = 0
        for channel in range(song.channel_count):
            active_instrument: Optional[int] = None
            active_volume: Optional[int] = None
            for tick_index, (source_order, source_row) in enumerate(output_source_states):
                chunk_index, target_row = divmod(tick_index, pattern_len)
                target_key = (channel, chosen_indices[channel][chunk_index])
                target_doc = pattern_doc_map[target_key]
                target_item = next((item for item in target_doc["rows"]
                                    if int(item["row"]) == target_row), None)
                if target_item is None:
                    continue
                if "instrument" in target_item:
                    active_instrument = int(target_item["instrument"])
                if "volume" in target_item:
                    active_volume = int(target_item["volume"])
                if not _is_note_event(target_item.get("note")):
                    continue
                source_pattern_index = original_orders[channel][source_order]
                expected_instrument, expected_volume = desired_state.get(
                    (channel, source_pattern_index, source_row), (None, None)
                )
                if expected_instrument is not None and active_instrument != expected_instrument:
                    target_item["instrument"] = expected_instrument
                    active_instrument = expected_instrument
                    inserted_instruments += 1
                elif expected_instrument is None and active_instrument is not None:
                    raise fur2mid.FurFormatError(
                        "Cannot safely reverse channel %d: a source note had no active instrument, "
                        "but the repacked sequence would inherit instrument %d." %
                        (channel + 1, active_instrument)
                    )
                if expected_volume is not None and active_volume != expected_volume:
                    target_item["volume"] = expected_volume
                    active_volume = expected_volume
                    inserted_volumes += 1
                elif expected_volume is None and active_volume is not None:
                    raise fur2mid.FurFormatError(
                        "Cannot safely reverse channel %d: a source note had no active volume, "
                        "but the repacked sequence would inherit volume %d." %
                        (channel + 1, active_volume)
                    )

        # Sort repacked rows and make sure every output order points at a
        # unique pattern ID. Unplayed tail orders are padded with pattern 00.
        for channel in range(song.channel_count):
            for chunk_index in range(output_order_count):
                target_doc = pattern_doc_map[(channel, chosen_indices[channel][chunk_index])]
                target_doc["rows"].sort(key=lambda item: int(item["row"]))
            seq["orders"][channel] = chosen_indices[channel] + [0] * (seq_len - output_order_count)

        # Only the final tick needs a flow marker. Complete intermediate chunks
        # play naturally to their final row, so no 0Dxx is written at order
        # boundaries. If the output fills the entire order table, finite output
        # can also terminate naturally without FF00.
        final_chunk_length = len(output_source_states) - (output_order_count - 1) * pattern_len
        flow_markers_inserted = {"0D": 0, "FF": 0, "0B": 0}
        if loop_back_to_start:
            final_marker = "0B"
        elif output_order_count == seq_len and final_chunk_length == pattern_len:
            final_marker = None
        else:
            final_marker = "FF"

        if final_marker is not None:
            final_chunk = output_order_count - 1
            marker_row = final_chunk_length - 1
            marker_placed = False
            candidates = sorted(
                range(song.channel_count),
                key=lambda channel: len(next((item.get("effects", [])
                    for item in pattern_doc_map[(channel, chosen_indices[channel][final_chunk])]["rows"]
                    if int(item["row"]) == marker_row), [])),
            )
            for channel in candidates:
                target_doc = pattern_doc_map[(channel, chosen_indices[channel][final_chunk])]
                row_item = next((item for item in target_doc["rows"]
                                 if int(item["row"]) == marker_row), None)
                if row_item is None:
                    row_item = {"row": marker_row}
                    target_doc["rows"].append(row_item)
                    target_doc["rows"].sort(key=lambda item: int(item["row"]))
                effects = list(row_item.get("effects", []))
                max_effects = (song.subsong.effect_columns[channel]
                               if channel < len(song.subsong.effect_columns) else 1)
                if len(effects) < max_effects:
                    effects.append({"command": final_marker, "value": "00"})
                    row_item["effects"] = effects
                    flow_markers_inserted[final_marker] += 1
                    _log(log, "Inserted %s00 at output order %02X row %02X on channel %d." % (
                        final_marker, final_chunk, marker_row, channel + 1))
                    marker_placed = True
                    break
            if not marker_placed:
                raise fur2mid.FurFormatError(
                    "Cannot place the final %s00 marker: every channel has all effect columns occupied "
                    "on the final output tick. Free one effect column on that tick and retry." % final_marker
                )

        seq["patterns"] = original_pattern_docs
        output_path = Path(output_path) if output_path is not None else input_path.with_name(
            input_path.stem + "_reversed.fur"
        )
        if output_path.resolve() == input_path.resolve():
            raise fur2mid.FurFormatError("Output .fur path must be different from the original module")
        sequence_path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n",
                                 encoding="utf-8")
        report = apply_sequence(input_path, sequence_path, output_path, log)

    output_layout = inspect_fur(output_path)
    output_analysis = analyze_playback(output_layout)
    if loop_back_to_start:
        valid_output_end = (output_analysis.termination == "loop"
                            and output_analysis.loop_start == (0, 0)
                            and output_analysis.flow_counts.get("0B", 0) >= 1)
        expected_end_description = "a 0B00 loop back to order 00 / row 00"
    elif final_marker is None:
        valid_output_end = output_analysis.termination == "natural_end"
        expected_end_description = "the natural order-table end"
    else:
        valid_output_end = output_analysis.termination == "explicit_stop"
        expected_end_description = "the generated FF00 stop marker"

    expected_output_rows = len(output_source_states)
    if len(output_analysis.states) != expected_output_rows:
        Path(output_path).unlink(missing_ok=True)
        raise fur2mid.FurFormatError(
            "Post-write playback length mismatch: expected %d packed ticks, got %d." %
            (expected_output_rows, len(output_analysis.states))
        )
    output_single_empty_ticks = count_isolated_empty_ticks(output_layout, output_analysis.states)
    if compact_empty_ticks and output_single_empty_ticks:
        Path(output_path).unlink(missing_ok=True)
        raise fur2mid.FurFormatError(
            "Post-write validation found %d isolated empty tick(s); refusing to emit a sequence with one-tick gaps." %
            output_single_empty_ticks
        )
    if output_analysis.flow_counts.get("0D", 0) != 0:
        Path(output_path).unlink(missing_ok=True)
        raise fur2mid.FurFormatError(
            "Post-write validation found an unexpected 0Dxx pattern break in the repacked output."
        )
    if not valid_output_end:
        Path(output_path).unlink(missing_ok=True)
        raise fur2mid.FurFormatError(
            "Post-write playback analysis did not find %s." % expected_end_description
        )

    # Compare musical note counts per channel before and after the reversal.
    # This catches accidental event loss when rows are repacked across source
    # order boundaries, regardless of whether the source used 0Dxx.
    source_note_counts = [0] * song.channel_count
    output_note_counts = [0] * song.channel_count
    for order, source_row in analysis.states:
        for channel in range(song.channel_count):
            pattern_index = original_orders[channel][order]
            cell = song.subsong.patterns.get((channel, pattern_index))
            cell = cell.rows.get(source_row) if cell is not None else None
            if cell is not None and _is_note_event(cell.note):
                source_note_counts[channel] += 1
    out_song = output_layout.song
    for order, output_row in output_analysis.states:
        for channel in range(song.channel_count):
            pattern_index = out_song.subsong.orders[channel][order]
            pattern = out_song.subsong.patterns.get((channel, pattern_index))
            cell = pattern.rows.get(output_row) if pattern is not None else None
            if cell is not None and _is_note_event(cell.note):
                output_note_counts[channel] += 1
    if source_note_counts != output_note_counts:
        Path(output_path).unlink(missing_ok=True)
        raise fur2mid.FurFormatError(
            "Post-write validation found a per-channel note-count mismatch: source=%s output=%s." %
            (source_note_counts, output_note_counts)
        )

    report.update({
        "operation": "reverse_entire_sequence",
        "playback_analysis": analysis.as_dict(),
        "output_playback_analysis": output_analysis.as_dict(),
        "order_positions_reversed": output_order_count,
        "source_playback_segments": len(analysis.segments),
        "channels_reversed_together": song.channel_count,
        "pattern_rows_mirrored": len(output_source_states),
        "patterns_reversed": output_order_count * song.channel_count,
        "instrument_references_inserted": inserted_instruments,
        "volume_values_inserted": inserted_volumes,
        "navigation_effects_removed": removed_flow,
        "stop_marker_inserted": final_marker == "FF",
        "natural_end_used": final_marker is None,
        "loop_back_to_start": bool(loop_back_to_start),
        "loop_marker_inserted": final_marker == "0B",
        "flow_markers_inserted": flow_markers_inserted,
        "initial_navigation_tick_trimmed": bool(boundary_trim_rows),
        "trim_boundary_empty_ticks_enabled": bool(trim_boundary_empty_ticks),
        "initial_navigation_tick_shift_rows": boundary_trim_rows,
        "isolated_empty_ticks_removed": dropped_empty_ticks,
        "single_tick_gaps_removed": single_tick_gaps_removed,
        "single_tick_empty_ticks_preserved_shared": 0,
        "single_tick_gap_compaction_complete": bool(compact_empty_ticks),
        "empty_tick_compaction_enabled": bool(compact_empty_ticks),
        "output_playback_rows_expected": expected_output_rows,
        "source_note_counts_by_channel": source_note_counts,
        "output_note_counts_by_channel": output_note_counts,
        "automatic_0D_insertions": 0,
        "reverse_policy": (
            "Simulated Furnace playback to the first detected loop, explicit FFxx stop, "
            "or natural order-table end; reversed the reachable tick stream; %s; "
            "repacked the continuous result into full-length patterns; did not insert "
            "0Dxx at order boundaries; and ended with %s."
        ) % (
            "removed isolated one-tick empty gaps (opt-in)" if compact_empty_ticks
            else "preserved every tick, including one-tick empty rows (same length)",
            "0B00 to loop back to order 00 / row 00" if loop_back_to_start
            else "FF00 stop" if final_marker == "FF" else "the natural order-table end",
        ),
    })
    try:
        expected_stream = list(reversed(_note_stream(input_path)))[boundary_trim_rows:]
        output_stream = _note_stream(output_path)
        mismatched = sum(1 for a, b in zip(expected_stream, output_stream) if a != b)
        report["exact_note_verification"] = {
            "expected_ticks": len(expected_stream),
            "output_ticks": len(output_stream),
            "mismatched_ticks": mismatched,
            "matches": bool(len(expected_stream) == len(output_stream) and mismatched == 0),
            "scope": "note values per playback tick (instrument/volume state fills are excluded)",
        }
    except Exception as exc:  # verification is advisory; never hide the output
        report["exact_note_verification"] = {"error": str(exc)}
    _log(log, "Exact note verification: %s" % (
        "all ticks match the reversed source" if report["exact_note_verification"].get("matches")
        else "see report exact_note_verification"))
    report_path = Path(output_path).with_suffix(".revfur-report.json")
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if compact_empty_ticks:
        _log(log, "Compacted %d isolated empty tick(s); retained multi-tick rests." % single_tick_gaps_removed)
    else:
        _log(log, "Preserved all ticks, including one-tick empty rows (no timing change).")
    _log(log, "Repacked %d ticks into %d order(s); inserted 0Dxx markers: 0." % (
        expected_output_rows, output_order_count))
    _log(log, "Reversed reachable playback path successfully.")
    output_end_description = ("0B00 loop to order 00 / row 00" if final_marker == "0B"
                              else "explicit FF00 stop" if final_marker == "FF" else "natural order-table end")
    _log(log, "Source termination: %s | Output termination: %s." % (
        analysis.termination, output_end_description))
    _log(log, "Output: %s" % output_path)
    _log(log, "Assets preserved byte-for-byte: instruments=%d, samples=%d, wavetables=%d." % (
        report["instrument_blocks_checked"], report["sample_blocks_checked"],
        report["wavetable_blocks_checked"]))
    return report

def _orders_from_bytes(data: bytes, offset: int, channels: int, orders: int) -> List[List[int]]:
    out = []
    for channel in range(channels):
        start = offset + channel * orders
        out.append(list(data[start:start + orders]))
    return out
