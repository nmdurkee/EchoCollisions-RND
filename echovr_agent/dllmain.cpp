// echovr_agent — stage 0/1 actuation DLL for Echo VR.
//
// Injects into echovr.exe and redirects the game's cached LibOVR function
// pointers to our own hooks. See ECHOVR_INPUT_NOTES.md sec 7b for the discovery.
//
// SCOPE, ON PURPOSE:
//   Stage 0 — hook installed, passes everything through, logs. Proves we are at
//             the right seam and not crashing.
//   Stage 1 — force one button (grip) so a single bit of injection can be
//             confirmed against the /session API's holding_* field.
//
// NOT here yet: pose injection (ovr_GetTrackingState / ovr_GetDevicePoses).
// That function returns a large struct by value (a distinct x64 ABI path) and
// also needs the tracking-space <-> world-space transform, which is unverified.
// Its two slots are located and logged, but left passing through.
//
// The addresses below are RVAs derived from the notes' VAs minus the preferred
// image base 0x140000000. They are resolved against the ACTUAL runtime base, so
// ASLR is handled. They are valid only for the build the notes were taken from;
// against any other build, re-derive them (echovr_symbols.py + the sec 7b VAs).

#include <windows.h>
#include <winhttp.h>
#include <cstdint>
#include <cstdio>
#include <cstddef>
#include "session_parse.h"

// Bump this every build so the log shows which version is actually running.
// If the log's "loaded" line shows an old tag, the game is running a stale DLL
// (re-injection does not replace an already-loaded module) — restart the game.
#define AGENT_BUILD "2026-08-28 thrust-YB"

// --------------------------------------------------------------------------
// Minimal LibOVR CAPI surface. Types/offsets are from the public Oculus SDK
// (OVR_CAPI.h). VERIFY against your SDK header before trusting the offsets.
// --------------------------------------------------------------------------
typedef void*    ovrSession;
typedef int32_t  ovrResult;            // >= 0 is success
typedef int32_t  ovrControllerType;
typedef char     ovrBool;

// ovrInputState, as laid out by the SDK. We touch only the analog fields.
struct ovrInputState {
    double            TimeInSeconds;        // +0x00
    unsigned int      Buttons;              // +0x08
    unsigned int      Touches;              // +0x0C
    float             IndexTrigger[2];      // +0x10  [Left, Right]
    float             HandTrigger[2];       // +0x18  grip; [Left, Right]
    float             Thumbstick[2][2];     // +0x20  [hand][x,y]
    ovrControllerType ControllerType;       // +0x30
    // ...deadzone-variant fields follow; unused here.
};

typedef ovrResult (*PFN_ovr_GetInputState)(ovrSession, ovrControllerType, ovrInputState*);

// ---- Pose types, from the public OVR SDK (OVR_CAPI.h). VERIFY offsets. -----
struct ovrQuatf   { float x, y, z, w; };            // 16
struct ovrVector3f{ float x, y, z; };               // 12
struct ovrPosef   { ovrQuatf Orientation; ovrVector3f Position; };            // 28
struct ovrPoseStatef {
    ovrPosef     ThePose;                // +0
    ovrVector3f  AngularVelocity;        // +28
    ovrVector3f  LinearVelocity;         // +40
    ovrVector3f  AngularAcceleration;    // +52
    ovrVector3f  LinearAcceleration;     // +64
    double       TimeInSeconds;          // +80 (8-aligned)
};
struct ovrTrackingState {
    ovrPoseStatef HeadPose;              // +0
    unsigned int  StatusFlags;           // +88
    ovrPoseStatef HandPoses[2];          // +96  [0]=Left, [1]=Right
    unsigned int  HandStatusFlags[2];    // +272
    ovrPosef      CalibratedOrigin;      // +280
};

// If MinGW lays these out differently than the SDK, catch it at build time.
static_assert(sizeof(ovrPosef) == 28, "ovrPosef layout");
static_assert(sizeof(ovrPoseStatef) == 88, "ovrPoseStatef layout");
static_assert(offsetof(ovrTrackingState, HandPoses) == 96, "HandPoses offset");

