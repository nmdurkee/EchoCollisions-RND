"""
Echo VR goalie trainer - general-purpose arena collision model for
trajectory bounce prediction. (NOT bounce_engine.py - that's the
shot-planning module.)

FINAL MODEL (2026-08-17, after several failed iterations - see below):
  1. Arena interior shell: `Arena V4/Models/arena_insides.fbx` from the
     DemoViewer repo (github.com/EchoTools/DemoViewer), parsed via
     fbx_extract.py. The file stores ONE half of the arena (Z 0..40.5);
     both halves are baked into arena_mesh.npz by mirroring Z. Its
     coordinates match the game's convention DIRECTLY (X width +-16,
     Y height, Z length +-40.5) - no axis swap, no scaling. VALIDATED
     against 18 real logged bounces (detect_real_bounces.py data):
     median prediction error 0.73 m casting from 8 m out.
  2. Goal shields (both ends): Arena.fbx geometry #2, whose baked
     coordinates match positions.json's real shield rect exactly
     (X -2.08..2.08, Y -1.22..0.81, Z 28.76..29.11) - independent
     cross-validation that these meshes sit at true world positions.
  3. Analytic backboard funnel planes (flat panel + 4 bevels, both
     ends) - real coordinates confirmed live earlier in this project.
  4. Goal-opening exclusion: hits inside the goal diamond near either
     backboard are discarded so scoring shots pass through.

DELIBERATELY EXCLUDED (all tried, all made predictions WORSE):
  - Full_Map.obj meshes: visual-only, ~26% ray coverage (mostly holes).
  - Convex hull fallback: smooths over concave regions -> confident
    phantom "hits air" bounces up to 60m off. Worse than no prediction.
  - Blocks_2 (floating obstacles): measured 2-4 m displaced from real
    obstacle bounce positions -> phantom mid-air bounces.
  - Arena.fbx goal-end structures (geometry 6 etc.): tunnels/frames with
    open middles; as triangle soup they block paths the disc really
    flies through (made case #6 go 0.19m -> 7.61m).

KNOWN LIMITATION: bounces off the floating obstacle blocks are NOT
predicted (the line will pass through them). That is intentional - we
have no accurately-positioned obstacle geometry, and a wrong bounce is
worse for training than a missing one. If accurate obstacle transforms
are ever recovered (e.g. from the DemoViewer Unity scene YAML), add
them as another triangle layer.
"""

import os

import numpy as np

_MESH_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arena_mesh.npz")
_data = np.load(_MESH_PATH)
_verts = _data["verts"]
_faces = _data["faces"]
_v0 = _verts[_faces[:, 0]]
_edge1 = _verts[_faces[:, 1]] - _v0
_edge2 = _verts[_faces[:, 2]] - _v0

# Bounding-sphere prefilter (2026-08-17): the baked mesh is ~65k tris and
# full Möller-Trumbore over all of them costs ~20ms/prediction - over the
# 33ms poll budget. Precompute each triangle's bounding sphere; per ray,
# a cheap distance-to-ray-line test culls ~95%+ of triangles before the
# full intersection math runs. Brings a prediction back to a few ms.
_tri_center = (_v0 + _verts[_faces[:, 1]] + _verts[_faces[:, 2]]) / 3.0
_tri_radius = np.sqrt(np.maximum.reduce([
    np.einsum("ij,ij->i", _v0 - _tri_center, _v0 - _tri_center),
    np.einsum("ij,ij->i", _verts[_faces[:, 1]] - _tri_center, _verts[_faces[:, 1]] - _tri_center),
    np.einsum("ij,ij->i", _verts[_faces[:, 2]] - _tri_center, _verts[_faces[:, 2]] - _tri_center),
]))

_EPS = 1e-7

# Goal geometry (real, confirmed): opening diamond + backboard funnel.
_GOAL_CENTER_XY = (-0.018, 0.031)
_GOAL_HALF_W = 1.39
_GOAL_HALF_H = 1.40
_S45 = 0.7071

_BACKBOARD_SURFACES_BLUE = [
    ((0.0, 0.0, 1.0), (0.0, 0.0, -40.192), (-5.69, 5.69), (-5.5, 6.2)),   # flat panel
    ((0.0, _S45, _S45), (0.0, -2.0, -39.0), (-3.0, 3.0), (-3.5, -0.5)),   # bottom bevel
    ((0.0, -_S45, _S45), (0.0, 2.0, -39.0), (-3.0, 3.0), (0.5, 3.5)),     # top bevel
    ((-_S45, 0.0, _S45), (-3.0, 0.0, -39.0), (-5.0, -2.0), (-2.0, 2.0)),  # left bevel
    ((_S45, 0.0, _S45), (3.0, 0.0, -39.0), (2.0, 5.0), (-2.0, 2.0)),      # right bevel
]
_BACKBOARD_SURFACES = list(_BACKBOARD_SURFACES_BLUE)
for _n, _p, _bx, _by in _BACKBOARD_SURFACES_BLUE:
    _delta = _p[2] - (-40.192)
    _BACKBOARD_SURFACES.append(((_n[0], _n[1], -_n[2]), (_p[0], _p[1], 39.7 - _delta), _bx, _by))


