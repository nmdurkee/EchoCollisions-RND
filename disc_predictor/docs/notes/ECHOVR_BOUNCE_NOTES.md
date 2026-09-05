# Echo VR — collision *functions* and the disc bounce response

Companion to `ECHOVR_COLLISION_NOTES.md` (which covers the static *geometry*).
This file covers the *code path* a collision takes at runtime, ending in the
function that decides the disc's post-bounce velocity. Static analysis of
`echovr.exe` (image base `0x140000000`) via ReVault, 2026-09-04. Bracketed DB
labels are auto-generated and several are wrong (noted).

---

## 0. TL;DR

* The physics engine (R15, in-house) only **resolves penetration**. The disc's
  rebound velocity is computed in the **game layer** by
  `CR15BounceCS::OnCollision` @ **`0x140923aa0`** (RVA `0x923AA0`), from four
  per-actor restitution numbers in the disc's `SR15BounceCD`.
* Every contact the engine produces is written to a **collision-event record
  pool on the `CPhZone`** — `zone+0x568`, stride `0x110`, count `zone+0x598` —
  each record carrying **contact point, contact normal, relative velocity and
  both body handles**. Walking that pool from the DLL is the cheapest way to
  get empirical wall positions + normals; hooking `0x140923aa0` is the cheapest
  way to get everything needed for a bounce predictor (v_in, n, v_out, and the
  restitution constants).
* The formula (section 3) is a two-coefficient reflection, with the tangential
  coefficient optionally lerped by incidence angle.

---

## 1. Runtime pipeline

```
CPhZone step  (fcn.1406a47d0)
 ├─ CPhNarrowPhase::ProcessTrianglePairs   0x1406f9e20   dispatches per pair-type kernel
 │    └─ fcn.1406ef650 (task wrapper) → kernels:
 │         0x1406f9460  "ph|narrow|par-tri"  dyn particle vs kin triangle   ← disc/player vs WALL
 │         0x1406f9940  "ph|narrow|tri-par"  dyn triangle vs kin particle
 │         0x1406f6270  "ph|narrow|edg-edg"  edge vs edge
 │       pair-type selection = body flag bits at CPhBody+0xbc: 0x2000 / 0x4000 / 0x8000
 ├─ ExcludeFailedBodies  0x140693710  (misnamed: this is the constraint solve loop;
 │    contains "KINEMATIC COLLISION FAILURE: INSUFFICIENT ITERATIONS")
 │    └─ SolveConstraintPass 0x140692400 → fcn.14069aa50 (one constraint; 5 kinds,
 │         strides 0x1c8 / 0x1f0 / 0x178 / 0x1a0 / 0x1b0 in the constraint pools at
 │         zone+0xf58 → +0x1e0/+0x218/+0x250/+0x288/+0x2c0)
 ├─ SaveCache 0x1406fb510  ("ph|narrow|save-coll", "save-dynpartkintricolls", ...)
 │    caches this frame's contacts for warm-starting next frame
 └─ collision-event records appended to  zone+0x568  (section 2)

Game layer, after physics (registration in fcn.1408e2150 / fcn.1408d9470):
   CR15CollisionCS::UpdateAfterPhysics  0x14096d410   fires component events
                                                       (event id 0xa36d83485b47aa86)
   CR15ContactTrackerCS::Update         0x140971f70   -> per-record 0x140971640
                                                       ("[COLLISION] Detected contact...",
                                                        "contact speed multiplier")
   CR15BounceCS::OnCollision            0x140923aa0   <- THE BOUNCE (section 3)
   CR15ThrustCS::Update                 0x140a5c930   (player thrust; unrelated)
```

`ProcessAnimationEvents` @ `0x140923260` is **misnamed** — it is the generic
per-actor component-instance iterator used by BounceCS's init slot
(`0x140923940`). Ignore the name.

Constructors / vtables (for hooking or RTTI checks):

| Class | ctor | vtable |
|---|---|---|
| `CR15BounceCS` | `0x1408aae50` | `0x141c61458` |
| `CR15CollisionCS` | `0x1408aaef0` | `0x141c61758` |
| `CR15GripSurfaceCS` | `0x140924420` | `0x141c6eea0` |

Component type symbols: Bounce CS = `0x7a8046009a26458d` (paired with
`0xd3efed8d87b3d7ba`, which is what `OnCollision` checks the body's component
list for). `0x5b8cc538e22ad937` = CR15NetPhysicsCS (from its registration).

---

## 2. `SPhCollisionEventRecord` — 0x110 bytes

Pool: `zone = *(g_Physics 0x1420A00F0 + 8) + zoneIdx*0x1E18`;
records at `*(zone+0x568)`, count `*(zone+0x598)`. Resolver: `fcn.140638920`.

