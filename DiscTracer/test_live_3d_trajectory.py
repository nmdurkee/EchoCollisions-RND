"""
Echo VR goalie trainer - REAL 3D world-space trajectory line, live in the
headset. Combines every proven piece from this project:
  - Personal disc position + velocity, straight from the local HTTP API
    (`disc.position`, `disc.velocity` - confirmed live 2026-08-17, no Frida
    memory reads needed for this at all).
  - Head pose (`player.head.position/forward/left/up`) from the same API -
    confirmed live to be a clean orthonormal basis (forward x left == up).
  - The confirmed, validated D3D12 splice/draw pipeline
    (test_live_splice_echovr.py / draw_dll_v5.dll's
    DrawThickLineStereoIndependent) - 2688x1600
    DXGI_FORMAT_R8G8B8A8_UNORM_SRGB target.

Architecture: Python polls the HTTP API on its own fast timer (~30Hz,
matching the proven poll rate from test_geo_field_probe.py), computes the
predicted trajectory endpoint (`pos + velocity * 1.5`), and projects BOTH
endpoints TWICE - once from a left-eye camera position and once from a
right-eye camera position (head position offset by +-IPD/2 along the
head's own "left" vector, both using the same head orientation) - into
per-eye local NDC via a camera transform built from live head pose + an
assumed FOV. This gives REAL depth-varying stereo disparity (unlike the
earlier DrawFrameStereo/DrawThickLineStereo approach, which used one
shared NDC line + a single hand-tuned convergence constant - that only
looked right at the one depth it was tuned against; for a moving 3D point
at varying depth, disparity must vary with depth too). Pushes the 8
resulting NDC floats into the already-running Frida splice hook via
`rpc.exports.updateLineStereo(...)`. The JS-side render splice hook itself
does NOT read any new game memory - it just draws whatever line Python
last told it to, every real frame.

FOV IS AN ASSUMPTION, NOT YET CALIBRATED. Start with CALIBRATE=True: this
draws a short fixed-length vertical marker AT the disc's real position
(not the real predicted trajectory) so you can visually compare it against
the real rendered disc and tell me which way it's off (too high/low/left/
right, too big/small) - same empirical-tuning workflow already used
successfully for the eye-split boundary. Once the marker visibly tracks
the real disc correctly, flip CALIBRATE=False to switch to the real
predicted-trajectory line.

Run: python test_live_3d_trajectory.py
Needs an ACTIVE match/practice session with the disc in play (the API
reports disc.position=[0,0,0] in the pre-match lobby).
"""

import frida
import sys
import os
import time
import json
import math
import threading
import urllib.request
from datetime import datetime

import arena_geometry

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log.txt")
DLL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cpp_reference", "draw_dll_v5.dll")

# 2026-08-17: THE REAL FIX. Found the actual per-eye FOV live via static
# analysis (echovr.exe_CLINET.c, FUN_14072d110 - confirmed correct because
# its hardcoded 1344x1600 fallback matches the real headset buffer
# exactly) + a live memory read (read_real_eye_fov.py). The real per-eye
# FOV is genuinely ASYMMETRIC (e.g. left eye: leftTan=0.965 vs
# rightTan=0.715 - more FOV toward the outer/temple side than the nose;
# also more FOV down than up) - this is exactly why NDC (0,0) was never
# "straight ahead" for either eye, which is what broke every earlier
# attempt (the flat eyeOffsetNDC hack was accidentally compensating for
# this; the independent-eye attempt didn't compensate at all). Using the
# real asymmetric FOV in project_point() (see its docstring) makes both
# earlier hacks (CONST_EYE_OFFSET_NDC, eyeOffsetNDC) unnecessary.
LEFT_FOV_RVA = 0x20C7860
RIGHT_FOV_RVA = 0x20C7870

PRESENT_DXGI_RVA = 0x19960
RUN_SECONDS = 0   # 0 = run indefinitely until Ctrl+C (or the game exits)

TARGET_WIDTH = 2688
TARGET_HEIGHT = 1600
TARGET_FORMAT = 29  # DXGI_FORMAT_R8G8B8A8_UNORM_SRGB, confirmed headset-visible

