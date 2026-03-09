#!/usr/bin/env python3
"""
BSA / BA2 NIF Extractor
=======================
Recursively scans a directory of Bethesda mods, opens every .bsa and .ba2
archive found (including those nested inside sub-directories), extracts all
.nif files, and copies them to a structured output directory that mirrors the
original mod layout.

Supported archive formats
-------------------------
    BSA v102  – Morrowind
                Always uncompressed. Different magic (0x00000100), hash table
                layout, and filename block from later BSA versions.
    BSA v103  – Oblivion
    BSA v104  – Fallout 3, Fallout New Vegas, Skyrim (2011)
                Note: FNV BSAs often ship uncompressed; files_prefixed (0x100)
                is typically OFF in vanilla archives — both handled correctly.
    BSA v105  – Skyrim Special Edition, Skyrim VR
    BA2 GNRL  – Fallout 4, Fallout 76 (general files incl. NIFs)
    BA2 DX10  – Fallout 4, Fallout 76 (textures – NIFs not expected here,
                skipped automatically)

Directory layout produced:
    <output_root>/
        <mod_folder_name>/
            <archive_stem>/
                meshes/
                    ...
                    model.nif

Usage
-----
    python bsa_nif_extractor.py -i "D:/Mods" -o "D:/ExtractedNIFs"

Options
-------
    -i  Input root directory that contains mod folders (default: current dir)
    -o  Output root directory for extracted NIFs (default: ./nif_output)
    -v  Verbose – print every NIF path as it is written
    --dry-run  Scan and report what would be extracted without writing files
    --errors-file  Path to write a log of archives that errored
    --debug-bsa  Dump header + first NIF file record details for a single
                 archive and exit. Works for all supported BSA versions
                 including Morrowind v102.
"""

import argparse
import os
import struct
import sys
import time
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Generator, List, Optional

# ---------------------------------------------------------------------------
# No third-party archive dependencies — BSA (all versions) and BA2 are read
# natively. This avoids all bethesda_structs bugs (LZ4 frame vs block,
# CString utf8, etc.).
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BSA_SUFFIX = ".bsa"
BA2_SUFFIX = ".ba2"
ARCHIVE_SUFFIXES = (BSA_SUFFIX, BA2_SUFFIX)
NIF_SUFFIX = ".nif"

BA2_MAGIC = b"BTDX"
BA2_TYPE_GNRL = "GNRL"
BA2_TYPE_DX10 = "DX10"

# Morrowind BSA magic: first 4 bytes interpreted as little-endian uint32 = 0x100
_MW_BSA_MAGIC = 0x00000100

# zlib second-byte values that indicate a zlib-wrapped stream (first byte 0x78)
_ZLIB_SECOND_BYTES = {0x01, 0x5E, 0x9C, 0xDA}


# ---------------------------------------------------------------------------
# Decompression helpers
# ---------------------------------------------------------------------------

def _decompress_auto(data: bytes) -> bytes:
    """
    Decompress deflate data without knowing ahead of time whether it is
    zlib-wrapped (0x78 xx header) or raw deflate (no header).

    SharpZipLib's Inflater (used by BSA Browser) handles both transparently.
    Python's zlib does not — we detect via the magic bytes instead.

    zlib-wrapped streams always start with 0x78 followed by one of a small
    set of second bytes that satisfy the zlib header checksum constraint.
    Everything else is raw deflate.
    """
    if len(data) >= 2 and data[0] == 0x78 and data[1] in _ZLIB_SECOND_BYTES:
        return zlib.decompress(data)           # zlib-wrapped (wbits=15, default)
    return zlib.decompress(data, wbits=-15)    # raw deflate (no header)


try:
    import lz4.frame as _lz4_frame
    import lz4.block as _lz4_block
    _LZ4_AVAILABLE = True
except ImportError:
    _LZ4_AVAILABLE = False

_LZ4_FRAME_MAGIC = b"\x04\x22\x4d\x18"