def _backboard_hit(pos, direction, max_dist):
    best = None
    for normal, point, bounds_x, bounds_y in _BACKBOARD_SURFACES:
        dir_dot_n = direction[0]*normal[0] + direction[1]*normal[1] + direction[2]*normal[2]
        if abs(dir_dot_n) < 1e-6:
            continue
        to_plane = ((point[0]-pos[0])*normal[0] + (point[1]-pos[1])*normal[1] + (point[2]-pos[2])*normal[2])
        t = to_plane / dir_dot_n
        if t <= 0.05 or t > max_dist:
            continue
        hit = [pos[i] + direction[i]*t for i in range(3)]
        if not (bounds_x[0] <= hit[0] <= bounds_x[1]):
            continue
        if not (bounds_y[0] <= hit[1] <= bounds_y[1]):
            continue
        if abs(normal[0]) < 1e-6 and abs(normal[1]) < 1e-6:  # flat panel: respect goal opening
            dx = abs(hit[0] - _GOAL_CENTER_XY[0]) / _GOAL_HALF_W
            dy = abs(hit[1] - _GOAL_CENTER_XY[1]) / _GOAL_HALF_H
            if dx + dy <= 1.0:
                continue
        if best is None or t < best[0]:
            best = (t, hit, normal)
    return best


def _in_goal_opening(hit_point):
    for z_plane in (-40.192, 39.7):
        if abs(hit_point[2] - z_plane) > 2.0:
            continue
        dx = abs(hit_point[0] - _GOAL_CENTER_XY[0]) / _GOAL_HALF_W
        dy = abs(hit_point[1] - _GOAL_CENTER_XY[1]) / _GOAL_HALF_H
        if dx + dy <= 1.0:
            return True
    return False


def _raycast_triangles(pos, direction, max_dist):
    """Vectorized Möller-Trumbore against the baked mesh, with a
    bounding-sphere prefilter (see comment at _tri_center above)."""
    pos_np = np.asarray(pos, dtype=np.float64)
    dir_np = np.asarray(direction, dtype=np.float64)

    # prefilter: triangles whose bounding sphere the ray line passes near
    rel = _tri_center - pos_np
    t_along = rel @ dir_np
    perp_sq = np.einsum("ij,ij->i", rel, rel) - t_along * t_along
    cand = (perp_sq <= _tri_radius * _tri_radius) & \
           (t_along >= -_tri_radius) & (t_along <= max_dist + _tri_radius)
    if not np.any(cand):
        return None
    ci = np.nonzero(cand)[0]
    v0 = _v0[ci]; edge1 = _edge1[ci]; edge2 = _edge2[ci]

    h = np.cross(dir_np, edge2)
    a = np.einsum("ij,ij->i", edge1, h)
    valid = np.abs(a) > _EPS
    f = np.zeros_like(a)
    f[valid] = 1.0 / a[valid]
    s = pos_np - v0
    u = f * np.einsum("ij,ij->i", s, h)
    valid &= (u >= -_EPS) & (u <= 1.0 + _EPS)
    q = np.cross(s, edge1)
    v = f * np.einsum("j,ij->i", dir_np, q)
    valid &= (v >= -_EPS) & (u + v <= 1.0 + _EPS)
    t = f * np.einsum("ij,ij->i", edge2, q)
    valid &= (t > 0.05) & (t <= max_dist)
    if not np.any(valid):
        return None
    idx = np.nonzero(valid)[0]
    best = idx[np.argmin(t[idx])]
    hit_point = pos_np + dir_np * t[best]
    normal = np.cross(edge1[best], edge2[best])
    n_len = np.linalg.norm(normal)
    if n_len < _EPS:
        return None
    return (float(t[best]), hit_point.tolist(), tuple((normal / n_len).tolist()))


def raycast_arena_mesh(pos, direction, max_dist):
    """Nearest hit among mesh (shell+shields) and analytic funnel planes,
    with goal-opening exclusion. Returns (t, hit_point, normal) or None."""
    mesh_hit = _raycast_triangles(pos, direction, max_dist)
    if mesh_hit is not None and _in_goal_opening(mesh_hit[1]):
        mesh_hit = None
    board_hit = _backboard_hit(pos, direction, max_dist)
    if mesh_hit is None:
        return board_hit
    if board_hit is None:
        return mesh_hit
    return mesh_hit if mesh_hit[0] < board_hit[0] else board_hit


def _normalize(v):
    length = (v[0]**2 + v[1]**2 + v[2]**2) ** 0.5
    if length < 1e-6:
        return (0.0, 0.0, 0.0)
    return (v[0]/length, v[1]/length, v[2]/length)


def _reflect(direction, normal):
    d = direction[0]*normal[0] + direction[1]*normal[1] + direction[2]*normal[2]
    return (direction[0] - 2*d*normal[0],
            direction[1] - 2*d*normal[1],
            direction[2] - 2*d*normal[2])


def predict_bounce_trajectory(pos, vel, predict_seconds, max_bounces=2):
    """Straight-line prediction with up to max_bounces reflections.
    Returns [pos, bounce1, ..., predicted_end] world-space waypoints."""
    speed = (vel[0]**2 + vel[1]**2 + vel[2]**2) ** 0.5
    if speed < 0.01:
        return [pos, pos]

    current_pos = list(pos)
    current_dir = _normalize(vel)
    remaining_dist = speed * predict_seconds
    waypoints = [list(pos)]

    for _ in range(max_bounces):
        result = raycast_arena_mesh(current_pos, current_dir, remaining_dist)
        if result is None:
            break
        t, hit_point, normal = result
        waypoints.append(hit_point)
        current_dir = _reflect(current_dir, normal)
        remaining_dist -= t
        current_pos = hit_point

    end = [current_pos[i] + current_dir[i] * remaining_dist for i in range(3)]
    waypoints.append(end)
    return waypoints