THICKNESS_PIXELS = 8.0
IPD_M = 0.064               # typical human interpupillary distance, meters -
                             # real per-eye disparity computed from two
                             # separate eye cameras (real FOV handles the
                             # rest - no flat convergence hack needed).

API_URL = "http://127.0.0.1:6721/session"
POLL_INTERVAL_S = 0.033    # ~30Hz, matches the API's own proven poll rate

# --- Calibration knobs ---
CALIBRATE = False          # True: draw a fixed marker at the disc's real
                            # position instead of the real predicted line,
                            # for visual alignment checking.
CALIBRATE_MARKER_WORLD_UP_M = 0.4   # marker extends this far along world +Y
PREDICT_SECONDS = 2.5       # 2026-08-17: "mid-air" bounces traced to the Blocks_2 obstacle
                             # mesh (likely decorative, not real collision geometry) - removed
                             # from arena_mesh.npz. 2.0s then reported as "not showing bounces
                             # often" (many throws don't reach any wall in just 2s of travel) -
                             # nudged back up slightly now that the false-positive source is gone.


MAX_BOUNCES = 2  # "predict the next 2 bounces"


# 2026-09-05: prediction now runs against the game's OWN collision mesh and
# bounce law (echo_disc_predict, from the EchoCollisions-RND extraction) rather
# than the replay-viewer FBX assets in arena_geometry.py. Validated on the 19
# real logged wall bounces in log.txt (validate_bounces.py): correct surface
# 14/19 vs the old model's 12/19, and - the part the old model could not do at
# all - the outgoing velocity is exact in 13/19. The old model reflected without
# energy loss; real bounces retain a median 0.51 of speed, so every post-bounce
# segment used to be about twice as long as it should have been.
# Set USE_REAL_COLLISION = False to fall back to arena_geometry.py.
USE_REAL_COLLISION = True

_predictor = None
if USE_REAL_COLLISION:
    try:
        from echo_disc_predict import DiscPredictor
        _predictor = DiscPredictor.load()
        print(f"[predict] echo_disc_predict: {len(_predictor.tris)} tris, "
              f"hulls={_predictor.hulls}, disc r={_predictor.radius:.3f}m")
    except Exception as exc:
        print(f"[predict] echo_disc_predict unavailable ({exc}); "
              f"falling back to arena_geometry")
        _predictor = None


def predict_bounce_trajectory(pos, vel, predict_seconds, max_bounces=MAX_BOUNCES, up=None):
    """Predicted disc-centre waypoints: [start, bounce1, ..., end].

    `up` is the disc's plane normal from the API; with it the real 40-particle
    ring collider is swept, so the disc clips corners the way it really does.
    Without it the sweep degrades to a centre ray.
    """
    if _predictor is not None:
        path = _predictor.predict_from(pos, vel, up, horizon=predict_seconds,
                                       max_bounces=max_bounces)
        return path.waypoints
    return arena_geometry.predict_bounce_trajectory(pos, vel, predict_seconds, max_bounces)


def project_point(world_pos, head_pos, fwd, left, up, fov):
    """
    World-space point -> (ndc_x, ndc_y, cam_forward_depth). Returns None
    if behind camera. Uses the REAL asymmetric per-eye FOV (fov = dict
    with upTan/downTan/leftTan/rightTan, read live from the game's own
    memory - see read_real_eye_fov.py) instead of an assumed symmetric
    FOV. This replaces the earlier CONST_EYE_OFFSET_NDC hack entirely -
    the asymmetry IS the reason NDC (0,0) wasn't "straight ahead" for
    either eye, which is what actually broke stereo fusion before.
    """
    dx = world_pos[0] - head_pos[0]
    dy = world_pos[1] - head_pos[1]
    dz = world_pos[2] - head_pos[2]

    cam_right = -(dx * left[0] + dy * left[1] + dz * left[2])
    cam_up = dx * up[0] + dy * up[1] + dz * up[2]
    cam_fwd = dx * fwd[0] + dy * fwd[1] + dz * fwd[2]

    if cam_fwd < 0.05:
        return None

    tan_x = cam_right / cam_fwd
    tan_y = cam_up / cam_fwd

    left_tan, right_tan = fov['leftTan'], fov['rightTan']
    up_tan, down_tan = fov['upTan'], fov['downTan']

    ndc_x = (2 * tan_x + (left_tan - right_tan)) / (right_tan + left_tan)
    ndc_y = (2 * tan_y + (down_tan - up_tan)) / (up_tan + down_tan)
    return (ndc_x, ndc_y, cam_fwd)


