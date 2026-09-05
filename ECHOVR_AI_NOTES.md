# Echo VR — the built-in AI, and the symbol table that cracks it open

Deep dive on the shipped bot AI (`CRxAI*` / `CR15AI*`), aimed at reusing its design
knowledge for a learned agent. Static analysis of `echovr.exe` only.

Two results here. The AI architecture is the expected payload. The symbol table
format in §1 was incidental and is worth more.

---

## 1. The string table format is `[hash][magic][name]`

Every "string" that looked like `OlPrEfIxfoo` in a search is actually a record in a
symbol table. `OlPrEfIx` is a literal 8-byte magic marker, and **the 8 bytes
immediately before it are the 64-bit hash of the name that follows.**

```
struct SSymbolRecord {          // 8-byte aligned
    uint64_t hash;              // FNV-style 64-bit hash of name
    char     magic[8];          // "OlPrEfIx"
    char     name[];            // NUL-terminated, padded to 8
};
```

Verified at `0x141cf4cd0`:

```
3d 49 a6 f7 df af 5d 60  "OlPrEfIx"  "blue_tube1_ai_navpt1"
3e 49 a6 f7 df af 5d 60  "OlPrEfIx"  "blue_tube1_ai_navpt2"
3f 49 a6 f7 df af 5d 60  "OlPrEfIx"  "blue_tube1_ai_navpt3"
38 49 a6 f7 df af 5d 60  "OlPrEfIx"  "blue_tube1_ai_navpt4"
39 49 a6 f7 df af 5d 60  "OlPrEfIx"  "blue_tube1_ai_navpt5"
```

Independently confirmed on a second, unrelated run at `0x141c9a740` (the input
actions) and a third at `0x141c9a9f0`, where the adjacent `X` / `Y` / `Z` records
differ by exactly the character XOR: `0x...d15e`, `0x...d15f`, `0x...d15c`.
Every record in all three regions is 8-byte aligned.

**Why this matters:** `ECHOVR_INPUT_NOTES.md` §3 flagged that input action IDs are
64-bit hashes with no direct xrefs, and listed recovering them as a blocker. That
blocker is gone. A linear scan for the magic yields the complete name -> hash map
for every symbol in the binary, including `inputgrableft`, `inputironmanright`,
and the rest of the control surface. No hash reversing required.

### The hash is XOR-linear (observation, not a full recovery)

Where two names differ in one character, the hashes differ in exactly one byte, and
that byte's delta equals the characters' XOR delta:

| Name | Hash | Differing byte |
|---|---|---|
| `blue_tube1_exit` | `70 c5 af cf 84 48 0f a1` | — |
| `blue_tube2_exit` | `70 c5 af cf 84 4b 0f a1` | `[5]`, `0x48^0x4b = 3 = '1'^'2'` |
| `blue_tube3_exit` | `70 c5 af cf 84 4a 0f a1` | `[5]`, delta 2 = `'1'^'3'` |
| `blue_tube4_exit` | `70 c5 af cf 84 4d 0f a1` | `[5]`, delta 5 = `'1'^'4'` |
| `blue_tube5_exit` | `70 c5 af cf 84 4c 0f a1` | `[5]`, delta 4 = `'1'^'5'` |

Byte index appears to be `(len - 1 - i) - 1` for character `i` — characters fold
into bytes in reverse order. The `navpt` set (char at distance 1 from end -> byte 0)
and the `exit` set (distance 6 -> byte 5) both fit.

That rules out a standard multiplicative FNV and points at a custom XOR fold. The
mixing constants are not recovered. **This is a curiosity, not a dependency** — the
table gives name and hash side by side, so nothing needs the algorithm.

## 2. AI architecture

A Behavior / Reaction / Blackboard triad, fiber-based and fully data-driven.

| Prefix | Role |
|---|---|
| `CR15AIB*` | **B**ehavior — what the bot does |
| `CR15AIR*` | **R**eaction — condition that selects a behavior |
| `CR15AIBB*` | **B**lack**b**oard — the bot's world model |

Bases: `CRxAIBehavior` (with nested `CAIFiber` — behaviors are coroutines),
`CRxAIReaction`, `CRxAIBlackboard`, `CRxAIFactory` / `CRxAIFactoryT<T>`,
component system `CRxAICS` / `CRxAICR`.

Blackboards: `CR15AIBBNetbot`, `CR15AIBBLobbyProfile`, `CRxAIBBSelf`.

**Every behavior has a matching reaction** — the two lists are identical. Plus
`CR15AIRAIRole` and `CR15AIRReactionSet` for arbitration, implying role-based
selection over a reaction set.

### The behavior set — 21 options

```
Idle          RandomMove     MoveToProp     Stunned        Dead
GetDisc       ClearDisc      Throw          Punch          Block
Defense       Goalie
TravelToPass  TravelToScore  TravelToTunnel
EnterTube     TubeEscape     TubeLaunch
PreGame       PostRound      PostGame
```

Behaviors have named sub-states, e.g. `EnterTube`:
`MovingToTubeFront`, `ArrivedAtTubeFront`, `GetReadyTubeFront`,
`MovingToTubeBack`, `ArrivedAtTubeBack`, `GetReadyAtTubeBack`.

