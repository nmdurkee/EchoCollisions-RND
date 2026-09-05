# Echo VR — driving the player from inside the game

Reconnaissance for controlling a player (hands, head, thrust, buttons) without an
HMD, aimed at an eventual AI player. Static analysis of `echovr.exe` only —
**nothing here has been confirmed at runtime**, and the RE database's function
boundaries are badly merged in places (several "functions" span megabytes), so
treat addresses as leads to verify, not as facts.

Companion files: `ECHOVR_COLLISION_NOTES.md` (map geometry — the AI's world model),
`RESOURCE_CONTAINER.md`.

---

## 1. Answer

Drive it in-process. Emulating an HMD is the worst of the four options, and not
mainly for the reasons you'd expect — a null driver is cheap to run. It is bad
because it makes every control an inverse-kinematics problem: you can only say
"the hand is at this pose now," never "thrust 60%," and the game re-derives intent
from pose deltas. The runtime's pose prediction and reprojection also sit between
what you write and what the game reads, adding variable latency to a control loop.

The game has at least three internal seams that skip all of that.

## 2. The layers, bottom to top

```
LibOVR (LibOVRRT DLL, GetProcAddress'd by name)
  loader                 fcn.141360790   0x141360790
  HMODULE                                DAT_1420eb870
  ovr_GetInputState fn ptr               DAT_1420eb610
  wrapper module                         ~0x141365a80 - 0x141366900
  CVRSystem::Shutdown                    0x14072e230
      |
CInputDriverCS / CInputDevice / CInputContext      <- device abstraction
      |
CInputCS / CPlayerInputCS                          <- named input actions  *** TARGET ***
      |
R15EchoUnit locomotion  ~0x140740000 - 0x140750000 <- physics, grab, thrust
```

Confirmed RTTI: `CInputCR`, `CInputCRI`, `CInputCS`, `CInputDriverCR`,
`CInputDriverCRI`, `CInputDriverCS`, `CInputContext`, `CInputDevice`,
`CInputExpression` (all `@NRadEngine`).

## 3. The control surface is named, and it is the Echo Arena control set

These action-name strings are in the binary verbatim:

| Action | What it almost certainly is |
|---|---|
| `inputgrableft` / `inputgrabright` | grip — grab wall/player/disc |
| `inputironmanleft` / `inputironmanright` | per-hand thruster (arms-out "Iron Man" pose) |
| `inputanalogleft` / `inputanalogright` | thumbsticks |
| `inputspeedboost` | boost |
| `inputbrake` | brake |
| `inputbumpers` | bumpers |
| `inputsync` / `inputdetach` | sync / detach |
| `inputrecenter` | recenter |
| `inputdown`, `inputall` | modifier / aggregate |

Related: `thrusterinput`, `lefthandinput`, `righthandinput`, and headlook tuning
params (`headlookleftrightminangleinput`, `smoothpursuit*`).

**There is a name -> offset table.** Assert string @ `0x141714f90`:

> `PlayerInput's input action name/offset map is incomplete--is a new input action not being registered with the map?`

Recovering that table gives the entire input struct layout in one shot. It is the
single highest-value target in this document.

Action IDs are 64-bit hashes, not strings, at the call sites (the same pattern as
`Lookup(ctx, 0x66845d16a79233bd)` seen throughout). The name strings have no direct
xrefs, which is consistent with hashing at build time.

**RESOLVED** — see `ECHOVR_AI_NOTES.md` §1. Every one of these names lives in a
symbol table record of the form `[hash:8]["OlPrEfIx":8][name]`, so the 8 bytes
immediately preceding the magic marker *are* the action ID. A linear scan of the
binary for the magic yields the complete name -> hash map. No hash reversing and
no runtime observation needed. `echovr_symbols.py` does the scan.

### The action IDs

Read directly out of the table at `0x141c9a740` – `0x141c9a8f8`, one contiguous
run of records. Every record is 8-byte aligned, exactly as the format predicts.

| Action | ID |
|---|---|
| `inputall` | `0x3FE384023DFE886A` |
| `inputgrableft` | `0x8CF48ECA490AA504` |
| `inputgrabright` | `0xAFB0482CEB8F7B8C` |
| `inputironmanleft` | `0xBCA5855C42414A74` |
| `inputironmanright` | `0x200D71793BEB506C` |
| `inputanalogleft` | `0xEA137C7A1445A3A4` |
| `inputanalogright` | `0x0EF9732740EE0730` |
| `inputsync` | `0xB7746C41384C50C5` |
| `inputdetach` | `0xF2AA68AFDAF4A734` |
| `inputbumpers` | `0x58B0E141A1283CC7` |
| `inputspeedboost` | `0xB6AB7BE74A8CB356` |
| `inputbrake` | `0xD6C634631CFBDB93` |
| `inputrecenter` | `0x99E38CCD4B1BA602` |

`inputdown` sits in a different run (near `0x141ccb000`) and is not included.
A second input set — `BarrelRollLeft` / `BarrelRollRight`, plus gamepad variants,
`boost` and `brake` — lives near `0x141c44880` and is probably the spectator or
gamepad scheme rather than the in-match player controls. Not yet decoded.

These IDs are unverified at runtime. They are what the table says; whether the
input accessor takes this ID directly or an index derived from it is exactly the
open question in the "low-confidence lead" below.

### Low-confidence lead

Prior reconstruction notes in the DB label two functions:

- `0x1404bbf90` (132 bytes) — "CheckInputBinding, resolve input handle + check state"
- `0x14049d990` (132 bytes) — "GetAnalogInput, GetInputState against hash-based action IDs"

**I do not trust these labels.** Both bodies are structurally identical generic
templated component-handle accessors differing only in a tail call
(`FUN_140f9b770` vs `FUN_140f99090`), and their callers (`ProcessTeamBalancing`,
`ProcessWeaponFiredEvent`, `UpdateEntityPhysicsWithCollision`) read more like
replicated-variable plumbing than input polling. Verify before building on them.

`fcn.1407413e0` @ `0x1407413e0` is a thin wrapper that pulls an input handle from
`obj+0x3f0 / +0x3f8 / +0x400` and calls `0x14049d990` — that offset triple is a
useful runtime signature for finding the real input component.

## 4. Locomotion module — 0x140740000 to 0x140750000

Named by prior work, and clearly the player movement code:

| VA | Name |
|---|---|
| `0x140742450` | `GetWristAngularVelocity` |
| `0x140742aa0` | `GetCurrentTransform` |
| `0x140743a10` | `GetBoneTransform` |
| `0x140743ca0` | `GetInvTransform` |
| `0x14074ab30` | `GripHand_WriteRefVelocity` |
| `0x140747500` | `KillPlayer` |

`GripHand_WriteRefVelocity` is reached from `fcn.140740770`, `fcn.140749a70`,
`fcn.140754a00`. This is where a grab turns into momentum — the reference
implementation for how Echo movement actually works.

## 5. The game already ships an AI that plays Echo

This is the find that most changes the plan. Full native bot subsystem:

- `CR15NetBotCS`, `CR15NetBotAnimCS`, `CR15NetBotCR`, `CR15AIBBNetbot` (AI blackboard)
- `CR15EchoPathingCS` — **zero-g navigation**, source `cr15echopathingcs.cpp`,
  with an explicit state machine: `FloatingIdle`, `FloatingMove`, `FloatingStop`,
  `HoldingWall`, `HoldingPlayer`, `ChasingPlayer`
- `CSVOPathPlannerCS` — sparse voxel octree path planner
- Script nodes: `R15NetSpawnBotNode`, `R15SetEchoPathingGoalNode`,
  `R15EchoPathingForceStopNode`, `R15NetBotSetLookDirectionNode`,
  `R15NetBotStartAIScenarioNode`, `R15NetBotFireHeldExpression`
- Config keys: `bluebotcount`, `orangebotcount`, `bot_arena`, `bot_combat`,
  `arenabot`, `lobbybot`, `isbot`
- Net messages: `SR15NetBotRPC`, `SR15NetBotActivateScenario`,
  `SR15NetBotDisableScenario`, `SNSLobbySetSpawnBotOnServer`
- Logs: `[NETGAME] Adding bot user to slot %llu, idx %hu`, `[NSLOBBY] adding bot`,
  `generating bot account id`, `AIPointWon_BotScored`

Dependency asserts pin the component graph:
`R15EchoPathing requires CR15EchoUnitCS / CTransformCS / CPhysicsCS / CSVOPathPlannerCS / CR15NetBotAnimCS`.

Level nav data exists too: `blue_goal_ai_nav`, `orange_goal_ai_nav`,
`{blue,orange}_tube{1,2}_ai_nav` with `pt1`..`pt5` waypoints.

**Caveat:** a bot is a separate networked entity spawned server-side, not your
avatar. It answers "make an AI that plays Echo," not "move the user."

## 6. Built-in synthetic input path

`CR15NetDebugInputRecorderCS` — a real shipped component system (resource,
factory, RTTI, CS all present) with network messages
`SR15StartDebugInputRecording` / `SR15StartDebugInputPlayback` and
`SR15RequestDebugInputRecording` / `SR15RequestDebugInputPlayback`.

The game can already record a session's input and replay it into a player. That is
a sanctioned synthetic-input driver. Unknown: how playback is triggered, whether
it is file-backed, and whether it can be fed live rather than from a recording.

## 7. Ranked recommendation

1. **Hook the input-action read path (`CInputCS`).** Best fit for "move the user."
   Two hook points cover the entire control surface. The game runs its own physics
   and emits legitimate movement, so nothing downstream needs to be faked. Blocked
   on: recovering the name->offset map and the action-ID hashes.
2. **Drive `CR15EchoPathingCS` / the bot system.** Best fit for "AI that plays
   Echo." Zero-g navigation and pathfinding are already solved for you. Read it
   first regardless — it is a working reference for Echo movement.
3. **Debug input playback.** Good for deterministic replay and for validating a
   control loop offline; probably too coarse for closed-loop AI.
4. **OpenVR/OpenXR emulation.** Rejected, see §1.

Sensor side needs no RE at all: Echo VR's local HTTP API (`localhost:6721/session`)
returns per-player head/body/left-hand/right-hand transforms, velocities, and disc
position/possession as JSON. Pair that with the collision mesh from
`ECHOVR_COLLISION_NOTES.md` and the AI has a complete world model.

## 7b. The action space has two halves

An AI cannot drive Echo through the input actions in §3 alone. Echo is a VR game:
thrust direction is derived from **hand orientation**, not from a stick. Holding
`inputironmanright` with the hand pointing backwards and pointing forwards produce
opposite motion. So the full action space is:

| Half | Content | Source |
|---|---|---|
| **Poses** | head 6DoF, left hand 6DoF, right hand 6DoF | the VR tracking layer |
| **Buttons** | grab L/R, thrust L/R, sticks, boost, brake, bumpers | `CInputCS` action IDs (§3) |

Both must be driven. §3 covers the second half only.

### Pose injection — the LibOVR proc table

The engine `GetProcAddress`es LibOVR by name into **one contiguous pointer table
at `0x1420EB570`, `0x2E0` bytes** (92 slots, 8 bytes each), zeroed at
`0x141360867` and filled by the loader `fcn.141360790`. Slot order matches the
name-string order at `0x141d0c418` onward.

| Slot | Function | Pointer VA |
|---|---|---|
| 17 | `ovr_GetTrackingState` | `0x1420EB5F8` |
| 18 | `ovr_GetDevicePoses` | `0x1420EB600` |
| 20 | `ovr_GetInputState` | `0x1420EB610` |

**Confirmed.** The slot arithmetic was derived from the string ordering and then
checked independently: slot 20 lands on `0x1420EB610`, which is exactly the
`DAT_1420eb610` the decompiler already attributes to `ovr_GetInputState`. Both
other slots are confirmed by their store instructions —
`0x14136177a MOV [0x1420EB5F8], RAX` and `0x141361857 MOV [0x1420EB600], RAX` —
each immediately following that name's `GetProcAddress` call. The game reads
`0x1420EB5F8` back at `0x141366549`, inside the VR wrapper module.

Overwrite these three pointers in-process and the game consumes whatever poses and
controller state you supply. **This is not HMD emulation.** There is no driver, no
external process, and nothing between you and the game — the runtime's pose
prediction and reprojection are already upstream of this point, so what you write
is exactly what the engine reads. It also degrades safely: leave a pointer alone
and that call passes through to the real runtime.

Unknown: whether the game calls `ovr_GetTrackingState` or `ovr_GetDevicePoses` for
hands (both are resolved), and the struct layouts, which are public LibOVR
(`ovrTrackingState`, `ovrPoseStatef`, `ovrInputState`) and can be taken from the
Oculus SDK headers rather than reverse engineered.

## 8. Movement authority — probably client-side

Matters for any client-side movement mod: if the server simulated locomotion, a
local velocity change would be corrected away on the next update.

Evidence that the client owns its own locomotion:

- **`CR15PlayerBroadcasterCS`** (`cr15playerbroadcastercs.cpp`) *requires*
  `R15EchoUnit` and **broadcasts** it. The client sends out its own unit state
  rather than requesting it.
- **No movement input message exists.** Discrete actions use an
  input -> replicate pair (`SR15NetGameInputFireGunMsg` ->
  `SR15NetGameReplicateFireGunMsg`, same for `PullPin`) and teleport uses
  request -> action (`SR15NetGameplayTeleportPlayerRequest` ->
  `SR15NetGameplayTeleportPlayer`). There is no equivalent for locomotion — so
  movement is not sent as input for the server to simulate.
- **`UpdateLocalEchoUnits`** (`0x141cb9318`) distinguishes local units.
- `pospredictionstrength` / `oripredictionstrength` and
  `R15SetHandPredictionStrengthNode` are prediction knobs for *remote* bodies,
  which is what you need when peers send state rather than input.
- `[NETGAME] [LOCALCONNSTATS]` reports `predictionoffset`, `pendingserverdelta`,
  `discardedframepct` — clock sync and prediction, not authoritative movement
  correction.

**Inference, not proof.** No reconciliation or correction path was found, but
absence of evidence in a binary with merged function boundaries is weak. The
`isauthority` symbol (`0x141cc8bb8`) exists and was not traced.

### `R15EchoUnitSetVelocityNode`

`0x141c915e8`. The engine's own script node for setting an Echo unit's velocity —
a sanctioned entry point for exactly what a movement mod wants, in whatever frame
the engine already uses. Preferable to hooking `GripHand_WriteRefVelocity` and
reconstructing the impulse. Its implementation sits behind a merged boundary at
`0x140bc95e3` and has not been read.

## 9. Next step

Everything above is static. In order:

1. **Scan the binary for the `OlPrEfIx` symbol table** and dump every name -> hash
   pair. Unblocked, needs nothing but the exe, and yields the action IDs directly
   (`ECHOVR_AI_NOTES.md` §1). Do this first — it is cheap and it feeds everything
   else.
2. Recover the input-action name->offset map via the assert at `0x141714f90`
   (blocked on the merged function boundaries around `0x14042af06` — needs a clean
   disassembly of that site, not this DB's blob).
3. Runtime confirmation of the input component, using `obj+0x3f0/+0x3f8/+0x400`
   as the search signature.

Only #3 needs an installed game.
