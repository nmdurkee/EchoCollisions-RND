#!/usr/bin/env python3
"""
echovr_symbols.py — recover the name/hash symbol table from echovr.exe.

The RAD R15 engine identifies named things (input actions, AI parameters, level
actors, network fields, animation states) by a 64-bit hash, not by string. Call
sites look like `Lookup(ctx, 0x66845d16a79233bd)`. The shipping binary still
carries a table mapping those hashes back to their original names, presumably so
logs and tooling could print something readable.

Record layout (see ECHOVR_AI_NOTES.md §1 for provenance):

  SSymbolRecord, 8-byte aligned:
    +0x00  u64      hash of the name
    +0x08  char[8]  magic, literally "OlPrEfIx"
    +0x10  char[]   name, NUL-terminated, padded to 8

Verified against consecutive records at VA 0x141cf4cd0:

  3d 49 a6 f7 df af 5d 60  OlPrEfIx  blue_tube1_ai_navpt1
  3e 49 a6 f7 df af 5d 60  OlPrEfIx  blue_tube1_ai_navpt2
  3f 49 a6 f7 df af 5d 60  OlPrEfIx  blue_tube1_ai_navpt3

CONFIRMED: the magic, the adjacency of hash and name, and 8-byte alignment, all
read directly out of the binary.

UNVERIFIED: that the u64 preceding the magic is *always* the hash of the name
that follows, rather than a trailer belonging to the previous record. The two
readings are indistinguishable in a packed table and produce identical output
here. It matters only if the table is ever sparse — `scan` reports alignment and
collision statistics so a wrong reading shows up as garbage rather than passing
silently.

This tool has been tested against synthetic data only. It has never seen a real
echovr.exe.

Usage:
    python echovr_symbols.py scan   <echovr.exe> [-o syms.tsv] [--json]
    python echovr_symbols.py grep   <echovr.exe> <pattern> [--regex]
    python echovr_symbols.py lookup <echovr.exe> <name-or-0xhash>
    python echovr_symbols.py selftest

`scan` dumps every record. `grep` filters by name. `lookup` resolves either
direction: a name to its hash, or a hash to its name.
"""

from __future__ import annotations

import argparse
import json
import mmap
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Format constants
# ---------------------------------------------------------------------------

MAGIC = b"OlPrEfIx"
HASH_SIZE = 8
MAGIC_SIZE = 8
RECORD_ALIGN = 8

# A name longer than this is taken as a failed NUL scan, not a real symbol.
MAX_NAME = 256

# Names are engine identifiers: printable ASCII, no whitespace or quotes.
_NAME_OK = re.compile(rb"^[!-~]+$")


@dataclass(frozen=True)
class Symbol:
    """One recovered symbol table record."""

    hash: int
    name: str
    file_offset: int
    va: int | None
    aligned: bool

    def as_row(self) -> str:
        va = f"0x{self.va:x}" if self.va is not None else "-"
        return f"0x{self.hash:016x}\t{va}\t{self.name}"


# ---------------------------------------------------------------------------
# PE section mapping — file offset to virtual address
# ---------------------------------------------------------------------------