Scenarios: `CR15AIOneVOneScenario`, `CR15AIThrowTestScenario`,
`CR15AIBDebugArenaScenario`. Utility: `CR15AIRequestPositionUtil`.

## 3. The tuning parameter table — the devs' feature engineering

Contiguous at roughly `0x141cf4f00` – `0x141cf5c00`. This is what RAD decided
determines Echo Arena decisions.

**Shooting** — `shotaccuracy`, `shotoffset`, `bounceshotprobability`,
`dunkshotprobability`, `launchspeedaggressive`, `launchspeeddefensive`,
`minthrowspeed`, `maxthrowspeed`, `missfirstshot`, `forcemissprob`

**Passing** — `passaccuracy`, `passoffset`, `passtoplayeraccuracy`,
`preferpasstoplayer`, `passbackdistance`, `minpassspeed`, `maxpassspeed`

**Blocking** — `blockchance`, `blockcooldown`, `blockanimtime`, `minblocktime`,
`blockdecayrate`, `blockdistfront`, `canpunchblocking`, `ttgtodisableblocking`

**Defending** — `defendrangemult`, `defendnoppdist`, `defensegoaldist`,
`defendgp2dist`, `defendgp3dist`, `defendplayerradius`

**Catching** — `catcharmreach`, `catchhandradius`, `catchtwitchrange`,
`catchtwitchrangegoalie`, `mingoaliecatchchance`, `maxgoaliecatchchance`,
`maxgoaliecatchreactiontime`, `goalielungecooldown`, `lungedistance`

**Disc handling** — `discpredmult`, `minholdtime`, `regrabdelay`, `stealdiscprob`,
`cleardiscdist`, `cleartoscoredist`, `mincleartti`, `maxcleartti`,
`mintravelcleardiscdist`, `mintravelwithdiscweight`, `maxtravelwithdiscweight`,
`speedclampdiscradiusmin`, `speedclampdiscradiusmax`, `discradiusmaxspeed`

**Tubes** — `defaulttubelaunchdist`, `goalietubelaunchdist`,
`advantagetubelaunchdist`, `net_catapult_time`

**Stun / punch** — `stuntime`, `stunpunchdelay`, `repunchdelay`

**Perception** — `perceptionfreq`, `checkbehindintervalmax`, `enablechasesteering`

Blackboard fields (CamelCase, distinct from the lowercase resource params):
`DefendGoal`, `DefendRange`, `MaxHoldTime`, `CanPunchOppWithDisc`,
`LaunchSpeedMult`, `role`, `aggressive`.

Note `tti` (time-to-intercept) and `ttg` (time-to-goal) as first-class quantities:
`mincleartti`, `maxcleartti`, `ttgtogetdisc`, `ttgtodisableblocking`. The engine
reasons in time-to-contact, not distance.

## 4. The bot is handicapped on purpose

`forcemissprob`, `missfirstshot`, `blockchance`, `mingoaliecatchchance`,
`maxgoaliecatchchance`, `maxgoaliecatchreactiontime`, `stealdiscprob` are all
probabilistic failure injections, driven by difficulty tiers loaded from an
external config:

> `[NETLOBBY] 'mp_arena_ai_difficulty.json' skill thresholds are out of order, using default difficulty`

with `ai_difficulty`, `initial_ai_difficulty`, `mp_arena_ai_difficulty`, and
`R15AIDifficultiesExpression` / `R15AIDifficultyIdExpression`.

So "the built-in AI is dumb" is partly a design decision, not a capability ceiling.
The *decomposition* is competent; the execution is deliberately degraded and the
policy is hand-authored. That supports keeping the ontology and scrapping the logic.

## 5. What to take for the learned agent

1. **The 21 behaviors are an action ontology.** A developer-authored, complete
   decomposition of Echo Arena into high-level options. Directly usable as the
   option set for hierarchical imitation learning, or as segment labels over the
   replay corpus. You would not derive a better one from scratch.
2. **The parameter names are a feature set.** `ttgtogetdisc`, `discpredmult`,
   `catcharmreach`, `defendgp2dist` and friends name the quantities the designers
   believed were decision-relevant. Most are computable from `/session` replay
   state. Strong prior for feature engineering, and a sanity check on what a
   learned model attends to.
3. **The difficulty tiers are an evaluation ladder.** A calibrated opponent at
   several fixed skill levels is exactly what you need to measure an agent
   against, and it is already in the game.
4. **`CR15EchoPathingCS` remains the movement reference** — see
   `ECHOVR_INPUT_NOTES.md` §4/§5.

## 6. Roles, events, and level anchors

### Positional roles

`CR15AIRAIRole` and the `role` blackboard field resolve to a concrete taxonomy,
visible in the reaction event names:

```
Goalie          MiddleDefender          ForwardDefender
```

plus a `ClosestTeammate` relation. Three defensive positions, not a flat team.

### Reaction events

The named triggers the AI reacts to. Numbered suffixes are variant picks — the
engine has several responses per situation and chooses among them.