JS_SCRIPT = r"""
var dllPath = DLL_PATH_PLACEHOLDER;
var PRESENT_RVA = PRESENT_RVA_PLACEHOLDER;
var TARGET_WIDTH = TARGET_WIDTH_PLACEHOLDER;
var TARGET_HEIGHT = TARGET_HEIGHT_PLACEHOLDER;
var TARGET_FORMAT = TARGET_FORMAT_PLACEHOLDER;
var THICKNESS_PIXELS = THICKNESS_PIXELS_PLACEHOLDER;
var LEFT_FOV_RVA = LEFT_FOV_RVA_PLACEHOLDER;
var RIGHT_FOV_RVA = RIGHT_FOV_RVA_PLACEHOLDER;

var echoBase = Process.getModuleByName("echovr.exe").base;
function readFovPort(p) {
    return { upTan: p.add(0).readFloat(), downTan: p.add(4).readFloat(), leftTan: p.add(8).readFloat(), rightTan: p.add(12).readFloat() };
}
rpc.exports.getEyeFov = function() {
    return { left: readFovPort(echoBase.add(LEFT_FOV_RVA)), right: readFovPort(echoBase.add(RIGHT_FOV_RVA)) };
};

var dxgi = Process.getModuleByName("dxgi.dll");
var presentAddr = dxgi.base.add(PRESENT_RVA);

var IID_ID3D12Device = [0xf1,0x19,0x98,0x18, 0xb6,0x1d, 0x57,0x4b, 0xbe,0x54,0x18,0x21,0x33,0x9b,0x85,0xf7];
var IID_ID3D12CommandQueue = [0xa6,0x70,0xc8,0x0e, 0x7e,0x5d, 0x22,0x4c, 0x8c,0xfc,0x5b,0xaa,0xe0,0x76,0x16,0xed];

function vt(ptr, index) { return ptr.readPointer().add(index * 8).readPointer(); }

var attempted = false;
var setupError = null;
var setupDone = false;
var targetResource = null;
var spliceAttempts = 0;
var spliceFailures = 0;
var stopped = false;
var device = null;
var getDeviceRemovedReason = null;
var drawFrame = null;
var descCache = {};

// Up to 3 segments (initial + 2 possible bounces), each in LOCAL per-eye
// NDC + the PROVEN eyeOffsetNDC convergence hack (DrawThickLineStereo) -
// updated from Python via rpc.exports.updateSeg1/2/3. seg[i].active=false
// means "don't draw this segment" (no data yet, or fewer bounces this
// frame than last frame - must be explicitly cleared, not just left
// stale).
// Each segment now carries INDEPENDENT left/right-eye NDC endpoints (real
// per-eye camera projection + the constant per-eye offset, computed in
// Python - see CONST_EYE_OFFSET_NDC comment above), not a single shared
// NDC + convergence hack.
function makeSeg(r, g, b) { return { active: false, lx0:0,ly0:0,lx1:0,ly1:0, rx0:0,ry0:0,rx1:0,ry1:0, r:r, g:g, b:b }; }
var seg1 = makeSeg(0.0, 1.0, 0.0);
var seg2 = makeSeg(0.0, 1.0, 1.0);
var seg3 = makeSeg(1.0, 0.0, 1.0);

function updateSeg(seg, lx0,ly0,lx1,ly1, rx0,ry0,rx1,ry1) {
    seg.lx0=lx0; seg.ly0=ly0; seg.lx1=lx1; seg.ly1=ly1;
    seg.rx0=rx0; seg.ry0=ry0; seg.rx1=rx1; seg.ry1=ry1;
    seg.active = true;
}
rpc.exports.updateSeg1 = function(lx0,ly0,lx1,ly1, rx0,ry0,rx1,ry1) { updateSeg(seg1, lx0,ly0,lx1,ly1, rx0,ry0,rx1,ry1); };
rpc.exports.updateSeg2 = function(lx0,ly0,lx1,ly1, rx0,ry0,rx1,ry1) { updateSeg(seg2, lx0,ly0,lx1,ly1, rx0,ry0,rx1,ry1); };
rpc.exports.updateSeg3 = function(lx0,ly0,lx1,ly1, rx0,ry0,rx1,ry1) { updateSeg(seg3, lx0,ly0,lx1,ly1, rx0,ry0,rx1,ry1); };
rpc.exports.clearSeg1 = function() { seg1.active = false; };
rpc.exports.clearSeg2 = function() { seg2.active = false; };
rpc.exports.clearSeg3 = function() { seg3.active = false; };

Interceptor.attach(presentAddr, {
    onEnter: function(args) {
        if (attempted) return;
        attempted = true;
        var swapChain = args[0];
        setTimeout(function() {
            try {
                send({type: "step", msg: "swapChain=" + swapChain});
                var getDevice = new NativeFunction(vt(swapChain, 7), 'int32', ['pointer', 'pointer', 'pointer']);
                var riid = Memory.alloc(16); riid.writeByteArray(IID_ID3D12Device);
                var ppDevice = Memory.alloc(8);
                var hr = getDevice(swapChain, riid, ppDevice);
                if ((hr>>>0) !== 0) { setupError = "GetDevice failed"; send({type:"error", msg: setupError}); return; }
                device = ppDevice.readPointer();
                send({type: "step", msg: "real device=" + device});
                getDeviceRemovedReason = new NativeFunction(vt(device, 37), 'int32', ['pointer']);

                var kernel32 = Process.getModuleByName("kernel32.dll");
                var loadLibraryW = new NativeFunction(kernel32.getExportByName("LoadLibraryW"), 'pointer', ['pointer']);
                var getProcAddress = new NativeFunction(kernel32.getExportByName("GetProcAddress"), 'pointer', ['pointer', 'pointer']);
                var pathBuf = Memory.allocUtf16String(dllPath);
                var hModule = loadLibraryW(pathBuf);
                if (hModule.isNull()) { setupError = "LoadLibraryW failed"; send({type:"error", msg: setupError}); return; }
                send({type: "step", msg: "draw_dll_v4.dll loaded, hModule=" + hModule});

                var nameInit = Memory.allocUtf8String("InitDraw");
                var nameDraw = Memory.allocUtf8String("DrawThickLineStereoIndependent");
                var addrInit = getProcAddress(hModule, nameInit);
                var addrDraw = getProcAddress(hModule, nameDraw);
                if (addrInit.isNull() || addrDraw.isNull()) { setupError = "GetProcAddress failed"; send({type:"error", msg: setupError}); return; }

                var initDraw = new NativeFunction(addrInit, 'int32', ['pointer']);
                drawFrame = new NativeFunction(addrDraw, 'int32', ['pointer', 'float', 'float', 'float', 'float', 'float', 'float', 'float', 'float', 'float', 'float', 'float', 'float', 'float', 'float']);

                var initResult = initDraw(device);
                send({type: "step", msg: "InitDraw returned " + initResult});
                if (initResult !== 0) { setupError = "InitDraw failed"; send({type:"error", msg: setupError}); return; }

                var createCommandQueue = new NativeFunction(vt(device, 8), 'int32', ['pointer', 'pointer', 'pointer', 'pointer']);
                var qdesc = Memory.alloc(16);
                var riid2 = Memory.alloc(16); riid2.writeByteArray(IID_ID3D12CommandQueue);
                var ppQueue = Memory.alloc(8);
                var hr2 = createCommandQueue(device, qdesc, riid2, ppQueue);
                if ((hr2>>>0) !== 0) { setupError = "CreateCommandQueue failed"; send({type:"error", msg: setupError}); return; }
                var throwawayQueue = ppQueue.readPointer();
                var execAddr = vt(throwawayQueue, 10);
                var release = new NativeFunction(vt(throwawayQueue, 2), 'uint32', ['pointer']);
                release(throwawayQueue);

                var resourceBarrierAddr = null;
                Interceptor.attach(execAddr, {
                    onEnter: function(args2) {
                        if (resourceBarrierAddr) return;
                        var numLists = args2[1].toInt32();
                        if (numLists < 1) return;
                        var realCmdList = args2[2].readPointer();
                        resourceBarrierAddr = vt(realCmdList, 26);
                        send({type: "step", msg: "ResourceBarrier real address=" + resourceBarrierAddr});

                        Interceptor.attach(resourceBarrierAddr, {
                            onEnter: function(args3) {
                                if (stopped) return;
                                var numBarriers = args3[1].toInt32();
                                var pBarriers = args3[2];
                                if (numBarriers < 1) return;
                                var type0 = pBarriers.add(0).readU32();
                                var stateBefore0 = pBarriers.add(20).readU32();
                                var pResource0 = pBarriers.add(8).readPointer();
                                if (type0 !== 0 || stateBefore0 !== 4) return;

                                var key = pResource0.toString();

                                if (!targetResource) {
                                    if (!(key in descCache)) {
                                        try {
                                            var getDescR = new NativeFunction(vt(pResource0, 10), 'pointer', ['pointer', 'pointer']);
                                            var descBufR = Memory.alloc(64);
                                            getDescR(pResource0, descBufR);
                                            descCache[key] = {
                                                width: parseInt(descBufR.add(16).readU64().toString()),
                                                height: descBufR.add(24).readU32(),
                                                format: descBufR.add(32).readU32()
                                            };
                                        } catch (e) { descCache[key] = {error: true}; }
                                    }
                                    var d = descCache[key];
                                    if (d && !d.error && d.width === TARGET_WIDTH && d.height === TARGET_HEIGHT && d.format === TARGET_FORMAT) {
                                        targetResource = key;
                                        send({type: "result", msg: "LOCKED ON to " + key});
                                    }
                                    return;
                                }

                                if (key !== targetResource) return;
                                if (!seg1.active) return;

                                spliceAttempts++;
                                try {
                                    drawFrame(args3[0], TARGET_WIDTH * 1.0, TARGET_HEIGHT * 1.0, seg1.lx0, seg1.ly0, seg1.lx1, seg1.ly1, seg1.rx0, seg1.ry0, seg1.rx1, seg1.ry1, THICKNESS_PIXELS, seg1.r, seg1.g, seg1.b);
                                    if (seg2.active) {
                                        drawFrame(args3[0], TARGET_WIDTH * 1.0, TARGET_HEIGHT * 1.0, seg2.lx0, seg2.ly0, seg2.lx1, seg2.ly1, seg2.rx0, seg2.ry0, seg2.rx1, seg2.ry1, THICKNESS_PIXELS, seg2.r, seg2.g, seg2.b);
                                    }
                                    if (seg3.active) {
                                        drawFrame(args3[0], TARGET_WIDTH * 1.0, TARGET_HEIGHT * 1.0, seg3.lx0, seg3.ly0, seg3.lx1, seg3.ly1, seg3.rx0, seg3.ry0, seg3.rx1, seg3.ry1, THICKNESS_PIXELS, seg3.r, seg3.g, seg3.b);
                                    }
                                    var removedReason = getDeviceRemovedReason(device);
                                    if ((removedReason >>> 0) !== 0) {
                                        spliceFailures++;
                                        stopped = true;
                                        send({type: "error", msg: "DEVICE REMOVED after splice #" + spliceAttempts + " reason=0x" + (removedReason>>>0).toString(16)});
                                    }
                                } catch (e) {
                                    spliceFailures++;
                                    stopped = true;
                                    send({type: "error", msg: "splice #" + spliceAttempts + " THREW: " + e.message});
                                }
                            }
                        });
                        setupDone = true;
                        send({type: "result", msg: "Live 3D trajectory hooks installed - watching for target resource..."});
                    }
                });
            } catch (e) {
                setupError = e.message;
                send({type: "error", msg: "setup failed: " + e.message});
            }
        }, 0);
    }
});

send({type: "ready", msg: "Present hook attached"});

rpc.exports.getSummary = function() {
    return { setupDone: setupDone, setupError: setupError, targetResource: targetResource, spliceAttempts: spliceAttempts, spliceFailures: spliceFailures, stopped: stopped };
};
""".replace("DLL_PATH_PLACEHOLDER", repr(DLL_PATH.replace("\\", "\\\\"))) \
   .replace("PRESENT_RVA_PLACEHOLDER", hex(PRESENT_DXGI_RVA)) \
   .replace("TARGET_WIDTH_PLACEHOLDER", str(TARGET_WIDTH)) \
   .replace("TARGET_HEIGHT_PLACEHOLDER", str(TARGET_HEIGHT)) \
   .replace("TARGET_FORMAT_PLACEHOLDER", str(TARGET_FORMAT)) \
   .replace("THICKNESS_PIXELS_PLACEHOLDER", str(THICKNESS_PIXELS)) \
   .replace("LEFT_FOV_RVA_PLACEHOLDER", hex(LEFT_FOV_RVA)) \
   .replace("RIGHT_FOV_RVA_PLACEHOLDER", hex(RIGHT_FOV_RVA))


