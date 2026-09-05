#!/usr/bin/env python3
"""
echovr_collision.py — extract static level collision geometry from Echo VR assets.

Reverse-engineered from echovr.exe (RAD R15 engine). Walls and floors are
triangles in kinematic physics bodies inside a CPhysicsResource.

Layout (see the format notes for provenance):

  SPhKinematicBodyCD, 0x298 bytes:
    +0x010  float[3]   bounds min
    +0x034  float[3]   bounds max
    +0x180  CMemBlock  vertices   (stride 0x0C, float3, body-local)
    +0x1B8  CMemBlock  vertex->triangle map (u32)
    +0x1F0  CMemBlock  triangles  (stride 0x5C)
    +0x228  CMemBlock  edges      (stride 0x18)
    +0x260  CMemBlock  collision masks (stride 0x30)

  CMemBlock, 0x38 bytes:  +0x00 data pointer, +0x30 element count

  Triangle record, 0x5C bytes:
    +0x00  u24  vertex index 0
    +0x03  u24  vertex index 1
    +0x06  u24  vertex index 2
    +0x09  u8   material id
    rest: precomputed plane / k-DOP data

CONFIRMED offsets came from decompiled reads. Three element counts (vertices,
map, edges) are inferred from the CMemBlock stride and are validated at runtime
rather than trusted.

UNVERIFIED: how pointers are encoded on disk. The engine appears to load
resources in place (cbinarystreaminspectorattach.h), but whether a stored
CMemBlock pointer is an absolute blob offset, self-relative, or struct-relative
was never traced. This tool tries each scheme and reports which one validates.

Usage:
    python echovr_collision.py scan    <shared_dir> [--limit N]
    python echovr_collision.py extract <path> [-o out.obj] [--merge]

`scan` sweeps a content-addressed store and reports blobs containing collision
geometry. `extract` pulls the geometry out of one blob (or a whole directory
with --merge) and writes Wavefront OBJ.
"""

from __future__ import annotations

import argparse
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Format constants
# ---------------------------------------------------------------------------

KDEF_SIZE = 0x298
MEMBLOCK_SIZE = 0x38
MEMBLOCK_COUNT_OFF = 0x30

OFF_BOUNDS_MIN = 0x010
OFF_BOUNDS_MAX = 0x034
OFF_VERTS = 0x180
OFF_VERTMAP = 0x1B8
OFF_TRIS = 0x1F0
OFF_EDGES = 0x228
OFF_MASKS = 0x260

VERT_STRIDE = 0x0C
TRI_STRIDE = 0x5C
EDGE_STRIDE = 0x18

# Sanity ceilings. A body that trips these is a false positive, not a huge map:
# the engine itself refuses kinematic bodies that scan-convert into >100k cells.
MAX_VERTS = 4_000_000
MAX_TRIS = 4_000_000
MIN_TRIS = 4
COORD_LIMIT = 1.0e6  # metres; anything beyond this is not level geometry

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
LZ4_FRAME_MAGIC = b"\x04\x22\x4d\x18"

# Resource container header — see decompress(). Codec tags are the four bytes
# in file order; note that LZ4's has a *leading* space.
CONTAINER_HEADER_SIZE = 0x18
CODEC_NONE = b"NONE"
CODEC_ZSTD = b"ZSTD"
CODEC_LZ4 = b" LZ4"


# ---------------------------------------------------------------------------
# Primitive reads
# ---------------------------------------------------------------------------

def u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def u64(buf: bytes, off: int) -> int:
    return struct.unpack_from("<Q", buf, off)[0]


def u24(buf: bytes, off: int) -> int:
    """Little-endian 24-bit. Triangle indices are packed three-to-nine-bytes."""
    return buf[off] | (buf[off + 1] << 8) | (buf[off + 2] << 16)


def vec3(buf: bytes, off: int) -> tuple[float, float, float]:
    return struct.unpack_from("<3f", buf, off)


def finite(v: tuple[float, ...]) -> bool:
    return all(x == x and abs(x) < COORD_LIMIT for x in v)


# ---------------------------------------------------------------------------
# Pointer resolution
#
# The stored CMemBlock pointer is one of these encodings; which one is an open
# question in the analysis, so resolve() tries all three and the caller keeps
# whichever produces geometry that validates.
# ---------------------------------------------------------------------------

PTR_SCHEMES = ("absolute", "self_relative", "struct_relative")


def resolve(raw: int, blob_len: int, field_pos: int, struct_pos: int, scheme: str) -> int | None:
    """Map a stored pointer value to a blob offset, or None if out of range."""
    if raw == 0:
        return None
    if scheme == "absolute":
        off = raw
    elif scheme == "self_relative":
        off = field_pos + _signed64(raw)
    elif scheme == "struct_relative":
        off = struct_pos + _signed64(raw)
    else:
        raise ValueError(f"unknown pointer scheme: {scheme}")
    return off if 0 <= off < blob_len else None