| Group | Events |
|---|---|
| Scoring | `AIPointWon_BotScored`, `AIPointWon_ClosestTeammate`, `AIPointWon_Goalie` |
| Conceding | `AIPointLost_Goalie`, `AIPointLost_MiddleDefender`, `AIPointLost_ForwardDefender` |
| Match flow | `AIPreGame_0/1`, `AIWonGame_PostRound0/1/2`, `AILostGame_PostRound0/1/2`, `AIWonGame_Celebration0/1/2`, `AILostGame_Celebration0/1/2` |
| Stun | `AIStunnedStart0/1/2`, `AIStunnedEnd` |
| Tubes | `AITubeLaunch`, `AIEnterTube_{MovingTo,ArrivedAt,GetReady}{TubeFront,TubeBack}` |

Note the asymmetry: the bot distinguishes *which role* conceded a point but not
which role scored beyond goalie/teammate. Credit assignment is coarser than blame.

### Level anchors

Authored actor names the AI navigates against:

- `team_slot_00` .. `team_slot_04` and `team_slot_10` .. `team_slot_14` — two
  teams, five slots each (Arena is 4v4, so one slot is spare or non-playing).
- `blue_base_gp_r1` / `_r2` / `_l1`, `orange_base_gp_r1` / `_r2` / `_l1` — goal-post
  anchors. These pair with the `defendgp2dist` / `defendgp3dist` parameters in §3.
- `blue_goal_ai_nav`, `orange_goal_ai_nav`
- `{blue,orange}_tube{1..5}_exit` and `{blue,orange}_tube{1,2}_ai_navpt{1..5}`

## 7. `CR15EchoPathingCS` internals

The one AI component read at the code level rather than the symbol level.

Component system object: instance array at `cs+0x100`, index map at `cs+0xd0`,
**instance stride `0x198`**. Constructor at `0x140980f50`, vtable at `0x141c7c820`.

| Instance offset | Meaning |
|---|---|
| `+0x30` | current plan; zeroed on planning failure and on reset |
| `+0x68` | goal position, float3 (raw, as requested) |
| `+0x74` | goal position, float3 (resolved to unoccluded space) |
| `+0x80` | flags — bit `0x02` = path request active, bit `0x10` gates planning |
| `+0xb0`,`+0xc0`,`+0xd0`,`+0xe0`,`+0xf0`,`+0x100` | six component handles, `{ptr:8, id:4, gen:2}`, generation at `+0x0a` |
| `+0xe0` | the path-planner component handle (one of the six) |
| `+0x110` | outstanding path request |
| `+0x190` | script/event object |

Methods:

| VA | Role |
|---|---|
| `0x1409849b0` | `PlanToGoal(cs, idx, startPos, goalPos)` |
| `0x140983850` | resolve a position to the nearest unoccluded point (4140 bytes) |
| `0x1409836c0` | the occlusion probe — builds a raycast query and calls `IsPointVisible`. **Decoded in `ECHOVR_COLLISION_NOTES.md` §9**; it queries real physics collision, not the SVO |
| `0x141020390` | the SVO path search itself |
| `0x140984880` | vslot[11] — force stop: release request, clear flag `0x02`. Backs `CR15EchoPathingForceStopNode` |
| `0x140984900` | vslot[12] — reset: clear plan, bump all six handle generations |

`PlanToGoal` resolves *both* endpoints to unoccluded space before searching, and
logs distinctly for each (`Could not find unoccluded goal pos!` vs `start pos!`).
The bot fails to move when it is itself inside geometry, not only when the target
is — worth knowing if a learned agent is ever compared against it.

**Not found:** the step that turns a plan into thrust. `0x140986350`, despite being
named `update`, is an instance *move* (it copies all `0x198` bytes field by field
for pool compaction), not a tick. The actuation path is still open.

## 8. Confidence

**Confirmed** (read directly from the binary): symbol record layout and the hash
adjacency; the XOR-delta observations in §1; every class, parameter, event, and
actor name listed; the difficulty config strings; the `CR15EchoPathingCS` vtable
address, instance stride, and every field offset in §7 — those came from cleanly
bounded functions and are corroborated by ReVault's own `vslot[]` labelling.

**Inferred**: that `AIB`/`AIR`/`AIBB` mean Behavior/Reaction/Blackboard (from the
base classes and the 1:1 behavior/reaction pairing); the exact byte-index rule for
the hash fold (fits two samples, not proven); that `tti`/`ttg` mean
time-to-intercept / time-to-goal; the meaning of flag bits `0x02` and `0x10` at
`+0x80` (from which routine sets and clears them, not from a definition); that the
six handles at `+0xb0`..`+0x100` are the six components the `R15EchoPathing
requires ...` asserts name — plausible by count, unverified individually.

**Not done**: the actuation step — how a plan becomes thrust and grabs — which is
the piece that would most inform a movement model. Behavior logic beyond the
pathing component is unread; the parameter *values* live in resource files, not the
exe. `mp_arena_ai_difficulty.json` is unread for the same reason.

Nothing in this document has been verified at runtime.
