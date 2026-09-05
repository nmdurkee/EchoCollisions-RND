# Echo VR — static collision geometry format

Reverse-engineered from `echovr.exe` (RAD "R15" engine, x86_64 PE, image base
`0x140000000`, 55,259 functions). Derived by static analysis only — the binary
has ~11.7% naming coverage and **zero** in the physics and resource subsystems,
so every label below came from decompilation and assert strings, not symbols.

Companion files:
- `echovr_collision.py` — parser + OBJ extractor, unit-tested against synthetic data.

Offsets are valid for the build indexed as `echovr.exe`; re-check against any other.

---

## 1. What the engine stores

There is no wall primitive and no floor primitive. Both are **triangles in
kinematic physics bodies**, authored into the level's `CPhysicsResource`.

Physics is in-house (`d:\projects\rad\dev\src\engine\libs\physics\`), not
Havok/PhysX. It solves particles and triangles — narrow-phase profile markers
are `ph|narrow|par-tri`, `tri-par`, `edg-edg`, with triangle k-DOPs for culling.

- **Kinematic** bodies = level geometry. Static vertex array, packed u24 indices.
- **Dynamic** bodies = players, discs, props. Simulated particles, u32 indices.

Different structures. Only the kinematic side is the map.

Each body is scan-converted into a uniform grid at load; zone cell size is at
`zone+0x148`. Blowing the budget produces the assert that confirmed several
offsets below (`cphysicsresourceinstance.cpp:270`):

```
Kinematic body in resource %s is too large! Bounds extend from
(%f, %f, %f) to (%f, %f, %f) and the mesh will scan convert
into over a hundred thousand cells.
```

---

## 2. Runtime pointer chain

```
g_Physics                     0x1420A00F0
  -> CPhZone[]                +0x08   stride 0x1E18, indexed by entity+0x0C
       -> CPhKinematicBody[]  +0x2A8  stride 0x4C0,  count at +0x2D8
            -> body def       +0x38   0x298 bytes
                 -> triangles +0x1F0  stride 0x5C
```

Dynamic bodies sit in a parallel pool on the same zone: `+0x268`, stride
`0xF38`, count at `+0x298`.

---

## 3. `SPhKinematicBodyCD` — the level mesh (0x298 bytes)

Reached via `*(void**)(kinBody + 0x38)`.

| Offset  | Type      | Field                | Status    |
|---------|-----------|----------------------|-----------|
| `+0x010`| float[3]  | bounds min           | confirmed |
| `+0x034`| float[3]  | bounds max           | confirmed |
| `+0x180`| CMemBlock | vertices, stride 0x0C (float3, body-local) | confirmed |
| `+0x1B0`| u64       | vertex count         | INFERRED  |
| `+0x1B8`| CMemBlock | vertex -> triangle map (u32), for material lookup | confirmed |
| `+0x1E8`| u64       | map count            | INFERRED  |
| `+0x1F0`| CMemBlock | triangles, stride 0x5C | confirmed |
| `+0x220`| u64       | triangle count       | confirmed |
| `+0x228`| CMemBlock | edges, stride 0x18   | confirmed |
| `+0x258`| u64       | edge count           | INFERRED  |
| `+0x260`| CMemBlock | collision masks, stride 0x30 | confirmed |
| `+0x290`| u64       | mask count           | confirmed |
| `0x298` | —         | sizeof               | exact     |

**Self-check:** the struct is a 0x180-byte header plus five `CMemBlock`s
(5 x 0x38 = 0x118). `0x180 + 0x118 = 0x298` exactly — which is the stride
`SetupKinematicBodies` uses to walk the array. Two of the five counts were
observed directly at `block+0x30`; the three marked INFERRED follow from that
spacing but were never watched being read.

### `CMemBlock` — 0x38 bytes

Recurs across the entire engine, not just physics. Worth internalising.

| Offset  | Field                                             |
|---------|---------------------------------------------------|
| `+0x00` | data pointer                                      |
| `+0x1C` | flags — tested as `& 6` for owns-heap vs aliases-external |
| `+0x30` | element count                                     |

### Triangle record — 0x5C bytes

| Offset  | Type | Field            |
|---------|------|------------------|
| `+0x00` | u24  | vertex index 0   |
| `+0x03` | u24  | vertex index 1   |
| `+0x06` | u24  | vertex index 2   |
| `+0x09` | u8   | material id      |
| rest    | —    | precomputed plane / k-DOP data |

**Twenty-four-bit little-endian indices** — three of them packed into nine
bytes, sharing byte 9 with the material id. Do *not* read as padded u32. Caps a
body at ~16.7M vertices.

### Local -> world

Kinematic vertices are **body-local**. Column-major 3x4 at `kinBody+0x208`:

```
X axis      +0x208  +0x20C  +0x210
Y axis      +0x214  +0x218  +0x21C
Z axis      +0x220  +0x224  +0x228
translation +0x22C  +0x230  +0x234

world = v.x*X + v.y*Y + v.z*Z + T
```

Further copies at `+0x238` and `+0x268` are previous / interpolated poses,
selected by a blend factor. For a static dump, use `+0x208`.

> **Trap:** dynamic-body particles at `CPhBody+0x408` are *already world-space*.
> Kinematic vertices are not. Mixing these up yields a map collapsed at origin.

---

## 4. Dynamic bodies (not the map, but you will hit them)

### `CPhBody` — 0xF38 bytes, zone `+0x268`

| Offset  | Type  | Field                                        |
|---------|-------|----------------------------------------------|
| `+0x00C`| u32   | zone index — selects the CPhZone             |
| `+0x038`| void* | body definition (0x388 bytes, different layout) |
| `+0x0BC`| u32   | flags; bit 2 = has subbodies                 |
| `+0x408`| CMemBlock | particle positions, stride 0x0C, **world-space** |
| `+0x438`| u64   | particle count                               |
| `+0x440`| CMemBlock | previous positions                       |
| `+0x760`| void* | grid pointer array                           |
| `+0x790`| u64   | grid count                                   |

### Dynamic body definition — `*(void**)(body + 0x38)`

| Offset  | Type  | Field                                          |
|---------|-------|------------------------------------------------|
| `+0x010`| void* | triangles, stride 0x34 — three u32 at +0/+4/+8 |
| `+0x044`| u32   | triangle count                                 |
| `+0x0A8`| void* | subbodies, stride 0x54 — u16 firstTri at +8, u16 count at +10 |
| `+0x0B0`| u32   | subbody count                                  |

---

## 5. On-disk (offline route)

> **2026-09-04 UPDATE — this section is superseded.** This build ships no `shared/` store;
> resources live in `_data/<id>/rad15/win10/packages/*` + `manifests/*`. The package/manifest
> format, the CSymbol64 hash, and the **on-disk CPhysicsResource layout** are decoded in
> `ECHOVR_PACKAGE_FORMAT.md`, and the arena mesh is extracted to `arena/mpl_arena_a_collision.obj`.
> `echovr_collision.py scan` cannot find anything in this build — the on-disk triangle record is
> the 0x34/u32 layout (§4 here), not the 0x5C/u24 runtime layout, and bounds are ±INF on disk.

### Path layout — content-addressed store

One `snprintf` at `0x141528780` builds every resource path:

```
"shared/%02hhx/%02hhx/%016llx%016llx"
    |      |       |       \__ 128-bit content hash, 32 hex chars
    |      \_______\_________ top two bytes of that hash
    \________________________ <gamedir>/shared/
```

No archive to unpack — every resource is a discrete file, sharded two levels.

### Manifest

Resources are keyed by a **pair** of 64-bit symbols (name, type).
`LookupResourcePath` @ `0x140FA2A20` walks entries at `manifest+0x40`, stride `0x28`:

| Offset  | Type | Field                            |
|---------|------|----------------------------------|
| `+0x00` | u64  | name symbol                      |
| `+0x08` | u64  | type symbol                      |
| `+0x10` | u128 | content hash -> the shared/ path |
| `+0x20` | u64  | build stamp                      |

On-disk encoding of the manifest itself: **not decoded**. Sidestepped by
brute-force sweeping every blob under `shared/`.

### Compression — SOLVED, see `RESOURCE_CONTAINER.md`

Every resource file opens with a **0x18-byte header** (not 0x40 — that earlier
reading was a block-alignment detail in the async streaming path, not the file
header):

| Offset  | Type    | Field |
|---------|---------|-------|
| `+0x00` | char[4] | codec: `NONE` / `ZSTD` / ` LZ4` (**leading** space on LZ4) |
| `+0x04` | u32     | allocation alignment |
| `+0x08` | u64     | uncompressed size |
| `+0x10` | u64     | compressed size |
| `+0x18` | —       | payload |

From the decompressor at `0x14152AE50` (`csyscompression.cpp`). Implemented and
tested in `echovr_collision.py` against all three codecs.

### Why memory offsets should apply to file bytes

The engine ships `cbinarystreaminspectorattach.h` alongside its write and
compute-size inspectors. *Attach* means the blob is read once and a reflection
walk repoints struct members into that buffer rather than parsing into fresh
allocations. Corroborated by the `CMemBlock` flag word being tested as `& 6`
throughout — the owns-heap vs aliases-external distinction the scheme needs.

**UNVERIFIED.** Strong inference from header names and flag tests; never traced
end to end through an actual load. If wrong, section 5 needs redoing —
sections 1-4 are unaffected.

Consequence: the stored `CMemBlock` pointer could be an absolute blob offset,
self-relative, or struct-relative. `echovr_collision.py` tries all three and
reports which validates, so the first real run settles this empirically.

---

## 6. Provenance

Addresses are authoritative. **Bracketed labels come from the ReVault DB and
several are auto-generated and wrong** — noted where they misled.

| Address       | DB label | What it established |
|---------------|----------|---------------------|
| `0x1406B5F60` | —        | Kinematic triangle fetch: u24 indices, vertex array at def+0x180 |
| `0x1406DD440` | —        | Particle vs kinematic triangle; the +0x208 transform |
| `0x1406DF660` | —        | Edge array at def+0x228, stride 0x18 |
| `0x1406DAD90` | —        | Material lookup via def+0x1F0, byte at +9 |
| `0x141053020` | `SetupKinematicBodies` | Kin body pool stride 0x4C0; AABB at +0x10 / +0x34 |
| `0x1406B8330` | `Inspect<CBindingsOffsetOfInspector<int>>` — **WRONG**, is `CPhKinematicBody::Setup` | def pointer at kinBody+0x38; mask array |
| `0x1410517C0` | `SetupBodies` | Dynamic body pool; resource container at res+0x70 |
| `0x14066C970` | —        | Dynamic triangles at def+0x10, stride 0x34 |
| `0x14066C690` | —        | `CPhBody::UpdateGridsData`; particle array at +0x408 |
| `0x140709AA0` | —        | Resource container: dyn stride 0x388, kin stride 0x298 |
| `0x141528780` | —        | The `shared/` path format string |
| `0x140FA2A20` | `LookupResourcePath` | Manifest entry layout |
| `0x141525310` | —        | Block reader; the 0x40-byte header |
| `0x141050580` | `CPhysicsResource::CPhysicsResource` | vtable at `0x141CDF4B8` |
| `0x1400ED080` | `Inspect<CPointerFixupInspector>` — **WRONG**, is a UI behavior validator | (cost a detour; ignore this name) |

Ghidra's function boundaries are broken around `0x14066D680` — it merges ~141KB
into one "function". Disassemble at exact VAs there rather than trusting the
function view.

---

## 7. Open questions

1. ~~**Manifest file encoding**~~ — **mostly closed.** `ReadManifestFile`
   (`0x140FA2C30`) and the entry reader (`0x140F9F6B0`) are decoded in
   `RESOURCE_CONTAINER.md` §2. Only the stream's exact alignment arithmetic is
   unresolved; a scan-for-the-entry-table workaround is documented there.
2. ~~**The 0x40-byte header**~~ — **closed.** It is 0x18 bytes; see §5 above.
3. **Attach confirmed end to end** — still open. Trace one resource from file
   read through the reflection walk to a live pointer. Settles the section 5
   caveat about in-place loading, which is the last load-bearing assumption in
   the offline route.

Only #3 remains, and none of these touch the geometry parsing.

## 8. Runbook — first contact with real files

```
pip install numpy zstandard lz4
```
numpy is a 25x speedup on the scan (measured). zstandard/lz4 are only needed if
payloads turn out to be compressed, but installing up front avoids a failed run.

**Step 1 — find the store.** Echo VR ships through Oculus; look under the
Oculus software directory for the Echo Arena install. Confirm by *shape*, not
path: a directory named `shared` containing two levels of 2-hex-char
subdirectories, whose leaves are 32-hex-char filenames with no extension.

**Step 2 — smoke test one blob before the whole store.**
```
python echovr_collision.py scan <shared>/<XX>/<YY>/<somefile>
```
Confirms the tool runs and shows whether decompression engaged.

**Step 3 — full sweep, logged.**
```
python echovr_collision.py scan <shared> > scan.log 2>&1
```

**Step 4 — read the outcome.**

| What you see | Meaning | Next |
|---|---|---|
| Bodies listed + `pointer encoding: <scheme>` | Working. Gap 3 is now answered. | `extract ... --merge -o map.obj`, open in Blender/MeshLab |
| `0 with collision geometry`, no `zstd@`/`lz4@` lines | Compression detection failed — payloads aren't framed where expected | Capture first 256 bytes of a blob (below) |
| `0 with collision geometry`, decompression *did* engage | Attach-in-place assumption wrong, or offsets differ in this build | Capture a decompressed blob sample |
| `! zstd decompress failed` | Frame found but truncated/keyed, or a custom container | Capture first 256 bytes |

**Capturing a header sample:**
```
python -c "import sys;print(open(sys.argv[1],'rb').read(256).hex(' '))" <a-blob>
```

**Note on scale.** A body-per-map is not guaranteed; a level may be split across
many kinematic bodies. `--merge` handles either. If `extract` emits a wildly
small mesh, check the scan log for how many bodies were found before assuming a
parse failure.

## 9. The runtime raycast interface

Found while reading the bot AI (`ECHOVR_AI_NOTES.md` §7). Two things here matter
for collision work.

**The AI queries real collision, not the navigation octree.** `CR15EchoPathingCS`
resolves a position to unoccluded space by raycasting the physics world, and only
uses the SVO for coarse path search. So the triangle data in section 3 is the
authoritative representation for everything in the game — the baked `CSVOResource`
adds nothing you would not already have. That closes the question of whether the
octree was worth chasing as an alternative route: it is not.

**The shared world raycast.** `IsPointVisible` @ `0x1405051f0` — 103 bytes,
20 callers including `RaycastAimAssist`, `EstimateCollisionTime`, and the combat
and weapon paths. This is *the* engine raycast, not an AI-specific helper.

```
IsPointVisible(world, query, mask = 0xffffffff) -> bool
```

Query struct, partially recovered from the AI call site (`0x1409836c0`) and from
`IsPointVisible` itself:

| Offset | Type   | Field |
|--------|--------|-------|
| `+0x00` | float3 | ray start |
| `+0x0C` | float3 | ray end |
| `+0x58` | u32    | collision mask — `0x4000` normally, `0x2` in an alternate mode |
| `+0x6C` | u32    | flags; low nibble forced to `2` |
| `+0x80` | s32    | **hit id, `-1` means miss** — this is the return test |
| `+0x84` | float  | hit distance along the ray; initialised to `+INF` |
| `+0x88` | float3 | **hit position**; initialised to the ray end, so a miss reads as the endpoint |
| `+0x94` | float3 | hit normal, **returned unnormalized**; the caller normalizes |
| `+0xA0` | s32    | secondary id; initialised to `-1` |
| `+0xA4` | s32    | initialised to `4` |
| `+0xA8` | —      | three words zeroed |

Struct is at least `0xB4` bytes; true size unknown, so allocate generously and
zero it. The output fields above come from the initialisation prologue of the
inner raycast `0x1406A1280`, which sets each one to its miss sentinel — that is
strong evidence for the layout but is not the same as watching them get written
on a hit.

**`IsPointVisible` ignores its first argument.** The disassembly overwrites `RCX`
from the global `[0x1420A0470] + 0x2CEC0` before ever reading it, resolves an
entity handle from `+0x98`, and calls the real raycast. So no world or scene
pointer is needed at the call site — pass anything:

```
bool IsPointVisible(ignored, RayQuery* q, uint32_t mask)   // RVA 0x5051F0
```

Two consequences:

1. `0x4000` (bit 14) is a real, named collision layer — the one used for
   line-of-sight. Section 3 already decodes a collision mask array at `+0x260`
   (stride `0x30`) in `SPhKinematicBodyCD`. If those masks are the same namespace,
   bit 14 marks LOS-blocking triangles, which is a validation hook: an extracted
   map should have walls set and decorative geometry clear. **Unverified** — that
   the `+0x58` mask indexes the `+0x260` array the same way is a lead, not a fact.
2. The engine hands back unnormalized normals. Worth knowing before trusting any
   normal read out of a runtime dump.

This also makes the runtime route in sections 2-4 less awkward than it looked.
Rather than walking `CPhZone` and reconstructing structs, a live process could
raycast a grid through the arena and recover surfaces plus normals directly
through this one function. Still needs a live process, so still blocked — but it
is a much smaller injection surface than the pointer chain.

## 10. Status

- Format decoded; parser written and unit-tested against synthetic data
  (all three pointer schemes, exact vertex/index/material recovery, zero false
  positives on 400KB of random bytes).
- ~~Blocked on access to `shared/`~~ **Resolved 2026-09-04:** geometry extracted from the
  package files — see `ECHOVR_PACKAGE_FORMAT.md`.
- Runtime route (inject, walk `CPhZone`, dump) is fully mapped in sections 2-4
  but needs a live process; Echo VR's servers have been down since 2023.
  Section 9 adds a lighter-weight variant of the same route via `IsPointVisible`.
- The navigation octree (`CSVOResource`) was evaluated as an alternative source
  of map geometry and rejected — it is dilated free space, it ships in the same
  `shared/` container, and the engine itself treats the physics triangles as
  authoritative. See section 9.