def _decompress_lz4(data: bytes, original_size: int) -> bytes:
    """
    Decompress LZ4 data from a Skyrim SE BSA file.

    Vanilla SSE BSAs (Bethesda-created) use LZ4 *frame* format, which carries
    its own size metadata and is detected by the 4-byte magic 0x04224D18.
    Some mod-created BSAs use LZ4 *block* format (raw LZ4, no frame header),
    which requires the original_size from the BSA file record as the output
    buffer size.

    Detection mirrors _decompress_auto: inspect magic bytes before choosing
    strategy.
    """
    if not _LZ4_AVAILABLE:
        raise RuntimeError(
            "lz4 package required for Skyrim SE BSAs.\n"
            "Install with: pip install lz4"
        )
    if data[:4] == _LZ4_FRAME_MAGIC:
        return _lz4_frame.decompress(data)
    else:
        return _lz4_block.decompress(data, uncompressed_size=original_size)


# ---------------------------------------------------------------------------
# Native BA2 GNRL reader  (Fallout 4 / Fallout 76)
# ---------------------------------------------------------------------------
# We bypass bethesda_structs entirely for BA2 files because:
#   1. It uses PascalString(..., "utf8") for the name table, which throws on
#      any non-ASCII byte (Windows-1252 apostrophes, etc.).
#   2. It passes raw bytes to Python's zlib which expects a zlib header, but
#      BA2 GNRL uses *raw deflate* (no header). This causes "Error -5".
#
# BSA Browser (Sharp.BSA.BA2) avoids both by using:
#   - BinaryReader.ReadChars() with a byte-safe encoding (UTF-7 / latin-1)
#   - SharpZipLib's Inflater which handles raw deflate natively
#
# We replicate that approach: read the name table with latin-1 (never throws),
# and decompress with zlib.decompressobj(wbits=-15) (negative = raw deflate).

@dataclass
class BA2GnrlEntry:
    name_hash:    int
    extension:    str
    dir_hash:     int
    flags:        int
    offset:       int
    packed_size:  int
    unpacked_size: int
    align:        int
    full_path:    str = ""  # filled in after name table is read


def _read_ba2_gnrl(path: Path):
    """
    Parse a BA2 GNRL archive and yield (filepath_str, data_bytes) for every
    file.

    Raises ValueError for unsupported or non-GNRL archives (caller decides
    skip vs error).
    """
    with open(path, "rb") as fh:
        raw = fh.read()

    # --- Header (4+4+4+4+8 = 24 bytes) ---
    if len(raw) < 24:
        raise ValueError("File too small to be a valid BA2")

    magic        = raw[0:4]
    version      = struct.unpack_from("<I", raw, 4)[0]
    ba2_type     = raw[8:12].decode("ascii").rstrip("\x00")
    num_files    = struct.unpack_from("<I", raw, 12)[0]
    names_offset = struct.unpack_from("<Q", raw, 16)[0]

    if magic != BA2_MAGIC:
        raise ValueError(f"Not a BA2 archive (magic={magic!r})")
    if ba2_type == BA2_TYPE_DX10:
        raise ValueError("BA2 is DX10 (textures only) — no NIFs expected, skipping")
    if ba2_type != BA2_TYPE_GNRL:
        raise ValueError(f"Unsupported BA2 type: {ba2_type!r}")

    # --- File records (36 bytes each) ---
    # name_hash(4) + ext(4) + dir_hash(4) + flags(4) + offset(8) +
    # packed_size(4) + unpacked_size(4) + align(4)
    RECORD_SIZE = 36
    HEADER_SIZE = 24
    entries: List[BA2GnrlEntry] = []

    pos = HEADER_SIZE
    for _ in range(num_files):
        if pos + RECORD_SIZE > len(raw):
            break
        name_hash, = struct.unpack_from("<I", raw, pos)
        extension   = raw[pos+4:pos+8].decode("ascii").rstrip("\x00")
        dir_hash,   = struct.unpack_from("<I", raw, pos+8)
        flags,      = struct.unpack_from("<I", raw, pos+12)
        offset,     = struct.unpack_from("<Q", raw, pos+16)
        packed,     = struct.unpack_from("<I", raw, pos+24)
        unpacked,   = struct.unpack_from("<I", raw, pos+28)
        align,      = struct.unpack_from("<I", raw, pos+32)
        entries.append(BA2GnrlEntry(
            name_hash=name_hash, extension=extension, dir_hash=dir_hash,
            flags=flags, offset=offset, packed_size=packed,
            unpacked_size=unpacked, align=align,
        ))
        pos += RECORD_SIZE

    # --- Name table ---
    # Each entry: Int16ul length, then that many bytes, decoded as latin-1.
    # latin-1 maps bytes 0-255 directly to Unicode codepoints 0-255, so it
    # never raises — exactly how BSA Browser's ReadChars() behaves.
    if names_offset > 0 and names_offset < len(raw):
        npos = names_offset
        for entry in entries:
            if npos + 2 > len(raw):
                break
            name_len = struct.unpack_from("<H", raw, npos)[0]
            npos += 2
            name_bytes = raw[npos: npos + name_len]
            npos += name_len
            # Decode with latin-1; replace any truly unrepresentable byte with '?'
            entry.full_path = name_bytes.decode("latin-1", errors="replace")

    # --- Yield file data ---
    for entry in entries:
        if not entry.full_path.lower().endswith(NIF_SUFFIX):
            continue

        file_raw = raw[entry.offset: entry.offset + (entry.packed_size or entry.unpacked_size)]

        if entry.packed_size > 0:
            # Auto-detect zlib-wrapped vs raw deflate by inspecting magic bytes.
            # BA2 archives use both depending on the tool that created them.
            data = _decompress_auto(file_raw)
        else:
            data = file_raw

        yield entry.full_path, data


