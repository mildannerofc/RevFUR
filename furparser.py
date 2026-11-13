#!/usr/bin/env python3
"""Shared Furnace .fur parser used by fur2mid and RevFUR."""
import sys

# RevFUR shares the modern parser code with fur2mid. Require a supported
# interpreter up front so older Python versions fail with an actionable message.
if sys.version_info < (3, 10):
    detected = ".".join(str(part) for part in sys.version_info[:3])
    raise SystemExit(
        "RevFUR requires Python 3.10 or newer; detected Python " + detected + ".\n"
        "Install Python 3.10+ and launch with 'py -3.10 main.py' or run_windows.bat."
    )

import argparse
import json
import math
import re
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Iterable

MAGIC = b"-Furnace module-"

# Furnace system IDs and nominal channel counts. The list follows papers/format.md.
SYSTEMS: Dict[int, Tuple[str, int]] = {
    0x01: ("YMU759", 17), 0x03: ("SN76489", 4), 0x04: ("Game Boy", 4),
    0x05: ("PC Engine", 6), 0x06: ("NES", 5), 0x07: ("C64 8580", 3),
    0x47: ("C64 6581", 3), 0x80: ("AY-3-8910", 3), 0x81: ("Amiga", 4),
    0x82: ("YM2151", 8), 0x83: ("YM2612", 6), 0x84: ("TIA", 2),
    0x85: ("VIC-20", 4), 0x86: ("PET", 1), 0x87: ("SNES", 8),
    0x88: ("VRC6", 3), 0x89: ("YM2413 OPLL", 9), 0x8A: ("FDS", 1),
    0x8B: ("MMC5", 3), 0x8C: ("Namco 163", 8), 0x8D: ("YM2203", 6),
    0x8E: ("YM2608", 16), 0x8F: ("YM3526 OPL", 9), 0x90: ("YM3812 OPL2", 9),
    0x91: ("YMF262 OPL3", 18), 0x92: ("MultiPCM", 28), 0x93: ("PC Speaker", 1),
    0x94: ("POKEY", 4), 0x95: ("RF5C68", 8), 0x96: ("WonderSwan", 4),
    0x97: ("SAA1099", 6), 0x98: ("YM2414 OPZ", 8), 0x99: ("Pokémon Mini", 1),
    0x9A: ("AY8930", 3), 0x9B: ("SegaPCM", 16), 0x9C: ("Virtual Boy", 6),
    0x9D: ("VRC7", 6), 0x9E: ("YM2610B", 16), 0x9F: ("ZX Spectrum Beeper", 6),
    0xA0: ("YM2612 Extended", 9), 0xA1: ("SCC", 5), 0xA2: ("YM3526 OPL Drums", 11),
    0xA3: ("YM3812 OPL2 Drums", 11), 0xA4: ("YMF262 OPL3 Drums", 20),
    0xA5: ("Neo Geo YM2610", 14), 0xA6: ("Neo Geo YM2610 Extended", 17),
    0xA7: ("YM2413 OPLL Drums", 11), 0xA8: ("Atari Lynx", 4),
    0xAA: ("MSM6295", 4), 0xAB: ("MSM6258", 1), 0xAC: ("VERA", 17),
    0xAD: ("Bubble System WSG", 2), 0xAE: ("YMF278B OPL4", 42),
    0xAF: ("YMF278B OPL4 Drums", 44), 0xB0: ("X1-010", 16), 0xB1: ("ES5506", 32),
    0xB2: ("Y8950", 10), 0xB3: ("Y8950 Drums", 12), 0xB4: ("SCC+", 5),
    0xB5: ("Furnace Sound Unit", 8), 0xB6: ("YM2203 Extended", 9),
    0xB7: ("YM2608 Extended", 19), 0xB8: ("YMZ280B", 8), 0xB9: ("Namco WSG", 3),
    0xBA: ("Namco C15", 8), 0xBB: ("Namco C30", 8), 0xBC: ("MSM5232", 8),
    0xBD: ("YM2612 DualPCM Extended", 11), 0xBE: ("YM2612 DualPCM", 7),
    0xBF: ("T6W28", 4), 0xC0: ("PCM DAC", 1), 0xC1: ("YM2612 CSM", 10),
    0xC2: ("Neo Geo CSM", 18), 0xC3: ("YM2203 CSM", 10), 0xC4: ("YM2608 CSM", 20),
    0xC5: ("YM2610B CSM", 20), 0xC6: ("K007232", 2), 0xC7: ("GA20", 4),
    0xC8: ("SM8521", 3), 0xCA: ("ZX Spectrum QuadTone", 5), 0xCB: ("PV-1000", 3),
    0xCC: ("K053260", 4), 0xCD: ("TED", 2), 0xCE: ("C140", 24), 0xCF: ("C219", 16),
    0xD1: ("ESFM", 18), 0xD4: ("PowerNoise", 4), 0xD5: ("Dave", 6),
    0xD6: ("Nintendo DS", 16), 0xD7: ("GBA Direct Sound", 2), 0xD8: ("GBA MinMod", 16),
    0xD9: ("Bifurcator", 4), 0xE0: ("QSound", 19), 0xE2: ("C64 6581 PCM", 4),
    0xE3: ("Watara Supervision", 4), 0xE4: ("Namco Pole Position WSG", 8),
    0xE5: ("µPD1771C", 4), 0xE7: ("Klattsch", 1), 0xE9: ("SAA1099 Timers", 8),
    0xF0: ("SID2", 3), 0xF1: ("5E01", 5), 0xF5: ("SID3", 7), 0xFC: ("Pong", 1),
    0xFD: ("Dummy System", 8),
    # Legacy compound systems
    0x02: ("Genesis (YM2612 + SN76489)", 10),
    0x08: ("Arcade (YM2151 + SegaPCM)", 13),
    0x42: ("Genesis Extended (YM2612 + SN76489)", 13),
    0x43: ("SMS + OPLL", 13), 0x46: ("NES + VRC7", 11),
    0x09: ("Neo Geo CD", 13), 0x49: ("Neo Geo CD Extended", 16), 0xA9: ("SegaPCM Legacy", 5),
}


