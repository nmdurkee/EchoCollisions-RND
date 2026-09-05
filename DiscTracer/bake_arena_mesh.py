"""
Bakes arena_mesh.npz - the complete arena collision model used by
arena_geometry.py. One script, end to end, so the bake is reproducible.

Needs the DemoViewer repo cloned WITH git-lfs (plain ZIP gives LFS
pointer stubs): git clone https://github.com/EchoTools/DemoViewer
Set DEMOVIEWER below to its "Demo Viewer/Assets" folder.

Layers baked (all validated against real logged bounces - see CLAUDE.md
"Trajectory-prediction line trainer" section for the full evidence
trail; final validation: median 0.48m over 18 real bounces, 14/18 < 2m,
the remaining outliers are consistent with player deflections logged as
bounces):
  1. Arena interior shell: arena_insides.fbx, both halves via Z-mirror.
     Coordinates are already game-frame (X width, Y up, Z length).
  2. Goal shields: Arena.fbx geometry #2 (baked world coords match
     positions.json's shield rect exactly), Z-mirrored.
  3. Goal-end structures: Arena.fbx geometries #6 and #8 (the frames
     above/around each goal - validated to fix above-goal bounces
     without blocking the goal mouth), Z-mirrored.
  4. Floating obstacle blocks: placements parsed from the Unity scene
     Assets/Scenes/Environments/Arena_V4.unity (PrefabInstance blocks
     for the three Block V4 prefab GUIDs, FULL parent-chain transform
     resolution - the scene root carries a 90-degree Y rotation, the
     Orange group mirrors via localScale z=-1, locker-room copies are
     filtered out by arena bounds). Baked as oriented boxes with
     half-extents 1.509 x 1.509 x 1.072 (Block_V4_LOD0.fbx bbox)
     INFLATED by DISC_RADIUS - the disc is ~0.3m wide and collides
     when its EDGE touches, so effective collision surfaces sit ~one
     radius out; without this, grazing block hits get missed and the
     live prediction visibly "recalculates" late.

The world->game axis mapping for scene-derived data is the same swap
as Full_Map.obj: game = (world_Z, world_Y, world_X). arena_insides.fbx
needs no swap (authored in game frame directly).

Run: python bake_arena_mesh.py   (overwrites arena_mesh.npz)
"""

import re

import numpy as np

from fbx_extract import extract_geometries

DEMOVIEWER = r"C:/Users/nmdur/AppData/Local/Temp/claude/C--Users-nmdur-Desktop-EchoVRClientStuff/3cc03376-7f00-4a92-8d43-2738c1866bdb/scratchpad/DemoViewer/Demo Viewer/Assets"
OUT = "arena_mesh.npz"
DISC_RADIUS = 0.15

BLOCK_GUIDS = {"63d91343182729a4e83ff81405612f81",
               "1064a21d3435c744da59f777cffba066",
               "dc0a3aad2eff88542805b7d0f3c3925b"}

all_verts, all_faces = [], []
_off = [0]

def add(verts, faces, mirror_z=False):
    for m in ([False, True] if mirror_z else [False]):
        v = np.asarray(verts, dtype=np.float64).copy()
        if m:
            v = v * np.array([1.0, 1.0, -1.0])
        all_verts.append(v)
        all_faces.append(np.asarray(faces, dtype=np.int64) + _off[0])
        _off[0] += len(v)

# --- 1) interior shell ---
for v, f in extract_geometries(DEMOVIEWER + "/Arena V4/Models/arena_insides.fbx"):
    add(v, f, mirror_z=True)

# --- 1b) glass side walls (2026-08-17): arena_insides has big HOLES on
# the flat left/right walls (the glass panels are a separate model -
# live feedback: "flat walls not showing bounces at all... where it
# usually is glass wall"; probe confirmed HOLEs at most side-wall
# spots). wall_grid.fbx is that wall geometry (X +-16, Y +-8, one half
# in Z like arena_insides) - both halves via mirror.
for v, f in extract_geometries(DEMOVIEWER + "/Arena V4/Models/wall_grid.fbx"):
    add(v, f, mirror_z=True)

