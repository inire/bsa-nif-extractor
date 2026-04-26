# bsa-nif-extractor

### Unpack every NIF from every Bethesda archive. Morrowind through Fallout 4.

[![Version 1.0.0](https://img.shields.io/badge/version-1.0.0-blue)](https://github.com/inire/bsa-nif-extractor)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white)](https://python.org)
[![No Dependencies](https://img.shields.io/badge/deps-none*-green)](#requirements)
[![Games: 6](https://img.shields.io/badge/games-6-orange)](https://github.com/inire/bsa-nif-extractor)

Recursively scans a directory of Bethesda mod archives, extracts every `.nif` file found inside them, and writes the results to a structured output directory that mirrors the original mod layout. Pure Python — BSA and BA2 formats are parsed natively with no third-party archive dependencies.

---

## Supported Formats

| Format | Version | Games | Compression |
|--------|---------|-------|-------------|
| BSA/MW | v102 | Morrowind | None (always uncompressed) |
| BSA | v103 | Oblivion | zlib / raw deflate |
| BSA | v104 | Fallout 3, Fallout New Vegas, Skyrim (2011) | zlib / raw deflate |
| BSA/SSE | v105 | Skyrim Special Edition, Skyrim VR | LZ4 frame or LZ4 block |
| BA2 GNRL | — | Fallout 4, Fallout 76 | zlib / raw deflate |
| BA2 DX10 | — | Fallout 4, Fallout 76 | *(texture-only, skipped automatically)* |

---

## Requirements

Python 3.10+. For **Skyrim SE / Skyrim VR** BSAs only, `pip install lz4`. All other formats use Python's built-in `zlib` and `struct`.

---

## Quick Start

No installation required — just run the script:

```bash
python bsa_nif_extractor.py -i "D:/Mods" -o "D:/ExtractedNIFs"
```

| Flag | Default | Description |
|------|---------|-------------|
| `-i`, `--input` | `.` | Root directory containing mod folders to scan |
| `-o`, `--output` | `./nif_output` | Destination directory for extracted NIFs |
| `-v`, `--verbose` | off | Print every NIF path as it is written |
| `--dry-run` | off | Scan and report without writing any files |
| `--errors-file PATH` | none | Write a log of archives that errored |
| `--debug-bsa PATH` | none | Dump header and first NIF record details for a single archive |

---

## Output Layout

```
<output>/
    <mod_folder>/
        <archive_stem>/
            meshes/
                path/to/model.nif
```

Each mod folder and archive name is preserved from the input tree, so any extracted NIF traces back to its source archive.

---

## Dry Run

```bash
python bsa_nif_extractor.py -i "D:/Mods" -o "D:/ExtractedNIFs" --dry-run
```

Shows `[DRY-RUN]` beside each NIF path and prints a summary at the end.

---

## Diagnosing Problem Archives

```bash
python bsa_nif_extractor.py --debug-bsa "D:/Mods/SomeMod/SomeMod.bsa"
```

Reads a single archive and prints header fields, section offsets, and the first compressed NIF record's raw bytes. For SSE BSAs it tests both LZ4 frame and block decompression paths.

---

## Format Notes

- **Morrowind (v102):** Completely different layout from later formats. Magic bytes `0x00 0x01 0x00 0x00`, files never compressed. OpenMW modlists often unpack to loose files at install time.
- **Oblivion / FO3 / FNV (v103–v104):** Auto-detects both zlib-wrapped and raw deflate streams. Embedded filename blob flag (`0x100`) suppressed for v103 to handle mod tools that incorrectly set it on Oblivion-format BSAs.
- **Skyrim SE / VR (v105):** Auto-detects LZ4 frame vs LZ4 block compression. Requires `pip install lz4`.
- **Fallout 4 / 76 (BA2 GNRL):** `Int16ul` length-prefixed latin-1 name table. BA2 DX10 texture archives are skipped automatically.

---

## Tested Against

All tests performed March 7–9, 2026.

**Base game archives:** Morrowind (+ Tribunal, Bloodmoon), Oblivion GOTY, Fallout 3 GOTY, Fallout: New Vegas Ultimate Edition, Skyrim Anniversary Edition, Fallout 4 Anniversary Edition.

**Modlists:** Magnum Opus v9.2.6 (FO4), Path of the Incarnate v2.1.1 (Morrowind), Tempus Maledictum v8.0.6 (Skyrim), WildCard TTW v5.0 (FNV/TTW), Modding OpenMW Total Overhaul (no BSAs present).

---

## Related Tools

- **[sniff-emitter-fix](https://github.com/inire/sniff-emitter-fix)** — Pre/post-SNIFF toolchain for Fallout 4 NIFs with emitter effects. Same author, designed to work together as a pipeline.

---

## Built with Claude

This tool was built collaboratively with [Claude](https://claude.ai) (Anthropic). The full implementation — format parsing, compression auto-detection, debug tooling, and test coverage across six base games and five modlists — was developed iteratively through conversation over several sessions.

---

## License

Do whatever you want with it.
