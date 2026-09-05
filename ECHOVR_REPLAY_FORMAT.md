# Echo VR `.echoreplay` format — decoded from real files

Read directly from the corpus at
`C:\Users\nmdur\OneDrive\Documents\Spark\replays` (161 files, 0.83 GB, recorded
2021-07 to 2022-05 by Spark/IgniteVR). Unlike everything else in this repo, these
findings come from **real data, not static analysis** — every claim below was
observed in parsed files.

Relevant to `ECHOVR_AI_NOTES.md` (the agent project) and `ECHOVR_INPUT_NOTES.md`
(the action space this data has to map onto).

---

## 1. Container

`.echoreplay` is a **ZIP archive** containing a single entry with the same name as
the file. Inside is UTF-8 text, one frame per line:

```
2021/07/14 21:43:11.219<TAB>{...json...}
```

- Timestamp format `%Y/%m/%d %H:%M:%S.%f`
- Median inter-frame delta **34 ms (~30 Hz)**
- Compression ~**7.4x** (3.5 MB file -> 25.8 MB text)
- Parsing is clean: 150,936 of 150,936 sampled lines parsed with zero failures

The JSON is the Echo VR `/session` API payload verbatim.

## 2. Replays are NOT state-only

This corrects the assumption the agent plan was built on. Three kinds of action
information are present.

### `holding_left` / `holding_right` — grab labels for every player

Per-player strings, present for **all eight players**, not just the recorder:

| Value | Meaning | Observed count (1 file) |
|---|---|---|
| `none` | hand free | 30186 |
| `geo` | gripping level geometry (a wall) | 3899 |
| `disc` | holding the disc | 1999 |
| `"0"`..`"7"` | gripping **that playerid** | 322 |

This is a direct, per-hand, per-frame label for the grab action — the single most
important control in Echo — and it says *what* was grabbed, which raw button state
would not. `playerid` values run 0-7 and match the ids in the `teams` array.

### Shoulder buttons — local player only

Four top-level floats: `left_shoulder_pressed`, `left_shoulder_pressed2`,
`right_shoulder_pressed`, `right_shoulder_pressed2`.

**Binary in practice** — only two distinct values (0.0 / 1.0) across a whole file,
despite the float type. Nonzero on roughly 3-15% of frames depending on channel.
These are only for the client that recorded the file, so they label one player out
of eight. Their mapping onto the `inputbumpers` / `inputspeedboost` / `inputbrake`
action names in `ECHOVR_INPUT_NOTES.md` §3 is **not established**.

### `last_throw` — the engine's own throw decomposition

Populated after each throw, with the game's internal physics breakdown:

```
arm_speed, total_speed, speed_from_arm, speed_from_movement, speed_from_wrist,
rot_per_sec, pot_speed_from_rot, off_axis_spin_deg, wrist_align_to_throw_deg,
throw_align_to_movement_deg, off_axis_penalty, throw_move_penalty,
wrist_throw_penalty
```

This is a labelled reward signal for throw quality, free. `last_score` similarly
carries `disc_speed`, `goal_type` (e.g. `INSIDE SHOT`), `point_amount`,
`distance_thrown`, `person_scored`, `assist_scored`.

## 3. What is still missing

Status below is for the **2021-2022 format** in this corpus.

| Action | Status |
|---|---|
| Grab L/R | **Labelled** (`holding_*`) |
| Throw release | **Labelled** (`last_throw` fires on release) |
| Thrust L/R (`inputironman*`) | **Absent** — must be inferred |
| Thumbsticks (`inputanalog*`) | **Absent** |
| Boost / brake / bumpers | Partially — 4 binary channels, local player only, mapping unknown |

### Newer format reportedly includes thrusters (UNVERIFIED)

The user reports that **newer `.echoreplay` files carry thruster data** that these
older files lack — a later Spark/API version presumably added fields. Not yet
seen; no new-format file is on this machine. If confirmed, it removes the need for
an inverse-dynamics model (sec 7) and makes behavior cloning fully supervised over
the whole action space.

Three things to check on the first new file, because they decide the training
architecture:

1. **Continuous or binary?** Echo thrust is analog; a 0/1 flag loses magnitude.
2. **All 8 players, or local only?** `holding_*`-style (all players) is usable;
   `shoulder_pressed`-style (recorder only) shrinks the labelled corpus ~8x.
3. **Input or derived?** The firing button is the clone target; a post-hoc
   "speed_from_thrust" number (cf. `last_throw`) is a different quantity.

Best case (analog, per-hand, all players): the hardest data problem in the agent
project disappears. Worst case (binary, local-only): the IDM is still needed but
gets a stronger prior.

