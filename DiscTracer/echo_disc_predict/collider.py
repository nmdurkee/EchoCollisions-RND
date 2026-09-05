"""The disc's real collider, extracted from the game's CPhysicsResource.

40 particles in two rings of 20 - radius 0.309 m at local z = -0.003 and
0.299 m at z = +0.036, i.e. a flat frisbee 0.618 m across and 0.039 m thick.
Local +z is the disc axis, so it maps onto the `/session` API's `disc.up`.

The engine collides dynamic bodies particle-vs-triangle, so which point of
the disc touches first depends on the disc's plane - that is the whole reason
to carry the ring around instead of a single ray.  Local coordinates are used
verbatim (the collider is not centred on its body origin; that ~1.6 cm offset
is the game's, not an artefact, and `disc.position` is the body origin).
"""

import os

import numpy as np

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# CR15BounceCR values for mpl_arena_a, read from the package files.
# flags 0x30 = BounceLikeABall | LerpPerpendicularRestitution.
BOUNCE_DEFAULT = {
    "e_par": 0.5,            # parallelrestitution   - normal component
    "e_perp_begin": 0.5,     # head-on
    "e_perp_end": 1.0,       # grazing
}


def load_disc_points(path=None):
    path = path or os.path.join(_DATA, "disc_collider.npz")
    pts = np.asarray(np.load(path, allow_pickle=True)["vertices"], dtype=np.float64)
    return pts


def disc_radius(points):
    return float(np.linalg.norm(points[:, :2], axis=1).max())


def orient(points, up):
    """Rotate disc-local particles into world space for a given `up`.

    Spin about `up` is irrelevant by symmetry, so any basis perpendicular to
    it will do.  Returns (40,3) offsets to add to the disc centre.
    """
    u = np.asarray(up, dtype=np.float64)
    n = np.linalg.norm(u)
    if n < 1e-9:
        return None
    u = u / n
    # pick the world axis least aligned with u to seed the basis
    seed = np.zeros(3)
    seed[int(np.argmin(np.abs(u)))] = 1.0
    e1 = np.cross(u, seed)
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(u, e1)
    basis = np.stack([e1, e2, u], axis=0)      # rows map local x,y,z
    return points @ basis


def bounce(velocity, normal, consts=None):
    """CR15BounceCS::OnCollision, BounceLikeABall + Lerp branch.

        t      = clamp(1 - |v_hat . n_hat|, 0, 1)     0 head-on .. 1 grazing
        e_perp = e_perp_begin + (e_perp_end - e_perp_begin) * t
        v'     = e_perp * (v - n(v.n)) - e_par * n(v.n)
    """
    c = consts or BOUNCE_DEFAULT
    v = np.asarray(velocity, dtype=np.float64)
    n = np.asarray(normal, dtype=np.float64)
    n = n / np.linalg.norm(n)
    speed = np.linalg.norm(v)
    if speed < 1e-9:
        return v
    vn = float(v @ n)
    t = 1.0 - abs(vn / speed)
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    e_perp = c["e_perp_begin"] + (c["e_perp_end"] - c["e_perp_begin"]) * t
    v_t = v - n * vn
    return e_perp * v_t - c["e_par"] * (n * vn)