def _default_channel_names(system_id: int, count: int) -> List[str]:
    if system_id in (0x02, 0x42):
        return ["FM1", "FM2", "FM3", "FM4", "FM5", "FM6/DAC"] + ["PSG1", "PSG2", "PSG3"][:max(0, count-7)] + (["Noise"] if count > 10 else [])
    if system_id == 0x83:
        return ["FM1", "FM2", "FM3", "FM4", "FM5", "FM6/DAC"][:count]
    if system_id == 0xA0:
        return ["FM1", "FM2", "FM3", "FM4", "FM5", "FM6/DAC"] + [f"FM{i+1}" for i in range(6, count)]
    if system_id in (0xBD, 0xBE):
        return ["FM1", "FM2", "FM3", "FM4", "FM5", "FM6/DAC"] + [f"PCM{i+1}" for i in range(max(0, count-6))]
    if system_id == 0xC1:
        return [f"FM/CSM {i+1}" for i in range(count)]
    if system_id == 0x03 or system_id == 0xBF:
        return ["PSG1", "PSG2", "PSG3", "Noise"][:count]
    if system_id == 0x82:
        return [f"FM{i+1}" for i in range(count)]
    if system_id == 0x04:
        return ["Square 1", "Square 2", "Wave", "Noise"][:count]
    if system_id in (0x05,):
        return ["Wave 1", "Wave 2", "Wave 3", "Wave 4", "Wave 5", "Noise"][:count]
    if system_id in (0x06, 0x8B):
        names = ["Pulse 1", "Pulse 2", "Triangle", "Noise", "DPCM"]
        return names[:count]
    if system_id in (0x80, 0x9A):
        return [f"Tone {i+1}" for i in range(count)]
    if system_id in (0x87,):
        return [f"Voice {i+1}" for i in range(count)]
    if system_id in (0x81,):
        return [f"Sample {i+1}" for i in range(count)]
    label = SYSTEMS.get(system_id, (f"Chip {system_id:02X}", count))[0]
    return [f"{label} {i+1}" for i in range(count)]


