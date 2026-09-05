# echo_agent — one-file Echo VR actuation hook

Drives the Echo VR player from code: reads live state from the game's `/session`
API and writes head/hand poses + buttons by hooking LibOVR in-process. Built for
the AI-player project (see `../ECHOVR_INPUT_NOTES.md`, `../ECHOVR_AI_NOTES.md`).

**One file to deploy: `echo_agent.exe`.** The DLL is embedded inside it — the exe
drops it next to itself and injects it. Nothing else to copy, no Python on the
game PC.

## Use

Copy `echo_agent.exe` to the game PC. Start Echo VR, get into a match. Then, in a
terminal (elevated if the game is elevated), from the exe's folder:

```
echo_agent.exe                 inject + log the API to the log file (stage 0)
echo_agent.exe --calibrate     inject, log API, run the full pose sweep, then paste the log
echo_agent.exe --grip-right 1  force right grip      (0 to release)
echo_agent.exe --pose-test N   inject one pose test  (N = 1,3,4,5; 0 = off)
echo_agent.exe --off           stop injecting; input passes through untouched
```

Log: `%USERPROFILE%\echo_agent.log` (its own first line prints the exact path).

**The calibration run is the one that matters right now:** `--calibrate` turns on
API logging and sweeps the pose tests automatically (+X, +Z, +Y hand, +Y head,
off), ~3s each. Every phase records the injected tracking pose and the resulting
world-space hand position from the API, in one log. Paste that log to derive the
tracking->world coordinate mapping.

## Windows Defender

The exe injects code, which Defender flags as malware behavior. On your own
machine, exclude its folder once:

```powershell
Add-MpPreference -ExclusionPath "C:\path\to\the\exe\folder"
```

Run elevated so the DLL drops into that (excluded) folder; otherwise it falls back
to `%USERPROFILE%`.

## What it can do

| Capability | Status |
|------------|--------|
| Read live state (`/session`) into the log | works |
| Inject grab (buttons) | proven live |
| Inject head/hand poses | proven live |
| tracking->world coordinate map | pending `--calibrate` |
| policy driving it | future |

## Rebuild

```
./build.sh            # needs MinGW-w64 g++ and python on PATH
```

Compiles the DLL, embeds it into the exe, produces `echo_agent.exe`.

## Files

| File | Role |
|------|------|
| `echo_agent.exe` | the one deliverable: launcher with the DLL embedded |
| `echo_agent.cpp` | launcher/injector source |
| `dllmain.cpp` | the hook: LibOVR patch, pose/button injection, API poller |
| `session_parse.h` | targeted `/session` field extractor (validated vs a real frame) |
| `build.sh` | DLL -> embed -> exe |
| `inject.cpp` | older two-file injector (superseded by echo_agent.exe) |