// ovr_GetTrackingState returns a large struct by value. On Win64 that lowers to
// a hidden return-buffer pointer in RCX and the real args shift right; this
// explicit signature matches that ABI exactly (MinGW defaults to ms_abi here).
typedef ovrTrackingState* (*PFN_ovr_GetTrackingState)(ovrTrackingState* ret,
                                                      ovrSession, double, ovrBool);

// --------------------------------------------------------------------------
// Slot RVAs (VA - 0x140000000). See ECHOVR_INPUT_NOTES.md sec 7b.
// --------------------------------------------------------------------------
static const uintptr_t kPreferredBase      = 0x140000000ull;
static const uintptr_t kRVA_GetTrackingSt  = 0x1420EB5F8ull - kPreferredBase; // pose (stub)
static const uintptr_t kRVA_GetDevicePoses = 0x1420EB600ull - kPreferredBase; // pose (stub)
static const uintptr_t kRVA_GetInputState  = 0x1420EB610ull - kPreferredBase; // buttons

// --------------------------------------------------------------------------
// Shared control block. A future policy thread writes here; the hook reads it.
// Kept POD and updated with plain aligned writes so the hook needs no lock.
// --------------------------------------------------------------------------
struct AgentControl {
    volatile long   force_grip_right;   // stage 1: 1 => pin right grip to 1.0
    volatile long   force_grip_left;
    volatile long   passthrough_only;   // 1 => touch nothing (stage 0)
    volatile long   inject_pose;        // 1 => allow the pose test to modify poses
    volatile long   pose_test;          // which relative-nudge test (see applyPoseTest)
    volatile long   log_api;            // 1 => background thread polls /session into the log
    volatile long   thrust_test;        // which input field to force as thrust (see below)
};
static AgentControl g_ctrl = { 0, 0, 1, 0, 0, 0, 0 };   // default: stage 0, change nothing

// Nudge magnitude in whatever units tracking space uses (meters, presumably).
// The test exists to discover how this maps to the API's world axes.
static const float kNudge = 0.30f;

// --------------------------------------------------------------------------
// State
// --------------------------------------------------------------------------
static PFN_ovr_GetInputState    g_realGetInputState = nullptr;
static PFN_ovr_GetTrackingState g_realGetTrackingState = nullptr;
static void**  g_slotGetInputState = nullptr;
static void**  g_slotGetTrackingState = nullptr;
static FILE*   g_log = nullptr;
static HMODULE g_self = nullptr;   // this DLL, for locating its own folder
static volatile long g_inputCalls = 0;
static volatile long g_trackCalls = 0;

static void logf(const char* fmt, ...) {
    if (!g_log) return;
    SYSTEMTIME t; GetLocalTime(&t);
    fprintf(g_log, "[%02d:%02d:%02d.%03d] ", t.wHour, t.wMinute, t.wSecond, t.wMilliseconds);
    va_list ap; va_start(ap, fmt);
    vfprintf(g_log, fmt, ap);
    va_end(ap);
    fprintf(g_log, "\n");
    fflush(g_log);
}

// --------------------------------------------------------------------------
// Our hook. Stage 0: call real, log, return untouched. Stage 1: overwrite grip.
// --------------------------------------------------------------------------
static ovrResult MyGetInputState(ovrSession s, ovrControllerType t, ovrInputState* out) {
    ovrResult r = g_realGetInputState ? g_realGetInputState(s, t, out) : -1;

    long n = InterlockedIncrement(&g_inputCalls);
    if (n <= 3 || (n % 900) == 0)   // first calls, then ~once / 10s at 90Hz
        logf("GetInputState #%ld ret=%d buttons=0x%08x hgrip[L,R]=%.2f,%.2f",
             n, r, out ? out->Buttons : 0u,
             out ? out->HandTrigger[0] : 0.f, out ? out->HandTrigger[1] : 0.f);

    if (out && r >= 0 && !g_ctrl.passthrough_only) {
        if (g_ctrl.force_grip_right) out->HandTrigger[1] = 1.0f;
        if (g_ctrl.force_grip_left)  out->HandTrigger[0] = 1.0f;

        // Thrust candidates. Echo's thruster (inputironman*) is a different action
        // than grab (=HandTrigger, proven). Try each field; watch API vel to see
        // which one produces motion.
        switch (g_ctrl.thrust_test) {
            // THRUST = Y (left, 0x200) + B (right, 0x2) held together = 0x202.
            case 6: out->Buttons |= 0x00000202u; break;   // both thrusters
            case 7: out->Buttons |= 0x00000002u; break;   // B only (right thruster)
            case 8: out->Buttons |= 0x00000200u; break;   // Y only (left thruster)
            // legacy guesses (kept for reference):
            case 1: out->IndexTrigger[0] = 1.0f; out->IndexTrigger[1] = 1.0f; break;
            case 2: out->IndexTrigger[1] = 1.0f; break;
            case 3: out->HandTrigger[0]  = 1.0f; out->HandTrigger[1]  = 1.0f; break;
            case 4: out->Buttons |= 0x00000001u; break;
            case 5: out->Thumbstick[0][1] = 1.0f; out->Thumbstick[1][1] = 1.0f; break;
            default: break;
        }
    }
    return r;
}

