"""Validate echo_disc_predict against real logged bounces.

Ground truth comes from detect_real_bounces.py's blocks in log.txt: the disc's
position one poll before a velocity reversal, plus the velocity either side of
it.  Method is the one that validated the old model - back off 8 m along the
incoming velocity, predict forward, and measure how far the predicted contact
lands from the logged position.

Two things get measured that the old model could not check at all:
  * which collision body (hull) the disc actually collides with;
  * the bounce law itself, by comparing the predicted outgoing velocity with
    the logged one.

NOTE ON `up`: detect_real_bounces.py does not log `disc.up`, so the ring
collider cannot be oriented for this corpus and these runs are point-mode.
Re-run the (now up-logging) detector to get a ring-mode corpus.
"""

import os
import re
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from echo_disc_predict import DiscPredictor, bounce  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = [os.path.join(HERE, "log.txt"), os.path.join(os.path.dirname(HERE), "log.txt")]
BACKOFF_M = 8.0

_VEC = r"\[([-\d.e+, ]+)\]"
_RE = re.compile(
    r"BOUNCE #(\d+) detected!\s*\n"
    r"\s*position ~ " + _VEC + r"\s*\n"
    r"\s*velocity before: " + _VEC + r"[^\n]*\n"
    r"\s*velocity after:\s*" + _VEC,
    re.M)


def parse_bounces():
    out = []
    for path in LOGS:
        if not os.path.exists(path):
            continue
        text = open(path, encoding="utf-8", errors="replace").read()
        for m in _RE.finditer(text):
            pos = np.array([float(x) for x in m.group(2).split(",")])
            v_in = np.array([float(x) for x in m.group(3).split(",")])
            v_out = np.array([float(x) for x in m.group(4).split(",")])
            if len(pos) == 3 and len(v_in) == 3 and len(v_out) == 3:
                out.append((pos, v_in, v_out, os.path.basename(path), int(m.group(1))))
    return out


def evaluate(pred, events, horizon_pad=1.0):
    """Contact-position error and outgoing-velocity error per event."""
    pos_err, vel_err, vel_ang, missed = [], [], [], []
    for pos, v_in, v_out, src, idx in events:
        speed = np.linalg.norm(v_in)
        if speed < 1e-6:
            continue
        d = v_in / speed
        start = pos - d * BACKOFF_M
        horizon = (BACKOFF_M + horizon_pad) / speed
        path = pred.predict_from(start, v_in, None, horizon=horizon, max_bounces=1)
        if not path.bounces:
            missed.append((src, idx))
            continue
        b = path.bounces[0]
        pos_err.append(float(np.linalg.norm(b.centre - pos)))
        pv = b.v_out
        vel_err.append(float(np.linalg.norm(pv - v_out)))
        na, nb = np.linalg.norm(pv), np.linalg.norm(v_out)
        if na > 1e-6 and nb > 1e-6:
            vel_ang.append(float(np.degrees(np.arccos(np.clip(pv @ v_out / (na * nb), -1, 1)))))
    return np.array(pos_err), np.array(vel_err), np.array(vel_ang), missed


def summarise(tag, pos_err, vel_err, vel_ang, missed, n):
    if len(pos_err) == 0:
        print("%-28s  no hits (%d missed)" % (tag, len(missed)))
        return
    print("%-28s  hit %2d/%2d | contact med %5.2f m  p90 %5.2f m | "
          "v_out med %5.2f m/s  angle med %5.1f deg"
          % (tag, len(pos_err), n, np.median(pos_err), np.percentile(pos_err, 90),
             np.median(vel_err), np.median(vel_ang)))


def main():
    events = parse_bounces()
    print("parsed %d real bounces from %s\n"
          % (len(events), ", ".join(os.path.basename(p) for p in LOGS if os.path.exists(p))))
    if not events:
        print("no ground truth found - run detect_real_bounces.py first")
        return

    # 1. which hull(s) does the disc collide with?
    print("--- hull selection (point mode) ---")
    candidates = [(0,), (2,), (4,), (0, 2), (0, 4), (2, 4), (0, 2, 4),
                  tuple(range(9))]
    results = {}
    for hulls in candidates:
        pred = DiscPredictor.load(hulls=hulls, mode="point")
        r = evaluate(pred, events)
        results[hulls] = r
        summarise("hulls %-16s" % (str(hulls),), *r, n=len(events))

    best = min((h for h in results if len(results[h][0])),
               key=lambda h: (np.median(results[h][0]) if len(results[h][0]) else 1e9,
                              -len(results[h][0])))
    print("\nbest by median contact error: hulls=%s" % (best,))

    # 2. baseline: the current shipped model, same events, same method
    print("\n--- baseline: arena_geometry.py (replay-viewer assets) ---")
    try:
        import arena_geometry
        errs = []
        miss = 0
        for pos, v_in, v_out, src, idx in events:
            speed = np.linalg.norm(v_in)
            d = v_in / speed
            start = pos - d * BACKOFF_M
            hit = arena_geometry.raycast_arena_mesh(start.tolist(), d.tolist(), BACKOFF_M + 1.0)
            if hit is None:
                miss += 1
                continue
            errs.append(float(np.linalg.norm(np.array(hit[1]) - pos)))
        errs = np.array(errs)
        if len(errs):
            print("%-28s  hit %2d/%2d | contact med %5.2f m  p90 %5.2f m"
                  % ("arena_mesh.npz", len(errs), len(events),
                     np.median(errs), np.percentile(errs, 90)))
    except Exception as exc:                                  # pragma: no cover
        print("baseline unavailable: %s" % exc)

    # 3. bounce law, using the best hull set: is v_out right given the normal?
    print("\n--- bounce law check (hulls=%s) ---" % (best,))
    pred = DiscPredictor.load(hulls=best, mode="point")
    ratios_n, ratios_t, incid = [], [], []
    for pos, v_in, v_out, src, idx in events:
        speed = np.linalg.norm(v_in)
        d = v_in / speed
        start = pos - d * BACKOFF_M
        path = pred.predict_from(start, v_in, None,
                                 horizon=(BACKOFF_M + 1.0) / speed, max_bounces=1)
        if not path.bounces:
            continue
        n = path.bounces[0].normal
        vn_in, vn_out = v_in @ n, v_out @ n
        vt_in = v_in - n * vn_in
        vt_out = v_out - n * vn_out
        if abs(vn_in) > 0.5:
            ratios_n.append(-vn_out / vn_in)
        if np.linalg.norm(vt_in) > 0.5:
            ratios_t.append(np.linalg.norm(vt_out) / np.linalg.norm(vt_in))
        incid.append(1.0 - abs(vn_in / speed))
    if ratios_n:
        print("normal restitution  : median %.3f  (expected 0.50)  n=%d"
              % (np.median(ratios_n), len(ratios_n)))
    if ratios_t:
        print("tangential restit.  : median %.3f  (expected 0.50 head-on .. 1.00 grazing)  n=%d"
              % (np.median(ratios_t), len(ratios_t)))
    if incid:
        print("grazing parameter t : median %.2f" % np.median(incid))


if __name__ == "__main__":
    main()