**The live API lacking thrust is a separate, non-issue.** Thrust is an action the
policy *emits*, not a state it *reads*: at inference the agent decides to fire
thrusters, so it never needs the API to report them. If the policy ever benefits
from knowing its own current thrust, that comes from feeding its own previous
action back as input (action history), not from the API. Thrust labels are needed
only for *training*, and only *replays* need to carry them.

So an inverse dynamics model is still needed, but its job shrank from "recover the
whole action vector" to "recover per-hand thrust." That is a far better-posed
problem, because the replay gives full hand **orientation** and body velocity at
30 Hz: during free flight, acceleration not explained by drag must lie along the
thrust axes the hands define. Grab intervals are known exactly, so contact-driven
velocity changes can be excluded rather than confused for thrust.

## 4. Frame schema

### Top level

```
disc, teams, player, possession, game_status, game_clock, game_clock_display,
blue_points, orange_points, blue_round_score, orange_round_score,
total_round_count, last_score, last_throw, pause, map_name, match_type,
private_match, tournament_match, client_name, sessionid, sessionip,
blue_team_restart_request, orange_team_restart_request,
left_shoulder_pressed, left_shoulder_pressed2,
right_shoulder_pressed, right_shoulder_pressed2
```

`player` is **not** a player — it is the local VR rig transform:
`vr_position`, `vr_forward`, `vr_left`, `vr_up` (room-scale playspace origin).

### Pose encoding

Every transform is a position plus a **full orthonormal basis**, not a quaternion:

```json
{"position": [x,y,z], "forward": [...], "left": [...], "up": [...]}
```

`disc`, `head`, `body` use key `position`; `lhand` and `rhand` use `pos`. Mind that
inconsistency when writing the parser. Values are floats with ~3 decimal places of
real precision.

### Player object

```
name, playerid, userid, number, level, ping, packetlossratio,
head{}, body{}, lhand{}, rhand{}, velocity[3],
holding_left, holding_right, possession, blocking, stunned, invulnerable,
stats{}
```

`stats` is cumulative per match: `points, goals, assists, saves, steals, stuns,
passes, catches, blocks, interceptions, shots_taken, possession_time`.

### Teams

`teams` is a 3-element array: `BLUE TEAM`, `ORANGE TEAM`, `SPECTATORS`, each with
`team`, `players[]`, `possession`, `stats{}`. Arena is 4v4.

### Disc

`position`, `forward`, `left`, `up`, `velocity[3]`, `bounce_count`.
`bounce_count` is a free label for wall-contact events.

## 5. Only ~21% of frames are live play

Sampled 25 files, 150,936 frames:

| `game_status` | Frames | Share |
|---|---|---|
| `None` (absent) | 33053 | 21.9% |
| **`playing`** | **32085** | **21.3%** |
| `pre_match` | 31723 | 21.0% |
| `post_match` | 25473 | 16.9% |
| `round_start` | 16832 | 11.2% |
| `""` (empty) | 4496 | 3.0% |
| `score` | 4471 | 3.0% |
| `round_over` | 2803 | 1.9% |

**Filter on `game_status == "playing"` before anything else.** Four fifths of the
corpus is lobby, warmup, and post-match idling — during which players still move,
which would poison a behavior-cloned policy with aimless floating.

Note also that the whole first file inspected (`rec_2021-07-14_21-43-13`) contains
zero `playing` frames. Per-file yield varies enormously; do not assume a file
contains gameplay because it is large.

## 6. Corpus accounting

The local corpus is **much smaller than the 2 TB** the project assumes:

| | |
|---|---|
| Files | 161 |
| Compressed | 0.83 GB |
| Uncompressed (est. 7.4x) | ~6 GB |
| Live play (extrapolated) | **~115 minutes** |

At ~30 Hz that is roughly 200k usable training frames. Enough to build and debug
the whole pipeline; **not** enough to train a strong agent. A sweep of `C:` and
`D:` found no other `.echoreplay` files, so the bulk of the corpus is on media not
attached to this machine.

## 7. Consequences for the agent

1. **Behavior cloning on grab is available immediately** — no IDM, no labelled
   capture session, no game install. Grab is the highest-leverage control in Echo
   and it is fully supervised across all eight players.
2. **Thrust needs an IDM**, but a narrow one, and physics-constrained.
3. **30 Hz is the ceiling on temporal resolution.** The game runs at 90 Hz, so
   inputs shorter than ~33 ms are invisible. Policies trained here should act at
   30 Hz and be interpolated, not asked to reproduce 90 Hz twitches.
4. **`last_throw` and `last_score` are ready-made reward signals** for any RL or
   ranking stage.
5. **Eight players per frame means 8x the trajectories per file** — the recorder
   is not privileged except for the shoulder channels.
