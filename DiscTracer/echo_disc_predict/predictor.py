"""Disc trajectory prediction against the game's own collision mesh.

Replaces the replay-viewer-asset model in `arena_geometry.py` with:
  * `mpl_arena_a_collision.npz` - the mesh the game actually collides against,
    extracted from the package files (9 bodies, 91,656 tris, world space).
  * the disc's real 40-particle ring collider.
  * `CR15BounceCS::OnCollision`'s restitution law with the arena's constants.

Flight is a straight line at constant speed (zero-g, no drag).
"""

import os

import numpy as np

from . import collider
from .bvh import BVH

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_EPS_T = 1e-4          # minimum travel before a hit counts, metres
_NUDGE = 1e-3          # push off the surface after a bounce, metres
_MERGE_DIST = 0.05     # contacts within this distance count as simultaneous, metres


class Bounce:
    __slots__ = ("t", "point", "centre", "normal", "v_in", "v_out", "body", "triangle")

    def __init__(self, t, point, centre, normal, v_in, v_out, body, triangle):
        self.t = t                 # seconds from the start of the prediction
        self.point = point         # contact point on the mesh
        self.centre = centre       # disc centre at contact
        self.normal = normal       # surface normal, facing the incoming disc
        self.v_in = v_in
        self.v_out = v_out
        self.body = body
        self.triangle = triangle

    def __repr__(self):
        return "Bounce(t=%.3f, point=[%.2f %.2f %.2f], body=%d)" % (
            self.t, self.point[0], self.point[1], self.point[2], self.body)


class Path:
    __slots__ = ("waypoints", "bounces", "confidence", "horizon")

    def __init__(self, waypoints, bounces, confidence, horizon):
        self.waypoints = waypoints    # disc-centre polyline: start, each bounce, end
        self.bounces = bounces
        self.confidence = confidence  # set of flag strings; empty = nominal
        self.horizon = horizon

    def sample(self, dt):
        """Resample the polyline at fixed dt (the spec's `points (K,3)`)."""
        pts = [np.asarray(w, dtype=np.float64) for w in self.waypoints]
        if len(pts) < 2:
            return np.array(pts)
        seg_t = [b.t for b in self.bounces] + [self.horizon]
        out, t0, p0 = [], 0.0, pts[0]
        for p1, t1 in zip(pts[1:], seg_t):
            n = max(1, int(round((t1 - t0) / dt)))
            for i in range(n):
                out.append(p0 + (p1 - p0) * (i / n))
            t0, p0 = t1, p1
        out.append(pts[-1])
        return np.array(out)