# ---------------------------------------------------------------------------
# Native BSA reader — Morrowind v102
# ---------------------------------------------------------------------------
# Morrowind uses a completely different BSA format from Oblivion onward:
#
#   Magic:   First 4 bytes as little-endian uint32 == 0x00000100 (NOT "BSA\x00")
#   Layout:
#     [0x00]  uint32  version/magic  (0x100)
#     [0x04]  uint32  hash_offset    offset to hash table, relative to byte 12
#                                    (i.e. absolute = hash_offset + 12)
#     [0x08]  uint32  file_count
#
#   File records  (file_count × 8 bytes, starting at 0x0C):
#     uint32  size    uncompressed file size in bytes
#     uint32  offset  offset into data region, relative to DATA_BASE
#                     where DATA_BASE = hash_offset + 12 + file_count*8
#                     (i.e. immediately after the hash table)
#
#   Name offsets  (file_count × 4 bytes, immediately after file records):
#     uint32  name_offset  byte offset into the name block
#
#   Name block  (immediately after name offsets):
#     Null-terminated strings, one per file.
#
#   Hash table   (file_count × 8 bytes, at hash_offset + 12):
#     Two uint32 values per file (hash halves) — not needed for extraction.
#
#   Data region  (immediately after hash table):
#     Raw (uncompressed) file data, back-to-back.
#     Morrowind BSAs are NEVER compressed.
#
# Reference: OpenMW source, BSA Browser, UESP wiki "Morrowind BSA format".

def _read_bsa_mw(path: Path) -> Generator:
    """
    Parse a Morrowind BSA (magic 0x00000100) and yield (filepath_str, data_bytes)
    for every .nif file found.

    Morrowind BSAs are always uncompressed — no decompression is performed.
    """
    raw = path.read_bytes()

    if len(raw) < 12:
        raise ValueError("File too small to be a valid Morrowind BSA")

    hash_offset = struct.unpack_from("<I", raw, 4)[0]
    file_count  = struct.unpack_from("<I", raw, 8)[0]

    if file_count == 0:
        return  # empty archive

    # Absolute offsets for each section
    FILE_REC_BASE  = 12                                 # file size+offset pairs
    NAME_OFF_BASE  = FILE_REC_BASE  + file_count * 8   # name offset table
    NAME_BLK_BASE  = NAME_OFF_BASE  + file_count * 4   # name strings
    HASH_TBL_BASE  = hash_offset + 12                  # hash table (skip)
    DATA_BASE      = HASH_TBL_BASE  + file_count * 8   # raw file data

    # Sanity check
    min_required = DATA_BASE
    if len(raw) < min_required:
        raise ValueError(
            f"Morrowind BSA appears truncated "
            f"(need >= {min_required} bytes, have {len(raw)})"
        )

    # Read file size + data-region offset for every file
    sizes   = []
    offsets = []
    for i in range(file_count):
        sz  = struct.unpack_from("<I", raw, FILE_REC_BASE + i * 8)[0]
        off = struct.unpack_from("<I", raw, FILE_REC_BASE + i * 8 + 4)[0]
        sizes.append(sz)
        offsets.append(off)

    # Read per-file name offsets (into the name block)
    name_offsets = []
    for i in range(file_count):
        no = struct.unpack_from("<I", raw, NAME_OFF_BASE + i * 4)[0]
        name_offsets.append(no)

    # Extract NIFs
    for i in range(file_count):
        npos = NAME_BLK_BASE + name_offsets[i]
        try:
            end = raw.index(b"\x00", npos)
        except ValueError:
            # No null terminator found — skip malformed entry
            continue
        filepath = raw[npos:end].decode("latin-1", errors="replace")

        if not filepath.lower().endswith(NIF_SUFFIX):
            continue

        dpos = DATA_BASE + offsets[i]
        data = raw[dpos : dpos + sizes[i]]
        yield filepath, data