| Offset | Type | Field | Source |
|---|---|---|---|
| `+0x08` | s32 | record id; `-1` = free slot | `fcn.140638920` |
| `+0x10` | s32 | generation (must match handle) | `fcn.140638920` |
| `+0x38` | handle[0x18] | body A entity handle (`+0x38` type tag `== DAT_1420bb210`) | `fcn.140638f70(rec, out, 0)` |
| `+0x50` | handle[0x18] | body B entity handle | `fcn.140638f70(rec, out, 1)` |
| `+0x68` | float3? | per-side data for A (passed to impulse-at-point) | `0x140923aa0`: `(side*5+0x1a)*4` |
| `+0x7c` | float3? | per-side data for B | same |
| `+0xa0` | float3 | **contact point, world** | `0x140923aa0`, `0x140971640` |
| `+0xac` | float3 | **contact normal** (BounceCS negates it for side 1) | same |
| `+0xb8` | float3 | relative velocity at contact (ContactTracker dots it with the normal) | `0x140971640` |

The *handle* the CS callbacks receive (`param_2`) is a separate small struct:
`+0x08` record index, `+0x0c` zone index, `+0x10` generation, `+0x18` side
(0 = we are body A, 1 = body B).

Body (`CPhBody`, stride 0xF38 — see COLLISION_NOTES §4) fields used here:
`+0xbc` flags (bit 2 = kinematic), `+0xc8` float (priority / "mass class" — the
lower of the two bodies is the one that bounces), `+0xf0` float mass,
`+0x91c` float3 **linear velocity**, `+0xc50` component-list pointer,
`+0xc58` component index.

---

## 3. `CR15BounceCS::OnCollision` @ 0x140923aa0 — the bounce formula

`SR15BounceCD` (JSON reader `0x140780ed0`; copied verbatim into the per-actor
instance array `CS[0x17]`, stride 0x30):

| Offset | Field |
|---|---|
| `+0x18` | flags: `0x10 BounceLikeABall`, `0x20 LerpPerpendicularRestitution` (+ engine bits 1,2,4,8) |
| `+0x20` | `parallelrestitution`      — applied to the component **parallel to the normal** |
| `+0x24` | `perpendicularrestitution` — applied to the component **perpendicular to the normal** (tangential) |
| `+0x28` | `perpendicularrestitutionbegin` (used when Lerp flag set) |
| `+0x2c` | `perpendicularrestitutionend`   (used when Lerp flag set) |
| `+0x10` | (runtime) frame counter of last bounce |

Decoded logic:

```
rec  = collision record;  side = handle+0x18
A, B = bodies from rec+0x38 / rec+0x50
if A.c8 == B.c8 and same kinematic flag: return            # nothing to bounce
body = (B kinematic && A not) || B.c8 < A.c8 ? B : A       # the dynamic/lighter one
if body has no Bounce component: return
if OTHER body has component 0x2e40131422f7b1fc and its check (fcn.14075fd80) passes: return
        # unidentified component - suppresses bounce vs certain actors (players/hands?)

v  = body+0x91c
n  = rec+0xac ;  if side != 0: n = -n
vn = dot(v, n)
e_par  = cd+0x20
e_perp = cd+0x24
if cd.flags & 0x20:                                        # LerpPerpendicularRestitution
    t      = clamp(1 - |vhat . nhat|, 0, 1)                # 0 = head-on, 1 = grazing
    e_perp = (1-t)*cd+0x28 + t*cd+0x2c

v_t = v - n*vn                                             # tangential part, direction kept
v_n = -n*vn                                                # normal part, reflected

if cd.flags & 0x10:                                        # BounceLikeABall
    SetVelocity(body, v_t*e_perp + v_n*e_par)              # fcn.1406320e0
else:
    SetVelocity(body, v_t*e_perp)
    ApplyImpulseAtPoint(body, rec+0xa0, rec+(side? 0x7c:0x68), v_n*e_par*body.mass)   # fcn.140644c50
    # normal rebound delivered as an impulse at the contact point -> also spins the body
inst+0x10 = GetFrameCounter()
```

So a predictor is:  **`v' = e_perp*(v - n(v.n)) - e_par*n(v.n)`**, with `e_perp`
angle-dependent when the Lerp flag is set. Which flags/values the *disc* uses
are data (its actor JSON in `shared/`), not code — read them live from the
instance array or log them from the hook (section 5).

**Caveats.** (1) The engine's position-based solver may still apply its own
contact damping (`ApplyContactDamping` @ `0x14068b2f0` exists) before this
runs; whether that changes the *velocity* the disc carries into `OnCollision`
is unverified — the hook in section 5 measures it for free. (2) The impulse
branch changes angular velocity; a point-mass predictor ignores that.

---

## 3b. The disc's actual constants and collider (from the package files, 2026-09-04)

