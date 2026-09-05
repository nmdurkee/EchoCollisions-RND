# Echo VR reverse engineering

Static analysis of `echovr.exe` (Ready At Dawn "R15" engine), aimed at getting
map collision geometry out of the game offline, plus reconnaissance on replacing
visual assets.

All findings derived from decompilation and assert strings — the binary has
~11.7% naming coverage and none of it in the physics, resource, or graphics
subsystems. **Addresses are authoritative; disassembler labels are sometimes
auto-generated and wrong.**

## Files

| File | Contents |
|------|----------|
| `ECHOVR_COLLISION_NOTES.md` | Collision geometry format — struct layouts, pointer chain, runbook. The main result. |
| `RESOURCE_CONTAINER.md` | Container header and package manifest. Shared plumbing under every asset type. |
| `ASSET_MODDING_NOTES.md` | Leads for icons, particles, and goal effects. Entry points, not decoded formats. |
| `ECHOVR_INPUT_NOTES.md` | Driving the player without an HMD. Input layers, the action IDs, locomotion module. |
| `ECHOVR_AI_NOTES.md` | The shipped bot AI, and the symbol table format. |
| `ECHOVR_REPLAY_FORMAT.md` | `.echoreplay` format, decoded from real files. Action labels, schema, corpus accounting. |
| `echovr_collision.py` | Collision extractor. Scans a `shared/` store, writes OBJ. |
| `echovr_symbols.py` | Symbol table scanner. Recovers every name/hash pair from the exe. |
| `echovr_apilatency.py` | Characterises the `/session` API as a control-loop source: jitter, staleness, internal cadence. |

## State

**Decoded and tested**
- Static collision geometry: vertices, packed u24 triangle indices, materials,
  edges, local-to-world transform.
- Resource container header (0x18 bytes, `NONE`/`ZSTD`/` LZ4`).
- Package manifest structure and the 0x28-byte resource entry.
- Symbol table records, `[hash:8]["OlPrEfIx":8][name]` — confirmed against the
  real binary. Gives the name for any 64-bit ID the engine passes around,
  including the input action IDs.

**Open**
- Confirming resources are attach-loaded in place. This is the last assumption
  the offline route rests on; if it is wrong, the file offsets diverge from the
  memory offsets and `RESOURCE_CONTAINER.md` §2 onward needs rework. The
  geometry layouts are unaffected either way.
- `CGTextureResource` and `CGParticleEffectResource` formats — both untouched.

**Blocked**
- Everything downstream of having an installed game's `shared/` directory.
  Nothing further can be *validated* without it.

## Quick start

```
pip install numpy zstandard lz4
python echovr_collision.py scan    <shared_dir>
python echovr_collision.py extract <shared_dir> -o map.obj --merge
```

numpy is a measured 25x speedup on the scan. The runbook, including how to read
a failed run, is section 8 of `ECHOVR_COLLISION_NOTES.md`.

## Testing

The extractor is verified against synthetic data only — exact geometry recovery
under all three pointer-encoding hypotheses, zero false positives across 20 MB
of random bytes, and round-trips through all three container codecs. It has
never seen a real Echo VR file.