# ---------------------------------------------------------------------------
# Native BSA reader — Oblivion v103 / FO3+FNV+Skyrim v104 / SSE v105
# ---------------------------------------------------------------------------
# bethesda_structs has two unfixable bugs without patching the installed package:
#   1. LZ4: uses lz4.frame.decompress() without checking frame magic.
#      Vanilla Bethesda SSE BSAs use lz4.FRAME; some mod tools use lz4.BLOCK.
#      Fix: auto-detect by inspecting 4-byte magic before choosing variant.
#   2. CString("utf8"): throws on non-ASCII bytes in filenames (Windows-1252
#      chars). Fix: read filenames with latin-1 which never throws.
#
# BSA Browser (Sharp.BSA.BA2 / BSAUtil/BSA.cs) avoids both by using:
#   - K4os.Compression.LZ4.LZ4Codec.Decode() = LZ4 block with known output size
#   - BinaryReader with Encoding.UTF7 = byte-safe char reading (like latin-1)


@dataclass
class BSAFileRecord:
    filepath:   str
    offset:     int
    size:       int    # raw size field (may have compressed-toggle bit set)
    compressed: bool   # true if this file is compressed


def _read_bsa(path: Path) -> Generator:
    """
    Parse a BSA archive (v103 Oblivion / v104 FO3+FNV+Skyrim / v105 SSE) and
    yield (filepath_str, data_bytes) for every .nif file found.

    Reference: BSA Browser Sharp.BSA.BA2/BSAUtil/BSA.cs
    """
    raw = path.read_bytes()
    pos = 0

    def u32(p): return struct.unpack_from("<I", raw, p)[0]
    def u64(p): return struct.unpack_from("<Q", raw, p)[0]
    def cstr_latin1(p):
        """Read null-terminated string at p using latin-1 (never throws)."""
        end = raw.index(b"\x00", p)
        return raw[p:end].decode("latin-1", errors="replace"), end + 1

    # --- Header (36 bytes) ---
    magic         = raw[0:4]
    version       = u32(4)
    dir_offset    = u32(8)
    archive_flags = u32(12)
    dir_count     = u32(16)
    file_count    = u32(20)
    # dir_names_len  = u32(24)  # not needed
    # file_names_len = u32(28)  # not needed
    # file_flags     = u32(32)  # not needed

    if magic != b"BSA\x00":
        raise ValueError(f"Not a BSA archive (magic={magic!r})")
    if version not in (103, 104, 105):
        raise ValueError(f"Unsupported BSA version {version} (expected 103/104/105)")

    archive_compressed = bool(archive_flags & 0x004)
    # Flag 0x100 (embedded filename blobs) is a v104+ feature introduced in
    # Fallout 3. Some mod tools incorrectly set this flag on v103 BSAs.
    # Honouring it on v103 causes the file pointer to land in the wrong place,
    # producing garbage original_size values and decompression failures.
    # Safe fix: ignore the flag entirely for v103.
    files_prefixed = bool(archive_flags & 0x100) and version >= 104

    is_sse = (version == 105)

    # --- Directory records ---
    # v103/104: hash(8) + file_count(4) + name_offset(4)          = 16 bytes
    # v105:     hash(8) + file_count(4) + unk(4) + name_offset(8) = 24 bytes
    DIR_RECORD_SIZE = 24 if is_sse else 16
    pos = dir_offset  # always 36 for standard BSAs

    dir_file_counts = []
    for _ in range(dir_count):
        fc = u32(pos + 8)
        dir_file_counts.append(fc)
        pos += DIR_RECORD_SIZE

    # --- Directory blocks (name + file records) ---
    FILE_RECORD_SIZE = 16  # hash(8) + size(4) + offset(4)
    all_files: list[BSAFileRecord] = []

    for fc in dir_file_counts:
        # Pascal-style directory name: 1-byte length prefix + chars + null
        name_len = raw[pos]
        dir_name = raw[pos+1 : pos+name_len].decode("latin-1", errors="replace")
        pos += name_len + 1  # skip length byte + chars + null

        for _ in range(fc):
            raw_size = u32(pos + 8)
            offset   = u32(pos + 12)

            # bit 30 toggles compression relative to archive default
            toggle     = bool(raw_size & 0x40000000)
            compressed = (archive_compressed != toggle)
            clean_size = raw_size & 0x3FFFFFFF

            all_files.append(BSAFileRecord(
                filepath=dir_name,
                offset=offset,
                size=clean_size,
                compressed=compressed,
            ))
            pos += FILE_RECORD_SIZE

    # --- File name table ---
    # Null-terminated strings, one per file, in the same order as file records
    for i, frec in enumerate(all_files):
        name, pos = cstr_latin1(pos)
        frec.filepath = frec.filepath + "\\" + name

    # --- Extract NIFs ---
    for frec in all_files:
        if not frec.filepath.lower().endswith(NIF_SUFFIX):
            continue

        fpos = frec.offset

        # Skip embedded filename blob if present (Fallout 3+ with flag 0x100).
        blob_len = 0
        if files_prefixed:
            blob_len = raw[fpos]
            fpos += blob_len + 1

        if frec.compressed:
            # 4-byte original (uncompressed) size prefix, then compressed data.
            # BSA Browser WriteDataToStream/GetSizeInArchive (BSAFileEntry.cs):
            #   filesz = Size & 0x3fffffff
            #   if ContainsFileNameBlobs: filesz -= (blob_len + 1)
            #   if Compressed:           filesz -= 4
            #   DecompressLZ4(stream, filesz, uncompressed_size)
            original_size   = struct.unpack_from("<I", raw, fpos)[0]
            fpos           += 4
            compressed_size = frec.size - 4
            if files_prefixed:
                compressed_size -= (blob_len + 1)
            cdata = raw[fpos : fpos + compressed_size]

            if is_sse:
                # Skyrim SE: LZ4 frame (vanilla) or LZ4 block (some mod tools).
                data = _decompress_lz4(cdata, original_size)
            else:
                # v103/v104: zlib or raw deflate
                data = _decompress_auto(cdata)
        else:
            data = raw[fpos : fpos + frec.size]

        yield frec.filepath, data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_archive_files(root: Path) -> Generator[Path, None, None]:
    """Yield every .bsa and .ba2 file found anywhere beneath *root*."""
    for dirpath, _dirs, files in os.walk(root):
        for fname in files:
            if fname.lower().endswith(ARCHIVE_SUFFIXES):
                yield Path(dirpath) / fname