class Tee:
    def __init__(self, filename):
        self.terminal = sys.stdout
        self.log = open(filename, "a", encoding="utf-8", buffering=1)

    def write(self, message):
        self.terminal.write(message)
        self.log.write(message)

    def flush(self):
        self.terminal.flush()
        self.log.flush()

    def close(self):
        self.log.close()


def on_message(message, data):
    if message["type"] == "send":
        p = message["payload"]
        if isinstance(p, dict):
            t = p.get("type", "")
            marker = ">>> " if t in ("result", "error") else "  "
            print(f"{marker}[{t}] {p.get('msg','')}")
    elif message["type"] == "error":
        print("FRIDA ERROR: " + message.get("stack", ""))


_stop_flag = threading.Event()


def poll_loop(script):
    eye_fov = script.exports_sync.get_eye_fov()
    left_fov = eye_fov["left"]
    right_fov = eye_fov["right"]
    print(f"[*] Real per-eye FOV (from live game memory): LEFT={left_fov} RIGHT={right_fov}")

    last_status_print = 0
    ok_count = 0
    skip_reasons = {}
    last_sample_printed = 0

    def skip(reason):
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

    # Velocity smoothing (2026-08-17): raw per-poll velocity direction is
    # slightly noisy, and that noise swings the predicted bounce point
    # around ("constantly moving lightly", worse on the 2nd bounce where
    # the error compounds through the reflection). EMA-smooth the velocity
    # during steady flight, but RESET instantly when direction/speed
    # changes for real (throw, bounce, catch) so the line snaps to the
    # new trajectory with zero lag instead of swinging through a blend.
    vel_ema = None
    prev_waypoints = None

    while not _stop_flag.is_set():
        t0 = time.monotonic()
        try:
            req = urllib.request.urlopen(API_URL, timeout=1)
            data = json.loads(req.read().decode())

            disc = data.get("disc", {})
            disc_pos = disc.get("position")
            disc_vel = disc.get("velocity", [0.0, 0.0, 0.0])
            disc_up = disc.get("up")   # disc plane normal -> orients the ring collider

            if disc_vel:
                new_v = disc_vel
                if vel_ema is None:
                    vel_ema = list(new_v)
                else:
                    dot_num = sum(a * b for a, b in zip(vel_ema, new_v))
                    mag_e = math.sqrt(sum(a * a for a in vel_ema))
                    mag_n = math.sqrt(sum(a * a for a in new_v))
                    if mag_e < 0.3 or mag_n < 0.3 or dot_num / (mag_e * mag_n) < 0.95 or \
                       not (0.7 < (mag_n / mag_e if mag_e > 0 else 9) < 1.4):
                        vel_ema = list(new_v)  # real change - snap, don't blend
                    else:
                        alpha = 0.35
                        vel_ema = [(1 - alpha) * e + alpha * n for e, n in zip(vel_ema, new_v)]
                disc_vel = vel_ema

            client_name = data.get("client_name")
            player = None
            for team in data.get("teams", []):
                for pl in team.get("players", []):
                    if client_name and pl.get("name") == client_name:
                        player = pl
                        break
                if player:
                    break

            if not disc_pos:
                skip("no_disc_pos")
            elif not player:
                skip("no_player_match")
            else:
                head = player.get("head", {})
                head_pos = head.get("position")
                fwd = head.get("forward")
                left = head.get("left")
                up = head.get("up")

                if not (head_pos and fwd and left and up):
                    skip("no_head_data")
                else:
                    if CALIBRATE:
                        segments_world = [(disc_pos, [disc_pos[0], disc_pos[1] + CALIBRATE_MARKER_WORLD_UP_M, disc_pos[2]])]
                    else:
                        waypoints = predict_bounce_trajectory(
                            disc_pos, disc_vel, PREDICT_SECONDS, up=disc_up)
                        # Waypoint hysteresis (2026-08-17): damp the residual
                        # "recalculating mid-air" wobble. If the new prediction
                        # has the same shape (same bounce count) and every
                        # waypoint is within 2m of the previous smoothed one,
                        # blend toward it instead of jumping; any structural
                        # change (bounce appears/disappears, or a waypoint
                        # jumps >2m = the hit surface changed) snaps instantly.
                        if prev_waypoints is not None and len(prev_waypoints) == len(waypoints) and \
                           all(sum((a - b) ** 2 for a, b in zip(pw, nw)) < 4.0
                               for pw, nw in zip(prev_waypoints, waypoints)):
                            beta = 0.3
                            waypoints = [[(1 - beta) * p + beta * n for p, n in zip(pw, nw)]
                                         for pw, nw in zip(prev_waypoints, waypoints)]
                        prev_waypoints = waypoints
                        segments_world = [(waypoints[i], waypoints[i + 1]) for i in range(len(waypoints) - 1)]

                    # Real per-eye camera position (depth-varying
                    # disparity) + real asymmetric FOV (see project_point)
                    # - no hand-tuned constants needed anymore.
                    half_ipd = IPD_M / 2.0
                    eye_left = [head_pos[0] + half_ipd * left[0],
                                head_pos[1] + half_ipd * left[1],
                                head_pos[2] + half_ipd * left[2]]
                    eye_right = [head_pos[0] - half_ipd * left[0],
                                 head_pos[1] - half_ipd * left[1],
                                 head_pos[2] - half_ipd * left[2]]

                    # Per-segment near-plane clipping (2026-08-17). The old
                    # code dropped the ENTIRE line if ANY endpoint of ANY
                    # segment fell behind the head - which happens constantly
                    # once a bounce sends the path back past the player
                    # ("sometimes disappearing"). Instead: clip each segment
                    # against a plane just in front of the head; a segment
                    # fully behind is dropped alone, a straddling segment is
                    # shortened to its visible part, the rest keep drawing.
                    NEAR = 0.2

                    def clip_seg(wa, wb):
                        da = sum((wa[i] - head_pos[i]) * fwd[i] for i in range(3))
                        db = sum((wb[i] - head_pos[i]) * fwd[i] for i in range(3))
                        if da < NEAR and db < NEAR:
                            return None
                        if da < NEAR:
                            s = (NEAR - da) / (db - da)
                            wa = [wa[i] + s * (wb[i] - wa[i]) for i in range(3)]
                        elif db < NEAR:
                            s = (NEAR - db) / (da - db)
                            wb = [wb[i] + s * (wa[i] - wb[i]) for i in range(3)]
                        return wa, wb

                    update_fns = [script.exports_sync.update_seg1, script.exports_sync.update_seg2, script.exports_sync.update_seg3]
                    clear_fns = [script.exports_sync.clear_seg1, script.exports_sync.clear_seg2, script.exports_sync.clear_seg3]

                    drawn = 0
                    for i in range(3):
                        seg_ok = False
                        if i < len(segments_world):
                            clipped = clip_seg(list(segments_world[i][0]), list(segments_world[i][1]))
                            if clipped is not None:
                                wa, wb = clipped
                                la = project_point(wa, eye_left, fwd, left, up, left_fov)
                                lb = project_point(wb, eye_left, fwd, left, up, left_fov)
                                ra = project_point(wa, eye_right, fwd, left, up, right_fov)
                                rb = project_point(wb, eye_right, fwd, left, up, right_fov)
                                if la and lb and ra and rb:
                                    update_fns[i](la[0], la[1], lb[0], lb[1], ra[0], ra[1], rb[0], rb[1])
                                    seg_ok = True
                                    drawn += 1
                        if not seg_ok:
                            clear_fns[i]()

                    if drawn:
                        ok_count += 1
                        if time.monotonic() - last_sample_printed > 3:
                            print(f"[poll] disc={disc_pos} head={head_pos} segs_drawn={drawn}/{len(segments_world[:3])}")
                            last_sample_printed = time.monotonic()
                    else:
                        skip("all_segments_clipped")
        except Exception as e:
            skip("exception:" + str(e))
            if time.monotonic() - last_status_print > 5:
                print(f"[poll] error: {e}")

        if time.monotonic() - last_status_print > 5:
            print(f"[poll] ok={ok_count} skip_reasons={skip_reasons}")
            last_status_print = time.monotonic()

        elapsed = time.monotonic() - t0
        sleep_left = POLL_INTERVAL_S - elapsed
        if sleep_left > 0:
            time.sleep(sleep_left)


