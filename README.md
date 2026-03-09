# BSA / BA2 NIF Extractor

Recursively scans a directory of Bethesda mod archives, extracts every `.nif`
file found inside them, and writes the results to a structured output directory
that mirrors the original mod layout.

Pure Python — no third-party archive dependencies. BSA and BA2 formats are
parsed natively.

---

## Supported Formats

| Format | Version | Games | Compression |
|---|---|---|---|
| BSA/MW | v102 | Morrowind | None (always uncompressed) |
| BSA | v103 | Oblivion | zlib / raw deflate |
| BSA | v104 | Fallout 3, Fallout New Vegas, Skyrim (2011) | zlib / raw deflate |
| BSA/SSE | v105 | Skyrim Special Edition, Skyrim VR | LZ4 frame or LZ4 block |
| BA2 GNRL | — | Fallout 4, Fallout 76 | zlib / raw deflate |
| BA2 DX10 | — | Fallout 4, Fallout 76 | *(texture-only, skipped automatically)* |

---

## Requirements

Python 3.10 or newer.

For **Skyrim SE / Skyrim VR** BSAs only, the `lz4` package is required:

```
pip install lz4
```

All other formats use Python's built-in `zlib` and `struct` — no additional
packages needed.

---

## Installation

No installation required. Just place `bsa_nif_extractor.py` wherever you like
and run it directly.

---

## Usage

```
python bsa_nif_extractor.py -i "D:/Mods" -o "D:/ExtractedNIFs"
```

### Options

| Flag | Default | Description |
|---|---|---|
| `-i`, `--input` | `.` (current dir) | Root directory containing mod folders to scan |
| `-o`, `--output` | `./nif_output` | Destination directory for extracted NIFs |
| `-v`, `--verbose` | off | Print every NIF path as it is written |
| `--dry-run` | off | Scan and report what would be extracted without writing any files |
| `--errors-file PATH` | none | Write a log of archives that errored to the specified file |
| `--debug-bsa PATH` | none | Dump header and first NIF record details for a single archive, then exit |

---

## Output Layout

```
<output>/
    <mod_folder>/
        <archive_stem>/
            meshes/
                path/
                    to/
                        model.nif
```

Each mod folder and archive name is preserved from the input tree, so it is
easy to trace any extracted NIF back to its source archive.

---

## Dry Run

Use `--dry-run` to see exactly what would be extracted without touching the
filesystem:

```
python bsa_nif_extractor.py -i "D:/Mods" -o "D:/ExtractedNIFs" --dry-run
```

Output will show `[DRY-RUN]` beside each NIF path and print a summary at the
end.

---

## Diagnosing Problem Archives

The `--debug-bsa` flag reads a single archive and prints its header fields,
section offsets, and the first compressed NIF record's raw bytes. This is
useful when an archive errors out during a normal run.

```
python bsa_nif_extractor.py --debug-bsa "D:/Mods/SomeMod/SomeMod.bsa"
```

Works for all supported formats including Morrowind v102. For SSE BSAs it also
tests both LZ4 frame and LZ4 block decompression paths and reports which
succeeds.

---

## Error Logging

Use `--errors-file` to capture a list of archives that failed during a run:

```
python bsa_nif_extractor.py -i "D:/Mods" -o "D:/ExtractedNIFs" --errors-file errors.txt
```

Archives that are skipped (unsupported type, DX10 texture BA2, etc.) are
reported in the console summary but are not written to the error log — only
genuine parse or decompression failures are logged.

---

## Notes on Specific Formats

### Morrowind (BSA/MW v102)

Morrowind BSAs use a completely different layout from all later formats. The
magic bytes are `0x00 0x01 0x00 0x00` (not `BSA\x00`), and the archive is
divided into a file record table, a name offset table, a name block, a hash
table, and a raw data region. Files are never compressed. The extractor
detects this format automatically.

OpenMW-based modlists (e.g. those built with [Modding OpenMW's umo installer](https://modding-openmw.com/)) typically unpack BSAs to loose files at install time, so there may
be no BSAs present to scan even for Morrowind modlists.

### Oblivion / FO3 / FNV (BSA v103 / v104)

Files may be compressed with zlib or left uncompressed. Some mod tools produce
raw deflate streams (no zlib header) rather than standard zlib-wrapped streams.
The extractor auto-detects both by inspecting the first two bytes.

The embedded filename blob flag (`0x100`) introduced in Fallout 3 is suppressed
for v103 archives — some mod tools incorrectly set this flag on Oblivion-format
BSAs, which would otherwise corrupt the file pointer math.

### Skyrim SE / VR (BSA v105)

Vanilla Bethesda SSE BSAs use LZ4 frame compression. Some mod-creation tools
produce LZ4 block compression instead. The extractor auto-detects both by
checking for the LZ4 frame magic (`0x04224D18`) before choosing a decompression
path.

Requires `pip install lz4`.

### Fallout 4 / 76 (BA2 GNRL)

The BA2 name table uses `Int16ul` length-prefixed strings encoded with latin-1.
Compressed entries may use either zlib-wrapped or raw deflate streams depending
on the tool that created them; both are handled automatically.

BA2 DX10 archives (texture atlases) contain no NIF files and are skipped with
a `SKIPPED` notice in the console output.

---

## Tested Against

All tests performed March 7–9, 2026.

**Base game archives**
- Morrowind (+ Tribunal, Bloodmoon)
- Oblivion GOTY
- Fallout 3 GOTY
- Fallout: New Vegas Ultimate Edition
- Skyrim Anniversary Edition
- Fallout 4 Anniversary Edition

**Modlists**
- Magnum Opus v9.2.6 — Fallout 4 (1.10.163)
- Path of the Incarnate v2.1.1 — Morrowind
- Tempus Maledictum v8.0.6 — Skyrim (1.6.1170)
- WildCard TTW v5.0 — Fallout: New Vegas / Tale of Two Wastelands
- Modding OpenMW's Total Overhaul — no BSAs present (unpacked to loose files)

---

## Built with Claude

This tool was built collaboratively with [Claude](https://claude.ai) (Anthropic),
an AI assistant. The full implementation — format parsing, compression
auto-detection, debug tooling, and test coverage across six base games and five
modlists — was developed iteratively through conversation over several sessions.

If you're curious what AI-assisted tool development looks like in practice, this
is a reasonable example: a focused, single-purpose script with real-world testing
and no external archive dependencies.

---

## License

Do whatever you want with it.