def relative_mod_path(archive_path: Path, input_root: Path) -> Path:
    return archive_path.relative_to(input_root)


def build_output_path(output_root: Path, rel_archive: Path, nif_filepath: str) -> Path:
    """
    Construct the destination path for a single NIF file.
    Layout: <output_root>/<mod_dir>/<archive_stem>/<nif_internal_path>
    """
    archive_stem = rel_archive.stem
    mod_part     = rel_archive.parent
    nif_parts    = PurePosixPath(nif_filepath.replace("\\", "/"))
    return output_root / mod_part / archive_stem / nif_parts


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process_ba2(
    archive_path: Path,
    input_root: Path,
    output_root: Path,
    verbose: bool,
    dry_run: bool,
) -> dict:
    result = {"archive": str(archive_path), "type": "BA2/GNRL",
              "nif_count": 0, "skipped": False, "error": None}
    try:
        rel = relative_mod_path(archive_path, input_root)
        for filepath_str, data in _read_ba2_gnrl(archive_path):
            dest = build_output_path(output_root, rel, filepath_str)
            if verbose or dry_run:
                tag = "[DRY-RUN]" if dry_run else "[WRITE]"
                print(f"  {tag} {dest}")
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            result["nif_count"] += 1
    except ValueError as exc:
        result["skipped"] = True
        result["error"]   = str(exc)
    except Exception as exc:
        result["error"] = f"BA2 error: {exc}"
    return result