// Write a full, VALID pose to a hand: identity orientation, absolute position,
// and mark it tracked. This is the correct way to inject — a bare position on a
// degenerate zero-quaternion base makes the avatar IK misbehave (calibration
// run 1). ovrStatus: 0x1 = orientation tracked, 0x2 = position tracked.
static void setHand(ovrTrackingState* ts, int hand, float x, float y, float z) {
    ovrPosef& p = ts->HandPoses[hand].ThePose;
    p.Orientation.x = 0.0f; p.Orientation.y = 0.0f;
    p.Orientation.z = 0.0f; p.Orientation.w = 1.0f;   // identity
    p.Position.x = x; p.Position.y = y; p.Position.z = z;
    ts->HandStatusFlags[hand] = 0x3;                  // tracked
}

// Full pose with an explicit orientation quaternion, at a natural reach position
// so the arm is not clamped. For testing whether hand ORIENTATION (thrust aim)
// maps cleanly to the API's forward/up vectors.
static void setHandOri(ovrTrackingState* ts, int hand,
                       float qx, float qy, float qz, float qw) {
    ovrPosef& p = ts->HandPoses[hand].ThePose;
    p.Orientation.x = qx; p.Orientation.y = qy;
    p.Orientation.z = qz; p.Orientation.w = qw;
    p.Position.x = 0.20f; p.Position.y = -0.20f; p.Position.z = -0.30f;
    ts->HandStatusFlags[hand] = 0x3;
}

// Full pose: explicit orientation + position for one hand.
static void setHandFull(ovrTrackingState* ts, int hand,
                        float qx, float qy, float qz, float qw,
                        float x, float y, float z) {
    ovrPosef& p = ts->HandPoses[hand].ThePose;
    p.Orientation.x = qx; p.Orientation.y = qy;
    p.Orientation.z = qz; p.Orientation.w = qw;
    p.Position.x = x; p.Position.y = y; p.Position.z = z;
    ts->HandStatusFlags[hand] = 0x3;
}

// Tests 1-5: legacy relative nudges (superseded — they fought the zero base).
// Tests 10+: full absolute poses, the correct method. Read the resulting avatar
// hand off the API to map tracking -> world cleanly.
static void applyPoseTest(ovrTrackingState* ts) {
    switch (g_ctrl.pose_test) {
        case 1: ts->HandPoses[1].ThePose.Position.y += kNudge; break;
        case 2: ts->HandPoses[0].ThePose.Position.y += kNudge;
                ts->HandPoses[1].ThePose.Position.y += kNudge; break;
        case 3: ts->HeadPose.ThePose.Position.y     += kNudge; break;
        case 4: ts->HandPoses[1].ThePose.Position.x += kNudge; break;
        case 5: ts->HandPoses[1].ThePose.Position.z += kNudge; break;

        case 13: setHand(ts, 1, 0.00f, 0.00f,  0.00f); break;  // R origin, valid ori (baseline)
        case 10: setHand(ts, 1, 0.30f, 0.00f,  0.00f); break;  // R +X
        case 11: setHand(ts, 1, 0.00f, 0.30f,  0.00f); break;  // R +Y
        case 12: setHand(ts, 1, 0.00f, 0.00f,  0.30f); break;  // R +Z
        case 14: setHand(ts, 1, 0.20f,-0.20f, -0.30f); break;  // R natural reach (both-check)

        // Orientation tests: 90-deg rotations about each axis (q = (axis*sin45, cos45)).
        case 20: setHandOri(ts, 1, 0.0f,   0.0f,   0.0f,   1.0f);   break;  // identity
        case 21: setHandOri(ts, 1, 0.0f,   0.7071f,0.0f,   0.7071f);break;  // 90 about Y
        case 22: setHandOri(ts, 1, 0.7071f,0.0f,   0.0f,   0.7071f);break;  // 90 about X
        case 23: setHandOri(ts, 1, 0.0f,   0.0f,   0.7071f,0.7071f);break;  // 90 about Z
        default: break;
    }
}