def _signed64(v: int) -> int:
    return v - (1 << 64) if v >= (1 << 63) else v


# ---------------------------------------------------------------------------
# Parsed geometry
# ---------------------------------------------------------------------------

@dataclass
class CollisionMesh:
    """One kinematic body's triangle mesh, in body-local space."""
    source: str
    struct_off: int
    scheme: str
    bounds_min: tuple[float, float, float]
    bounds_max: tuple[float, float, float]
    vertices: list[tuple[float, float, float]] = field(default_factory=list)
    triangles: list[tuple[int, int, int]] = field(default_factory=list)
    materials: list[int] = field(default_factory=list)

    def __str__(self) -> str:
        lo = " ".join(f"{c:.1f}" for c in self.bounds_min)
        hi = " ".join(f"{c:.1f}" for c in self.bounds_max)
        return (f"+0x{self.struct_off:06X}  {len(self.vertices):>7} verts  "
                f"{len(self.triangles):>7} tris  [{lo}] .. [{hi}]  ({self.scheme})")


# ---------------------------------------------------------------------------
# Container handling
# ---------------------------------------------------------------------------

def decompress(data: bytes) -> tuple[bytes, str]:
    """
    Return (payload, how).

    Resource files begin with a 0x18-byte container header, decoded from the
    decompressor at 0x14152AE50 (csyscompression.cpp):

        +0x00  char[4]  codec: "NONE" | "ZSTD" | " LZ4"   (note leading space)
        +0x04  u32      allocation alignment, 0 = default
        +0x08  u64      uncompressed size
        +0x10  u64      compressed size
        +0x18           payload

    An unrecognised codec means the whole file is the payload — the engine
    passes it through untouched in that case.
    """
    if len(data) >= CONTAINER_HEADER_SIZE:
        codec = data[0:4]
        if codec in (CODEC_NONE, CODEC_ZSTD, CODEC_LZ4):
            raw_size = struct.unpack_from("<Q", data, 0x08)[0]
            comp_size = struct.unpack_from("<Q", data, 0x10)[0]
            payload = data[CONTAINER_HEADER_SIZE:]
            tag = codec.decode("ascii", "replace").strip() or "NONE"
            if codec == CODEC_NONE:
                return payload[:raw_size], f"{tag} {raw_size}B"
            try:
                fn = _zstd if codec == CODEC_ZSTD else _lz4
                out = fn(payload[:comp_size] if comp_size else payload)
                if raw_size and len(out) != raw_size:
                    print(f"    ! {tag}: got {len(out)}B, header said {raw_size}B",
                          file=sys.stderr)
                return out, f"{tag} {comp_size}->{len(out)}B"
            except Exception as exc:  # noqa: BLE001 — fall through to the scan
                print(f"    ! {tag} decompress failed: {exc}", file=sys.stderr)

    # Fallback: no recognised container. Scan for a bare compression frame in
    # case this build wraps payloads differently than the one analysed.
    for magic, name, fn in ((ZSTD_MAGIC, "zstd", _zstd), (LZ4_FRAME_MAGIC, "lz4", _lz4)):
        pos = data.find(magic, 0, 0x200)
        if pos < 0:
            continue
        try:
            return fn(data[pos:]), f"{name}@0x{pos:X} (scanned)"
        except Exception as exc:  # noqa: BLE001
            print(f"    ! {name} scan-decompress failed: {exc}", file=sys.stderr)
    return data, "raw"


def _zstd(data: bytes) -> bytes:
    try:
        import zstandard
    except ImportError:
        raise RuntimeError("zstd payload needs `pip install zstandard`") from None
    return zstandard.ZstdDecompressor().decompressobj().decompress(data)


def _lz4(data: bytes) -> bytes:
    try:
        import lz4.frame
    except ImportError:
        raise RuntimeError("lz4 payload needs `pip install lz4`") from None
    return lz4.frame.decompress(data)


# ---------------------------------------------------------------------------
# Detection and parsing
# ---------------------------------------------------------------------------

def check_bounds(blob: bytes, pos: int) -> tuple[tuple[float, ...], tuple[float, ...]] | None:
    """
    The cheap discriminator: a plausible AABB at +0x10 / +0x34. Rejects almost
    every offset in a blob, and is independent of the pointer scheme — so it is
    hoisted out of the scheme loop rather than repeated three times.
    """
    if pos + KDEF_SIZE > len(blob):
        return None
    lo = vec3(blob, pos + OFF_BOUNDS_MIN)
    hi = vec3(blob, pos + OFF_BOUNDS_MAX)
    if not (finite(lo) and finite(hi)):
        return None
    if not all(a < b for a, b in zip(lo, hi)):
        return None
    return lo, hi