class DiscPredictor:
    def __init__(self, verts, tris, body, material, hulls, mode="ring",
                 bounce_consts=None, disc_points=None):
        self.mode = mode
        self.bounce_consts = bounce_consts or dict(collider.BOUNCE_DEFAULT)
        self.disc_points = collider.load_disc_points() if disc_points is None else disc_points
        self.radius = collider.disc_radius(self.disc_points)

        keep = np.isin(body, list(hulls))
        self.hulls = tuple(hulls)
        self.tris = tris[keep]
        self.body = body[keep]
        self.material = material[keep] if material is not None else None
        self.verts = verts

        self._v0 = verts[self.tris[:, 0]]
        self._e1 = verts[self.tris[:, 1]] - self._v0
        self._e2 = verts[self.tris[:, 2]] - self._v0
        self.bvh = BVH(verts, self.tris)

        self._hist = []       # (t, pos, vel, up) ring buffer for observe()

    # ---------------------------------------------------------------- loading
    @classmethod
    def load(cls, path=None, hulls=tuple(range(9)), mode="ring", bounce_consts=None):
        # All nine bodies by default.  validate_bounces.py on the 19 real
        # logged wall bounces: all-bodies finds the correct surface 14/19
        # (hulls (0,2,4) 13/19, (0,2) 12/19, single hulls 2-8/19) with no
        # extra phantom hits, so the "which hull" question is moot - take
        # them all.
        path = path or os.path.join(_DATA, "arena_collision.npz")
        d = np.load(path, allow_pickle=True)
        material = np.asarray(d["material"], dtype=np.int64) if "material" in d.files else None
        return cls(np.asarray(d["vertices"], dtype=np.float64),
                   np.asarray(d["triangles"], dtype=np.int64),
                   np.asarray(d["body"], dtype=np.int64),
                   material,
                   hulls, mode=mode, bounce_consts=bounce_consts)

    # ------------------------------------------------------------ narrow phase
    def _sweep(self, centre, direction, length, up):
        """Earliest contact of the swept disc with the mesh.

        Returns (t_dist, contact_point, centre_at_contact, normal, tri, body)
        in units of distance along `direction`, or None.
        """
        cand = self.bvh.query_capsule(centre, direction, length, self.radius + 1e-3)
        if cand is None or len(cand) == 0:
            return None

        v0 = self._v0[cand]
        e1 = self._e1[cand]
        e2 = self._e2[cand]
        d = np.asarray(direction, dtype=np.float64)

        if self.mode == "ring" and up is not None:
            offsets = collider.orient(self.disc_points, up)
            if offsets is None:
                offsets = np.zeros((1, 3))
        else:
            offsets = np.zeros((1, 3))
        origins = np.asarray(centre, dtype=np.float64)[None, :] + offsets   # (P,3)

        # Moller-Trumbore, all particles against all candidate triangles
        h = np.cross(d, e2)                                   # (C,3)
        a = np.einsum("ij,ij->i", e1, h)                      # (C,)
        ok_a = np.abs(a) > 1e-12
        if not ok_a.any():
            return None
        f = np.zeros_like(a)
        f[ok_a] = 1.0 / a[ok_a]

        s = origins[:, None, :] - v0[None, :, :]              # (P,C,3)
        u = f[None, :] * np.einsum("pcj,cj->pc", s, h)
        q = np.cross(s, e1[None, :, :])                       # (P,C,3)
        v = f[None, :] * np.einsum("pcj,j->pc", q, d)
        t = f[None, :] * np.einsum("pcj,cj->pc", q, e2)

        good = (ok_a[None, :] & (u >= -1e-9) & (v >= -1e-9) &
                (u + v <= 1.0 + 1e-9) & (t > _EPS_T) & (t <= length))
        if not good.any():
            return None

        t_masked = np.where(good, t, np.inf)
        flat = int(np.argmin(t_masked))
        pi, ci = np.unravel_index(flat, t_masked.shape)
        t_hit = float(t_masked[pi, ci])

        # Simultaneous contacts.  The ring is 0.62 m across, so clipping a
        # corner puts several particles through different faces in the same
        # step.  The engine's solver resolves those together; taking only the
        # first face instead produced two "bounces" 10 ms apart off adjacent
        # faces of the same obstacle.  Average the normals of everything
        # contacting within _MERGE_DIST and treat it as one contact.
        near = good & (t <= t_hit + _MERGE_DIST)
        tri_ids = np.unique(np.nonzero(near)[1])
        n = np.cross(e1[tri_ids], e2[tri_ids])
        n /= np.linalg.norm(n, axis=1, keepdims=True)
        n[np.einsum("ij,j->i", n, d) > 0.0] *= -1.0     # face the incoming disc
        n = n.mean(axis=0)
        n_len = np.linalg.norm(n)
        if n_len < 1e-9:        # opposed faces cancelled: disc is wedged
            return None
        n /= n_len
        contact = origins[pi] + d * t_hit
        centre_at = np.asarray(centre, dtype=np.float64) + d * t_hit
        return t_hit, contact, centre_at, n, int(cand[ci]), int(self.body[cand[ci]])

    # ------------------------------------------------------------- prediction
    def predict_from(self, pos, vel, up=None, horizon=2.0, max_bounces=8):
        pos = np.asarray(pos, dtype=np.float64)
        vel = np.asarray(vel, dtype=np.float64)
        speed = float(np.linalg.norm(vel))
        conf = set()
        if up is None:
            conf.add("no_up_point_mode")
        if speed < 0.1:
            return Path([pos.tolist(), pos.tolist()], [], conf | {"stationary"}, horizon)

        centre = pos.copy()
        direction = vel / speed
        v = vel.copy()
        t_now = 0.0
        waypoints = [centre.tolist()]
        bounces = []

        while t_now < horizon and len(bounces) < max_bounces:
            remaining = (horizon - t_now) * speed
            hit = self._sweep(centre, direction, remaining, up)
            if hit is None:
                break
            t_dist, contact, centre_at, n, tri, body = hit
            t_hit = t_now + t_dist / speed
            v_out = collider.bounce(v, n, self.bounce_consts)
            bounces.append(Bounce(t_hit, contact, centre_at, n, v.copy(), v_out.copy(), body, tri))
            waypoints.append(centre_at.tolist())

            centre = centre_at + n * _NUDGE
            v = v_out
            speed = float(np.linalg.norm(v))
            if speed < 0.1:
                conf.add("stopped")
                break
            direction = v / speed
            t_now = t_hit

        end = centre + direction * (horizon - t_now) * speed
        waypoints.append(end.tolist())
        if len(bounces) >= max_bounces:
            conf.add("max_bounces")
        return Path(waypoints, bounces, conf, horizon)

    # ------------------------------------------------- live state estimation
    def observe(self, t, pos, vel, up=None, window=8):
        """Feed one API frame.  Keeps a short history and resets it on a
        discontinuity (throw, catch, touch, bounce) so a stale pre-event
        sample can never drag the fitted direction - that is the flicker."""
        vel = np.asarray(vel, dtype=np.float64)
        if self._hist:
            prev = self._hist[-1][2]
            sp, spv = np.linalg.norm(vel), np.linalg.norm(prev)
            if sp > 1e-6 and spv > 1e-6:
                cos = float(vel @ prev) / (sp * spv)
                if cos < np.cos(np.radians(5.0)) or abs(sp / spv - 1.0) > 0.05:
                    self._hist = []
        self._hist.append((t, np.asarray(pos, dtype=np.float64), vel,
                           None if up is None else np.asarray(up, dtype=np.float64)))
        if len(self._hist) > window:
            self._hist.pop(0)

    def predict(self, horizon=2.0, max_bounces=8):
        if not self._hist:
            return Path([], [], {"no_data"}, horizon)
        t_last, p_last, v_last, up_last = self._hist[-1]
        if len(self._hist) < 3:
            path = self.predict_from(p_last, v_last, up_last, horizon, max_bounces)
            path.confidence.add("low_confidence")
            return path
        # straight-line least squares over the window, evaluated at t_last
        ts = np.array([h[0] for h in self._hist]) - t_last
        ps = np.stack([h[1] for h in self._hist])
        A = np.stack([np.ones_like(ts), ts], axis=1)
        coef = np.linalg.lstsq(A, ps, rcond=None)[0]
        p0 = coef[0]
        speed = float(np.median([np.linalg.norm(h[2]) for h in self._hist]))
        vdir = v_last / max(np.linalg.norm(v_last), 1e-9)
        return self.predict_from(p0, vdir * speed, up_last, horizon, max_bounces)

    def reset(self):
        self._hist = []