// Pose hook. Let the runtime fill the struct, log it, optionally nudge it.
static ovrTrackingState* MyGetTrackingState(ovrTrackingState* ret, ovrSession s,
                                            double t, ovrBool m) {
    if (g_realGetTrackingState) g_realGetTrackingState(ret, s, t, m);

    long n = InterlockedIncrement(&g_trackCalls);
    if (ret && (n <= 3 || (n % 900) == 0)) {
        ovrVector3f& h = ret->HeadPose.ThePose.Position;
        ovrVector3f& l = ret->HandPoses[0].ThePose.Position;
        ovrVector3f& r = ret->HandPoses[1].ThePose.Position;
        logf("GetTrackingState #%ld head=(%.2f,%.2f,%.2f) L=(%.2f,%.2f,%.2f) R=(%.2f,%.2f,%.2f)",
             n, h.x, h.y, h.z, l.x, l.y, l.z, r.x, r.y, r.z);
    }

    // Gate on pose_test alone: a non-zero test IS the intent to inject.
    if (ret && !g_ctrl.passthrough_only && g_ctrl.pose_test != 0)
        applyPoseTest(ret);

    // During a thrust test, point BOTH hands forward (+Z in the base frame, via
    // the 90-about-Y quaternion that run 4 showed yields forward=+Z). Thrust
    // exhausts behind the palms, so the player should accelerate along +Z.
    if (ret && !g_ctrl.passthrough_only && g_ctrl.thrust_test != 0) {
        setHandFull(ret, 0, 0.0f,0.7071f,0.0f,0.7071f, -0.20f,-0.20f,-0.30f);
        setHandFull(ret, 1, 0.0f,0.7071f,0.0f,0.7071f,  0.20f,-0.20f,-0.30f);
    }
    return ret;
}

// --------------------------------------------------------------------------
// Patch one 8-byte pointer slot. Returns the previous value.
// --------------------------------------------------------------------------
static void* patchSlot(void** slot, void* newval) {
    DWORD old;
    if (!VirtualProtect(slot, sizeof(void*), PAGE_EXECUTE_READWRITE, &old))
        return nullptr;
    void* prev = *slot;
    *slot = newval;
    VirtualProtect(slot, sizeof(void*), old, &old);
    return prev;
}

