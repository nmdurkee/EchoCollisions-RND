# Echo VR — package/manifest format and the on-disk CPhysicsResource

Decoded 2026-09-04 against the real install on this machine
(`C:\echovr\ready-at-dawn-echo-arena`, server build, echovr.exe dated 2023-05-24).
This **supersedes** `ECHOVR_COLLISION_NOTES.md` §5/§8 and `RESOURCE_CONTAINER.md`
§2's "scan for 0x28 records" workaround: this build has **no `shared/` store**.
Everything is in two packed files plus a manifest.

Result: the arena's full static collision mesh is extracted —
`arena/mpl_arena_a_collision.obj` / `.npz` (9 bodies, 53,545 verts, 91,656 tris),
raw resource in `arena/mpl_arena_a_CPhysicsResource.bin`. No game session needed.

---

## 1. Where the data lives

```
<gamedir>\_data\5932408047\rad15\win10\
    manifests\2b47aab238f60515              1.5 KB   (small "core" package, 43 resources)
    manifests\48037dc70b0ecab2              1.9 MB   (main package, 69,651 resources)
    packages\2b47aab238f60515_0            16 MB
    packages\48037dc70b0ecab2_0 / _1 / _2  2.1 GB + 2.1 GB + 0.7 GB
```

Package files are **back-to-back raw zstd frames** (magic `28 b5 2f fd` at offset 0),
no per-frame header. Manifests are wrapped in the 0x18-byte `ZSTD` container from
`RESOURCE_CONTAINER.md` §1 (`echovr_collision.decompress()` handles it).

## 2. Manifest layout (after decompression)

Header 0xC0 bytes = three array descriptors at `0x00`, `0x40`, `0x80`
(`+0x10` byte size, `+0x38` **count** — use this one; `+0x30` can be capacity).
Arrays follow immediately, in order A, B, C, no padding.

| Array | Stride | Fields | Meaning |
|---|---|---|---|
| A | 0x20 | `u64 typeSym, u64 nameSym, u64 loc, u32 size, u32 align` | **resource locator**: `loc & 0xffffffff` = frame index into C, `loc >> 32` = byte offset inside that frame's *uncompressed* data |
| B | 0x28 | `u64 typeSym, u64 nameSym, u128 contentHash, u64 buildStamp` | the entry `LookupResourcePath` walks (see RESOURCE_CONTAINER §2); parallel to A |
| C | 0x10 | `u32 pkgFileIdx, u32 fileOffset, u32 csize, u32 usize` | **frame table**; one trailing entry per package file with `usize==0` (offset = file size) is an end marker |

Verified: every C entry matches the zstd frames enumerated with
`ZstdDecompressor().decompressobj()` (offsets and both sizes, byte-exact).
Frames are ≤ ~4 MB uncompressed (aligned to 0x100000 + a small header).

To fetch a resource: `pkg, off, cs, us = C[frame]`; seek `off` in
`packages\<manifest>_<pkg>`, read `cs` bytes, zstd-decompress, slice `[off32 : off32+size]`.

## 3. Symbols — the CSymbol64 hash (verified 3/3 against the exe's symbol table)

Seed table is built at startup by `0x1400cfcd0` from polynomial
`0x95ac9329ac4bc9b5`. The decompiler's literal form is the one that verifies
(the textbook CRC-64 table does **not**):

```python
POLY=0x95ac9329ac4bc9b5; M=(1<<64)-1
T=[]
for i in range(256):
    v=(POLY<<1)&M if i&0x80 else 0
    if i&0x40: v=0xbef5b57af4dc5adf if i&0x80 else POLY
    for bit in (0x20,0x10,8,4,2,1):
        v=((v*2)^POLY)&M if i&bit else (v*2)&M
    T.append((v*2)&M)
def sym(name, seed=M):                      # CSymbol64::Lookup @ 0x1400ce120
    v=seed
    for ch in name.encode():
        c=ch+32 if 65<=ch<=90 else ch       # ASCII tolower
        v=(T[(v>>56)&0xff]^c^((v<<8)&M))&M
    return v
```

`sym("mpl_arena_a") == 0x576ed3f8428ebc4b`, `sym("CPhysicsResource") == 0xf41ae3b4afa07479`.

**Resource type symbols are platform-qualified**: `typeSym = sym("Win10", seed=sym(ClassName))`
for the main variant and `sym("Win10GPU", seed=…)` for the GPU/bulk variant
(`CResourceID_BuildPath` @ `0x140fa19a0`; suffix table at `0x142026220` = `""`, `"GPU"`).
All 233 types in the manifest resolve this way. Relevant ones:

| typeSym | class |
|---|---|
| `b7d338793fa37832` | **CPhysicsResource/Win10** (2,243 entries — one per physics-bearing actor; the level's is the big one) |
| `e8e38d7781a338a6` | CGameLevelResource/Win10 |
| `358b53c17825d154` | CBVHResource/Win10 |
| `4230b4e0957b5462` | CMaterialTypesBVHResource/Win10 |
| `bce9c410b354b078` | CSVOResource/Win10 (nav octree) |
| `74ede05b09640cea` | CR15BounceCR/Win10 (10 entries — the disc's restitution values live in one of these) |
| `4e426f88c1b5d7ac` / `e642bfb1abcf76df` | CGMeshListResource Win10 / Win10GPU (visual meshes) |

Names resolve through the exe's own hash→name table (`echovr_symbols.py scan`; 7,992 records);
map names like `mpl_arena_a`, `mpl_lobby_b_arena`, `mpl_combat_*`, `mpl_tutorial_*` are all present.

The arena physics resource: type `b7d338793fa37832`, name `576ed3f8428ebc4b`,
frame 6139, offset 0, size 13,962,648 (package `_0`? — see frame table).

## 4. CPhysicsResource on disk (kinematic bodies)

The blob is a sequence of **bodies**; each body is

```
[header 0x2B4] [verts NV × float3] [tris NT × 0x34] [edges NE × 0x30] [tail: acceleration data]
```

Header fields (relative to body start; body 0's header also carries resource-level fields):

| Offset | Body 0 | Others | Meaning |
|---|---|---|---|
| `+0x04` | 1 | 0 | resource flag/version |
| `+0x14`,`+0x18` | 3.0, 0.5 | 0 | grid cell size / ? (matches `zone+0x148` cell size idea) |
| `+0x20` | **9** | 0 | **number of bodies in the resource** |
| `+0x34` / `+0x44` / `+0x54` | NV / NT / NE | same | counts (each in a 0x10 block, count in the last dword) |
| `+0x6C` / `+0x70` / `+0x74` | NV / NT / NE | same | counts again |
| `+0x78..+0xBF` | 9×+INF, 9×−INF | — | bounds, **uninitialised on disk** (computed at load — why the runtime-layout signature scans failed) |
| `+0xDC` | 261 | varies | count (masks? parts?) — undecoded |
| `+0x10C` / `+0x11C` | NV / NE | same | map / edge counts again |
| `+0x1A4` | 5 | 4–7 | ? |
| `+0x1BC` | 52 | varies | count — undecoded |
| `+0x29C` | NT | same | |

**Triangle record, 0x34 bytes, 13 × u32** (this is the notes' "dynamic body"
layout; the 0x5C/u24 layout only exists in memory after `SetupKinematicBodies`):

```
[0..2]  vertex indices (u32)
[3]     material id  (0xFFFFFFFF = default; the arena has 27 single-triangle special materials 0..50)
[4],[5] mostly -1 (flags / neighbour?)      [6..8] edge indices (< NE)
[9],[10] always -1                           [11],[12] always 0
```

Edge record 0x30 = 12 × u32: cols 0–2 and 6, 10 are vertex indices; rest floats/ids. Not needed for bounce prediction.

Tail after the edges (per body, 3.6 KB – 373 KB) starts with 0x30-stride records
`(3 × u16-pair, 9 floats)` — k-DOP / grid data. **Undecoded; not needed for geometry.**

Finding bodies robustly: scan for 0x34 records with `[9]==[10]==-1`, `[11]==[12]==0`,
three distinct indices; a run ≥ 50 is a triangle table; NV = max index + 1; the
vertex table is the NV×12 bytes before it; the header is 0x2B4 before that.

### The nine arena bodies (world-space; identity transform assumed — see §5)

| # | NV | NT | bbox x | bbox y | bbox z | guess |
|---|---|---|---|---|---|---|
| 0 | 11742 | 21451 | ±16.1 | −11.3..9.0 | ±78.6 | main shell incl. tubes; 329 connected pieces |
| 1 | 56 | 104 | ±2.1 | −1.3..0.9 | ±29.1 | two small panels at z=±29 |
| 2 | 12736 | 20135 | ±16.1 | −11.4..8.4 | −77.8..78.6 | second full-arena hull (different collision layer?) |
| 3 | 1200 | 1200 | ±5.2 | −7.9..5.5 | ±60.7 | strips (NT==NV) |
| 4 | 11846 | 20920 | ±16.1 | ±8.4 | ±72.9 | third full-arena hull |
| 5 | 8150 | 15300 | ±5.3 | −7.8..5.5 | ±60.8 | |
| 6 | 6519 | 10562 | ±8.0 | −8.2..2.7 | ±78.5 | |
| 7 | 144 | 256 | ±4 | ±10 | ±21.5 | |
| 8 | 1152 | 1728 | ±13.1 | −7.8..1.3 | ±70.1 | |

Backboard-shaped pieces (3.4 × 1.7 × 0.2 m) sit at z = ±36 in body 0.

## 5. Open questions (ordered by impact on the predictor)

1. **Which bodies does the disc collide with?** Three full-size hulls exist (0, 2, 4);
   they are probably per-collision-layer (disc / player / camera?). The per-body
   collision masks are in the undecoded tail or the `+0xDC` table. Empirical route:
   fit replay bounce points to each hull's surface and keep the one they land on.
2. **Body transforms.** Bodies are assumed world-space (body 0's bbox is centred and
   arena-shaped). The runtime transform comes from the owning actor
   (`CPhKinematicBody+0x208`); for the level actor it should be identity. Verify
   against replay bounce positions.
3. Header fields `+0xDC`, `+0x1A4`, `+0x1BC` and the tail tables.
4. `CR15BounceCR` entries (type `74ede05b09640cea`): the disc's actor JSON is
   compiled into one of these 10 — decode it to read `parallelrestitution` etc.
   directly instead of fitting.

## 6a. Dynamic-body CPhysicsResource (e.g. the disc)

Same body layout as §4 (`[hdr 0x2B4][verts][tris ×0x34][edges ×0x30][tail]`) but the
header carries `300.0, 300.0, 300.0` at `+0x08` (not ±INF) and `+0x20 = 1` body. In
dynamic bodies the triangle's `[3]` column is **not** a material id (it varies 0..113
per triangle). The disc: name `ca0c2a1dbd51f6db`, 40 verts / 58 tris, two rings
r = 0.309 / 0.299 m at z = ∓0.02 (see `ECHOVR_BOUNCE_NOTES.md` §3b). A second
puck-like body `8710789457dd6bda` (r 0.138, 26 verts) exists — not the disc.

## 7. Component resources (`*CR/Win10`) — e.g. `CR15BounceCR`

Level-scoped: one per map, holding the component data (CD) for every actor in that
map that has the component. Layout (verified on `mpl_arena_a`, `mpl_lobby_b_arena`,
`mpl_tutorial_arena`, `mpl_combat_dyson`):

```
+0x00 u64 0            +0x08 u64 totalEntryBytes     +0x1C u32 1
+0x28 u64 count        +0x30 u64 count
+0x38 entries, each 0x20 header + CD:
      +0x00 u64 componentTypeSym   (Bounce: 7a8046009a26458d = the CS type id)
      +0x08 u64 actorId            (in-level actor symbol; NOT a resource name)
      +0x10 u32 0xFFFFFFFF (combat maps: 0x000FFFFF)   +0x14 u32 0
      +0x18 u32 cdSize (0x30 for SR15BounceCD)        +0x1C u32 0
      +0x20 SR15BounceCD as the JSON reader lays it out (flags, 4 floats at +0x18..+0x2C of the CD)
```

Actor ids link component resources of the same level together (`CR15FrisbeeCR` and
`CR15BounceCR` share `91a4a5864d973e76` → the disc). The link from actor id to its
`CPhysicsResource` *name* was not found (not an 8-aligned symbol in `CPhysicsCR`).

## 6. Provenance (functions)

| VA | What |
|---|---|
| `0x140f9f6b0` / `0x140f9f310` | manifest array readers (0x28 entry; three arrays) |
| `0x1400cfcd0` | CSymbol64 seed-table initialiser (poly `0x95ac9329ac4bc9b5`) |
| `0x1400ce120` | `CSymbol64::Lookup` — the hash loop |
| `0x140fa19a0` | `CResourceID_BuildPath` — `"Win10"` + suffix, seeded with the class hash |
| `0x1410510c0` | `CFactory<CPhysicsResource>` — registers `(0xa71a14664bd4e047, 0xf41ae3b4afa07479)` |
| `0x14104c180`, `0x14104fd10` | `CBinaryStreamInspectorAttach` users for COccluderMesh / CSVO resources (same attach pattern; "Attach size doesn't match stream size") |