class FurFormatError(ValueError):
    """Raised when a .fur file is malformed or unsupported."""


class Reader:
    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def seek(self, pos: int) -> None:
        if pos < 0 or pos > len(self.data):
            raise FurFormatError(f"Offset outside file: 0x{pos:X}")
        self.pos = pos

    def read(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise FurFormatError(f"Unexpected end of file at 0x{self.pos:X} (wanted {n} bytes)")
        out = self.data[self.pos:self.pos+n]
        self.pos += n
        return out

    def u8(self) -> int: return self.read(1)[0]
    def i8(self) -> int: return struct.unpack("<b", self.read(1))[0]
    def u16(self) -> int: return struct.unpack("<H", self.read(2))[0]
    def i16(self) -> int: return struct.unpack("<h", self.read(2))[0]
    def u32(self) -> int: return struct.unpack("<I", self.read(4))[0]
    def i32(self) -> int: return struct.unpack("<i", self.read(4))[0]
    def f32(self) -> float: return struct.unpack("<f", self.read(4))[0]

    def cstring(self) -> str:
        end = self.data.find(b"\0", self.pos)
        if end < 0:
            raise FurFormatError(f"Unterminated string at 0x{self.pos:X}")
        raw = self.data[self.pos:end]
        self.pos = end + 1
        return raw.decode("utf-8", "replace")


@dataclass
class Cell:
    note: Optional[int] = None
    instrument: Optional[int] = None
    volume: Optional[int] = None
    raw_frequency: Optional[int] = None
    effects: List[Tuple[int, int]] = field(default_factory=list)


@dataclass
class Instrument:
    index: int
    name: str
    ins_type: int = 0
    gm_program: int = 80


@dataclass
class Pattern:
    subsong: int
    channel: int
    index: int
    name: str = ""
    rows: Dict[int, Cell] = field(default_factory=dict)


@dataclass
class SubSong:
    name: str = ""
    pattern_length: int = 64
    orders_length: int = 1
    highlight_a: int = 4
    highlight_b: int = 16
    hz: float = 60.0
    speed1: int = 6
    speed2: int = 6
    speed_pattern: List[int] = field(default_factory=list)
    virtual_tempo_n: int = 1
    virtual_tempo_d: int = 1
    orders: List[List[int]] = field(default_factory=list)  # channel -> order index
    effect_columns: List[int] = field(default_factory=list)
    channel_names: List[str] = field(default_factory=list)
    patterns: Dict[Tuple[int, int], Pattern] = field(default_factory=dict)  # (channel, pattern index)
    grooves: List[List[int]] = field(default_factory=list)


@dataclass
class FurSong:
    version: int
    name: str = ""
    author: str = ""
    system_name: str = ""
    master_volume: float = 1.0
    systems: List[Tuple[int, str, int, float, float, float]] = field(default_factory=list)
    instruments: List[Instrument] = field(default_factory=list)
    subsong: SubSong = field(default_factory=SubSong)
    warnings: List[str] = field(default_factory=list)

    @property
    def channel_count(self) -> int:
        return sum(s[2] for s in self.systems)

    def channel_meta(self) -> List[dict]:
        out = []
        global_index = 0
        for sid, label, count, volume, pan, _front in self.systems:
            defaults = _default_channel_names(sid, count)
            for i in range(count):
                explicit = self.subsong.channel_names[global_index] if global_index < len(self.subsong.channel_names) else ""
                ch_name = explicit.strip() or defaults[i]
                out.append({"index": global_index, "system_id": sid, "system": label,
                            "chip_channel": i, "name": ch_name,
                            "volume": volume, "pan": pan})
                global_index += 1
        return out


def _decompress_fur(raw: bytes) -> bytes:
    if raw.startswith(MAGIC):
        return raw
    try:
        data = zlib.decompress(raw)
    except zlib.error as exc:
        raise FurFormatError(f"Not a valid Furnace module or zlib stream: {exc}") from exc
    if not data.startswith(MAGIC):
        raise FurFormatError("The decompressed file does not contain the Furnace module signature")
    return data


def _read_pointer_list(r: Reader, count: int) -> List[int]:
    if count < 0 or count > 100000:
        raise FurFormatError(f"Implausible pointer count: {count}")
    return [r.u32() for _ in range(count)]


def _parse_old_info(data: bytes, info_pos: int, version: int) -> Tuple[FurSong, List[int], List[int]]:
    r = Reader(data, info_pos)
    if r.read(4) != b"INFO":
        raise FurFormatError("Invalid INFO header (legacy Furnace song header)")
    r.u32()  # block size
    time_base = r.u8()
    s1, s2, _arp = r.u8(), r.u8(), r.u8()
    hz = r.f32()
    pat_len, orders_len = r.u16(), r.u16()
    hilight_a, hilight_b = r.u8(), r.u8()
    ins_count, wave_count, sample_count = r.u16(), r.u16(), r.u16()
    pattern_count = r.u32()
    if pat_len < 1 or pat_len > 256 or orders_len < 1 or orders_len > 256:
        raise FurFormatError(f"Unsupported pattern/order dimensions: {pat_len} rows, {orders_len} orders")
    if ins_count > 256 or pattern_count > 100000:
        raise FurFormatError("Implausible instrument or pattern count")

    system_ids = list(r.read(32))
    old_volumes = [r.i8() / 64.0 for _ in range(32)]
    old_pans = [r.i8() / 127.0 for _ in range(32)]
    sys_flags = _read_pointer_list(r, 32)

    name, author = r.cstring(), r.cstring()
    tuning = r.f32() if version >= 33 else (r.u32() and 440.0)
    if not (20.0 <= tuning <= 2000.0):
        tuning = 440.0

    if version >= 37:
        r.read(20)  # legacy compatibility flags; values do not affect MIDI event extraction
    else:
        r.read(20)

    instrument_ptrs = _read_pointer_list(r, ins_count)
    if wave_count: r.read(4 * wave_count)
    if sample_count: r.read(4 * sample_count)
    pattern_ptrs = _read_pointer_list(r, pattern_count)

    # Expand legacy compound chip IDs the same way the Furnace loader does.
    systems: List[Tuple[int, str, int, float, float, float]] = []
    for idx, sid in enumerate(system_ids):
        if sid == 0:
            continue
        if sid not in SYSTEMS:
            raise FurFormatError(f"Unsupported Furnace system ID 0x{sid:02X}")
        label, count = SYSTEMS[sid]
        volume = old_volumes[idx]
        pan = old_pans[idx]
        if sid in (0x02, 0x42):
            if sid == 0x02:
                systems.append((0x83, "YM2612", 6, volume, pan, 0.0))
                systems.append((0x03, "SN76489", 4, volume * 0.375, pan, 0.0))
            else:
                systems.append((0xA0, "YM2612 Extended", 9, volume, pan, 0.0))
                systems.append((0x03, "SN76489", 4, volume * 0.375, pan, 0.0))
        elif sid == 0x08:
            systems.append((0x82, "YM2151", 8, volume, pan, 0.0))
            systems.append((0x9B, "SegaPCM", 5, volume, pan, 0.0))
        elif sid == 0x09:
            systems.append((0xA5, "Neo Geo YM2610", count, volume, pan, 0.0))
        elif sid == 0x49:
            systems.append((0xA6, "Neo Geo YM2610 Extended", count, volume, pan, 0.0))
        elif sid == 0xA9:
            systems.append((0x9B, "SegaPCM", count, volume, pan, 0.0))
        elif sid == 0x43:
            systems.append((0x03, "SN76489", 4, volume, pan, 0.0))
            systems.append((0x89, "YM2413 OPLL", 9, volume, pan, 0.0))
        elif sid == 0x46:
            systems.append((0x06, "NES", 5, volume, pan, 0.0))
            systems.append((0x9D, "VRC7", 6, volume, pan, 0.0))
        else:
            systems.append((sid, label, count, volume, pan, 0.0))

    total_channels = sum(s[2] for s in systems)
    if total_channels < 1 or total_channels > 128:
        raise FurFormatError(f"Invalid channel count: {total_channels}")

    # Orders are stored as channel-major rows.
    orders = [[r.u8() for _ in range(orders_len)] for _ in range(total_channels)]
    effect_columns = [r.u8() for _ in range(total_channels)]
    if any(c > 8 for c in effect_columns):
        raise FurFormatError("Unreasonably large effect-column count")
    channel_names = [""] * total_channels
    if version >= 39:
        r.read(total_channels)  # visibility
        r.read(total_channels)  # collapsed state
        channel_names = [r.cstring() for _ in range(total_channels)]
        r.read(total_channels)  # short names
        _song_notes = r.cstring()

    master_volume = r.f32() if version >= 59 else 2.0

    # Compatibility flags added after format 0.6pre0. The count is 28 bytes for v232.
    if version >= 70:
        ext_count = 28
        r.read(ext_count)

    if version >= 96:
        vt_n, vt_d = r.u16(), r.u16()
    else:
        r.read(4)
        vt_n, vt_d = 1, 1

    subsong_name = ""
    extra_subsong_ptrs: List[int] = []
    if version >= 95:
        subsong_name = r.cstring()
        r.cstring()  # subsong comment
        additional_subsongs = r.u8()
        r.read(3)
        extra_subsong_ptrs = _read_pointer_list(r, additional_subsongs)

    system_name = ""
    if version >= 103:
        system_name = r.cstring()
        r.cstring()  # category
        for _ in range(4): r.cstring()

    if version >= 135:
        updated = []
        for sid, label, count, _vol, _pan, _fr in systems:
            vol, pan, front = r.f32(), r.f32(), r.f32()
            updated.append((sid, label, count, vol, pan, front))
        systems = updated
        conn_count = r.u32()
        if conn_count > 100000: raise FurFormatError("Implausible patchbay connection count")
        r.read(conn_count * 4)
    if version >= 136:
        r.u8()  # automatic patchbay
    if version >= 138:
        r.read(8)  # newer compatibility flags

    speed_pattern: List[int] = []
    grooves: List[List[int]] = []
    if version >= 139:
        speed_len = r.u8()
        speeds = [r.u8() for _ in range(16)]
        if speed_len < 1 or speed_len > 16:
            speed_len = 1
        speed_pattern = [max(1, x * (time_base + 1)) for x in speeds[:speed_len]]
        groove_count = r.u8()
        if groove_count > 128: raise FurFormatError("Implausible groove count")
        for _ in range(groove_count):
            glen = r.u8()
            vals = [r.u8() for _ in range(16)]
            grooves.append(vals[:max(1, min(glen, 16))])
    else:
        # Before format 139 Furnace alternates between speed 1 and speed 2.
        speed_pattern = []

    if version >= 156:
        r.read(12)  # three asset-directory pointers

    sub = SubSong(name=subsong_name, pattern_length=pat_len, orders_length=orders_len,
                  highlight_a=max(1, hilight_a or 4), highlight_b=max(1, hilight_b or 16),
                  hz=hz if 1.0 <= hz <= 1000.0 else 60.0,
                  speed1=max(1, s1 * (time_base + 1)), speed2=max(1, s2 * (time_base + 1)),
                  speed_pattern=speed_pattern, virtual_tempo_n=max(1, vt_n),
                  virtual_tempo_d=max(1, vt_d), orders=orders,
                  effect_columns=effect_columns, channel_names=channel_names, grooves=grooves)
    song = FurSong(version=version, name=name, author=author, system_name=system_name,
                   master_volume=max(0.0, min(4.0, master_volume)), systems=systems, subsong=sub)
    if extra_subsong_ptrs:
        song.warnings.append(f"This module contains {len(extra_subsong_ptrs)} additional subsong(s); fur2mid exports the first subsong.")
    return song, instrument_ptrs, pattern_ptrs


def _parse_new_info(data: bytes, info_pos: int, version: int) -> Tuple[FurSong, List[int], List[int], List[int]]:
    r = Reader(data, info_pos)
    if r.read(4) != b"INF2":
        raise FurFormatError("Invalid INF2 header")
    r.u32()  # block size
    name, author = r.cstring(), r.cstring()
    system_name, _category = r.cstring(), r.cstring()
    for _ in range(4): r.cstring()  # Japanese metadata
    _tuning = r.f32()
    _auto_system = r.u8()
    master_volume = r.f32()
    channel_count, chip_count = r.u16(), r.u16()
    if channel_count < 1 or channel_count > 128 or chip_count < 1 or chip_count > 32:
        raise FurFormatError("Invalid INF2 channel/chip counts")
    systems = []
    total = 0
    for _ in range(chip_count):
        sid, ch_count = r.u16(), r.u16()
        vol, pan, front = r.f32(), r.f32(), r.f32()
        if sid not in SYSTEMS:
            label = f"Chip {sid:04X}"
        else:
            label = SYSTEMS[sid][0]
        systems.append((sid, label, ch_count, vol, pan, front))
        total += ch_count
    if total != channel_count:
        raise FurFormatError("INF2 total chip channels do not match the declared channel count")
    connections = r.u32()
    if connections > 100000: raise FurFormatError("Implausible patchbay connection count")
    r.read(connections * 4)
    r.u8()  # automatic patchbay

    element_ptrs: Dict[int, List[int]] = {}
    for _ in range(32):
        element_type = r.u8()
        if element_type == 0:
            break
        count = r.u32()
        element_ptrs[element_type] = _read_pointer_list(r, count)
    sub_ptrs = element_ptrs.get(1, [])
    instrument_ptrs = element_ptrs.get(4, [])
    pattern_ptrs = element_ptrs.get(7, [])
    if not sub_ptrs:
        raise FurFormatError("INF2 module does not contain a subsong")

    sr = Reader(data, sub_ptrs[0])
    if sr.read(4) != b"SNG2":
        raise FurFormatError("Invalid SNG2 subsong block")
    sr.u32()
    hz = sr.f32()
    _arp_speed, _effect_divider = sr.u8(), sr.u8()
    pattern_length, orders_length = sr.u16(), sr.u16()
    hilight_a, hilight_b = sr.u8(), sr.u8()
    vt_n, vt_d = max(1, sr.u16()), max(1, sr.u16())
    speed_len = sr.u8()
    speeds = [sr.i16() for _ in range(16)]
    if speed_len < 1 or speed_len > 16: speed_len = 1
    sub_name, _comment = sr.cstring(), sr.cstring()
    if pattern_length < 1 or pattern_length > 256 or orders_length < 1 or orders_length > 256:
        raise FurFormatError("Invalid SNG2 dimensions")
    orders = [[sr.u8() for _ in range(orders_length)] for _ in range(channel_count)]
    effect_cols = [sr.u8() for _ in range(channel_count)]
    sr.read(channel_count)  # hidden
    sr.read(channel_count)  # collapsed
    channel_names = [sr.cstring() for _ in range(channel_count)]
    sr.read(channel_count)  # short names
    # channel colors are 4 bytes each, used only for UI

    grooves: List[List[int]] = []
    for groove_ptr in element_ptrs.get(10, []):
        gr = Reader(data, groove_ptr)
        if gr.read(4) != b"GROV":
            raise FurFormatError(f"Invalid GROV block at 0x{groove_ptr:X}")
        gr.u32()  # block size
        groove_len = gr.u8()
        if groove_len < 1 or groove_len > 16:
            raise FurFormatError(f"Invalid GROV length: {groove_len}")
        groove_values = [gr.i16() for _ in range(16)]
        grooves.append([max(1, int(x)) for x in groove_values[:groove_len]])

    sub = SubSong(name=sub_name, pattern_length=pattern_length, orders_length=orders_length,
                  highlight_a=max(1, hilight_a or 4), highlight_b=max(1, hilight_b or 16),
                  hz=hz if 1.0 <= hz <= 1000.0 else 60.0,
                  speed_pattern=[max(1, int(x)) for x in speeds[:speed_len]],
                  virtual_tempo_n=vt_n, virtual_tempo_d=vt_d, orders=orders,
                  effect_columns=effect_cols, channel_names=channel_names, grooves=grooves)
    song = FurSong(version=version, name=name, author=author, system_name=system_name,
                   master_volume=max(0.0, min(4.0, master_volume)), systems=systems, subsong=sub)
    if len(sub_ptrs) > 1:
        song.warnings.append(f"This module contains {len(sub_ptrs)-1} additional subsong(s); fur2mid exports the first subsong.")
    return song, instrument_ptrs, pattern_ptrs, sub_ptrs


def _parse_instrument(data: bytes, ptr: int, index: int, version: int) -> Instrument:
    if ptr <= 0 or ptr + 12 > len(data):
        return Instrument(index, f"Instrument {index+1}")
    r = Reader(data, ptr)
    magic = r.read(4)
    block_size = r.u32()
    if magic not in (b"INS2", b"INST"):
        return Instrument(index, f"Instrument {index+1}")
    block_end = min(len(data), ptr + 8 + block_size)
    if magic == b"INS2":
        _format_version = r.u16()
        ins_type = r.u16()
        name = ""
        while r.pos + 4 <= block_end:
            code = r.read(2)
            size = r.u16()
            payload = r.read(size)
            if code == b"NA":
                name = payload.rstrip(b"\0").decode("utf-8", "replace")
            if code == b"EN":
                break
        return Instrument(index, name or f"Instrument {index+1}", ins_type)
    # Legacy INST instruments are uncommon in current modules. Read the version/type header
    # and keep a safe generated name rather than guessing at a version-specific structure.
    if r.pos + 3 <= block_end:
        _format_version = r.u16()
        ins_type = r.u8()
    else:
        ins_type = 0
    return Instrument(index, f"Instrument {index+1}", ins_type)


def _parse_pattern(data: bytes, ptr: int, version: int, default_subsong: int = 0, effect_columns=1, pattern_length: int = 256) -> Pattern:
    r = Reader(data, ptr)
    magic = r.read(4)
    block_size = r.u32()
    if magic == b"PATN":
        subsong = r.u8()
        channel = r.u16() if version >= 240 else r.u8()
        index = r.u16()
        pattern = Pattern(subsong, channel, index, r.cstring())
        end_pos = min(len(data), ptr + 8 + block_size)
        row = 0
        while row < 256 and r.pos < end_pos:
            mask = r.u8()
            if mask == 0xFF:
                break
            if mask & 0x80:
                row += (mask & 0x7F) + 2
                continue
            effect_mask = 0
            if mask & 0x20:
                effect_mask |= r.u8()
            if mask & 0x40:
                effect_mask |= r.u8() << 8
            if mask & 0x08: effect_mask |= 1
            if mask & 0x10: effect_mask |= 2
            cell = Cell()
            if mask & 0x01:
                note = r.u8()
                if note == 180: cell.note = 180
                elif note == 181: cell.note = 181
                elif note == 182: cell.note = 182
                elif note == 183:
                    # Preserve the raw chip frequency for a readable MIDI marker.
                    cell.raw_frequency = r.u32()
                    cell.note = None
                elif note < 180: cell.note = note
            if mask & 0x02: cell.instrument = r.u8()
            if mask & 0x04: cell.volume = r.u8()
            effects: List[Tuple[int, int]] = []
            for fx_index in range(8):
                code = None
                value = None
                if effect_mask & (1 << (fx_index * 2)):
                    code = r.u8()
                if effect_mask & (1 << (fx_index * 2 + 1)):
                    value = r.u8()
                if code is not None:
                    effects.append((code, value if value is not None else 0))
            cell.effects = effects
            if (cell.note is not None or cell.instrument is not None or cell.volume is not None
                    or cell.raw_frequency is not None or effects):
                pattern.rows[row] = cell
            row += 1
        return pattern

    if magic == b"PATR":
        channel, index = r.u16(), r.u16()
        subsong = r.u16() if version >= 95 else default_subsong
        r.u16()  # reserved
        # Legacy pattern payload: fixed-size rows of note/octave/ins/vol/effect pairs.
        pattern = Pattern(subsong, channel, index, "")
        channel_fx_cols = effect_columns[channel] if isinstance(effect_columns, list) and channel < len(effect_columns) else effect_columns
        for row in range(min(256, pattern_length)):
            note, octave = r.i16(), r.i16()
            ins, vol = r.i16(), r.i16()
            effects = []
            for _ in range(max(1, min(8, int(channel_fx_cols)))):
                fx, val = r.i16(), r.i16()
                if fx >= 0: effects.append((fx & 0xFF, val & 0xFF if val >= 0 else 0))
            cell = Cell()
            if not (note == 0 and octave == 0):
                if note == 0 and octave != 0:
                    note, octave = 12, octave - 1
                cell.note = ((octave + 5) * 12 + (note % 12))
            if ins >= 0: cell.instrument = ins & 0xFF
            if vol >= 0: cell.volume = vol & 0xFF
            cell.effects = effects
            if cell.note is not None or cell.instrument is not None or cell.volume is not None or effects:
                pattern.rows[row] = cell
        return pattern

    raise FurFormatError(f"Unknown pattern block signature {magic!r} at 0x{ptr:X}")


def load_fur(path: Path) -> FurSong:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise FurFormatError(f"Could not read {path}: {exc}") from exc
    data = _decompress_fur(raw)
    if len(data) < 32 or data[:16] != MAGIC:
        raise FurFormatError("Invalid Furnace module header")
    hr = Reader(data, 16)
    version = hr.u16()
    hr.u16()  # reserved
    info_pos = hr.u32()
    if info_pos <= 0 or info_pos >= len(data):
        raise FurFormatError("Invalid song-info offset in module header")

    if version >= 240:
        song, ins_ptrs, pat_ptrs, _subsong_ptrs = _parse_new_info(data, info_pos, version)
    else:
        song, ins_ptrs, pat_ptrs = _parse_old_info(data, info_pos, version)

    song.instruments = [_parse_instrument(data, ptr, i, version) for i, ptr in enumerate(ins_ptrs)]
    for ptr in pat_ptrs:
        # Effect-column count is channel-specific in the INFO/SNG2 table.
        pat = _parse_pattern(data, ptr, version, effect_columns=song.subsong.effect_columns or 1,
                             pattern_length=song.subsong.pattern_length)
        if pat.subsong == 0:
            song.subsong.patterns[(pat.channel, pat.index)] = pat
    if not song.subsong.patterns:
        song.warnings.append("No pattern data was found in the first subsong.")
    return song