def process_bsa(
    archive_path: Path,
    input_root: Path,
    output_root: Path,
    verbose: bool,
    dry_run: bool,
) -> dict:
    result = {"archive": str(archive_path), "type": "BSA",
              "nif_count": 0, "skipped": False, "error": None}
    try:
        raw_head = archive_path.read_bytes()[:12]

        # --- Morrowind v102 (magic 0x00000100, no "BSA\x00" header) ---
        if len(raw_head) >= 4:
            magic_u32 = struct.unpack_from("<I", raw_head, 0)[0]
            if magic_u32 == _MW_BSA_MAGIC:
                result["type"] = "BSA/MW"
                rel = relative_mod_path(archive_path, input_root)
                for filepath_str, data in _read_bsa_mw(archive_path):
                    dest = build_output_path(output_root, rel, filepath_str)
                    if verbose or dry_run:
                        tag = "[DRY-RUN]" if dry_run else "[WRITE]"
                        print(f"  {tag} {dest}")
                    if not dry_run:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(data)
                    result["nif_count"] += 1
                return result

        # --- Oblivion / FO3 / Skyrim / SSE (magic "BSA\x00") ---
        if raw_head[:4] != b"BSA\x00":
            result["skipped"] = True
            result["error"]   = "Not a BSA archive"
            return result

        version = struct.unpack_from("<I", raw_head, 4)[0]
        if version not in (103, 104, 105):
            result["skipped"] = True
            result["error"]   = f"Unsupported BSA version {version}"
            return result
        if version == 105:
            result["type"] = "BSA/SSE"

        rel = relative_mod_path(archive_path, input_root)
        for filepath_str, data in _read_bsa(archive_path):
            dest = build_output_path(output_root, rel, filepath_str)
            if verbose or dry_run:
                tag = "[DRY-RUN]" if dry_run else "[WRITE]"
                print(f"  {tag} {dest}")
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            result["nif_count"] += 1

    except ValueError as exc:
        result["skipped"] = True
        result["error"]   = str(exc)
    except Exception as exc:
        result["error"] = f"BSA error: {exc}"
    return result


def process_archive(
    archive_path: Path,
    input_root: Path,
    output_root: Path,
    verbose: bool,
    dry_run: bool,
) -> dict:
    if archive_path.suffix.lower() == BA2_SUFFIX:
        return process_ba2(archive_path, input_root, output_root, verbose, dry_run)
    else:
        return process_bsa(archive_path, input_root, output_root, verbose, dry_run)


def run(input_root: Path, output_root: Path, verbose: bool, dry_run: bool,
        errors_file: Optional[Path]) -> None:

    if not input_root.is_dir():
        sys.exit(f"ERROR: Input path does not exist or is not a directory: {input_root}")

    if not dry_run:
        output_root.mkdir(parents=True, exist_ok=True)

    archive_list = sorted(find_archive_files(input_root))

    if not archive_list:
        print(f"No .bsa or .ba2 files found under: {input_root}")
        return

    print(f"Found {len(archive_list)} archive(s) under: {input_root}")
    if dry_run:
        print("*** DRY-RUN mode – no files will be written ***\n")

    total_nifs    = 0
    total_skipped = 0
    total_errors  = 0
    error_log     = []
    start_time    = time.perf_counter()

    for idx, archive_path in enumerate(archive_list, start=1):
        rel = archive_path.relative_to(input_root)
        print(f"\n[{idx}/{len(archive_list)}] {rel}")

        result = process_archive(archive_path, input_root, output_root, verbose, dry_run)

        if result["skipped"]:
            total_skipped += 1
            print(f"  SKIPPED [{result['type']}] – {result['error']}")
        elif result["error"]:
            total_errors += 1
            error_log.append((str(rel), result["error"]))
            print(f"  ERROR   [{result['type']}] – {result['error']}")
            print(f"  NIFs before error: {result['nif_count']}")
            total_nifs += result["nif_count"]
        else:
            total_nifs += result["nif_count"]
            status = "would extract" if dry_run else "extracted"
            print(f"  OK [{result['type']}] – {status} {result['nif_count']} NIF(s)")

    elapsed = time.perf_counter() - start_time
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Archives processed : {len(archive_list) - total_skipped}")
    print(f"  Archives skipped   : {total_skipped}")
    print(f"  Archives with error: {total_errors}")
    print(f"  Total NIFs {'found ' if dry_run else 'written'}: {total_nifs}")
    print(f"  Time elapsed       : {elapsed:.2f}s")
    if not dry_run and total_nifs > 0:
        print(f"  Output directory   : {output_root}")

    if errors_file and error_log:
        errors_file.parent.mkdir(parents=True, exist_ok=True)
        with errors_file.open("w", encoding="utf-8") as f:
            f.write(f"Error log – {len(error_log)} archive(s) with errors\n")
            f.write("=" * 60 + "\n")
            for arch, msg in error_log:
                f.write(f"{arch}\n  {msg}\n\n")
        print(f"  Error log written  : {errors_file}")

    print("=" * 60)


