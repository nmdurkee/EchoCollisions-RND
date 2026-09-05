#!/usr/bin/env python3
"""
echovr_apilatency.py — characterise the Echo VR /session API as a control-loop
data source.

Echo VR runs a local HTTP listener serving the session state as JSON
(`[NETGAME] Bound HTTP listener to %s:%u, responding to /session`, gated by the
`EnableAPIAccess` setting). This is the same endpoint Spark polls to record
`.echoreplay` files, and the intended read path for an agent — see
ECHOVR_REPLAY_FORMAT.md and ECHOVR_INPUT_NOTES.md.

Before trusting it in a control loop you want to know three things. This tool
measures the first two; the third needs an in-process hook and is out of scope.

  1. JITTER AND STALENESS  (`measure`)
     Poll at a fixed rate. The payload carries `game_clock`, stamped by the game,
     so regressing it against local receipt time gives the *variation* in delay
     without needing a shared epoch.

  2. INTERNAL CADENCE  (`saturate`)
     Poll as fast as the endpoint answers and count DISTINCT payloads. This
     separates "the API is slow" from "my polling is slow", which a latency
     number alone cannot tell you.

  3. ABSOLUTE END-TO-END LATENCY  (not implemented)
     Requires injecting a known stimulus through the `ovr_GetTrackingState` hook
     and timing its appearance here. Both timestamps then come from one clock.
     Until that exists, no method in this file yields an absolute figure.

WHAT THIS CANNOT TELL YOU: absolute latency. `game_clock` is match time, not wall
time, so its zero point is arbitrary. Every delay figure here is relative
variation. Do not read the mean offset as a latency.

Usage:
    python echovr_apilatency.py probe     [--host H] [--port P]
    python echovr_apilatency.py measure   [--seconds N] [--rate HZ] [--json OUT]
    python echovr_apilatency.py saturate  [--seconds N] [--json OUT]
    python echovr_apilatency.py selftest

Standard library only; no pip install required.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6721
ENDPOINT = "/session"

# game_clock is served to hundredths; tighter than this is noise, not signal.
CLOCK_EPS = 1e-4


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


@dataclass
class Sample:
    """One poll."""

    t_send: float  # perf_counter before request
    t_recv: float  # perf_counter after full body read
    status: int
    body_sha1: str
    n_bytes: int
    game_clock: float | None
    game_status: str | None
    error: str | None = None

    @property
    def rtt_ms(self) -> float:
        return (self.t_recv - self.t_send) * 1000.0


class SessionClient:
    """Thin keep-alive client.

    Keep-alive is the default deliberately. Without it every poll pays a TCP
    handshake, and you end up measuring connection setup rather than the API.
    Use `keep_alive=False` only to quantify that difference.
    """

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=2.0,
                 keep_alive=True):
        self.host, self.port = host, port
        self.timeout, self.keep_alive = timeout, keep_alive
        self._conn: http.client.HTTPConnection | None = None

    def _connect(self) -> http.client.HTTPConnection:
        if self._conn is None:
            self._conn = http.client.HTTPConnection(
                self.host, self.port, timeout=self.timeout
            )
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def poll(self) -> Sample:
        t0 = time.perf_counter()
        try:
            conn = self._connect()
            conn.request("GET", ENDPOINT)
            resp = conn.getresponse()
            body = resp.read()
            status = resp.status
            if not self.keep_alive:
                self.close()
        except Exception as exc:  # noqa: BLE001 - report, never crash a run
            self.close()
            t1 = time.perf_counter()
            return Sample(t0, t1, 0, "", 0, None, None, error=f"{type(exc).__name__}: {exc}")

        t1 = time.perf_counter()
        clock = status_str = None
        if status == 200 and body:
            try:
                d = json.loads(body)
                gc = d.get("game_clock")
                clock = float(gc) if isinstance(gc, (int, float)) else None
                gs = d.get("game_status")
                status_str = gs if isinstance(gs, str) else None
            except (ValueError, TypeError):
                pass
        return Sample(
            t0, t1, status,
            hashlib.sha1(body).hexdigest(), len(body),
            clock, status_str,
        )


# ---------------------------------------------------------------------------
# Analysis — pure functions so they can be tested without a game
# ---------------------------------------------------------------------------


def _linfit(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Least-squares slope and intercept. Slope 0 if x has no spread."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return 0.0, my
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    return slope, my - slope * mx


def _pct(vals: list[float], p: float) -> float:
    """Nearest-rank percentile. Tails matter more than means for jitter."""
    if not vals:
        return float("nan")
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1)))))
    return s[k]


@dataclass
class PacingResult:
    n: int = 0
    n_ok: int = 0
    n_error: int = 0
    errors: dict = field(default_factory=dict)
    rtt_ms: dict = field(default_factory=dict)
    clock_slope: float | None = None
    clock_direction: str = "unknown"
    jitter_ms: dict = field(default_factory=dict)
    distinct_payloads: int = 0
    distinct_clocks: int = 0
    dwell_ms: dict = field(default_factory=dict)
    statuses: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)


def analyse_pacing(samples: list[Sample]) -> PacingResult:
    """Jitter and staleness from a paced run."""
    r = PacingResult(n=len(samples))
    ok = [s for s in samples if s.error is None and s.status == 200]
    r.n_ok, r.n_error = len(ok), len(samples) - len(ok)
    for s in samples:
        if s.error:
            key = s.error.split(":")[0]
            r.errors[key] = r.errors.get(key, 0) + 1

    if not ok:
        r.notes.append("no successful polls; nothing to analyse")
        return r

    rtts = [s.rtt_ms for s in ok]
    r.rtt_ms = {
        "p50": round(_pct(rtts, 50), 3),
        "p95": round(_pct(rtts, 95), 3),
        "p99": round(_pct(rtts, 99), 3),
        "max": round(max(rtts), 3),
    }

    for s in ok:
        if s.game_status:
            r.statuses[s.game_status] = r.statuses.get(s.game_status, 0) + 1
    r.distinct_payloads = len({s.body_sha1 for s in ok})

    clocked = [s for s in ok if s.game_clock is not None]
    if len(clocked) < 8:
        r.notes.append("too few samples carrying game_clock for a fit")
        return r

    r.distinct_clocks = len({round(s.game_clock, 4) for s in clocked})
    if r.distinct_clocks < 3:
        r.notes.append(
            "game_clock is not advancing — the match is probably not in "
            "'playing'. Jitter numbers below are meaningless; re-run during "
            "live play."
        )
        return r

    # Regress game_clock against local receipt time. Slope reveals direction
    # and rate; residuals are the delay variation we actually want.
    xs = [s.t_recv for s in clocked]
    ys = [s.game_clock for s in clocked]
    slope, intercept = _linfit(xs, ys)
    r.clock_slope = round(slope, 6)
    if slope < -0.5:
        r.clock_direction = "counting down"
    elif slope > 0.5:
        r.clock_direction = "counting up"
    else:
        r.clock_direction = "stalled or irregular"
        r.notes.append(
            f"clock slope {slope:.3f} is not near +/-1; the clock may be paused"
        )

    if abs(slope) > CLOCK_EPS:
        # Residual in clock-seconds converted back to wall milliseconds.
        resid_ms = [
            ((y - (slope * x + intercept)) / slope) * 1000.0
            for x, y in zip(xs, ys)
        ]
        centre = statistics.median(resid_ms)
        dev = [abs(v - centre) for v in resid_ms]
        r.jitter_ms = {
            "stdev": round(statistics.pstdev(resid_ms), 3),
            "median_abs_dev": round(statistics.median(dev), 3),
            "p95_abs": round(_pct(dev, 95), 3),
            "p99_abs": round(_pct(dev, 99), 3),
            "max_abs": round(max(dev), 3),
        }

    # Dwell: how long one distinct clock value keeps being served. This is the
    # source's real update period, independent of how fast we asked.
    dwells, run_start, prev = [], clocked[0].t_recv, clocked[0].game_clock
    for s in clocked[1:]:
        if abs(s.game_clock - prev) > CLOCK_EPS:
            dwells.append((s.t_recv - run_start) * 1000.0)
            run_start, prev = s.t_recv, s.game_clock
    if dwells:
        r.dwell_ms = {
            "p50": round(_pct(dwells, 50), 3),
            "p95": round(_pct(dwells, 95), 3),
            "max": round(max(dwells), 3),
            "implied_hz": round(1000.0 / statistics.median(dwells), 2),
        }
    return r


@dataclass
class SaturationResult:
    seconds: float = 0.0
    n_polls: int = 0
    n_ok: int = 0
    poll_hz: float = 0.0
    distinct_payloads: int = 0
    distinct_payload_hz: float = 0.0
    distinct_clocks: int = 0
    distinct_clock_hz: float = 0.0
    oversample_ratio: float = 0.0
    rtt_ms: dict = field(default_factory=dict)
    verdict: str = ""
    notes: list = field(default_factory=list)


def analyse_saturation(samples: list[Sample], elapsed: float) -> SaturationResult:
    """Internal update cadence from a max-rate run."""
    r = SaturationResult(seconds=round(elapsed, 3), n_polls=len(samples))
    ok = [s for s in samples if s.error is None and s.status == 200]
    r.n_ok = len(ok)
    if not ok or elapsed <= 0:
        r.verdict = "no data"
        return r

    r.poll_hz = round(len(ok) / elapsed, 2)
    r.distinct_payloads = len({s.body_sha1 for s in ok})
    r.distinct_payload_hz = round(r.distinct_payloads / elapsed, 2)
    clocks = {round(s.game_clock, 4) for s in ok if s.game_clock is not None}
    r.distinct_clocks = len(clocks)
    r.distinct_clock_hz = round(len(clocks) / elapsed, 2)
    r.oversample_ratio = round(
        len(ok) / r.distinct_payloads if r.distinct_payloads else 0.0, 2
    )

    rtts = [s.rtt_ms for s in ok]
    r.rtt_ms = {
        "p50": round(_pct(rtts, 50), 3),
        "p95": round(_pct(rtts, 95), 3),
        "max": round(max(rtts), 3),
    }

    hz = r.distinct_payload_hz
    if r.poll_hz < 35:
        r.verdict = (
            f"inconclusive — only reached {r.poll_hz} polls/s; the client or "
            f"transport is the bottleneck, not the API"
        )
    elif hz >= 60:
        r.verdict = f"~{hz} Hz distinct — looks frame-locked, serving live state"
    elif 20 <= hz < 60:
        r.verdict = (
            f"~{hz} Hz distinct while polling at {r.poll_hz} Hz — internally "
            f"throttled; that period is your floor"
        )
    else:
        r.verdict = (
            f"only ~{hz} Hz distinct — likely a cached snapshot on a timer"
        )

    if r.distinct_clocks <= 2:
        r.notes.append(
            "game_clock barely changed — probably not in live play; the "
            "payload-based figures may reflect an idle scene"
        )
    return r


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------


def _hint(sample: Sample) -> None:
    """Any transport failure has the same short checklist behind it."""
    if sample.error:
        print(
            "\nCould not reach the endpoint. Check that:\n"
            "  - Echo VR is running and in a match\n"
            "  - 'EnableAPIAccess' is on in the in-game settings menu\n"
            "  - the port matches (default 6721)\n"
            "  - no firewall rule is dropping loopback traffic (a silent drop\n"
            "    shows up as a timeout rather than a refusal)",
            file=sys.stderr,
        )


def cmd_probe(a: argparse.Namespace) -> int:
    c = SessionClient(a.host, a.port, a.timeout)
    s = c.poll()
    c.close()
    print(f"GET http://{a.host}:{a.port}{ENDPOINT}")
    if s.error:
        print(f"  FAILED: {s.error}")
        _hint(s)
        return 1
    print(f"  status      : {s.status}")
    print(f"  rtt         : {s.rtt_ms:.2f} ms")
    print(f"  body        : {s.n_bytes} bytes")
    print(f"  game_status : {s.game_status}")
    print(f"  game_clock  : {s.game_clock}")
    if s.game_status != "playing":
        print(
            "\n  NOTE: game_status is not 'playing'. Timing runs need live play;\n"
            "        the clock does not advance otherwise."
        )
    return 0


def _run(client: SessionClient, seconds: float, interval: float | None) -> tuple[list, float]:
    out: list[Sample] = []
    t_start = time.perf_counter()
    deadline = t_start + seconds
    n = 0
    while True:
        now = time.perf_counter()
        if now >= deadline:
            break
        out.append(client.poll())
        n += 1
        if interval:
            target = t_start + n * interval
            slack = target - time.perf_counter()
            if slack > 0:
                time.sleep(slack)
    return out, time.perf_counter() - t_start


def cmd_measure(a: argparse.Namespace) -> int:
    interval = 1.0 / a.rate
    c = SessionClient(a.host, a.port, a.timeout, keep_alive=not a.no_keepalive)
    first = c.poll()
    if first.error:
        print(f"FAILED: {first.error}", file=sys.stderr)
        _hint(first)
        return 1
    print(
        f"polling {a.host}:{a.port}{ENDPOINT} at {a.rate} Hz for {a.seconds}s "
        f"(keep-alive {'off' if a.no_keepalive else 'on'})...",
        file=sys.stderr,
    )
    samples, _ = _run(c, a.seconds, interval)
    c.close()

    r = analyse_pacing(samples)
    print(json.dumps(asdict(r), indent=2))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(
                {"result": asdict(r),
                 "samples": [asdict(s) for s in samples]}, fh, indent=2)
        print(f"\nraw samples -> {a.json}", file=sys.stderr)
    print(
        "\nREMINDER: jitter here is delay *variation*. Absolute latency needs "
        "the stimulus test through the ovr_GetTrackingState hook.",
        file=sys.stderr,
    )
    return 0


def cmd_saturate(a: argparse.Namespace) -> int:
    c = SessionClient(a.host, a.port, a.timeout, keep_alive=not a.no_keepalive)
    first = c.poll()
    if first.error:
        print(f"FAILED: {first.error}", file=sys.stderr)
        _hint(first)
        return 1
    print(
        f"polling {a.host}:{a.port}{ENDPOINT} flat out for {a.seconds}s...",
        file=sys.stderr,
    )
    samples, elapsed = _run(c, a.seconds, None)
    c.close()

    r = analyse_saturation(samples, elapsed)
    print(json.dumps(asdict(r), indent=2))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(
                {"result": asdict(r),
                 "samples": [asdict(s) for s in samples]}, fh, indent=2)
        print(f"\nraw samples -> {a.json}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# Self-test — synthetic samples, no game required
# ---------------------------------------------------------------------------


def _synth(n, poll_dt, clock_dt, direction=-1.0, jitter=None, start_clock=600.0):
    """Fabricate samples: source ticks every `clock_dt`, we poll every `poll_dt`."""
    jitter = jitter or [0.0] * n
    out, t = [], 0.0
    for i in range(n):
        # The source advances on its own regular schedule, so the clock value is
        # a function of the *unjittered* arrival. Jitter perturbs only when we
        # observe it. Deriving the clock from the jittered time instead would
        # make delay variation cancel out of the fit.
        base = t + 0.001
        recv = base + jitter[i]
        ticks = int(base / clock_dt)
        clock = start_clock + direction * ticks * clock_dt
        out.append(Sample(
            t_send=t, t_recv=recv, status=200,
            body_sha1=hashlib.sha1(f"{ticks}".encode()).hexdigest(),
            n_bytes=1000, game_clock=clock, game_status="playing"))
        t += poll_dt
    return out


def cmd_selftest(_a: argparse.Namespace) -> int:
    fails: list[str] = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: got {got!r}, want {want!r}")

    def close(label, got, want, tol):
        if got is None or abs(got - want) > tol:
            fails.append(f"{label}: got {got!r}, want ~{want} (+/-{tol})")

    # Countdown clock, 90 Hz source, 30 Hz polling.
    s = _synth(300, poll_dt=1 / 30, clock_dt=1 / 90, direction=-1.0)
    r = analyse_pacing(s)
    check("pacing n_ok", r.n_ok, 300)
    check("direction", r.clock_direction, "counting down")
    close("slope", r.clock_slope, -1.0, 0.05)
    close("dwell implied_hz", r.dwell_ms.get("implied_hz"), 30.0, 5.0)

    # Count-up clock is detected too.
    r2 = analyse_pacing(_synth(200, 1 / 30, 1 / 90, direction=+1.0))
    check("direction up", r2.clock_direction, "counting up")

    # Injected jitter must surface.
    jit = [0.0, 0.02] * 100
    rj = analyse_pacing(_synth(200, 1 / 30, 1 / 900, jitter=jit))
    if not rj.jitter_ms or rj.jitter_ms["max_abs"] < 5.0:
        fails.append(f"jitter not detected: {rj.jitter_ms}")

    # A frozen clock must be called out, not silently fitted.
    frozen = [Sample(i / 30, i / 30 + 0.001, 200, "same", 100, 42.0, "round_start")
              for i in range(60)]
    rf = analyse_pacing(frozen)
    if not any("not advancing" in n for n in rf.notes):
        fails.append(f"frozen clock not flagged: {rf.notes}")

    # Saturation: source 30 Hz, polled at 300 Hz -> throttled verdict.
    sat = _synth(3000, poll_dt=1 / 300, clock_dt=1 / 30)
    rs = analyse_saturation(sat, 10.0)
    close("distinct payload hz", rs.distinct_payload_hz, 30.0, 3.0)
    if "throttled" not in rs.verdict:
        fails.append(f"expected throttled verdict, got: {rs.verdict}")
    if rs.oversample_ratio < 5:
        fails.append(f"oversample ratio wrong: {rs.oversample_ratio}")

    # Source at 90 Hz reads as frame-locked.
    rs2 = analyse_saturation(_synth(3000, 1 / 300, 1 / 90), 10.0)
    if "frame-locked" not in rs2.verdict:
        fails.append(f"expected frame-locked verdict, got: {rs2.verdict}")

    # A slow client must not be blamed on the API.
    rs3 = analyse_saturation(_synth(100, 1 / 10, 1 / 90), 10.0)
    if "inconclusive" not in rs3.verdict:
        fails.append(f"expected inconclusive verdict, got: {rs3.verdict}")

    # Errors are counted, never raised.
    errs = [Sample(0, 0.001, 0, "", 0, None, None, error="ConnectionRefusedError: x")
            for _ in range(5)]
    re_ = analyse_pacing(errs)
    check("error count", re_.n_error, 5)
    check("no ok", re_.n_ok, 0)

    if fails:
        for f in fails:
            print(f"FAIL  {f}", file=sys.stderr)
        return 1
    print("all self-tests passed")
    return 0


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--host", default=DEFAULT_HOST)
        p.add_argument("--port", type=int, default=DEFAULT_PORT)
        p.add_argument("--timeout", type=float, default=2.0)

    p = sub.add_parser("probe", help="one request; check the API is reachable")
    common(p)
    p.set_defaults(func=cmd_probe)

    m = sub.add_parser("measure", help="paced poll; jitter and staleness")
    common(m)
    m.add_argument("--seconds", type=float, default=30.0)
    m.add_argument("--rate", type=float, default=30.0, help="polls/sec")
    m.add_argument("--no-keepalive", action="store_true",
                   help="reconnect each poll (measures handshake cost too)")
    m.add_argument("--json", default=None, help="write raw samples here")
    m.set_defaults(func=cmd_measure)

    s = sub.add_parser("saturate", help="max-rate poll; find internal cadence")
    common(s)
    s.add_argument("--seconds", type=float, default=10.0)
    s.add_argument("--no-keepalive", action="store_true")
    s.add_argument("--json", default=None)
    s.set_defaults(func=cmd_saturate)

    t = sub.add_parser("selftest", help="synthetic validation, no game needed")
    t.set_defaults(func=cmd_selftest)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