// --------------------------------------------------------------------------
// API poller. Reads the game's own /session endpoint over loopback on a slow
// background thread (NEVER the render thread) and logs the fields we care about.
// This is the read-side counterpart to the pose injection: it records the
// world-space result of whatever tracking-space pose we wrote.
// --------------------------------------------------------------------------
static bool httpGetSession(char* buf, DWORD cap, DWORD* outLen) {
    bool ok = false;
    HINTERNET hs = WinHttpOpen(L"echovr_agent", WINHTTP_ACCESS_TYPE_NO_PROXY,
                               WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (!hs) return false;
    HINTERNET hc = WinHttpConnect(hs, L"127.0.0.1", 6721, 0);
    if (hc) {
        HINTERNET hr = WinHttpOpenRequest(hc, L"GET", L"/session", nullptr,
                                          WINHTTP_NO_REFERER,
                                          WINHTTP_DEFAULT_ACCEPT_TYPES, 0);
        if (hr) {
            if (WinHttpSendRequest(hr, WINHTTP_NO_ADDITIONAL_HEADERS, 0,
                                   WINHTTP_NO_REQUEST_DATA, 0, 0, 0) &&
                WinHttpReceiveResponse(hr, nullptr)) {
                DWORD total = 0, got = 0;
                do {
                    DWORD avail = 0;
                    if (!WinHttpQueryDataAvailable(hr, &avail) || avail == 0) break;
                    if (total + avail >= cap) avail = cap - 1 - total;
                    if (avail == 0) break;
                    if (!WinHttpReadData(hr, buf + total, avail, &got)) break;
                    total += got;
                } while (got > 0);
                buf[total] = 0;
                *outLen = total;
                ok = total > 0;
            }
            WinHttpCloseHandle(hr);
        }
        WinHttpCloseHandle(hc);
    }
    WinHttpCloseHandle(hs);
    return ok;
}

static DWORD WINAPI ApiPoller(LPVOID) {
    static char buf[65536];
    while (true) {
        if (g_ctrl.log_api) {
            DWORD n = 0;
            if (httpGetSession(buf, sizeof(buf), &n)) {
                SessionFields f;
                if (parseSession(buf, n, f) && f.have_player) {
                    logf("API %s R=(%.3f,%.3f,%.3f) Rfwd=(%.3f,%.3f,%.3f) "
                         "Rup=(%.3f,%.3f,%.3f) H=(%.3f,%.3f,%.3f) "
                         "vel=(%.3f,%.3f,%.3f) hold[L,R]=%s,%s",
                         f.status, f.rhand[0], f.rhand[1], f.rhand[2],
                         f.rhandFwd[0], f.rhandFwd[1], f.rhandFwd[2],
                         f.rhandUp[0], f.rhandUp[1], f.rhandUp[2],
                         f.head[0], f.head[1], f.head[2],
                         f.vel[0], f.vel[1], f.vel[2], f.holdL, f.holdR);
                } else {
                    logf("API: no local player yet (in a match, spawned?)");
                }
            } else {
                logf("API: /session unreachable (EnableAPIAccess on?)");
            }
        }
        Sleep(500);   // 2 Hz — plenty for calibration and state watching
    }
    return 0;
}

// --------------------------------------------------------------------------
// Worker: wait for the game to resolve LibOVR, then install the input hook.
// Runs off the loader lock (DllMain only spawns it).
// --------------------------------------------------------------------------
static DWORD WINAPI Worker(LPVOID) {
    uintptr_t base = (uintptr_t)GetModuleHandleW(nullptr);
    void** trk = (void**)(base + kRVA_GetTrackingSt);
    void** dvp = (void**)(base + kRVA_GetDevicePoses);
    g_slotGetInputState    = (void**)(base + kRVA_GetInputState);
    g_slotGetTrackingState = (void**)(base + kRVA_GetTrackingSt);

    logf("attached. echovr base=0x%llx  input-slot=0x%llx  track-slot=0x%llx",
         (unsigned long long)base,
         (unsigned long long)(uintptr_t)g_slotGetInputState,
         (unsigned long long)(uintptr_t)g_slotGetTrackingState);

    // The loader fills these slots after LibOVR init; poll until non-null.
    for (int i = 0; i < 600; ++i) {          // up to ~60s
        if (*g_slotGetInputState) break;
        Sleep(100);
    }
    if (!*g_slotGetInputState) {
        logf("timed out waiting for LibOVR proc table; is the headset active?");
        return 0;
    }

    logf("pose slots: GetTrackingState=%p GetDevicePoses=%p", *trk, *dvp);

    g_realGetInputState = (PFN_ovr_GetInputState)patchSlot(g_slotGetInputState, (void*)&MyGetInputState);
    if (g_realGetInputState)
        logf("hooked ovr_GetInputState (real=%p)", (void*)g_realGetInputState);
    else
        logf("FAILED to patch input slot");

    if (*g_slotGetTrackingState) {
        g_realGetTrackingState = (PFN_ovr_GetTrackingState)patchSlot(g_slotGetTrackingState, (void*)&MyGetTrackingState);
        logf("hooked ovr_GetTrackingState (real=%p)", (void*)g_realGetTrackingState);
    } else {
        logf("GetTrackingState slot empty — game may use GetDevicePoses for hands");
    }
    logf("passthrough_only=%ld  pose_test=%ld  log_api=%ld",
         g_ctrl.passthrough_only, g_ctrl.pose_test, g_ctrl.log_api);
    CreateThread(nullptr, 0, ApiPoller, nullptr, 0, nullptr);   // idle until log_api=1
    return 0;
}

static void openLog() {
    // Try, in order: next to the DLL (nice when the folder is writable), the
    // user profile (always writable, easy to find), then TEMP. The game often
    // lives under Program Files, which a non-elevated process cannot write to,
    // so the DLL-folder attempt usually fails there and we land in the profile.
    wchar_t path[MAX_PATH] = {0};

    wchar_t mod[MAX_PATH];
    DWORD n = GetModuleFileNameW(g_self, mod, MAX_PATH);
    if (n > 0 && n < MAX_PATH) {
        wchar_t* slash = wcsrchr(mod, L'\\');
        if (slash) {
            *(slash + 1) = 0;
            swprintf(path, MAX_PATH, L"%secho_agent.log", mod);
            g_log = _wfopen(path, L"a");
        }
    }
    if (!g_log) {
        wchar_t prof[MAX_PATH];
        DWORD m = GetEnvironmentVariableW(L"USERPROFILE", prof, MAX_PATH);
        if (m > 0 && m < MAX_PATH) {
            swprintf(path, MAX_PATH, L"%ls\\echo_agent.log", prof);
            g_log = _wfopen(path, L"a");
        }
    }
    if (!g_log) {
        wchar_t tmp[MAX_PATH];
        GetTempPathW(MAX_PATH, tmp);      // has a trailing slash
        swprintf(path, MAX_PATH, L"%secho_agent.log", tmp);
        g_log = _wfopen(path, L"a");
    }
    logf("=== echovr_agent loaded (build: %s) ===", AGENT_BUILD);
    logf("log path: %ls", path);          // first content: where this file lives
}

BOOL APIENTRY DllMain(HMODULE hMod, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        g_self = hMod;
        DisableThreadLibraryCalls(hMod);
        openLog();
        CreateThread(nullptr, 0, Worker, nullptr, 0, nullptr);
    } else if (reason == DLL_PROCESS_DETACH) {
        // Restore, so ejecting the DLL cleanly un-hooks.
        if (g_slotGetInputState && g_realGetInputState)
            patchSlot(g_slotGetInputState, (void*)g_realGetInputState);
        if (g_slotGetTrackingState && g_realGetTrackingState)
            patchSlot(g_slotGetTrackingState, (void*)g_realGetTrackingState);
        logf("=== echovr_agent unloaded ===");
        if (g_log) fclose(g_log);
    }
    return TRUE;
}