# --- 2) shield only. Goal-end structures (Arena.fbx geometries 6 and 8)
# were tried 2026-08-17 and REVERTED: live feedback "almost felt worse,
# so many mid air predictions redirecting in midair" - their triangle
# soup spans open space the disc really flies through near the goals
# (frames/tunnels with open middles), so they create phantom bounces.
# The 18-bounce validation set was too sparse near the goals to catch
# this (only case #9 exercised that region). Do not re-add them as
# whole meshes; if the above-goal bounce region ever matters, model
# just its real solid faces narrowly.
arena_geoms = extract_geometries(DEMOVIEWER + "/Arena V4/Models/Arena.fbx")
v, f = arena_geoms[2]
add(v, f, mirror_z=True)

# --- 4) obstacle blocks from the Unity scene ---
scene_text = open(DEMOVIEWER + "/Scenes/Environments/Arena_V4.unity",
                  encoding="utf-8", errors="ignore").read()
chunks = re.split(r"(?m)^(--- !u!\d+ &\d+.*)$", scene_text)
docs = []
for i in range(1, len(chunks), 2):
    hm = re.match(r"--- !u!(\d+) &(\d+)", chunks[i])
    docs.append((hm.group(1), int(hm.group(2)), chunks[i + 1]))

def fvec(body, name, comps):
    m = re.search(rf"{name}:\s*{{\s*" + r",\s*".join(rf"{c}:\s*([-\d.eE]+)" for c in comps) + r"\s*}", body)
    return [float(m.group(k + 1)) for k in range(len(comps))] if m else None

transforms = {}
for utype, fid, body in docs:
    if utype == "4":
        fm = re.search(r"m_Father:\s*{fileID:\s*(\d+)", body)
        transforms[fid] = {"pos": fvec(body, "m_LocalPosition", "xyz") or [0, 0, 0],
                           "rot": fvec(body, "m_LocalRotation", "xyzw") or [0, 0, 0, 1],
                           "scl": fvec(body, "m_LocalScale", "xyz") or [1, 1, 1],
                           "father": int(fm.group(1)) if fm else 0}

def quat_mat(q):
    x, y, z, w = q
    return np.array([[1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
                     [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
                     [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]])

def world_of(tid):
    chain = []
    cur = tid
    while cur and cur in transforms:
        chain.append(transforms[cur])
        cur = chain[-1]["father"]
    M = np.eye(3); T = np.zeros(3)
    for t in reversed(chain):
        R = quat_mat(t["rot"]) @ np.diag(t["scl"])
        T = T + M @ np.array(t["pos"])
        M = M @ R
    return M, T

# Real rounded block mesh, radially inflated by the disc radius. A flat
# 12-tri box bounces edge hits with wrong outgoing directions (the real
# block's edges are heavily rounded - the logged real deflections don't
# match flat-face reflections); the actual mesh gives correct normals.
_bv, _bf = extract_geometries(DEMOVIEWER + "/Arena V4/Models/Block_V4_LOD0.fbx")[0]
_bl = np.linalg.norm(_bv, axis=1)
block_verts_local = _bv * ((_bl + DISC_RADIUS) / np.maximum(_bl, 1e-6))[:, None]
block_faces_local = _bf
kept = 0
for utype, fid, body in docs:
    if utype != "1001":
        continue
    if not (set(re.findall(r"guid: ([0-9a-f]{32})", body)) & BLOCK_GUIDS):
        continue
    pm = re.search(r"m_TransformParent:\s*{fileID:\s*(\d+)", body)
    parent = int(pm.group(1)) if pm else 0
    props = {}
    for m in re.finditer(r"propertyPath: (m_Local\w+\.[xyzw])\s*\n\s*value: ([-\d.eE]+)", body):
        props[m.group(1)] = float(m.group(2))
    lpos = np.array([props.get(f"m_LocalPosition.{a}", 0.0) for a in "xyz"])
    lrot = [props.get(f"m_LocalRotation.{a}", d) for a, d in zip("xyzw", (0.0, 0.0, 0.0, 1.0))]
    PM, PT = world_of(parent)
    wpos = PM @ lpos + PT
    WR = PM @ quat_mat(lrot)
    cw = (WR @ block_verts_local.T).T + wpos
    cg = np.stack([cw[:, 2], cw[:, 1], cw[:, 0]], axis=1)  # world->game swap
    center = cg.mean(axis=0)
    if abs(center[0]) > 17 or abs(center[2]) > 41:
        continue  # locker-room copies
    add(cg, block_faces_local)
    kept += 1

verts = np.vstack(all_verts)
faces = np.vstack(all_faces)
np.savez(OUT, verts=verts, faces=faces)
print(f"baked {OUT}: {len(verts)} verts, {len(faces)} tris ({kept} obstacle blocks, disc radius {DISC_RADIUS})")