# ---------------------------------------------------------------------------
# Debug utility
# ---------------------------------------------------------------------------

def debug_bsa(path: Path) -> None:
    """
    Print raw header and first NIF file record details for diagnosis.
    Handles Morrowind v102 as well as Oblivion/FO3/Skyrim/SSE formats.
    """
    raw = path.read_bytes()

    def u32(p): return struct.unpack_from("<I", raw, p)[0]

    print(f"=== BSA Debug: {path.name} ===")
    print(f"  File size         : {len(raw):,} bytes")

    # Detect format
    magic_u32 = u32(0)
    if magic_u32 == _MW_BSA_MAGIC:
        _debug_bsa_mw(raw, path)
        return

    magic = raw[0:4]
    if magic != b"BSA\x00":
        print(f"  Magic             : {magic!r}  (UNKNOWN — not a supported BSA)")
        return

    version       = u32(4)
    archive_flags = u32(12)
    dir_count     = u32(16)
    file_count    = u32(20)

    print(f"  Format            : BSA (Oblivion / FO3 / Skyrim / SSE)")
    print(f"  Magic             : {magic}")
    print(f"  Version           : {version}")
    print(f"  archive_flags     : 0x{archive_flags:04X}")
    print(f"  compressed        : {bool(archive_flags & 0x004)}")
    files_prefixed_raw = bool(archive_flags & 0x100)
    files_prefixed_eff = files_prefixed_raw and version >= 104
    print(f"  files_prefixed    : {files_prefixed_raw}  (EmbedFileNames flag 0x100)"
          + ("  [suppressed – v103 does not support this flag]"
             if files_prefixed_raw and not files_prefixed_eff else ""))
    print(f"  dir_count         : {dir_count}")
    print(f"  file_count        : {file_count}")
    print()

    is_sse = (version == 105)
    DIR_RECORD_SIZE = 24 if is_sse else 16
    pos = 36
    dir_file_counts = []
    for _ in range(dir_count):
        fc = u32(pos + 8)
        dir_file_counts.append(fc)
        pos += DIR_RECORD_SIZE

    files_prefixed    = bool(archive_flags & 0x100)
    archive_compressed = bool(archive_flags & 0x004)

    name_len = raw[pos]
    dir_name = raw[pos+1:pos+name_len].decode("latin-1", errors="replace")
    pos += name_len + 1
    print(f"  First dir         : '{dir_name}'")

    for i in range(dir_file_counts[0]):
        raw_size = u32(pos + 8)
        offset   = u32(pos + 12)
        toggle   = bool(raw_size & 0x40000000)
        clean_size = raw_size & 0x3FFFFFFF
        compressed = (archive_compressed != toggle)
        pos += 16

        if not compressed:
            continue

        fpos = offset
        blob_len = 0
        if files_prefixed:
            blob_len = raw[fpos]
            blob_str = raw[fpos+1:fpos+1+blob_len].decode("latin-1", errors="replace")
            print(f"  Blob at offset    : len={blob_len}, content='{blob_str}'")
            fpos += blob_len + 1

        original_size = struct.unpack_from("<I", raw, fpos)[0]
        fpos += 4
        compressed_size_calc = clean_size - 4
        if files_prefixed:
            compressed_size_calc -= (blob_len + 1)

        print(f"  File {i}: raw_size=0x{raw_size:08X} clean_size={clean_size} "
              f"compressed={compressed} offset={offset}")
        print(f"  original_size (u32 at offset)  : {original_size}")
        print(f"  compressed_size (calculated)   : {compressed_size_calc}")
        cdata = raw[fpos:fpos+compressed_size_calc]
        print(f"  first 8 bytes of cdata         : {cdata[:8].hex()}")
        print(f"  cdata[:4] == LZ4 frame magic   : {cdata[:4] == _LZ4_FRAME_MAGIC}")

        try:
            _lz4_frame.decompress(cdata)
            print(f"  lz4.frame.decompress           : OK")
        except Exception as e:
            print(f"  lz4.frame.decompress           : FAIL — {e}")

        try:
            _lz4_block.decompress(cdata, uncompressed_size=original_size)
            print(f"  lz4.block.decompress           : OK")
        except Exception as e:
            print(f"  lz4.block.decompress           : FAIL — {e}")

        try:
            result = _decompress_lz4(cdata, original_size)
            print(f"  _decompress_lz4 (auto-detect)  : OK — {len(result)} bytes")
        except Exception as e:
            print(f"  _decompress_lz4 (auto-detect)  : FAIL — {e}")
        break


