# RevFUR

**RevFUR** is a Furnace Tracker sequence reverser, playback-flow analyzer, disassembler and sequence-only editor. It simulates the reachable playback path, detects order loops and the actual end of a pass, then reverses that path across all channels. It can also export the sequence to an editable `.furseq.json` document and apply edits back to the exact original module.

RevFUR does **not** convert instrument definitions, sample data, wavetables, chip settings, or other asset blocks. When applying an edited sequence, it patches the order table and appends replacement pattern blocks, then updates only the corresponding pattern pointers. Original instrument, sample, and wavetable blocks remain byte-for-byte unchanged in the decompressed Furnace data.

## Why RevFUR exists

The supplied `RevSMPS.exe` was inspected as a behavioral reference. It identifies itself as **Reverse SMPS - by MarkeyJester** and contains strings for drag-and-drop processing of Sonic 1 SMPS tracks, channel counts (DAC/FM/PSG), tempo parameters, and detected voices. Its executable symbol table also exposes routines named `ProcessFile`, `UnpackChannel`, and `DumpChannel`, consistent with a channel-stream disassembler.

RevFUR follows that useful workflow idea—inspect a binary music sequence, separate channels, and produce a readable editable dump—but implements it for Furnace's own module format. SMPS and Furnace use different sequence encodings, so RevFUR does not reuse SMPS opcodes or pretend that an SMPS stream is a `.fur` pattern.

## Features

- Reads compressed (zlib) and uncompressed Furnace modules.
- Supports the legacy `INFO` song layout and modern `INF2` / `SNG2` layout.
- Decodes legacy `PATR` and modern `PATN` pattern blocks.
- Simulates Furnace playback across all channels and detects `0Bxx` order jumps, `0Dxx` pattern breaks, `FFxx` explicit stops, repeated order/row states (loops), and natural order-table completion.
- Reads the Furnace compatibility flags `jumpTreatment` and `ignoreJumpAtEnd`, including legacy INFO flag bytes and modern CFLG blocks.
- Reverses only the reachable playback pass, including the introduction and one traversal of a detected infinite loop; unreachable tail orders are not blindly included.
- Handles partial pattern segments by inserting `0D00` at reversed segment boundaries. The final output can either stop with `FF00` or loop to order `00` / row `00` with a native `0B00` jump.
- Re-establishes stateful instrument and volume-column settings for reversed notes by inserting only missing values into sequence rows; the original instrument definitions remain byte-for-byte unchanged.
- Re-analyzes the generated `.fur` and verifies its selected ending: an explicit `FF00`, a natural order-table end, or a `0B00` loop back to the beginning.
- Exports the channel-major order table and every first-subsong pattern row for manual editing.
- Preserves Furnace note values, instrument references, volume values, and effect command/value pairs in the editable sequence document.
- Preserves `0Bxx` order jumps exactly as Furnace effect commands; it does not flatten or reinterpret them into a different tracker dialect.
- Handles legacy 16-bit `PATR` effect values as well as byte-sized `PATN` effects.
- Rebuilds changed patterns using the correct `PATN` or `PATR` layout for the source format version.
- Uses the correct legacy `PATR` empty-note sentinel `(note=0, octave=0)` to avoid generating phantom notes in silent rows.
- Requires the exact original `.fur` file when applying a sequence document, using a SHA-256 fingerprint to prevent accidental application to a different module.
- Performs byte-for-byte checks of all referenced instrument, wavetable, and sample blocks before writing the result.
- Creates a JSON preservation report next to the output `.fur` file.
- English Tkinter GUI with optional drag-and-drop and a console log panel below the controls.
- Command-line interface for scripted workflows.

## Quick start on Windows