Read from `mpl_arena_a`'s `CR15BounceCR` (see `ECHOVR_PACKAGE_FORMAT.md` §7 for the
layout). Both bounce components in the arena (actor ids `91a4a5864d973e76`,
`1df6455a4e706d30` — the two actors that also carry `CR15FrisbeeCR`) have identical values:

| Field | Value |
|---|---|
| flags (`+0x18`) | `0x30` = **BounceLikeABall \| LerpPerpendicularRestitution** |
| `parallelrestitution` (normal) | **0.5** |
| `perpendicularrestitution` | 0.5 (unused: Lerp flag set) |
| `perpendicularrestitutionbegin` (head-on) | **0.5** |
| `perpendicularrestitutionend` (grazing) | **1.0** |

So the disc bounce is the simple-reflection branch:

```
t      = clamp(1 - |v̂·n̂|, 0, 1)          # 0 head-on … 1 grazing
e_perp = 0.5 + 0.5*t
v'     = e_perp*(v - n(v·n)) - 0.5*n(v·n)
```

Head-on: speed halves. Grazing: tangential speed kept, tiny normal component halved.
The lobby's three and the tutorial's one bounce component use the same numbers; the
combat maps' seven differ in the `+0x10` word (`0x000fffff` — a mask) but not the floats.

**Disc collider** (`CPhysicsResource` name `ca0c2a1dbd51f6db`, 18,564 B; saved as
`arena/disc_body_ca0c2a1dbd51f6db.npz`): a dynamic body of **40 particles** in two
rings of 20 — radius **0.309 m** at z = −0.02 and **0.299 m** at z = +0.02 — i.e. a
flat frisbee **0.618 m across, 0.039 m thick**, 58 triangles. Dynamic bodies collide
particle-vs-triangle (and edge-vs-edge), so **contact depends on the disc's plane
orientation**: the rim particle nearest the wall touches first. The API's `disc|up`
gives that plane; in flight (zero-g, no torque) it is constant, so a swept oriented
ring is exact and a swept sphere of r = 0.309 is the conservative envelope.
Identification is by shape/size (no actor→resource link decoded yet) — verify
against replay bounce offsets.

## 4. Other functions found on the way (for later)

| VA | What | Why it matters |
|---|---|---|
| `0x140cd4fa0` `EstimateCollisionTime` | swept raycast via `IsPointVisible`; **bullet/weapon** path (Echo Combat), caller `0x140d87c80` uses `GetWeaponInfo` | not the disc; the raycast pattern is reusable |
| `0x1401bd980` | AI throw-test SM, logs "Predicted disc pos has changed" | bot AI's own disc prediction; possible reference implementation |
| `0x140971640` | ContactTracker per-record handler; caps at 32 contacts | logs "[COLLISION] Detected contact between [%s] and [%s]" |
| `0x140472ba0` | dispatches `delegate_BounceBegin/End` | a *prop-animation* "bounce", not the disc |
| `0x140788410` | reads `staticfriction`/`dynamicfriction` | that is `CR15OrientationConstraintCS` (hinges) — not a surface material |
| strings `PushOffLeft/Right(NoVR)`, `pathingPushoffSpeed`, `pathingWallPushOffDelay` | wall push-off bindings/tunables | entry point for the zdrift "slap" half |
| `CR15GripSurfaceCS` / `SR15GripSurfaceCD` | grabbable-surface component | entry point for the "hold on" half |

---

## 5. How to use this from `echovr_agent` (plan)

**Hook A — bounce oracle.** Detour `0x140923aa0` (RVA `0x923AA0`,
signature `u64 fn(CS* self, Handle* h)`). Prologue: resolve the record with the
pool math in section 2, log `rec+0xa0`, `rec+0xac`, `body+0x91c` (v_in), and the
instance's `+0x18..+0x2c` (flags + restitutions). Epilogue: log `body+0x91c`
again (v_out). Every disc-wall bounce then yields one labelled sample
`(p, n, v_in, v_out, e_par, e_perp, flags)` — enough to validate section 3 to
the mm and to catch the caveats. Unlike the LibOVR hooks this is a direct call
target, not a proc-table slot, so it needs an inline detour (trampoline).

**Poll B — wall point cloud.** Each frame from the DLL: walk
`*(zone+0x568)[0 .. *(zone+0x598))`, skip `+0x08 == -1`, and log `+0xa0`/`+0xac`
for records whose one body is kinematic (`CPhBody+0xbc & 4`). Fly along the
walls and you get sampled wall surfaces + normals with no geometry parsing.
Combine with the triangle dump (COLLISION_NOTES §2–3) once that works.

**Mod hook (later).** For zdrift-style slap: the contact you want is the
*player-hand-vs-kinematic* one, which lands in the same record pool and is
consumed by `CR15CollisionCS::UpdateAfterPhysics` (`0x14096d410`) and the
push-off code behind `PushOffLeft/Right`. Locate that next.
