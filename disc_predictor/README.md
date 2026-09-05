# EchoVRCollisionRND — Echo VR disc bounce prediction

Reverse-engineering notes, extracted game data, and (soon) a Python engine that
predicts where the Echo VR disc will go over the next 2 seconds — every wall bounce
included — from the live `/session` API, using the game's **real collision mesh**
and **real bounce physics** instead of replay-viewer assets. Built to feed an
in-headset overlay.

## Status (2026-09-04)

- [x] Runtime collision pipeline located in `echovr.exe` (`CR15BounceCS::OnCollision`, contact-record pool)
- [x] Package/manifest format and the CSymbol64 hash decoded; all 233 resource types resolve by name
- [x] Arena collision mesh extracted from the game files (9 bodies, 53,545 verts, 91,656 tris)
- [x] Disc collider extracted (40-particle flat ring, r = 0.309 m) and bounce constants read (0.5 normal; 0.5→1.0 tangential by incidence)
- [x] Design spec approved
- [ ] `tools/echovr_pkg.py` — package reader / extractor
- [ ] `echo_disc_predict/` — the engine
- [ ] `tests/validate_replays.py` — replay validation harness (also decides which hull the disc uses)
- [ ] Integration with the in-headset overlay

## Layout

| Path | What |
|---|---|
| `docs/specs/2026-09-04-disc-bounce-predictor-design.md` | the approved design spec — start here |
| `docs/notes/` | snapshot of the reverse-engineering notes this rests on (canonical copies live in `..\`) |
| `data/mpl_arena_a_collision.npz` / `.obj` | arena collision mesh, 9 bodies (extracted 2026-09-04) |
| `data/mpl_arena_a_CPhysicsResource.bin` | the raw physics resource, for re-parsing |
| `data/disc_body_ca0c2a1dbd51f6db.npz` | the disc's 40-particle collider |
| `data/echovr_symbols.tsv` | hash→name table dumped from echovr.exe (needed to resolve resource names) |
| `tools/` | `echovr_pkg.py` (package reader / extractor) — to be written |
| `echo_disc_predict/` | the engine — to be written |
| `tests/` | unit tests + `validate_replays.py` — to be written |

## Key facts (details in docs/notes)

- Bounce: `t = clamp(1-|v·n|,0,1); e_perp = 0.5+0.5t; v' = e_perp(v - n(v·n)) - 0.5 n(v·n)`
- Disc collider: flat ring, r = 0.309 m, 3.9 cm thick — contact depends on the disc plane (`disc.up`)
- Flight: straight line, constant speed
- Game data source: `<gamedir>\_data\5932408047\rad15\win10\{manifests,packages}` (no `shared/` in this build)
- Open: which of hulls 0/2/4 the disc uses — the replay harness decides
