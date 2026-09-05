// session_parse.h — extract the fields we care about from a /session payload.
//
// Not a general JSON parser: targeted substring extraction for a known, stable
// schema. Validated against a real replay frame by test_parse.cpp. Shared by the
// DLL (which polls the API in-process) and that test.
//
// Local player = the one whose "name" matches "client_name" (falls back to the
// first player). The literal `"name":"` never matches inside `"client_name":`
// because there is no quote immediately before `name` there.

#pragma once
#include <cstring>
#include <cstdio>
#include <cstddef>

struct SessionFields {
    char  client[64] = {0};
    char  name[64]   = {0};
    char  holdL[24]  = {0};
    char  holdR[24]  = {0};
    float rhand[3]    = {0,0,0};
    float rhandFwd[3] = {0,0,0};   // right-hand orientation: forward vector
    float rhandUp[3]  = {0,0,0};   // right-hand orientation: up vector
    float lhand[3] = {0,0,0};
    float head[3]  = {0,0,0};
    float vel[3]   = {0,0,0};
    float rigPos[3]= {0,0,0};
    bool  have_player = false;
    char  status[24] = {0};
};

namespace sp_detail {

inline const char* findIn(const char* s, const char* e, const char* needle) {
    size_t n = strlen(needle);
    if (n == 0 || s > e) return nullptr;
    for (const char* p = s; p + n <= e; ++p)
        if (memcmp(p, needle, n) == 0) return p;
    return nullptr;
}

// "[a,b,c]" starting at or after `key`, within [s,e). Handles the nested pose
// case by searching for the sub-array key (e.g. "pos") after the group key.
inline bool vec3(const char* s, const char* e, const char* key, float out[3]) {
    const char* k = findIn(s, e, key);
    if (!k) return false;
    const char* br = findIn(k, e, "[");
    if (!br) return false;
    return sscanf(br, "[%f,%f,%f", &out[0], &out[1], &out[2]) == 3;
}

// Value of "key":"..." within [s,e).
inline bool str(const char* s, const char* e, const char* key, char* out, size_t n) {
    const char* k = findIn(s, e, key);
    if (!k) return false;
    const char* c = findIn(k, e, ":");
    if (!c) return false;
    const char* q1 = findIn(c, e, "\"");
    if (!q1) return false;
    ++q1;
    const char* q2 = findIn(q1, e, "\"");
    if (!q2) return false;
    size_t len = (size_t)(q2 - q1);
    if (len >= n) len = n - 1;
    memcpy(out, q1, len);
    out[len] = 0;
    return true;
}

} // namespace sp_detail

inline bool parseSession(const char* j, size_t len, SessionFields& f) {
    using namespace sp_detail;
    const char* s = j;
    const char* e = j + len;

    str(s, e, "\"game_status\"", f.status, sizeof(f.status));
    str(s, e, "\"client_name\"", f.client, sizeof(f.client));

    // Rig transform (top level, unique key).
    vec3(s, e, "\"vr_position\"", f.rigPos);

    // Locate the local player's object window.
    const char* lp = nullptr;
    if (f.client[0]) {
        char pat[80];
        snprintf(pat, sizeof(pat), "\"name\":\"%s\"", f.client);
        lp = findIn(s, e, pat);
    }
    if (!lp) lp = findIn(s, e, "\"name\":\"");   // fallback: first player
    if (!lp) return false;

    const char* nextName = findIn(lp + 1, e, "\"name\":\"");
    const char* winEnd = nextName ? nextName : e;

    f.have_player = true;
    str(lp, winEnd, "\"name\"", f.name, sizeof(f.name));

    const char* rh = findIn(lp, winEnd, "\"rhand\"");
    if (rh) {
        vec3(rh, winEnd, "\"pos\"", f.rhand);
        // forward/up are siblings of pos inside the rhand object.
        vec3(rh, winEnd, "\"forward\"", f.rhandFwd);
        vec3(rh, winEnd, "\"up\"", f.rhandUp);
    }
    const char* lh = findIn(lp, winEnd, "\"lhand\"");
    if (lh) vec3(lh, winEnd, "\"pos\"", f.lhand);
    const char* hd = findIn(lp, winEnd, "\"head\"");
    if (hd) vec3(hd, winEnd, "\"position\"", f.head);

    vec3(lp, winEnd, "\"velocity\"", f.vel);
    str(lp, winEnd, "\"holding_left\"", f.holdL, sizeof(f.holdL));
    str(lp, winEnd, "\"holding_right\"", f.holdR, sizeof(f.holdR));
    return true;
}
