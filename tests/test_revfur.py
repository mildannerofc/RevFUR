import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

import furparser
from revfur_core import (export_sequence, apply_sequence, inspect_fur, _encode_patn,
                         reverse_entire_sequence, analyze_playback,
                         count_leading_navigation_ticks, count_isolated_empty_ticks,
                         find_isolated_empty_ticks)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "options music.fur"


def _cstring(value):
    return value.encode("utf-8") + b"\0"


def make_inf2_fixture(path: Path, loop=True, stop=False, pattern_break=False):
    """Create a small valid-layout v240 INF2/SNG2 module for regression tests."""
    flow = [(0x0B, 0x00)] if loop else ([(0xFF, 0x00)] if stop else ([(0x0D, 0x01)] if pattern_break else []))
    rows = {0: furparser.Cell(note=60, instrument=0, volume=100, effects=flow),
            2: furparser.Cell(note=62, effects=[(0x04, 0x23)])}
    pattern = _encode_patn(0, 0, 0, "Test Pattern", rows, 4, 240)

    ins_payload = b"NA" + struct.pack("<H", 8) + b"TestIns\0" + b"EN\0\0"
    ins = b"INS2" + struct.pack("<IHH", 4 + len(ins_payload), 1, 0) + ins_payload
    wave_payload = _cstring("TestWave") + struct.pack("<III", 4, 0, 1) + struct.pack("<4i", 0, 1, 2, 3)
    wave = b"WAVE" + struct.pack("<I", len(wave_payload)) + wave_payload
    sample_payload = (_cstring("TestSample") + struct.pack("<III", 4, 22050, 22050)
                      + bytes((8, 0, 0, 0)) + struct.pack("<ii", -1, -1)
                      + bytes(16) + bytes((0, 64, 128, 255)))
    sample = b"SMP2" + struct.pack("<I", len(sample_payload)) + sample_payload

    sng_body = bytearray()
    sng_body.extend(struct.pack("<fBBHHBBHHB", 60.0, 1, 0, 4, 2, 4, 16, 1, 1, 1))
    sng_body.extend(struct.pack("<16h", 6, *([0] * 15)))
    sng_body.extend(_cstring("Test Subsong"))
    sng_body.extend(_cstring(""))
    sng_body.extend(bytes((0, 0)))  # channel 0 orders
    sng_body.extend(bytes((1,)))  # effect columns
    sng_body.extend(bytes((0,)))  # hidden
    sng_body.extend(bytes((0,)))  # collapsed
    sng_body.extend(_cstring("PSG1"))
    sng_body.extend(_cstring("PSG1"))
    sng_body.extend(struct.pack("<I", 0))  # channel color
    sng = b"SNG2" + struct.pack("<I", len(sng_body)) + bytes(sng_body)

    # INF2 is fixed-size once the pointer tables are laid out. Store pointer
    # offsets so the blocks can be appended and then patched.
    info = bytearray(b"INF2\0\0\0\0")
    for item in ("Test song", "Test author", "SN76489", "", "", "", "", ""):
        info.extend(_cstring(item))
    info.extend(struct.pack("<fBfHH", 440.0, 0, 1.0, 1, 1))
    info.extend(struct.pack("<HHfff", 0x03, 1, 1.0, 0.0, 0.0))
    info.extend(struct.pack("<I", 0))  # no patchbay connections
    info.append(0)  # automatic patchbay
    pointer_fields = {}
    for element_type, label in ((1, "sub"), (4, "ins"), (5, "wave"), (6, "sample"), (7, "pattern")):
        info.append(element_type)
        info.extend(struct.pack("<I", 1))
        pointer_fields[label] = len(info)
        info.extend(struct.pack("<I", 0))
    info.append(0)  # end element list
    struct.pack_into("<I", info, 4, len(info) - 8)

    data = bytearray(b"-Furnace module-" + struct.pack("<HHI", 240, 0, 32) + bytes(8))
    info_offset = len(data)
    data.extend(info)
    sub_ptr = len(data); data.extend(sng)
    ins_ptr = len(data); data.extend(ins)
    wave_ptr = len(data); data.extend(wave)
    sample_ptr = len(data); data.extend(sample)
    pat_ptr = len(data); data.extend(pattern)
    for label, ptr in (("sub", sub_ptr), ("ins", ins_ptr), ("wave", wave_ptr),
                       ("sample", sample_ptr), ("pattern", pat_ptr)):
        struct.pack_into("<I", data, info_offset + pointer_fields[label], ptr)
    path.write_bytes(data)