class PEMapper:
    """Maps raw file offsets to VAs using the PE section table.

    Everything in the companion notes is expressed in VAs, so output that
    matches them is worth the header parsing.
    """

    def __init__(self, image_base: int, sections: list[tuple[int, int, int]]):
        self.image_base = image_base
        # (raw_ptr, raw_size, virtual_addr), sorted by raw_ptr
        self.sections = sorted(sections)

    @classmethod
    def parse(cls, data) -> PEMapper | None:
        """Return a mapper, or None if this is not a PE we understand."""
        try:
            if data[:2] != b"MZ":
                return None
            e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
            if data[e_lfanew : e_lfanew + 4] != b"PE\0\0":
                return None

            coff = e_lfanew + 4
            num_sections = struct.unpack_from("<H", data, coff + 2)[0]
            size_opt = struct.unpack_from("<H", data, coff + 16)[0]

            opt = coff + 20
            magic = struct.unpack_from("<H", data, opt)[0]
            if magic == 0x20B:  # PE32+
                image_base = struct.unpack_from("<Q", data, opt + 24)[0]
            elif magic == 0x10B:  # PE32
                image_base = struct.unpack_from("<I", data, opt + 28)[0]
            else:
                return None

            sections = []
            table = opt + size_opt
            for i in range(num_sections):
                base = table + i * 40
                _vsize, vaddr, raw_size, raw_ptr = struct.unpack_from(
                    "<IIII", data, base + 8
                )
                if raw_size:
                    sections.append((raw_ptr, raw_size, vaddr))
            if not sections:
                return None
            return cls(image_base, sections)
        except (struct.error, IndexError):
            return None

    def to_va(self, offset: int) -> int | None:
        for raw_ptr, raw_size, vaddr in self.sections:
            if raw_ptr <= offset < raw_ptr + raw_size:
                return self.image_base + vaddr + (offset - raw_ptr)
        return None


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def scan_buffer(data, mapper: PEMapper | None = None) -> list[Symbol]:
    """Recover every symbol record in `data`.

    `data` may be bytes or an mmap. Records whose name fails validation are
    dropped: the magic is eight ordinary ASCII bytes and will occasionally
    occur inside unrelated data.
    """
    out: list[Symbol] = []
    end = len(data)
    pos = data.find(MAGIC)

    while pos != -1:
        hash_off = pos - HASH_SIZE
        name_off = pos + MAGIC_SIZE

        if hash_off >= 0 and name_off < end:
            stop = data.find(b"\0", name_off, min(name_off + MAX_NAME + 1, end))
            if stop > name_off:
                raw = bytes(data[name_off:stop])
                if _NAME_OK.match(raw):
                    value = struct.unpack_from("<Q", data, hash_off)[0]
                    out.append(
                        Symbol(
                            hash=value,
                            name=raw.decode("ascii"),
                            file_offset=hash_off,
                            va=mapper.to_va(hash_off) if mapper else None,
                            aligned=(hash_off % RECORD_ALIGN == 0),
                        )
                    )

        pos = data.find(MAGIC, pos + 1)

    return out


def scan_file(path: Path) -> tuple[list[Symbol], PEMapper | None]:
    with path.open("rb") as fh:
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as data:
            mapper = PEMapper.parse(data)
            return scan_buffer(data, mapper), mapper


# ---------------------------------------------------------------------------
# Validation reporting
# ---------------------------------------------------------------------------


