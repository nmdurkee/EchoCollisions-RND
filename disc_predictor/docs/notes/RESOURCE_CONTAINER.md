# Echo VR / R15 — resource container and package manifest

Companion to `ECHOVR_COLLISION_NOTES.md`. This file covers the *plumbing*: how a
resource gets from a file on disk into memory. Decoding this was blocking all
three asset formats (collision, textures, particles), so it is the shared
foundation.

Derived by static analysis of `echovr.exe`. Image base `0x140000000`.

---

## 1. Container header — SOLVED

Every resource file begins with a **0x18-byte header**. Decoded from the
decompressor at `0x14152AE50` (`csyscompression.cpp`).

| Offset  | Type    | Field |
|---------|---------|-------|
| `+0x00` | char[4] | codec tag |
| `+0x04` | u32     | allocation alignment (0 = default allocator) |
| `+0x08` | u64     | uncompressed size |
| `+0x10` | u64     | compressed size |
| `+0x18` | —       | payload begins |

### Codec tags

Four bytes in **file order**:

| Bytes    | LE u32 as seen in decompiled code | Meaning |
|----------|-----------------------------------|---------|
| `NONE`   | `0x454E4F4E`                      | payload is raw, memcpy'd from `+0x18` |
| `ZSTD`   | `0x4454535A`                      | zstd frame at `+0x18` |
| ` LZ4`   | `0x345A4C20`                      | lz4 frame at `+0x18` |

> **Gotcha:** the LZ4 tag has a **leading space**, not a trailing one. The
> packed table at `0x141D3FE00` reads `NULLNONEZSTD LZ4`, which splits as
> `NULL` / `NONE` / `ZSTD` / ` LZ4`. Getting this wrong means silently failing
> to recognise every LZ4 resource.

An unrecognised tag is a passthrough — the engine hands the whole buffer along
untouched (`CSmartPtr::MoveAssign` branch), so treat the entire file as payload.

Note `NULL` appears in the tag table but is not handled in the decompressor's
branch chain; it is presumably an uninitialised/sentinel value.

### Read path

```
DecompressAndReadFile          0x14152B090   (csyscompression.cpp)
  -> CFile open / size / read whole contents   (cfile.h — asserts
       "Failed to read entire contents of the file %s")
  -> decompressor              0x14152AE50
       -> zstd decompress      0x14152AB40
       -> lz4  decompress      0x14152AD30
```

Called from exactly two places: `AsyncResourceIOCallback` (`0x140FA1680`) and
`ReadManifestFile` (`0x140FA2C30`) — so the manifest uses the same container as
every other resource.

**Correction to earlier analysis:** the streaming reader at `0x141525310` copies
`0x40` bytes under a flag, which was initially read as a 0x40-byte file header.
It is not — that is a block-alignment concern in the async streaming path. The
actual container header is the 0x18 bytes above.

---

## 2. Package manifest — mostly solved

`ReadManifestFile` @ `0x140FA2C30` (`cpackagemanifest.cpp`):

1. Build the manifest path (`0x140FA1B40`).
2. Existence check; failure asserts *"Failed to find package manifest file %s"*.
3. `DecompressAndReadFile` — **so the manifest is itself container-wrapped** and
   may be zstd/lz4 compressed.
4. Wrap the decompressed bytes in a `CMemStream`.
5. `0x140F9F0C0` reads the header / array descriptors.
6. `0x140F9F310` reads the array contents.

Called from `CR15NetPackageDownloadCS::DownloadManifestAndPackages`
(`0x140CCDE70`) — the runtime package-download path.

### Three arrays

`0x140F9F310` allocates and fills three arrays. Element sizes come straight from
the resize calls:

| Array | Count field | Element size | Reader |
|-------|-------------|--------------|--------|
| A | `+0x38` | `0x20` (`count << 5`) | `0x140F9F550` |
| B | `+0x78` | `0x28` (`count * 0x28`) | `0x140F9F6B0` |
| C | `+0xB8` | `0x10` (`count << 4`) | inline bulk read |

**Array B is the resource table** — stride `0x28` matches what
`LookupResourcePath` walks at `manifest+0x40`.

