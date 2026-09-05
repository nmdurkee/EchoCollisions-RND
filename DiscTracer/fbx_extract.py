"""
Minimal binary-FBX geometry extractor (no SDK needed). Parses the
documented Kaydara binary FBX node tree and pulls every Geometry node's
Vertices (float64 array) + PolygonVertexIndex (int32 array, negative
value = last index of a polygon, stored as ~index). Handles zlib
(type 1) compressed property arrays. Enough to evaluate the DemoViewer
repo's alternative arena meshes as collision-surface candidates.

Usage: python fbx_extract.py <file.fbx>   (prints per-geometry stats)
Import: extract_geometries(path) -> list of (verts Nx3 float64, faces Mx3 int64)
"""

import struct
import sys
import zlib

import numpy as np


def _read_node(f, version):
    # >= 7500 uses 8-byte offsets
    wide = version >= 7500
    fmt = "<QQQ" if wide else "<III"
    hdr_size = 25 if wide else 13
    hdr = f.read((24 if wide else 12) + 1)
    if len(hdr) < hdr_size:
        return None
    end_offset, num_props, prop_list_len = struct.unpack(fmt, hdr[:-1])
    name_len = hdr[-1]
    if end_offset == 0:
        return None  # null terminator record
    name = f.read(name_len).decode("latin-1")
    props = []
    for _ in range(num_props):
        props.append(_read_property(f))
    children = []
    if f.tell() < end_offset:
        while f.tell() < end_offset - hdr_size:
            child = _read_node(f, version)
            if child is None:
                break
            children.append(child)
        f.seek(end_offset)
    return (name, props, children)


def _read_property(f):
    tc = f.read(1)
    if tc == b"Y":
        return struct.unpack("<h", f.read(2))[0]
    if tc == b"C":
        return f.read(1) != b"\x00"
    if tc == b"I":
        return struct.unpack("<i", f.read(4))[0]
    if tc == b"F":
        return struct.unpack("<f", f.read(4))[0]
    if tc == b"D":
        return struct.unpack("<d", f.read(8))[0]
    if tc == b"L":
        return struct.unpack("<q", f.read(8))[0]
    if tc in (b"f", b"d", b"l", b"i", b"b"):
        length, encoding, comp_len = struct.unpack("<III", f.read(12))
        elem = {b"f": ("<f", 4), b"d": ("<d", 8), b"l": ("<q", 8), b"i": ("<i", 4), b"b": ("<b", 1)}[tc]
        if encoding == 1:
            raw = zlib.decompress(f.read(comp_len))
        else:
            raw = f.read(length * elem[1])
        dtype = {b"f": np.float32, b"d": np.float64, b"l": np.int64, b"i": np.int32, b"b": np.int8}[tc]
        return np.frombuffer(raw, dtype=dtype)
    if tc == b"S" or tc == b"R":
        length = struct.unpack("<I", f.read(4))[0]
        data = f.read(length)
        return data.decode("latin-1", errors="replace") if tc == b"S" else data
    raise ValueError(f"unknown FBX property type {tc!r} at offset {f.tell()}")


def _walk(nodes, name, out):
    for n in nodes:
        if n[0] == name:
            out.append(n)
        _walk(n[2], name, out)


def extract_geometries(path):
    with open(path, "rb") as f:
        magic = f.read(23)
        if not magic.startswith(b"Kaydara FBX Binary"):
            raise ValueError("not a binary FBX file")
        version = struct.unpack("<I", f.read(4))[0]
        top = []
        while True:
            node = _read_node(f, version)
            if node is None:
                break
            top.append(node)

    geoms = []
    geo_nodes = []
    _walk(top, "Geometry", geo_nodes)
    for g in geo_nodes:
        verts = None
        idx = None
        for child in g[2]:
            if child[0] == "Vertices" and child[1]:
                verts = np.asarray(child[1][0], dtype=np.float64).reshape(-1, 3)
            elif child[0] == "PolygonVertexIndex" and child[1]:
                idx = np.asarray(child[1][0], dtype=np.int64)
        if verts is None or idx is None:
            continue
        # triangulate: polygons are delimited by a negative index (= ~last)
        faces = []
        poly = []
        for i in idx:
            if i < 0:
                poly.append(~i)
                for k in range(1, len(poly) - 1):
                    faces.append((poly[0], poly[k], poly[k + 1]))
                poly = []
            else:
                poly.append(i)
        geoms.append((verts, np.asarray(faces, dtype=np.int64)))
    return geoms


if __name__ == "__main__":
    for path in sys.argv[1:]:
        print(f"=== {path} ===")
        try:
            for i, (v, fc) in enumerate(extract_geometries(path)):
                print(f"  geometry {i}: {len(v)} verts, {len(fc)} tris")
                print(f"    X {v[:,0].min():9.2f} .. {v[:,0].max():9.2f}")
                print(f"    Y {v[:,1].min():9.2f} .. {v[:,1].max():9.2f}")
                print(f"    Z {v[:,2].min():9.2f} .. {v[:,2].max():9.2f}")
        except Exception as e:
            print(f"  FAILED: {e}")