def _debug_bsa_mw(raw: bytes, path: Path) -> None:
    """Print Morrowind v102 BSA header and first NIF record details."""
    def u32(p): return struct.unpack_from("<I", raw, p)[0]

    hash_offset = u32(4)
    file_count  = u32(8)

    FILE_REC_BASE = 12
    NAME_OFF_BASE = FILE_REC_BASE + file_count * 8
    NAME_BLK_BASE = NAME_OFF_BASE + file_count * 4
    HASH_TBL_BASE = hash_offset + 12
    DATA_BASE     = HASH_TBL_BASE + file_count * 8

    print(f"  Format            : BSA/MW (Morrowind v102 — 0x00000100)")
    print(f"  hash_offset       : {hash_offset}  (hash table at byte {HASH_TBL_BASE})")
    print(f"  file_count        : {file_count}")
    print(f"  FILE_REC_BASE     : {FILE_REC_BASE}")
    print(f"  NAME_OFF_BASE     : {NAME_OFF_BASE}")
    print(f"  NAME_BLK_BASE     : {NAME_BLK_BASE}")
    print(f"  HASH_TBL_BASE     : {HASH_TBL_BASE}")
    print(f"  DATA_BASE         : {DATA_BASE}")
    print(f"  Compressed        : never (Morrowind BSAs are always uncompressed)")
    print()

    # Walk entries looking for the first NIF
    nif_found = False
    for i in range(min(file_count, 1000)):
        sz  = u32(FILE_REC_BASE + i * 8)
        off = u32(FILE_REC_BASE + i * 8 + 4)
        no  = u32(NAME_OFF_BASE + i * 4)
        npos = NAME_BLK_BASE + no
        try:
            end = raw.index(b"\x00", npos)
        except ValueError:
            continue
        filepath = raw[npos:end].decode("latin-1", errors="replace")
        if filepath.lower().endswith(NIF_SUFFIX):
            dpos = DATA_BASE + off
            print(f"  First NIF entry   : [{i}] {filepath}")
            print(f"    size            : {sz}")
            print(f"    data offset     : {off}  (absolute: {dpos})")
            print(f"    first 8 bytes   : {raw[dpos:dpos+8].hex()}")
            nif_found = True
            break

    if not nif_found:
        print("  (No .nif entries found in first 1000 file records)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recursively scan a Bethesda mod directory, extract all .nif files "
            "from every .bsa and .ba2 archive, and write them to a structured "
            "output tree."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-i", "--input", default=".",
        help="Root mod directory to scan (default: current directory)")
    parser.add_argument("-o", "--output", default="nif_output",
        help="Destination directory for extracted NIFs (default: ./nif_output)")
    parser.add_argument("-v", "--verbose", action="store_true",
        help="Print every NIF path as it is written")
    parser.add_argument("--dry-run", action="store_true",
        help="List what would be extracted without writing any files")
    parser.add_argument("--errors-file", default=None,
        help="Optional path to write a log of archives that errored (e.g. errors.txt)")
    parser.add_argument("--debug-bsa", default=None, metavar="PATH",
        help=(
            "Dump BSA header + first NIF file record details for a single "
            "archive and exit. Supports all formats including Morrowind v102."
        ))
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.debug_bsa:
        debug_bsa(Path(args.debug_bsa).resolve())
        sys.exit(0)
    run(
        input_root=Path(args.input).resolve(),
        output_root=Path(args.output).resolve(),
        verbose=args.verbose,
        dry_run=args.dry_run,
        errors_file=Path(args.errors_file).resolve() if args.errors_file else None,
    )