### Resource entry — 0x28 bytes

`0x140F9F6B0` reads five consecutive `u64`s per entry, confirming the layout
inferred earlier from `LookupResourcePath`:

| Offset  | Type | Field |
|---------|------|-------|
| `+0x00` | u64  | name symbol (CSymbol64) |
| `+0x08` | u64  | type symbol (CSymbol64) |
| `+0x10` | u64  | content hash, low half |
| `+0x18` | u64  | content hash, high half |
| `+0x20` | u64  | build stamp |

`LookupResourcePath` compares `+0x20` against a caller-supplied stamp and
asserts *"Resource %s:%s it is out of date, please rebuild to load this file"* on
mismatch. **This is a version compare, not a content hash check** — see §4.

The `u128` at `+0x10` feeds the path formatter at `0x141528780`:

```
"shared/%02hhx/%02hhx/%016llx%016llx"
```

with the two `%02hhx` taken from bytes 7 and 6 of the *low* half.

### What is still unclear

The stream reader uses a typed interface — vtable slot `0x38` is
`Read(elem_size, dest, count)`, slot `0x30` is a seek/align — and both the
header reader and each array reader perform an alignment skip before bulk
reading. The exact padding arithmetic (`((-pos & (align-1)) - a) + b`) has not
been worked out byte-for-byte, so a from-scratch sequential parser risks
drifting.

**Practical workaround:** entries are highly self-identifying. Decompress the
manifest, then scan for runs of 0x28-byte records where `+0x20` (build stamp) is
identical across the run and `+0x00`/`+0x08` are non-zero. That locates the
table without modelling the stream.

---

## 3. Where this leaves the three asset formats

The container is shared, so all three now have the same front end:

```
read file -> parse 0x18 header -> zstd/lz4/none -> payload blob
                                                     |
        +--------------------------+-----------------+
        |                          |                 |
   collision              CGTextureResource   CGParticleEffectResource
   (DECODED)                 (not started)       (not started)
```

`echovr_collision.py` implements the front end correctly as of this writing and
is verified against synthetic NONE / ZSTD / ` LZ4` containers.

---

## 4. Modding implications

Two findings matter if the goal is replacing assets:

**Nothing verifies resource content.** The only checksum string in the binary is
`"Restored data doesn't match checksum"` (`0x141DC8680`), and that is savegame
restore. The manifest's `+0x20` check is a build stamp, not a hash of the bytes.
The store is content-*addressed* but content is not content-*verified* — so
editing a blob in place at its existing `shared/XX/YY/...` path is not, on the
evidence available, defeated by an integrity check.

**Uncompressed replacement is legal.** Since `NONE` is a first-class codec, a
replacement asset does not have to be recompressed — write the `NONE` tag, sizes,
and raw payload and the engine accepts it. That removes compression from the
authoring problem entirely.

Neither of these has been tested against a running game.

---

## 5. Function index

| Address       | Name | Role |
|---------------|------|------|
| `0x14152AE50` | — | Decompressor; **source of the 0x18 header layout** |
| `0x14152B090` | `DecompressAndReadFile` | Read file, then decompress |
| `0x14152AB40` | — | zstd decompress |
| `0x14152AD30` | — | lz4 decompress |
| `0x140FA2C30` | `ReadManifestFile` | Manifest load entry point |
| `0x140F9F0C0` | — | Manifest header / array descriptors |
| `0x140F9F310` | — | Manifest array contents |
| `0x140F9F6B0` | — | **Resource entry reader — 5x u64** |
| `0x140F9F550` | — | Array A entry reader (0x20 stride) |
| `0x140FA2A20` | `LookupResourcePath` | name+type -> path, with stamp check |
| `0x141528780` | — | `shared/` path format string |
| `0x140FA1680` | `AsyncResourceIOCallback` | Async load completion |
| `0x140FA36F0` | `SyncResourceLoad` | Synchronous load |
| `0x140FA2820` | `LoadResource` | — |
| `0x140FA1B40` | — | Builds the manifest file path |
| `0x140CCDE70` | `CR15NetPackageDownloadCS::DownloadManifestAndPackages` | Runtime package fetch |