def main():
    tee = Tee(LOG_PATH)
    sys.stdout = tee

    print()
    print("#" * 70)
    print(f"# RUN START: {datetime.now().isoformat(timespec='seconds')}")
    print("#" * 70)
    print()
    print("LIVE 3D TRAJECTORY TEST against echovr.exe")
    print("=" * 60)
    print(f"CALIBRATE={CALIBRATE} IPD_M={IPD_M} (real per-eye FOV read live from game memory)")

    if not os.path.exists(DLL_PATH):
        print(f"ERROR: {DLL_PATH} not found")
        sys.exit(1)

    try:
        _main_body()
    finally:
        print()
        print(f"# RUN END: {datetime.now().isoformat(timespec='seconds')}")
        print("#" * 70)
        sys.stdout = tee.terminal
        tee.close()


def _main_body():
    try:
        session = frida.attach("echovr.exe")
    except frida.ProcessNotFoundError:
        print("ERROR: echovr.exe not found")
        sys.exit(1)

    script = session.create_script(JS_SCRIPT)
    script.on("message", on_message)
    script.load()

    time.sleep(1.0)  # let the setTimeout-deferred setup finish before polling starts

    poll_thread = threading.Thread(target=poll_loop, args=(script,), daemon=True)
    poll_thread.start()

    if RUN_SECONDS > 0:
        print(f"[*] Running for {RUN_SECONDS} seconds - get into an active session with the disc visible...\n")
        time.sleep(RUN_SECONDS)
    else:
        print("[*] Running INDEFINITELY - Ctrl+C to stop.\n")
        try:
            while True:
                time.sleep(5)
                # exit cleanly if the game/Frida session died (e.g. game closed)
                try:
                    script.exports_sync.get_summary()
                except Exception:
                    print("[*] Frida session lost (game closed?) - exiting.")
                    break
        except KeyboardInterrupt:
            print("[*] Ctrl+C - stopping.")

    _stop_flag.set()
    poll_thread.join(timeout=2)

    try:
        s = script.exports_sync.get_summary()
        print(f"\n[*] setupDone={s['setupDone']} setupError={s['setupError']}")
        print(f"[*] targetResource={s['targetResource']}")
        print(f"[*] spliceAttempts={s['spliceAttempts']} spliceFailures={s['spliceFailures']} stopped={s['stopped']}")
    except Exception as e:
        print(f"[*] get_summary failed: {e}")

    print("\n[*] Done. Detaching Frida session (hooks removed).")
    try:
        session.detach()
    except Exception:
        pass


if __name__ == "__main__":
    main()