// --------------------------------------------------------------------------
// Exported control knobs. Set these from an injector or a small poker EXE.
// e.g. GetProcAddress(dll, "agent_set_force_grip_right")(1);
// --------------------------------------------------------------------------
extern "C" {
__declspec(dllexport) void agent_set_passthrough(long v)       { g_ctrl.passthrough_only = v; logf("passthrough_only=%ld", v); }
__declspec(dllexport) void agent_set_force_grip_right(long v)  { g_ctrl.force_grip_right = v;  logf("force_grip_right=%ld", v); }
__declspec(dllexport) void agent_set_force_grip_left(long v)   { g_ctrl.force_grip_left = v;   logf("force_grip_left=%ld", v); }
__declspec(dllexport) void agent_set_inject_pose(long v)       { g_ctrl.inject_pose = v;       logf("inject_pose=%ld", v); }
__declspec(dllexport) void agent_set_pose_test(long v)         { g_ctrl.pose_test = v;         logf("pose_test=%ld", v); }
__declspec(dllexport) void agent_set_log_api(long v)           { g_ctrl.log_api = v;           logf("log_api=%ld", v); }
__declspec(dllexport) void agent_set_thrust_test(long v)       { g_ctrl.thrust_test = v;       logf("thrust_test=%ld", v); }
__declspec(dllexport) long agent_input_call_count()            { return g_inputCalls; }
__declspec(dllexport) long agent_track_call_count()            { return g_trackCalls; }
}