def make_patr_legacy_fixture(path: Path, flow_effect=True):
    """Create a minimal legacy INFO/PATR format-v156 module."""
    pat_len, orders_len, channels = 4, 2, 4
    info = bytearray(b"INFO\0\0\0\0")
    info.extend(bytes((0, 6, 6, 1)))
    info.extend(struct.pack("<fHHBBHHHI", 60.0, pat_len, orders_len, 4, 16, 0, 0, 0, 1))
    systems = bytes((0x03,)) + bytes(31)
    info.extend(systems)
    info.extend(bytes((64,)) + bytes(31))  # old system volumes
    info.extend(bytes(32))  # old system panning
    info.extend(bytes(32 * 4))  # old system flag pointers
    info.extend(_cstring("Legacy PATR"))
    info.extend(_cstring("RevFUR test"))
    info.extend(struct.pack("<f", 440.0))
    info.extend(bytes(20))  # compatibility flags
    # No instruments, wavetables or samples; one PATR pattern pointer.
    pattern_pointer_field = len(info)
    info.extend(struct.pack("<I", 0))
    # Orders: four channels, two synchronized order positions.
    info.extend(bytes((0, 1)) * channels)
    info.extend(bytes((1,)) * channels)  # effect columns
    info.extend(bytes(channels))  # visibility
    info.extend(bytes(channels))  # collapsed
    for name in ("PSG1", "PSG2", "PSG3", "Noise"):
        info.extend(_cstring(name))
    for name in ("P1", "P2", "P3", "N"):
        info.extend(_cstring(name))
    info.extend(_cstring(""))  # song notes
    info.extend(struct.pack("<f", 1.0))  # master volume
    info.extend(bytes(28))  # extended compatibility flags
    info.extend(struct.pack("<HH", 1, 1))  # virtual tempo
    info.extend(_cstring("Legacy Subsong"))
    info.extend(_cstring(""))
    info.extend(bytes((0, 0, 0, 0)))  # additional subsongs and reserved
    # six metadata strings (>=103)
    for _ in range(6):
        info.extend(_cstring(""))
    info.extend(struct.pack("<fff", 1.0, 0.0, 0.0))  # chip output
    info.extend(struct.pack("<I", 0))  # patchbay connection count
    info.append(0)  # automatic patchbay
    info.extend(bytes(8))  # compatibility flags added at 138
    info.append(1)  # speed pattern length
    info.extend(bytes((6,)) + bytes(15))
    info.append(0)  # groove count
    info.extend(bytes(12))  # asset directories
    struct.pack_into("<I", info, 4, len(info) - 8)

    # Legacy PATR: channel 0, pattern 0, subsong 0, fixed row records.
    pat = bytearray(b"PATR\0\0\0\0")
    pat.extend(struct.pack("<HHHH", 0, 0, 0, 0))
    for row in range(pat_len):
        if row == 0:
            pat.extend(struct.pack("<hhhh", 4, 0, -1, 100))
            pat.extend(struct.pack("<hh", 0x0B if flow_effect else -1, 0x0123 if flow_effect else -1))
        else:
            pat.extend(struct.pack("<hhhh", 0, 0, -1, -1))
            pat.extend(struct.pack("<hh", -1, -1))
    pat.extend(_cstring("PATR Pattern"))
    struct.pack_into("<I", pat, 4, len(pat) - 8)

    data = bytearray(b"-Furnace module-" + struct.pack("<HHI", 156, 0, 32) + bytes(8))
    info_offset = len(data)
    data.extend(info)
    pat_ptr = len(data)
    data.extend(pat)
    struct.pack_into("<I", data, info_offset + pattern_pointer_field, pat_ptr)
    path.write_bytes(zlib.compress(bytes(data), 9))


class RevFURTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_legacy_export_import_noop_preserves_decompressed_bytes(self):
        module = self.work / "source.fur"
        module.write_bytes(EXAMPLE.read_bytes())
        sequence, dump = export_sequence(module, self.work / "sequence.furseq.json")
        output = self.work / "roundtrip.fur"
        report = apply_sequence(module, sequence, output)
        self.assertEqual(zlib.decompress(module.read_bytes()), zlib.decompress(output.read_bytes()))
        self.assertTrue(dump.exists())
        self.assertTrue(report["instruments_unchanged"])
        self.assertEqual(report["instrument_blocks_checked"], 14)
        self.assertEqual(report["patterns_replaced"], 0)

    def test_legacy_edit_changes_only_sequence_and_roundtrips(self):
        module = self.work / "source.fur"
        module.write_bytes(EXAMPLE.read_bytes())
        sequence, _ = export_sequence(module, self.work / "sequence.furseq.json")
        doc = json.loads(sequence.read_text(encoding="utf-8"))
        pattern = next(p for p in doc["sequence"]["patterns"] if p["channel"] == 0 and p["index"] == 0)
        pattern["rows"][0]["note"] += 1
        pattern["rows"][0]["effects"] = [{"command": "0B", "value": "00"}]
        doc["sequence"]["orders"][0][1] = 0
        edited = self.work / "edited.furseq.json"
        edited.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        output = self.work / "edited.fur"
        report = apply_sequence(module, edited, output)
        song = furparser.load_fur(output)
        self.assertEqual(song.subsong.orders[0], [0, 0])
        self.assertEqual(song.subsong.patterns[(0, 0)].rows[0].note, pattern["rows"][0]["note"])
        self.assertIn((0x0B, 0), song.subsong.patterns[(0, 0)].rows[0].effects)
        self.assertEqual(report["patterns_replaced"], 1)
        self.assertTrue(report["samples_unchanged"])
        self.assertTrue(report["wavetables_unchanged"])

    def test_reverse_materialises_sparse_instrument_state_without_touching_asset(self):
        module = self.work / "sparse_instrument.fur"
        make_inf2_fixture(module, loop=False)
        output = self.work / "sparse_instrument_reversed.fur"
        report = reverse_entire_sequence(module, output)
        song = furparser.load_fur(output)
        pattern = song.subsong.patterns[(0, 0)]

        # Source row 0 selects instrument 0; source row 2 only has a note.
        # In reverse playback the row-2 note comes first, so its instrument
        # reference must be materialised before that note is triggered.
        self.assertEqual(pattern.rows[1].note, 62)
        self.assertEqual(pattern.rows[1].instrument, 0)
        self.assertEqual(pattern.rows[1].volume, 100)
        self.assertEqual(pattern.rows[3].note, 60)
        self.assertEqual(pattern.rows[3].instrument, 0)
        self.assertEqual(pattern.rows[3].volume, 100)
        self.assertGreaterEqual(report["instrument_references_inserted"], 1)
        self.assertGreater(report["single_tick_empty_ticks_preserved_shared"], 0)
        self.assertFalse(report["single_tick_gap_compaction_complete"])
        self.assertTrue(report["instruments_unchanged"])
        self.assertTrue(report["samples_unchanged"])
        self.assertTrue(report["wavetables_unchanged"])

    def test_reverse_entire_sequence_reverses_all_channels_and_pattern_rows(self):
        module = self.work / "reverse_source.fur"
        make_patr_legacy_fixture(module, flow_effect=False)
        before = furparser.load_fur(module)
        original_note = before.subsong.patterns[(0, 0)].rows[0].note

        output = self.work / "reverse_output.fur"
        report = reverse_entire_sequence(module, output)
        after = furparser.load_fur(output)

        # All channel order tables are reversed in lockstep, not one channel
        # at a time and not merely exported as an unchanged disassembly.
        self.assertEqual(after.subsong.orders, [[1, 0]] * before.channel_count)
        # Row zero becomes the final row of the full four-row Furnace pattern.
        reversed_pattern = after.subsong.patterns[(0, 0)]
        self.assertNotIn(0, reversed_pattern.rows)
        self.assertEqual(reversed_pattern.rows[3].note, original_note)
        # The new reverse is finite: it strips source navigation and adds an
        # explicit FF00 at the exact end of the reversed reachable path.
        self.assertFalse(any(code == 0x0B for cell in reversed_pattern.rows.values()
                             for code, _value in cell.effects))
        self.assertEqual(report["navigation_effects_removed"]["0B"], 0)
        self.assertTrue(report["stop_marker_inserted"])
        self.assertEqual(report["output_playback_analysis"]["termination"], "explicit_stop")
        self.assertTrue(report["instruments_unchanged"])
        self.assertTrue(report["samples_unchanged"])
        self.assertTrue(report["wavetables_unchanged"])

    def test_playback_analysis_detects_0b_loop(self):
        module = self.work / "loop.fur"
        make_inf2_fixture(module, loop=True)
        layout = inspect_fur(module)
        analysis = analyze_playback(layout)
        self.assertEqual(analysis.termination, "loop")
        self.assertEqual(analysis.loop_start, (0, 0))
        self.assertEqual(analysis.flow_counts["0B"], 1)

    def test_playback_analysis_detects_explicit_stop_and_natural_end(self):
        stopped = self.work / "stopped.fur"
        make_inf2_fixture(stopped, loop=False, stop=True)
        stop_analysis = analyze_playback(inspect_fur(stopped))
        self.assertEqual(stop_analysis.termination, "explicit_stop")
        self.assertEqual(stop_analysis.end_state, (0, 0))

        natural = self.work / "natural.fur"
        make_inf2_fixture(natural, loop=False)
        natural_analysis = analyze_playback(inspect_fur(natural))
        self.assertEqual(natural_analysis.termination, "natural_end")
        self.assertEqual(natural_analysis.end_state, (1, 3))

    def test_playback_analysis_handles_0d_pattern_break(self):
        module = self.work / "break.fur"
        make_inf2_fixture(module, loop=False, pattern_break=True)
        analysis = analyze_playback(inspect_fur(module))
        self.assertEqual(analysis.termination, "natural_end")
        self.assertEqual(analysis.states[0], (0, 0))
        self.assertEqual(analysis.states[1], (1, 1))
        self.assertEqual(analysis.flow_counts["0D"], 1)

    def test_real_module_reverses_played_loop_pass_and_compacts_single_tick_gaps(self):
        module = ROOT / "examples" / "mcc attempt S3 drums_revfur.fur"
        if not module.exists():
            self.skipTest("Loop regression sample is not available in this checkout")
        source_analysis = analyze_playback(inspect_fur(module))
        self.assertEqual(source_analysis.termination, "loop")
        self.assertEqual(source_analysis.loop_start, (2, 0))
        self.assertEqual(source_analysis.loop_end, (4, 31))
        output = self.work / "sample_reversed.fur"
        report = reverse_entire_sequence(module, output)
        after = furparser.load_fur(output)
        self.assertEqual(report["playback_analysis"]["played_rows"], 280)
        self.assertEqual(report["output_playback_analysis"]["played_rows"], 215)
        self.assertEqual(report["single_tick_gaps_removed"], 65)
        output_analysis = analyze_playback(inspect_fur(output))
        self.assertEqual(count_isolated_empty_ticks(inspect_fur(output), output_analysis.states), 0)
        self.assertGreaterEqual(report["flow_markers_inserted"]["0D"], 2)
        self.assertEqual(report["output_playback_analysis"]["termination"], "explicit_stop")
        self.assertEqual(after.subsong.orders[0], [4, 3, 2, 1, 0])
        # The source's final played row (order 4, row 31) is only a 0B loop
        # command. The reversed arrangement begins at row 00 and compacts all
        # isolated one-tick gaps, shortening 280 playback rows to 215 without
        # losing notes. Longer silent runs are preserved.
        self.assertTrue(report["initial_navigation_tick_trimmed"])
        self.assertGreaterEqual(report["initial_navigation_tick_shift_rows"], 1)
        first_pattern = after.subsong.patterns[(0, after.subsong.orders[0][0])]
        first_note_row = min(row for row, cell in first_pattern.rows.items()
                             if cell.note is not None and 0 <= cell.note < 180)
        self.assertEqual(first_note_row, 0)
        source_song = furparser.load_fur(module)
        output_song = furparser.load_fur(output)
        for channel in range(source_song.channel_count):
            source_note_count = sum(
                1 for order, row in source_analysis.states
                for cell in [source_song.subsong.patterns.get(
                    (channel, source_song.subsong.orders[channel][order]), furparser.Pattern(0, channel, 0)
                ).rows.get(row)]
                if cell is not None and cell.note is not None and
                (0 <= cell.note < 180 or cell.note == 183)
            )
            output_note_count = sum(
                1 for order, row in output_analysis.states
                for cell in [output_song.subsong.patterns.get(
                    (channel, output_song.subsong.orders[channel][order]), furparser.Pattern(0, channel, 0)
                ).rows.get(row)]
                if cell is not None and cell.note is not None and
                (0 <= cell.note < 180 or cell.note == 183)
            )
            self.assertEqual(output_note_count, source_note_count, "channel %d note count changed" % channel)
        self.assertTrue(report["instruments_unchanged"])
        self.assertTrue(report["samples_unchanged"])

    def test_boundary_alignment_counts_multiple_navigation_only_ticks(self):
        patterns = {
            (0, 0): {"rows": [
                {"row": 2, "note": 60},
                {"row": 4, "effects": [{"command": "0B", "value": "00"}]},
            ]},
            (1, 0): {"rows": [
                {"row": 4, "effects": [{"command": "0D", "value": "00"}]},
            ]},
        }
        # Rows 4 (navigation only) and 3 (empty) are both removable from the
        # leading edge of the reversed arrangement. Row 2 contains a note and
        # stops the scan, so the fix cannot slide actual musical content away.
        self.assertEqual(
            count_leading_navigation_ticks(patterns, [[0], [0]], 2, 0, 0, 4), 2
        )
        patterns[(1, 0)]["rows"].append({"row": 3, "effects": [{"command": "0A", "value": "01"}]})
        self.assertEqual(
            count_leading_navigation_ticks(patterns, [[0], [0]], 2, 0, 0, 4), 1
        )

    def test_reverse_can_loop_back_to_song_start(self):
        module = ROOT / "examples" / "mcc attempt S3 drums_revfur.fur"
        if not module.exists():
            self.skipTest("Loop regression sample is not available in this checkout")
        output = self.work / "sample_loop_back.fur"
        report = reverse_entire_sequence(module, output, loop_back_to_start=True)
        output_analysis = analyze_playback(inspect_fur(output))
        self.assertTrue(report["loop_back_to_start"])
        self.assertTrue(report["loop_marker_inserted"])
        self.assertFalse(report["stop_marker_inserted"])
        self.assertEqual(output_analysis.termination, "loop")
        self.assertEqual(output_analysis.loop_start, (0, 0))
        self.assertEqual(output_analysis.flow_counts["0B"], 1)
        self.assertEqual(output_analysis.flow_counts["FF"], 0)
        self.assertEqual(report["navigation_effects_removed"]["0B"],
                         report["playback_analysis"]["flow_commands"]["0B"])
        self.assertEqual(output_analysis.states[0], (0, 0))
        self.assertEqual(len(output_analysis.states), report["output_playback_rows_expected"])
        self.assertEqual(count_isolated_empty_ticks(inspect_fur(output), output_analysis.states), 0)
        self.assertTrue(report["instruments_unchanged"])
        self.assertTrue(report["samples_unchanged"])
        self.assertTrue(report["wavetables_unchanged"])

    def test_single_empty_tick_compaction_only_removes_isolated_gaps(self):
        patterns = {
            (0, 0): {"rows": [
                {"row": 0, "note": 60},
                {"row": 2, "note": 62},
                {"row": 5, "note": 64},
                {"row": 7, "note": 65},
            ]}
        }
        states = [(0, row) for row in range(8)]
        # Row 1 and row 6 are isolated one-tick gaps. Rows 3-4 form a
        # two-tick rest, which is intentionally retained.
        self.assertEqual(
            find_isolated_empty_ticks(states, patterns, [[0]], 1),
            {(0, 1), (0, 6)},
        )

    def test_wrong_source_is_rejected(self):
        module = self.work / "source.fur"
        module.write_bytes(EXAMPLE.read_bytes())
        sequence, _ = export_sequence(module, self.work / "sequence.furseq.json")
        edited_module = self.work / "other.fur"
        altered = bytearray(zlib.decompress(module.read_bytes()))
        altered[-1] ^= 1
        edited_module.write_bytes(zlib.compress(bytes(altered)))
        with self.assertRaises(furparser.FurFormatError):
            apply_sequence(edited_module, sequence, self.work / "bad.fur")

    def test_new_inf2_module_and_asset_identity(self):
        module = self.work / "modern.fur"
        make_inf2_fixture(module)
        layout = inspect_fur(module)
        self.assertEqual(layout.version, 240)
        sequence, _ = export_sequence(module, self.work / "modern.furseq.json")
        doc = json.loads(sequence.read_text(encoding="utf-8"))
        doc["sequence"]["patterns"][0]["rows"][0]["note"] = 61
        sequence.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        output = self.work / "modern_edited.fur"
        report = apply_sequence(module, sequence, output)
        song = furparser.load_fur(output)
        self.assertEqual(song.subsong.patterns[(0, 0)].rows[0].note, 61)
        self.assertEqual(report["instrument_blocks_checked"], 1)
        self.assertEqual(report["wavetable_blocks_checked"], 1)
        self.assertEqual(report["sample_blocks_checked"], 1)
        self.assertTrue(report["instruments_unchanged"])
        self.assertTrue(report["wavetables_unchanged"])
        self.assertTrue(report["samples_unchanged"])

    def test_legacy_patr_roundtrip_preserves_16bit_effect_values(self):
        module = self.work / "legacy_patr.fur"
        make_patr_legacy_fixture(module)
        sequence, _ = export_sequence(module, self.work / "legacy_patr.furseq.json")
        doc = json.loads(sequence.read_text(encoding="utf-8"))
        pattern = next(p for p in doc["sequence"]["patterns"] if p["channel"] == 0 and p["index"] == 0)
        self.assertEqual(pattern["name"], "PATR Pattern")
        self.assertEqual(pattern["rows"][0]["effects"][0]["value"], "0123")
        pattern["rows"][0]["note"] = pattern["rows"][0]["note"] + 1
        sequence.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        output = self.work / "legacy_patr_edited.fur"
        report = apply_sequence(module, sequence, output)
        sequence2, _ = export_sequence(output, self.work / "legacy_patr_after.furseq.json")
        doc2 = json.loads(sequence2.read_text(encoding="utf-8"))
        pattern2 = next(p for p in doc2["sequence"]["patterns"] if p["channel"] == 0 and p["index"] == 0)
        self.assertEqual(pattern2["name"], "PATR Pattern")
        self.assertEqual(pattern2["rows"][0]["effects"][0]["value"], "0123")
        self.assertTrue(report["instruments_unchanged"])

    def test_text_dump_contains_channel_and_furnace_effect(self):
        module = self.work / "source.fur"
        make_inf2_fixture(module)
        sequence, dump = export_sequence(module, self.work / "sequence.furseq.json")
        text = dump.read_text(encoding="utf-8")
        self.assertIn("ORDER TABLE", text)
        self.assertIn("PATTERNS", text)
        self.assertIn("SN76489", text)
        self.assertIn("0B00", text)
        self.assertTrue(sequence.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