1. Install Python **3.10 or newer** from [python.org](https://www.python.org/downloads/windows/).
2. Extract the RevFUR ZIP to a normal folder.
3. Double-click `run_windows.bat`.
4. Choose **Analyze playback loops and song end** to inspect flow, or **Reverse entire sequence** to create the reversed module. **Extract sequence from FUR** / **Apply edited sequence to FUR** remain available for manual editing.
5. Select or drop the module and press **Analyze Playback**, **Reverse Sequence**, or **Run conversion**.

The application can run without `tkinterdnd2`; in that case, use the Browse buttons. The Python standard Tkinter package must be present. It is included with the standard Windows Python installer; on Debian-based Linux systems it is usually provided by `python3-tk`.

## GUI workflow

### Reverse entire sequence (main operation)

Choose `Reverse entire sequence`, select a Furnace module and press `Reverse Sequence`. RevFUR writes a separate file named `*_reversed.fur` by default. The **Loop output back to the beginning** checkbox is enabled by default; clear it for a finite stop/natural end.

Before editing, RevFUR simulates the actual playback path from order 00, row 00. It evaluates global flow effects across channels in Furnace channel/effect order and honors the module's compatibility settings:

- `0Bxx`: jump to the specified order. A repeated `(order, row)` state identifies an indefinite loop and its start/end boundaries.
- `0Dxx`: jump to the next order at the specified row, subject to `ignoreJumpAtEnd` and `jumpTreatment`.
- `FFxx`: explicit song stop after the current row.
- Natural end: reaching the end of the order table without an earlier stop or loop.

When a loop is detected, RevFUR reverses one finite pass consisting of the introduction plus one traversal of the loop. An infinite loop has no finite complete reverse, so this is the deterministic editable representation. The tool identifies the loop automatically; you do not need to delete `0Bxx` commands manually.

The reversal is synchronized across every channel:

1. The reachable order/row segments are reversed together, including partial final orders and pattern breaks.
2. Rows within each reachable segment are mirrored and normalized to begin at row 00. Unplayed rows outside the detected source path are excluded from the reversed arrangement.
3. `0D00` is inserted between reversed segments that occupy only part of a Furnace pattern. At the end, RevFUR inserts `FF00` to stop, or `0B00` to loop back to order `00` / row `00` when the loop option is enabled. If the last full order already ends naturally, an extra `FF00` is unnecessary.
4. RevFUR removes isolated globally empty one-tick gaps throughout the reachable playback pass, synchronously across all channels. Multi-tick rests are retained, as are all rows containing notes, instrument/volume changes, raw frequencies, or non-navigation effects. The output becomes shorter by the number of removed one-tick gaps; the program does not move the silence elsewhere.
5. Pattern slots shared by multiple order positions are preserved when compacting them would affect another occurrence or make a flow marker fire early. The console and report identify any one-tick gaps left because a shared pattern cannot be safely compacted without cloning it.
6. Stateful instrument and volume-column values are evaluated along the original playback path. When reversal would leave a note with a different inherited state, RevFUR inserts the required value on that note row without editing the instrument/sample data.
7. The generated module is parsed and simulated again. The output length must match the compacted row map, the requested end/loop behavior must be correct, and no isolated one-tick empty gap may remain when the affected pattern slots can be compacted safely.

The **Loop output back to the beginning** option (enabled by default in the GUI) writes `0B00` at the final reversed row. Furnace then jumps to order `00`, row `00` and repeats the whole reversed arrangement indefinitely. This is a native unconditional Furnace loop; `0Bxx` does not carry a finite repeat counter. Turn the option off to use `FF00` (or the natural order-table end when that is already sufficient).

The resulting module is an editable reversed arrangement, not a bit-exact reverse rendering of chip audio. Instrument envelopes, oscillator phase, sample playback direction and stateful hardware effects still run forward according to Furnace playback.

### Analyze playback loops and song end

Choose `Analyze playback loops and song end` to run the same playback simulation without creating or modifying a module. The console log reports the first repeated order/row state (if any), loop start/end, explicit `FFxx` stop, natural order-table end, reachable row count, and the number/location of flow commands.

### Extract sequence

Choose `Extract sequence from FUR`, select a `.fur` module, and run the conversion. RevFUR creates:

- `song.furseq.json` — editable sequence document.
- `song.furseq.txt` — readable channel/order/pattern disassembly.

The JSON contains the source module fingerprint, sequence dimensions, channel names, orders, and pattern rows. It does not contain instrument patches, sample bytes, wavetable contents, or chip configuration data.

### Apply edited sequence

Choose `Apply edited sequence to FUR`, select:

1. The exact original `.fur` used for extraction.
2. Its edited `.furseq.json` file.
3. An output `.fur` path.

RevFUR rejects the operation if the source module fingerprint does not match. The output is reopened with the Furnace parser before the conversion is considered successful. A `*.revfur-report.json` file records the preservation check results.

## Command-line usage

Export the sequence:

```bash
python main.py extract "song.fur"
```

Choose an output sequence name:

```bash
python main.py extract "song.fur" --output "song_sequence.furseq.json"
```

Apply edits to the exact source module:

```bash
python main.py apply "song.fur" "song_sequence.furseq.json" --output "song_edited.fur"
```

Analyze loops and the effective end without writing a module:

```bash
python main.py analyze "song.fur"
```

Reverse the reachable playback pass directly from the command line:

```bash
python main.py reverse "song.fur" --output "song_reversed.fur"
```

Loop the reversed arrangement back to the beginning with Furnace `0B00`:

```bash
python main.py reverse "song.fur" --output "song_reversed_looped.fur" --loop-back
```

Open the GUI:

```bash
python main.py
```

## Editing the `.furseq.json` file

The document has these main fields:

- `source.sha256`: identity check for the original `.fur` file. Do not edit it.
- `sequence.orders`: one byte-value array per Furnace channel. Each value is an order's pattern index (`00`–`FF`); JSON stores these as decimal integers.
- `sequence.patterns`: one entry per existing pattern in the first subsong.
- `patterns[].rows`: sparse list of non-empty pattern rows. `row` is zero-based.
- `note`: Furnace's native note code (`0`–`179` for notes, `180` note-off, `181` release, `182` macro release, `183` raw frequency).
- `instrument`: instrument index referenced by the sequence row. This changes only the reference in the pattern, not the instrument definition.
- `volume`: Furnace volume-column value.
- `effects`: ordered list of `{ "command": "0B", "value": "02" }`-style command/value pairs. Values are hexadecimal strings. Legacy `PATR` effects may use four hexadecimal digits.
- `source_block_sha256`: per-pattern safety checksum. Do not edit or remove it.

Example row:

```json
{
  "row": 12,
  "note": 97,
  "instrument": 3,
  "volume": 126,
  "effects": [
    { "command": "0B", "value": "02" },
    { "command": "04", "value": "31" }
  ]
}
```

This represents sequence data only. It does not define or modify instrument 3, a sample bank, or a wavetable.

## Safety and current limits

RevFUR is intentionally conservative:

- It edits the first subsong only. Additional subsongs are left untouched.
- The reverse operation targets the first subsong, as do the extraction and apply operations. Additional subsongs are left untouched.
- If one pattern slot is reused in different orders with conflicting inherited instrument or volume-column states, reversal is rejected rather than silently producing an incorrect arrangement. Split/clone that pattern in Furnace first, then retry.
- Manual sequence editing keeps the existing pattern rows, channels, order count, and pattern identities. This version does not add/remove channels, orders, or pattern slots.
- Pattern indices referenced by the order table are stored as byte values, as in the Furnace format.
- The original module is never overwritten by default; the GUI and CLI create a separate output file.
- Changed patterns are appended as new blocks. Original pattern blocks remain in the file, and only the pointer for each changed pattern is redirected.
- Unmodified pattern blocks are not rewritten. A no-edit export/apply round trip preserves the decompressed module bytes exactly.
- Instrument, sample, and wavetable asset blocks are checked byte-for-byte. A failed safety check aborts output generation.
- A loop-back marker must be placed on a unique pattern slot with an available effect column. If every terminal-channel pattern is shared with an earlier order, RevFUR reports the conflict instead of creating an early jump.

MIDI is not involved in this tool. RevFUR preserves Furnace sequence semantics instead of converting hardware effects into MIDI approximations. In Extract/Apply mode, `0Bxx` remains an ordinary Furnace effect row; in Reverse mode it is interpreted as playback flow so the reachable loop can be identified automatically.

## Build standalone applications

Install build dependencies:

```bash
python -m pip install --upgrade pip pyinstaller tkinterdnd2
```

Windows:

```bat
build_windows.bat
```

Linux:

```bash
chmod +x build_linux.sh
./build_linux.sh
```

macOS:

```bash
chmod +x build_macos.sh
./build_macos.sh
```

### Debian package

On Debian 12/13 or a compatible derivative, install `dpkg-dev` and build the native package:

```bash
sudo apt install dpkg-dev
./build_deb.sh
```

This creates `dist/revfur_0.1.5_all.deb`. The package runs the Python sources on the system interpreter and depends on `python3 (>= 3.10)` and `python3-tk`; drag-and-drop remains optional.

### GitHub Actions

The included `.github/workflows/build.yml` builds Windows x64, Linux x86_64, macOS ARM64, and macOS Intel artifacts. The Linux job also creates the Debian `.deb`. Run it manually from **Actions → Build RevFUR → Run workflow**, or let it run on pushes and pull requests. Artifacts are attached to the workflow run; the workflow does not publish a release.

Builds are native to the machine/OS where PyInstaller runs. The scripts do not cross-compile Windows binaries from Linux or macOS binaries from Windows.

## Tests

Run the regression suite:

```bash
python -m unittest discover -s tests -v
```

The suite contains 15 tests and covers:

- Full-song reverse: synchronized order reversal across channels, isolated one-tick gap compaction, preservation of multi-tick rests, restoration of sparse instrument/volume state, navigation-marker reconstruction and optional `0B00` loop-back.
- Real Furnace v232 module (`examples/options music.fur`).
- Legacy `INFO` / `PATN` export and no-op round trip.
- Legacy `INFO` / `PATR` with a 16-bit effect value.
- Modern `INF2` / `SNG2` / `PATN` and synthetic instrument/sample/wavetable blocks.
- Editing a note, an effect, and the order table.
- Refusal to apply a sequence document to a different source module.
- Asset block identity checks.

The modern v240 test is a synthetic fixture built from the published Furnace format layout. The real-world test module included here is Furnace format v232.

## References

- [Furnace repository](https://github.com/tildearrow/furnace)
- [Furnace `.fur` format specification](https://github.com/tildearrow/furnace/blob/master/papers/format.md)
- [Furnace pattern effects](https://github.com/tildearrow/furnace/blob/master/doc/3-pattern/effects.md)
- [Furnace instrument format](https://github.com/tildearrow/furnace/blob/master/papers/newIns.md)
- RevSMPS reference: the supplied `RevSMPS.exe` was inspected locally; no RevSMPS source code or executable is included in this project.