def report(symbols: list[Symbol], mapper: PEMapper | None) -> None:
    """Print health statistics to stderr.

    A wrong format hypothesis shows up here as misalignment or as one name
    carrying several hashes, rather than as plausible-looking output.
    """
    total = len(symbols)
    misaligned = sum(1 for s in symbols if not s.aligned)
    unmapped = sum(1 for s in symbols if s.va is None)

    by_name: dict[str, set[int]] = {}
    by_hash: dict[int, set[str]] = {}
    for s in symbols:
        by_name.setdefault(s.name, set()).add(s.hash)
        by_hash.setdefault(s.hash, set()).add(s.name)

    name_conflicts = {n: h for n, h in by_name.items() if len(h) > 1}
    hash_conflicts = {h: n for h, n in by_hash.items() if len(n) > 1}

    print(f"records          : {total}", file=sys.stderr)
    print(f"distinct names   : {len(by_name)}", file=sys.stderr)
    print(f"distinct hashes  : {len(by_hash)}", file=sys.stderr)
    print(
        f"misaligned       : {misaligned}"
        f"{'  <-- expected 0' if misaligned else ''}",
        file=sys.stderr,
    )
    if mapper is None:
        print("VA mapping       : unavailable (not a PE)", file=sys.stderr)
    elif unmapped:
        print(f"outside sections : {unmapped}", file=sys.stderr)

    if name_conflicts:
        print(
            f"name->many-hash  : {len(name_conflicts)}  <-- format may be wrong",
            file=sys.stderr,
        )
        for name, hashes in list(name_conflicts.items())[:5]:
            joined = ", ".join(f"0x{h:016x}" for h in sorted(hashes))
            print(f"    {name}: {joined}", file=sys.stderr)
    if hash_conflicts:
        print(f"hash->many-name  : {len(hash_conflicts)}", file=sys.stderr)
        for value, names in list(hash_conflicts.items())[:5]:
            print(f"    0x{value:016x}: {', '.join(sorted(names))}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_scan(args: argparse.Namespace) -> int:
    symbols, mapper = scan_file(Path(args.path))
    if not symbols:
        print("No symbol records found.", file=sys.stderr)
        return 1

    symbols.sort(key=lambda s: s.name)
    if args.json:
        payload = [
            {
                "hash": f"0x{s.hash:016x}",
                "name": s.name,
                "va": f"0x{s.va:x}" if s.va is not None else None,
                "file_offset": s.file_offset,
            }
            for s in symbols
        ]
        text = json.dumps(payload, indent=2)
    else:
        text = "\n".join(s.as_row() for s in symbols)

    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(text)

    report(symbols, mapper)
    return 0


def cmd_grep(args: argparse.Namespace) -> int:
    symbols, mapper = scan_file(Path(args.path))
    if args.regex:
        pattern = re.compile(args.pattern, re.IGNORECASE)
        hits = [s for s in symbols if pattern.search(s.name)]
    else:
        needle = args.pattern.lower()
        hits = [s for s in symbols if needle in s.name.lower()]

    for s in sorted(hits, key=lambda s: s.name):
        print(s.as_row())
    print(f"{len(hits)} of {len(symbols)} records matched", file=sys.stderr)
    return 0 if hits else 1


def cmd_lookup(args: argparse.Namespace) -> int:
    symbols, mapper = scan_file(Path(args.path))
    query = args.query

    if query.lower().startswith("0x"):
        try:
            wanted = int(query, 16)
        except ValueError:
            print(f"not a hex value: {query}", file=sys.stderr)
            return 2
        hits = [s for s in symbols if s.hash == wanted]
    else:
        hits = [s for s in symbols if s.name == query]

    if not hits:
        print(f"no match for {query}", file=sys.stderr)
        return 1
    for s in hits:
        print(s.as_row())
    return 0


# ---------------------------------------------------------------------------
# Self-test — synthetic data only
# ---------------------------------------------------------------------------


def _record(value: int, name: str) -> bytes:
    """Build one synthetic record, padded like the real table."""
    body = struct.pack("<Q", value) + MAGIC + name.encode("ascii") + b"\0"
    pad = (-len(body)) % RECORD_ALIGN
    return body + b"\0" * pad


def _synthetic_pe(payload: bytes, image_base: int, vaddr: int) -> tuple[bytes, int]:
    """Minimal PE32+ with one section holding `payload`. Returns (blob, raw_ptr)."""
    e_lfanew = 0x80
    raw_ptr = 0x400
    size_opt = 0xF0

    blob = bytearray(raw_ptr + len(payload))
    blob[0:2] = b"MZ"
    struct.pack_into("<I", blob, 0x3C, e_lfanew)
    blob[e_lfanew : e_lfanew + 4] = b"PE\0\0"

    coff = e_lfanew + 4
    struct.pack_into("<H", blob, coff + 2, 1)  # NumberOfSections
    struct.pack_into("<H", blob, coff + 16, size_opt)  # SizeOfOptionalHeader

    opt = coff + 20
    struct.pack_into("<H", blob, opt, 0x20B)  # PE32+
    struct.pack_into("<Q", blob, opt + 24, image_base)

    sec = opt + size_opt
    blob[sec : sec + 8] = b".rdata\0\0"
    struct.pack_into("<IIII", blob, sec + 8, len(payload), vaddr, len(payload), raw_ptr)

    blob[raw_ptr : raw_ptr + len(payload)] = payload
    return bytes(blob), raw_ptr


def cmd_selftest(_args: argparse.Namespace) -> int:
    failures: list[str] = []

    def check(label: str, got, want) -> None:
        if got != want:
            failures.append(f"{label}: got {got!r}, want {want!r}")

    # Recovery of well-formed records, using the real values read from the
    # binary at 0x141cf4cd0.
    known = [
        (0x605DAFDFF7A6493D, "blue_tube1_ai_navpt1"),
        (0x605DAFDFF7A6493E, "blue_tube1_ai_navpt2"),
        (0xA10F4884CFAFC570, "blue_tube1_exit"),
        (0xA10F4B84CFAFC570, "blue_tube2_exit"),
    ]
    payload = b"".join(_record(h, n) for h, n in known)
    found = scan_buffer(payload)
    check("record count", len(found), len(known))
    check("pairs", [(s.hash, s.name) for s in found], known)
    check("all aligned", all(s.aligned for s in found), True)

    # The XOR-linearity noted in ECHOVR_AI_NOTES.md §1, as a regression guard on
    # the constants above rather than a claim about the algorithm.
    a = dict(known)[0xA10F4884CFAFC570]
    _ = a
    d1 = 0xA10F4884CFAFC570 ^ 0xA10F4B84CFAFC570
    check("exit hash delta", d1, (ord("1") ^ ord("2")) << 40)

    # Garbage rejection: the magic occurring in data that is not a record.
    noise = b"\xff" * 8 + MAGIC + b"\x01\x02\x03\x00"
    check("binary name rejected", len(scan_buffer(noise)), 0)

    # Magic at the very start has no room for a hash.
    check("truncated head rejected", len(scan_buffer(MAGIC + b"abc\0")), 0)

    # Unterminated name at end of buffer.
    check("unterminated rejected", len(scan_buffer(b"\x00" * 8 + MAGIC + b"abc")), 0)

    # A name longer than MAX_NAME is a failed NUL scan, not a symbol.
    long_name = b"\x00" * 8 + MAGIC + b"a" * (MAX_NAME + 8) + b"\0"
    check("overlong rejected", len(scan_buffer(long_name)), 0)

    # PE mapping.
    image_base, vaddr = 0x140000000, 0x1CF4000
    blob, raw_ptr = _synthetic_pe(payload, image_base, vaddr)
    mapper = PEMapper.parse(blob)
    check("PE parsed", mapper is not None, True)
    if mapper:
        check("image base", mapper.image_base, image_base)
        mapped = scan_buffer(blob, mapper)
        # First record sits at the start of the section.
        check("first VA", mapped[0].va, image_base + vaddr)
        check("offset outside sections", mapper.to_va(0), None)

    # Conflict detection is what catches a wrong format hypothesis.
    dupes = _record(1, "same") + _record(2, "same")
    seen: dict[str, set[int]] = {}
    for s in scan_buffer(dupes):
        seen.setdefault(s.name, set()).add(s.hash)
    check("conflict visible", len(seen["same"]), 2)

    if failures:
        for f in failures:
            print(f"FAIL  {f}", file=sys.stderr)
        return 1
    print("all self-tests passed")
    return 0


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="dump every symbol record")
    s.add_argument("path", help="echovr.exe")
    s.add_argument("-o", "--output", default=None, help="write here instead of stdout")
    s.add_argument("--json", action="store_true", help="JSON instead of TSV")
    s.set_defaults(func=cmd_scan)

    g = sub.add_parser("grep", help="filter records by name")
    g.add_argument("path", help="echovr.exe")
    g.add_argument("pattern")
    g.add_argument("--regex", action="store_true", help="treat pattern as a regex")
    g.set_defaults(func=cmd_grep)

    lk = sub.add_parser("lookup", help="resolve a name to a hash, or a hash to a name")
    lk.add_argument("path", help="echovr.exe")
    lk.add_argument("query", help="a symbol name, or 0x-prefixed hash")
    lk.set_defaults(func=cmd_lookup)

    t = sub.add_parser("selftest", help="run synthetic validation")
    t.set_defaults(func=cmd_selftest)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