def try_parse(blob: bytes, pos: int, scheme: str, source: str,
              bounds: tuple[tuple[float, ...], tuple[float, ...]] | None = None) -> CollisionMesh | None:
    """
    Attempt to read a kinematic body definition at `pos` under one pointer
    scheme. Returns None unless everything cross-validates, so this doubles as
    the detector — a false positive has to satisfy every check below.

    `bounds` may be passed in when the caller has already run check_bounds.
    """
    checked = bounds if bounds is not None else check_bounds(blob, pos)
    if checked is None:
        return None
    lo, hi = checked

    vert_ptr = resolve(u64(blob, pos + OFF_VERTS), len(blob), pos + OFF_VERTS, pos, scheme)
    tri_ptr = resolve(u64(blob, pos + OFF_TRIS), len(blob), pos + OFF_TRIS, pos, scheme)
    if vert_ptr is None or tri_ptr is None:
        return None

    n_verts = u64(blob, pos + OFF_VERTS + MEMBLOCK_COUNT_OFF)
    n_tris = u64(blob, pos + OFF_TRIS + MEMBLOCK_COUNT_OFF)
    if not (0 < n_verts <= MAX_VERTS) or not (MIN_TRIS <= n_tris <= MAX_TRIS):
        return None
    if vert_ptr + n_verts * VERT_STRIDE > len(blob):
        return None
    if tri_ptr + n_tris * TRI_STRIDE > len(blob):
        return None

    verts: list[tuple[float, float, float]] = []
    for i in range(n_verts):
        v = vec3(blob, vert_ptr + i * VERT_STRIDE)
        if not finite(v):
            return None
        verts.append(v)

    # Vertices must lie inside the body's own stated bounds. This is the check
    # that kills nearly every coincidental match.
    pad = 1e-2
    for v in verts:
        for c, a, b in zip(v, lo, hi):
            if not (a - pad <= c <= b + pad):
                return None

    tris: list[tuple[int, int, int]] = []
    mats: list[int] = []
    for i in range(n_tris):
        rec = tri_ptr + i * TRI_STRIDE
        i0, i1, i2 = u24(blob, rec), u24(blob, rec + 3), u24(blob, rec + 6)
        if i0 >= n_verts or i1 >= n_verts or i2 >= n_verts:
            return None
        if i0 == i1 or i1 == i2 or i0 == i2:  # degenerate — wrong stride or scheme
            return None
        tris.append((i0, i1, i2))
        mats.append(blob[rec + 9])

    return CollisionMesh(source, pos, scheme, lo, hi, verts, tris, mats)


