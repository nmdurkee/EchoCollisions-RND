#!/usr/bin/env python3
"""
echovr_session_log.py — log just the useful fields from the Echo VR /session API.

The full /session payload is huge. This pulls out the local player's poses,
velocity, and grab state (plus the VR rig transform) and writes one compact line
per sample, so you can watch state or capture a calibration snapshot without
reading raw JSON. It is the read-side companion to the echovr_agent DLL: the DLL
knows what tracking-space pose it injected, this logs the world-space result the
game reports.

The "local player" is the one whose name matches the payload's `client_name`;
if none matches (e.g. solo), the first player is used.

Usage:
    python echovr_session_log.py once                      # one snapshot, to stdout
    python echovr_session_log.py watch [--rate HZ] [--out FILE] [--seconds N]
    python echovr_session_log.py selftest

    # calibration: with the DLL running and one pose test active,
    #   python echovr_session_log.py once
    # then compare rhand to the known tracking input.
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import time

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6721
ENDPOINT = "/session"


def fetch(host: str, port: int, timeout: float) -> dict | None:
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", ENDPOINT)
        r = conn.getresponse()
        body = r.read()
        if r.status != 200 or not body:
            return None
        return json.loads(body)
    except Exception:
        return None
    finally:
        conn.close()


def _v3(o, key="position"):
    """Pull a 3-vector from a pose dict; hands use 'pos', others 'position'."""
    if not isinstance(o, dict):
        return None
    v = o.get(key)
    if v is None and key == "position":
        v = o.get("pos")
    if v is None and key == "pos":
        v = o.get("position")
    if isinstance(v, list) and len(v) >= 3:
        return [round(float(v[0]), 4), round(float(v[1]), 4), round(float(v[2]), 4)]
    return None


def local_player(d: dict) -> dict | None:
    """The player matching client_name, else the first player found."""
    name = d.get("client_name")
    players = [p for t in (d.get("teams") or []) for p in (t.get("players") or [])]
    if not players:
        return None
    if name:
        for p in players:
            if p.get("name") == name:
                return p
    return players[0]


def extract(d: dict) -> dict:
    """The compact field set we care about."""
    p = local_player(d)
    rig = d.get("player") or {}
    out = {
        "status": d.get("game_status"),
        "clock": d.get("game_clock"),
        "client": d.get("client_name"),
    }
    if p:
        out.update({
            "name": p.get("name"),
            "head": _v3(p.get("head")),
            "lhand": _v3(p.get("lhand"), "pos"),
            "rhand": _v3(p.get("rhand"), "pos"),
            "vel": [round(float(x), 4) for x in p.get("velocity", [])] or None,
            "hold_L": p.get("holding_left"),
            "hold_R": p.get("holding_right"),
        })
    else:
        out["note"] = "no player in payload (in a match, spawned?)"
    # VR rig transform (room-scale origin) — needed for the tracking->world map.
    if rig:
        out["rig_pos"] = [round(float(x), 4) for x in rig.get("vr_position", [])] or None
        out["rig_fwd"] = [round(float(x), 4) for x in rig.get("vr_forward", [])] or None
        out["rig_left"] = [round(float(x), 4) for x in rig.get("vr_left", [])] or None
        out["rig_up"] = [round(float(x), 4) for x in rig.get("vr_up", [])] or None
    # Local-player shoulder buttons (top level, recorder only).
    for k in ("left_shoulder_pressed", "right_shoulder_pressed",
              "left_shoulder_pressed2", "right_shoulder_pressed2"):
        if d.get(k):
            out[k] = d[k]
    return out


def fmt(rec: dict) -> str:
    """One-line rendering; vectors compact, only present fields."""
    def g(v):
        if isinstance(v, list):
            return "(" + ",".join(f"{x:.3f}" for x in v) + ")"
        return str(v)
    order = ["status", "clock", "name", "rhand", "lhand", "head", "vel",
             "hold_L", "hold_R", "rig_pos", "rig_fwd", "rig_left", "rig_up"]
    parts = []
    for k in order:
        if k in rec and rec[k] is not None:
            parts.append(f"{k}={g(rec[k])}")
    for k, v in rec.items():
        if k not in order and k not in ("client",) and v is not None:
            parts.append(f"{k}={g(v)}")
    return "  ".join(parts) if parts else "(empty)"


def cmd_once(a):
    d = fetch(a.host, a.port, a.timeout)
    if d is None:
        print("no response (game running? in a match? EnableAPIAccess on?)", file=sys.stderr)
        return 1
    print(fmt(extract(d)))
    return 0


def cmd_watch(a):
    interval = 1.0 / a.rate
    out = open(a.out, "a", encoding="utf-8") if a.out else None
    if out:
        print(f"logging to {a.out} at {a.rate} Hz (Ctrl-C to stop)", file=sys.stderr)
    t0 = time.perf_counter()
    n = 0
    try:
        while True:
            if a.seconds and time.perf_counter() - t0 >= a.seconds:
                break
            d = fetch(a.host, a.port, a.timeout)
            stamp = time.strftime("%H:%M:%S") + f".{int((time.time()%1)*1000):03d}"
            line = f"[{stamp}] " + (fmt(extract(d)) if d else "no response")
            print(line)
            if out:
                out.write(line + "\n"); out.flush()
            n += 1
            slack = t0 + n * interval - time.perf_counter()
            if slack > 0:
                time.sleep(slack)
    except KeyboardInterrupt:
        pass
    finally:
        if out:
            out.close()
    return 0


def cmd_selftest(_a):
    fails = []
    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: {got!r} != {want!r}")

    payload = {
        "game_status": "playing", "game_clock": 250.0, "client_name": "me",
        "player": {"vr_position": [0.1, 0.0, -0.2], "vr_forward": [0, 0, 1],
                   "vr_left": [1, 0, 0], "vr_up": [0, 1, 0]},
        "left_shoulder_pressed": 1.0,
        "teams": [
            {"team": "BLUE", "players": [
                {"name": "other", "head": {"position": [9, 9, 9]},
                 "rhand": {"pos": [8, 8, 8]}, "lhand": {"pos": [7, 7, 7]},
                 "velocity": [0, 0, 0], "holding_left": "none", "holding_right": "none"}]},
            {"team": "ORANGE", "players": [
                {"name": "me", "head": {"position": [1.0, 2.0, 3.0]},
                 "rhand": {"pos": [0.30, 0.0, 0.0]}, "lhand": {"pos": [0, 0, 0]},
                 "velocity": [0.5, 0, 0], "holding_left": "none", "holding_right": "3"}]},
        ],
    }
    rec = extract(payload)
    check("local player picked by client_name", rec["name"], "me")
    check("rhand", rec["rhand"], [0.30, 0.0, 0.0])
    check("head", rec["head"], [1.0, 2.0, 3.0])
    check("hold_R", rec["hold_R"], "3")
    check("rig_pos", rec["rig_pos"], [0.1, 0.0, -0.2])
    check("shoulder surfaced", rec.get("left_shoulder_pressed"), 1.0)

    # Fallback to first player when no name matches.
    p2 = dict(payload); p2 = json.loads(json.dumps(payload)); p2["client_name"] = "absent"
    check("fallback to first player", extract(p2)["name"], "other")

    # No players.
    empty = {"game_status": "lobby", "teams": [{"team": "SPECTATORS", "players": []}]}
    r = extract(empty)
    check("no-player note", "note" in r, True)

    # 'pos' vs 'position' handling.
    check("_v3 pos key", _v3({"pos": [1, 2, 3]}, "pos"), [1.0, 2.0, 3.0])
    check("_v3 position key", _v3({"position": [4, 5, 6]}), [4.0, 5.0, 6.0])

    # fmt runs without error and includes rhand.
    line = fmt(rec)
    check("fmt has rhand", "rhand=(0.300,0.000,0.000)" in line, True)

    if fails:
        for f in fails:
            print("FAIL", f, file=sys.stderr)
        return 1
    print("all self-tests passed")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--host", default=DEFAULT_HOST)
        p.add_argument("--port", type=int, default=DEFAULT_PORT)
        p.add_argument("--timeout", type=float, default=2.0)

    o = sub.add_parser("once", help="print one compact snapshot")
    common(o); o.set_defaults(func=cmd_once)

    w = sub.add_parser("watch", help="poll continuously")
    common(w)
    w.add_argument("--rate", type=float, default=10.0, help="polls/sec")
    w.add_argument("--seconds", type=float, default=0.0, help="0 = until Ctrl-C")
    w.add_argument("--out", default=None, help="also append to this file")
    w.set_defaults(func=cmd_watch)

    t = sub.add_parser("selftest", help="synthetic validation, no game")
    t.set_defaults(func=cmd_selftest)

    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
