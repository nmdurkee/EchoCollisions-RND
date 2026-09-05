# Echo VR — visual asset modding leads

Reconnaissance for adding or replacing visual content (icons, particle effects).
Nothing here is a decoded format yet — these are entry points, class names, and
the source files to work from. Read `RESOURCE_CONTAINER.md` first; every route
below depends on that container.

---

## 1. Goal effects are already a cosmetic slot

The most useful single finding. `goal_fx` is **not** a hardcoded effect — it is
an equippable loadout item, serialised right beside `medal`:

```c
FUN_140175350(stream, obj + 0x50, "goal_fx");
if (!ok) FUN_140175350(stream, obj + 0x58, "medal");
```

Present in all three loadout paths:

| Address       | Name |
|---------------|------|
| `0x140136060` | `LoadoutSlot_Inspect_Deserialize` |
| `0x140136FC0` | `LoadoutSlot_Inspect_Serialize` |
| `0x1401ADB10` | (loadout read path) |

Corroborated at runtime by:

> `[NETGAME] Cannot play goal SFX: provided userid doesn't have a valid Customization component`
> — `0x1416E0E70`

So on a score the game resolves the **scoring player's** Customization component
and plays their equipped goal FX. There is an existing content pipeline for
exactly this; nothing needs to be bolted on.

Related strings: `last_score|goal_type` (`0x1416E4AE8`), `[goal_type]`,
`Goal` / `GOAL` / `SELF GOAL` / `[NO GOAL]` / `[INVALID GOAL TYPE]`,
stat names `OnePointGoals` / `TwoPointGoals` / `ThreePointGoals` /
`BounceGoals` / `HeadbuttGoals`.

Level actors: `blue_goal`, `orange_goal`, `blue_goal_ai_nav`,
`orange_goal_ai_nav`. Trigger volumes: `goalsphere`, `shotongoalsphere`,
`goalbox`.

### Consequence for multiplayer

Unlike a pure client-side texture swap, goal FX travel through the
customization/loadout system, which is server-validated and persisted
(`SaveLoadoutRequest`, `CurrentLoadoutRequest` / `Response`, and
`sns_register_r15net_save_loadout_*` in the message registry). A server that
knows about a new item could show it to everyone. A server that does not will
reject or ignore it. This is the one asset route that is **not** inherently
client-only.

---

## 2. Particles

Two layers, and it matters which you touch.

**Component layer** (references effects, holds parameters):

| Resource | RTTI / string |
|----------|---------------|
| `CParticleCR` | `0x1416F863F` |
| `CParticleEffectCR` | `0x1416F8790` |
| `CParticleForceCR` | `0x1416F88E8` |
| `CParticleForceCS` | `0x141713EA8` |

Tunable component properties, exposed by name — these are the cheap knobs:
`particlelifescale`, `particlescalex`, `particlescaley`, `particlescalez`
(`0x1416FF017`-`0x1416FF250`).

**Graphics-system layer** (the actual effect data):

| Resource | String |
|----------|--------|
| `CGParticleEffectResource` | `0x141C2FAD0` |
| `CGParticleGraphResource` | `0x141C2FB68` |

`CGParticleGraphResource` is the interesting one — a *graph* implies
node-authored effects, which is where emission counts, colours and lifetimes
would live.

**Script nodes** — particle spawning is reachable from the data-driven script
system, not only from the loadout:

- `SpawnParticleEffectNode` (`0x141CC63D8`)
- `CreateParticleEffectNode` (`0x141CC6400`)

Renderer paths, for context on what the runtime supports:
`Particles (Billboard)`, `Particles (Ribbon)`, `LowRes Particles (Billboard)`,
`LowRes Particles (Ribbon)`, plus mesh-particle compute shaders
(`meshparticleinstances2_cs`, `ribbonparticleverts2_cs`, `binparticleforces_cs`,
several `sortmeshparticles_*` variants).

**Not started.** Neither `CGParticleEffectResource` nor
`CGParticleGraphResource` has been examined.

---

## 3. Textures and icons

**The engine has a first-class override mechanism.** `CTextureOverrideCR` /
`CTextureOverrideCS` with callback registration:

- `UpdateOverrideTexture` (`0x141713F10`)
- `[TEXTURE OVERRIDE] Tried to register a callback ... but there was already a callback registerd` (`0x1416FD250`)
- `[TEXTURE OVERRIDE] no valid callback for component %s on actor %s` (`0x14170A010`)
- `[CANVAS] No texture override callback registered for component %s (%s) canvas may not render` (`0x1416F1490`)

Swapping what a texture slot displays is something the engine does by design.

**Resource classes:**

| Class | Source file |
|-------|-------------|
| `CGTextureResource` | `cgtextureresource.cpp` (`0x141C386B0`), `cgtextureresource_win10.cpp` (`0x141C38720`) |
| `CGTextureResourceInit` | `0x142063CF0` |
| `CGTextureStreamingResource` | `0x141C2FAD0` area; `ctexturestreamingcs.cpp` (`0x14170A0C0`) |
| texture cache | `cgtexturecache.cpp` (`0x141C385C8`) |

The `_win10` suffix means the on-disk payload is likely already in a D3D-ready
form (BCn blocks + mip chain), not a generic image container. Expect a small
header plus raw mip data rather than PNG/DDS.

**Named UI texture slots** — these resolve through the manifest by symbol, so
they are the natural replacement targets:

`icon_texture_texture`, `menu_season_panel_start_texture`,
`menu_season_panel_upsell_texture`, `lobby_poster_start_texture`,
`lobby_poster_upsell_texture`, `menu_store_panel_start_texture`,
`menu_store_panel_upsell_texture`.

**UI behaviours** worth knowing about: `CR15DynLoadTextureBehavior` (runtime
texture loading for UI), `CR15ItemPreviewBehavior`, `CR15ItemRarityFrameBehavior`,
`CR15StoreItemBehavior`. Layout lives in `CCanvasUICR` / `CR15UIPage2CR` /
`CR15UILayoutCR` — all unreversed, and required for *adding* elements as opposed
to *replacing* them.

**Not started.**

---

## 4. Route comparison

| Goal | Route | Blocked on | Client-only? |
|------|-------|-----------|--------------|
| Reskin an existing icon | Overwrite the texture blob | `CGTextureResource` format | yes |
| Change an existing goal FX | Overwrite the particle blob | `CGParticleEffectResource` / graph format | no — travels via loadout |
| Tweak effect scale/lifetime | Component properties on `CParticleCR` | component resource format | depends |
| New goal FX item | Item DB + server acceptance | item database, server control | no |
| Genuinely new UI element | Canvas UI layout data | `CCanvasUICR` (large) | yes |
| Anything, delivered to players | `CR15NetPackageDownload` | a server you control | no |

`CR15NetPackageDownload` is worth flagging: the game downloads packages at
runtime (`[PACKAGE] Level '%s' downloading '%s' (attempt %llu of %llu)`,
`0x141CB5990`). On a community server you operate, that is a sanctioned content
delivery path rather than a patched install.

---

## 5. Suggested order

1. **`CGTextureResource`** before particles. Textures are a simpler, flatter
   format, and getting one replacement to render end-to-end validates the whole
   container chain — no-checksum assumption included — on something with an
   obvious pass/fail signal.
2. **`CGParticleGraphResource`** second, once the pipeline is proven.
3. Canvas UI last, if ever. It is the biggest format and only needed for adding
   new elements rather than replacing existing ones.

All of this is downstream of having the game files in hand.
