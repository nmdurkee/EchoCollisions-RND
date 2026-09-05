# Echo VR disc bounce predictor — design spec

Date: 2026-09-04. Status: approved in conversation, pending written review.
Project root: `C:\Users\nmdur\Desktop\RND`.

## 1. Goal

A Python prediction engine that, given the live disc state from the `/session` API,
returns the disc's path for the next 2 seconds including every wall bounce, accurate
to the game's real collision mesh and real bounce physics. It replaces the current
in-headset overlay's dependency on Echo replay-viewer assets. The overlay itself
(Python, Frida-hooked, owned by the user) is out of scope; it will call this engine.

Problems with the current overlay that this must fix:

1. Prediction flickers mid-air (frame-to-frame instability).
2. Disc treated as a line; misses clipping the edges of floating geos.
3. Backboard read badly.
4. Only the next bounce; want the full 2 s path with multiple bounces.

## 2. Settled inputs (all from the game's own data — no guessing)

| Input | Value / source | Where documented |
|---|---|---|
| Arena collision mesh | `arena/mpl_arena_a_collision.npz` — 9 kinematic bodies, 53,545 verts, 91,656 tris, world space | `ECHOVR_PACKAGE_FORMAT.md` §4 |
| Disc collider | `arena/disc_body_ca0c2a1dbd51f6db.npz` — 40 particles in two rings, r 0.309/0.299 m, thickness 0.039 m (flat frisbee, 0.618 m across) | `ECHOVR_BOUNCE_NOTES.md` §3b |
| Bounce law | `CR15BounceCS::OnCollision` @ 0x140923aa0: `t = clamp(1−|v̂·n̂|,0,1)`, `e_perp = 0.5+0.5t`, `v' = e_perp(v − n(v·n)) − 0.5·n(v·n)` (flags BounceLikeABall+Lerp; constants from the arena's `CR15BounceCR`) | `ECHOVR_BOUNCE_NOTES.md` §3, §3b |
| Flight model | straight line, constant speed (user-confirmed) | — |
| Live input | `/session` API: `disc.position`, `disc.velocity`, `disc.up/forward/left` (~30 Hz in replays; API poll rate at the overlay's discretion) | `ECHOVR_REPLAY_FORMAT.md` |
| Contact-orientation rule | engine collides particles vs triangles (and edges), so contact depends on the disc plane; plane is constant in flight | `ECHOVR_BOUNCE_NOTES.md` §3b |

Open item (resolved by the harness, §5): which of the three full-arena hulls (bodies
0, 2, 4) the disc collides with. Assumed identity body transforms; verified by the
harness.

## 3. Components

```
echovr_pkg.py            game files  →  arena.npz (mesh + disc ring + constants)     [one-time]
echo_disc_predict/       arena.npz + live frames  →  Path                             [runtime]
validate_replays.py      .echoreplay files + predictor  →  metrics, corrected arena.npz [offline]
```

### 3.1 `echovr_pkg.py` — package reader / extractor
Replaces the throwaway spike code. Reads the manifest (three arrays; §2 of the package
notes), locates a resource by `(type, name)` using the CSymbol64 hash (§3), decompresses
its zstd frame, slices it, and parses `CPhysicsResource` bodies by the 0x34 triangle
signature (§4). Commands:

- `info <gamedir>` — build id, manifest hash, package sizes, resource-type histogram.
- `extract-arena <gamedir> <map> -o arena.npz` — all kinematic bodies of the map,
  the disc collider, and the map's `CR15BounceCR` constants into one file.
- `dump <gamedir> <type> <name> -o file` — raw resource, for further RE.

Read-only against the install. Written to also serve as the read half of a later
patch tool, but patching is **not** in this spec.

`arena.npz` contents: `vertices (N,3) f32`, `triangles (M,3) i64`, `body (M,) i64`,
`material (M,) i64`, `disc_points (40,3) f32` (disc-local), `bounce` =
`{e_par, e_perp_begin, e_perp_end, flags}`, `hulls` (default `[0]`), `meta`
(map name, build id, extraction date, harness-validated flag).

### 3.2 `echo_disc_predict/` — the engine
Pure Python + NumPy, no game/DLL/network dependency.

```python
pred = DiscPredictor.load("arena.npz", hulls=None)   # None → file's hulls
pred.observe(t, pos, vel, up)                          # every API frame
path = pred.predict(horizon=2.0, dt=1/60)             # → Path
```

`Path`: `points (K,3)` at fixed `dt`; `bounces: list[Bounce(t, point, normal, v_in,
v_out, body, triangle)]`; `confidence: {"low_confidence", "hull_unvalidated", ...}`.

**State estimation.** Ring buffer of the last ~8 frames. Straight-line fit
`p(t) = p0 + v t` by least squares over the window; speed = median `|vel|`; `up` =
latest. Discontinuity detector resets the window when direction changes > 5° or
speed > 5% between frames (throw, catch, touch, bounce). < 3 frames → raw state,
`low_confidence`.

**Collision query.** BVH over the selected hulls' triangles, built at load. The
disc is the oriented ring: the 40 particles rotated into the frame defined by `up`
(spin about `up` is irrelevant by symmetry). Each particle swept as a segment along
the direction for `speed × remaining_time`; earliest hit → `(t, point, normal,
triangle, body)`. Normals from winding, flipped to face the incoming direction. Ring
edges (particle-to-particle segments) also tested against triangle edges, matching
the engine's edge-edge phase. `sphere=True` option: one segment with r = 0.309.

**Bounce.** The formula above, constants read from `arena.npz` (never hardcoded).
Position continues from the contact point; `up` unchanged.

**Stepping.** `while t < horizon and bounces < 8: sweep → if no hit: straight to
horizon; else segment to hit, append Bounce, reflect, t = t_hit + ε (1 mm)`.
Terminals: horizon, max bounces, speed < 0.1 m/s. Goals are not terminal.

**Failure modes.** No `up` → sphere mode. Disc inside a hull or speed 0 (held) →
empty path. Hull unvalidated → run with `[0]`, flag it.

**Performance.** `predict()` ≤ 2 ms target; BVH is the only vectorisation hot spot and
is swappable (trimesh/embree) without touching the rest.

### 3.3 `validate_replays.py` — harness
Streams `.echoreplay` (zip, one JSON `/session` frame per line), filters
`game_status == "playing"`, extracts `t, position, velocity, up, bounce_count,
possession`. Builds free-flight segments (disc not held, moving, no discontinuity)
and bounce events (velocity direction change > 5° **and** `bounce_count` increment,
no player within 1.5 m). Contact point = intersection of the incoming and outgoing
lines (beats the 33 ms frame spacing).

Tests, in order; each writes its result into `arena.npz` where applicable:

1. **Hull selection** — residual of the oriented-ring contact to each of hulls 0/2/4;
   the hull with median ≈ 0 and tightest spread wins; written to `hulls`.
2. **Collider check** — ring vs sphere(0.309) vs point residuals, split by incidence
   angle. Expect ring to win edge-on, tie face-on.
3. **Bounce law** — normal-component ratio (expect 0.5) and tangential ratio vs
   incidence (expect 0.5→1.0). Systematic deviation → corrected constants written to
   `bounce`, code untouched.
4. **End-to-end** — at every free-flight frame, `predict(2.0)` vs actual: position
   error at 0.5/1/1.5/2 s, bounce-point error, missed/spurious bounce counts, split by
   bounces-in-window.
5. **Flicker** — frame-to-frame change of the predicted first bounce point during
   unchanged flight, before/after the input filter.

Held-out split: choose/fit on odd-numbered files, report on even. Outputs: JSON
summary + per-event CSV. Reports sample counts prominently — the local corpus is
much smaller than the nominal 2 TB.

Acceptance: contact residual median < 5 cm; normal ratio 0.5 ± 0.03; 1 s position
error < 0.2 m for single-bounce windows; flicker < 5 cm. A constant contact offset
points at body transforms, not the formula.

## 4. Integration with the existing overlay

The user's overlay (Python + Frida) will be supplied later. Integration contract:
it feeds `observe()` from its API poll and draws `Path.points` and `Path.bounces`.
Nothing in this spec depends on how it draws or hooks. Until it arrives, a minimal
`demo_live.py` that polls `/session` and prints the next bounce serves as the smoke
test.

## 5. Out of scope (deliberately)

- Modding/patching packages (separate spec; §1 of `ECHOVR_PACKAGE_FORMAT.md` covers
  the mechanics).
- Decoding the acceleration tail / collision masks in `CPhysicsResource` — the
  harness answers the hull question empirically instead.
- The in-game `OnCollision` hook from `ECHOVR_BOUNCE_NOTES.md` §5 — kept as a
  debugging aid if the harness shows a systematic deviation.
- Other maps — the extractor supports them, the harness/predictor are map-agnostic,
  but only `mpl_arena_a` is validated here.

## 6. Build order

1. `echovr_pkg.py` (extractor + `arena.npz`), tested against today's spike outputs
   (byte-identical mesh).
2. `echo_disc_predict/` with unit tests on synthetic geometry (single wall, corner,
   edge-on vs face-on ring hits, multi-bounce box).
3. `validate_replays.py`; run tests 1–3, write results into `arena.npz`.
4. End-to-end + flicker metrics; iterate on the input filter parameters.
5. `demo_live.py`; then integrate with the user's overlay when its code is available.

## 7. Reference documents

`ECHOVR_PACKAGE_FORMAT.md` (package/manifest/hash/on-disk physics),
`ECHOVR_BOUNCE_NOTES.md` (runtime collision pipeline, bounce formula, constants,
disc collider), `ECHOVR_COLLISION_NOTES.md` §1–4, §9 (runtime layout, raycast),
`ECHOVR_REPLAY_FORMAT.md` (replay structure).