def candidate_offsets(blob: bytes):
    """
    Pre-filter to offsets carrying a plausible AABB, so full validation runs on
    a handful of positions instead of every 8-byte step.

    Uses numpy when available — on a multi-megabyte blob that is the difference
    between seconds and minutes. Falls back to a plain stride when it is not
    installed; results are identical either way.
    """
    span = len(blob) - KDEF_SIZE
    if span <= 0:
        return []
    try:
        import numpy as np
    except ImportError:
        return range(0, span, 8)

    f = np.frombuffer(blob[: (len(blob) // 4) * 4], dtype="<f4")
    # Struct at 8k puts bounds min at float index 2k+4, max at 2k+13.
    cols = [f[4::2], f[5::2], f[6::2], f[13::2], f[14::2], f[15::2]]
    n = min(min(len(c) for c in cols), span // 8 + 1)
    if n <= 0:
        return []
    mn0, mn1, mn2, mx0, mx1, mx2 = (c[:n] for c in cols)

    with np.errstate(invalid="ignore"):
        ok = (
            np.isfinite(mn0) & np.isfinite(mn1) & np.isfinite(mn2)
            & np.isfinite(mx0) & np.isfinite(mx1) & np.isfinite(mx2)
            & (mn0 < mx0) & (mn1 < mx1) & (mn2 < mx2)
            & (np.abs(mn0) < COORD_LIMIT) & (np.abs(mx0) < COORD_LIMIT)
            & (np.abs(mn1) < COORD_LIMIT) & (np.abs(mx1) < COORD_LIMIT)
            & (np.abs(mn2) < COORD_LIMIT) & (np.abs(mx2) < COORD_LIMIT)
        )
    return (np.nonzero(ok)[0] * 8).tolist()


def find_meshes(blob: bytes, source: str, limit: int | None = None) -> list[CollisionMesh]:
    """
    Sweep a blob for kinematic body definitions.

    Pointers are 8-byte aligned in the struct, so candidates are 8-byte aligned.
    Once one scheme produces a hit we stay on it for the rest of the blob — a
    single file will not mix encodings.
    """
    found: list[CollisionMesh] = []
    schemes = list(PTR_SCHEMES)
    locked: str | None = None

    for pos in candidate_offsets(blob):
        bounds = check_bounds(blob, pos)
        if bounds is None:
            continue
        for scheme in ([locked] if locked else schemes):
            mesh = try_parse(blob, pos, scheme, source, bounds)
            if mesh is not None:
                found.append(mesh)
                locked = scheme
                if limit and len(found) >= limit:
                    return found
                break
    return found


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_obj(meshes: list[CollisionMesh], out: Path) -> tuple[int, int]:
    """Write meshes to Wavefront OBJ. Returns (vertex count, face count)."""
    total_v = total_f = 0
    with out.open("w", encoding="utf-8") as fh:
        fh.write("# Echo VR static collision geometry\n")
        fh.write(f"# {len(meshes)} kinematic bodies\n")
        base = 1  # OBJ indices are 1-based and continue across groups
        for n, mesh in enumerate(meshes):
            fh.write(f"\no body_{n:04d}_{Path(mesh.source).name}_{mesh.struct_off:06X}\n")
            for x, y, z in mesh.vertices:
                fh.write(f"v {x:.6g} {y:.6g} {z:.6g}\n")
            for i0, i1, i2 in mesh.triangles:
                fh.write(f"f {base + i0} {base + i1} {base + i2}\n")
            base += len(mesh.vertices)
            total_v += len(mesh.vertices)
            total_f += len(mesh.triangles)
    return total_v, total_f


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def load(path: Path) -> bytes | None:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        print(f"  ! {path}: {exc}", file=sys.stderr)
        return None
    if len(raw) < KDEF_SIZE:
        return None
    blob, how = decompress(raw)
    if how != "raw":
        print(f"  · {path.name}: {how}, {len(raw)} -> {len(blob)} bytes")
    return blob


def iter_blobs(root: Path):
    """Yield every file under a content-addressed store, or the file itself."""
    if root.is_file():
        yield root
        return
    for p in sorted(root.rglob("*")):
        if p.is_file():
            yield p


def cmd_scan(args) -> int:
    root = Path(args.path)
    if not root.exists():
        print(f"error: {root} does not exist", file=sys.stderr)
        return 2

    hits = scanned = 0
    schemes: dict[str, int] = {}

    for path in iter_blobs(root):
        scanned += 1
        blob = load(path)
        if blob is None:
            continue
        meshes = find_meshes(blob, str(path), limit=args.limit)
        if not meshes:
            continue
        hits += 1
        rel = path.relative_to(root) if root.is_dir() else path.name
        print(f"\n{rel}")
        for mesh in meshes:
            print(f"  {mesh}")
            schemes[mesh.scheme] = schemes.get(mesh.scheme, 0) + 1

    print(f"\nscanned {scanned} files, {hits} with collision geometry")
    if schemes:
        print("pointer encoding: " + ", ".join(f"{k}={v}" for k, v in schemes.items()))
    else:
        print("no geometry found — see the notes in this file on what that rules out")
    return 0


def cmd_extract(args) -> int:
    root = Path(args.path)
    if not root.exists():
        print(f"error: {root} does not exist", file=sys.stderr)
        return 2

    meshes: list[CollisionMesh] = []
    for path in iter_blobs(root):
        blob = load(path)
        if blob is None:
            continue
        found = find_meshes(blob, str(path))
        if found:
            print(f"  {path.name}: {len(found)} bodies")
            meshes.extend(found)
        if found and not args.merge and root.is_dir():
            break  # first file with geometry wins unless merging

    if not meshes:
        print("no collision geometry found", file=sys.stderr)
        return 1

    out = Path(args.output)
    nv, nf = write_obj(meshes, out)
    print(f"\nwrote {out}: {len(meshes)} bodies, {nv} vertices, {nf} triangles")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="report blobs containing collision geometry")
    s.add_argument("path", help="a shared/ directory, or a single file")
    s.add_argument("--limit", type=int, default=None,
                   help="stop after N bodies per file")
    s.set_defaults(func=cmd_scan)

    e = sub.add_parser("extract", help="write collision geometry to OBJ")
    e.add_argument("path", help="a blob, or a directory to search")
    e.add_argument("-o", "--output", default="collision.obj")
    e.add_argument("--merge", action="store_true",
                   help="merge every body found under a directory into one OBJ")
    e.set_defaults(func=cmd_extract)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
