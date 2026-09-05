// echo_agent — single-file launcher for the Echo VR agent hook.
//
// The DLL is embedded in this exe (embedded_dll.h). At runtime it is written out
// next to this exe and injected into echovr.exe. So you copy ONE file to the game
// PC and type ONE command. Nothing else to place, no Python.
//
//   echo_agent.exe                 inject + log the API to echo_agent.log (stage 0)
//   echo_agent.exe --calibrate     inject, log API, run the full pose sweep
//   echo_agent.exe --grip-right 1  force right grip     (--grip-right 0 to release)
//   echo_agent.exe --pose-test N   inject pose test N   (N: 1,3,4,5; 0 = off)
//   echo_agent.exe --off           stop injecting; keep passing input through
//
// Log lands in your user folder: %USERPROFILE%\echo_agent.log (its own first line
// states the exact path). Run from an elevated shell if the game is elevated.

#include <windows.h>
#include <tlhelp32.h>
#include <cstdio>
#include <cwchar>
#include <cstring>
#include "embedded_dll.h"

static DWORD findPid(const wchar_t* name) {
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return 0;
    PROCESSENTRY32W pe{}; pe.dwSize = sizeof(pe);
    DWORD pid = 0;
    if (Process32FirstW(snap, &pe))
        do { if (_wcsicmp(pe.szExeFile, name) == 0) { pid = pe.th32ProcessID; break; } }
        while (Process32NextW(snap, &pe));
    CloseHandle(snap);
    return pid;
}

static uintptr_t findModuleBase(DWORD pid, const wchar_t* dllName) {
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid);
    if (snap == INVALID_HANDLE_VALUE) return 0;
    MODULEENTRY32W me{}; me.dwSize = sizeof(me);
    uintptr_t base = 0;
    if (Module32FirstW(snap, &me))
        do { if (_wcsicmp(me.szModule, dllName) == 0) { base = (uintptr_t)me.modBaseAddr; break; } }
        while (Module32NextW(snap, &me));
    CloseHandle(snap);
    return base;
}

static bool callExport(HANDLE hProc, uintptr_t remoteBase, HMODULE localDll,
                       const char* fn, long arg) {
    FARPROC local = GetProcAddress(localDll, fn);
    if (!local) { wprintf(L"  export not found: %hs\n", fn); return false; }
    uintptr_t off = (uintptr_t)local - (uintptr_t)localDll;
    auto remote = (LPTHREAD_START_ROUTINE)(remoteBase + off);
    HANDLE th = CreateRemoteThread(hProc, nullptr, 0, remote, (LPVOID)(intptr_t)arg, 0, nullptr);
    if (!th) { wprintf(L"  remote call %hs failed (%lu)\n", fn, GetLastError()); return false; }
    WaitForSingleObject(th, INFINITE);
    CloseHandle(th);
    return true;
}

// Write the embedded DLL next to this exe; fall back to TEMP. Returns full path.
static bool dropDll(wchar_t* outPath, size_t cap) {
    wchar_t exe[MAX_PATH];
    if (GetModuleFileNameW(nullptr, exe, MAX_PATH)) {
        wchar_t* s = wcsrchr(exe, L'\\');
        if (s) {
            *(s + 1) = 0;
            swprintf(outPath, cap, L"%lsechovr_agent.dll", exe);
            HANDLE h = CreateFileW(outPath, GENERIC_WRITE, 0, nullptr,
                                   CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
            if (h != INVALID_HANDLE_VALUE) {
                DWORD w = 0; WriteFile(h, g_dll, g_dll_len, &w, nullptr);
                CloseHandle(h);
                if (w == g_dll_len) return true;
            }
            // Already loaded / folder not writable: reuse an existing copy if present.
            if (GetFileAttributesW(outPath) != INVALID_FILE_ATTRIBUTES) return true;
        }
    }
    // Fallback: the user folder (always writable, easy to add as a Defender
    // exclusion if needed). Preferred over TEMP, which is noisier.
    wchar_t prof[MAX_PATH];
    if (GetEnvironmentVariableW(L"USERPROFILE", prof, MAX_PATH))
        swprintf(outPath, cap, L"%ls\\echovr_agent.dll", prof);
    else {
        GetTempPathW(MAX_PATH, prof);
        swprintf(outPath, cap, L"%lsechovr_agent.dll", prof);
    }
    HANDLE h = CreateFileW(outPath, GENERIC_WRITE, 0, nullptr,
                           CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h != INVALID_HANDLE_VALUE) {
        DWORD w = 0; WriteFile(h, g_dll, g_dll_len, &w, nullptr);
        CloseHandle(h);
        if (w == g_dll_len) return true;
    }
    return GetFileAttributesW(outPath) != INVALID_FILE_ATTRIBUTES;
}

int wmain(int argc, wchar_t** argv) {
    const wchar_t* proc = L"echovr.exe";
    bool calibrate = false, off = false, thrust = false;
    struct { bool set; long v; } passthrough{false,0}, gr{false,1}, gl{false,1},
                                  pt{false,0}, la{false,1};

    for (int i = 1; i < argc; ++i) {
        if (!wcscmp(argv[i], L"--target") && i + 1 < argc) proc = argv[++i];
        else if (!wcscmp(argv[i], L"--calibrate")) calibrate = true;
        else if (!wcscmp(argv[i], L"--thrust"))    thrust = true;
        else if (!wcscmp(argv[i], L"--off"))       off = true;
        else if (!wcscmp(argv[i], L"--active"))      { passthrough.set = true; passthrough.v = 0; }
        else if (!wcscmp(argv[i], L"--passthrough")) { passthrough.set = true; passthrough.v = 1; }
        else if (!wcscmp(argv[i], L"--grip-right"))  { gr.set = true; if (i+1<argc && iswdigit(argv[i+1][0])) gr.v = _wtol(argv[++i]); }
        else if (!wcscmp(argv[i], L"--grip-left"))   { gl.set = true; if (i+1<argc && iswdigit(argv[i+1][0])) gl.v = _wtol(argv[++i]); }
        else if (!wcscmp(argv[i], L"--pose-test"))   { pt.set = true; if (i+1<argc && iswdigit(argv[i+1][0])) pt.v = _wtol(argv[++i]); }
        else if (!wcscmp(argv[i], L"--log-api"))     { la.set = true; if (i+1<argc && iswdigit(argv[i+1][0])) la.v = _wtol(argv[++i]); }
        else { wprintf(L"unknown arg: %ls\n", argv[i]); return 2; }
    }

    // Default with no flags: just inject and log the API. Calibrate/thrust drive sweeps.
    if (calibrate || thrust) { la.set = true; la.v = 1; passthrough.set = true; passthrough.v = 0; }
    else if (off)  { passthrough.set = true; passthrough.v = 1; pt.set = true; pt.v = 0; gr.set = gl.set = true; gr.v = gl.v = 0; }
    else if (!passthrough.set && !gr.set && !gl.set && !pt.set && !la.set) { la.set = true; la.v = 1; }

    wchar_t dllPath[MAX_PATH];
    if (!dropDll(dllPath, MAX_PATH)) { wprintf(L"could not write the embedded DLL\n"); return 1; }
    const wchar_t* dllName = wcsrchr(dllPath, L'\\'); dllName = dllName ? dllName + 1 : dllPath;

    DWORD pid = findPid(proc);
    if (!pid) { wprintf(L"process not found: %ls (is the game running?)\n", proc); return 1; }
    wprintf(L"target %ls pid=%lu\n", proc, pid);

    // If a copy is ALREADY loaded, LoadLibrary will NOT replace it with new code.
    // This silently runs stale behavior. Warn loudly.
    if (findModuleBase(pid, dllName)) {
        wprintf(L"\n*** WARNING: an agent DLL is already loaded in this game process.\n");
        wprintf(L"    Re-injecting does NOT load new code — the game keeps the old one.\n");
        wprintf(L"    To apply this build: fully CLOSE Echo VR, reopen it, then re-run.\n");
        wprintf(L"    (Control flags below act on the OLD DLL and may do nothing.)\n\n");
    }

    HANDLE h = OpenProcess(PROCESS_CREATE_THREAD | PROCESS_VM_OPERATION |
                           PROCESS_VM_WRITE | PROCESS_VM_READ | PROCESS_QUERY_INFORMATION,
                           FALSE, pid);
    if (!h) { wprintf(L"OpenProcess failed (%lu). Try an elevated shell.\n", GetLastError()); return 1; }

    // Inject (idempotent: LoadLibraryW returns the existing module if already loaded).
    SIZE_T bytes = (wcslen(dllPath) + 1) * sizeof(wchar_t);
    void* remote = VirtualAllocEx(h, nullptr, bytes, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    WriteProcessMemory(h, remote, dllPath, bytes, nullptr);
    auto loadLib = (LPTHREAD_START_ROUTINE)GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "LoadLibraryW");
    HANDLE th = CreateRemoteThread(h, nullptr, 0, loadLib, remote, 0, nullptr);
    if (!th) { wprintf(L"injection failed (%lu)\n", GetLastError()); CloseHandle(h); return 1; }
    WaitForSingleObject(th, INFINITE);
    CloseHandle(th);
    VirtualFreeEx(h, remote, 0, MEM_RELEASE);
    wprintf(L"injected\n");

    // Resolve the DLL for control calls.
    Sleep(200);   // let the loader register the module
    uintptr_t remoteBase = findModuleBase(pid, dllName);
    HMODULE localDll = LoadLibraryExW(dllPath, nullptr, DONT_RESOLVE_DLL_REFERENCES);
    if (!remoteBase || !localDll) {
        wprintf(L"injected, but could not resolve for control calls (base=%p)\n", (void*)remoteBase);
        CloseHandle(h); return 0;
    }

    if (calibrate) {
        wprintf(L"calibrating: API logging on, sweeping full absolute poses...\n");
        wprintf(L"(run this in a live match, game_status=playing, for clean data)\n");
        callExport(h, remoteBase, localDll, "agent_set_log_api", 1);
        callExport(h, remoteBase, localDll, "agent_set_passthrough", 0);
        // Position: baseline, +X, +Y, +Z, reach.  Orientation: identity, 90 Y/X/Z.  Off.
        const long tests[] = { 13, 10, 11, 12, 14, 20, 21, 22, 23, 0 };
        for (long t : tests) {
            wprintf(L"  pose_test %ld\n", t);
            callExport(h, remoteBase, localDll, "agent_set_pose_test", t);
            Sleep(3000);   // ~6 API samples per phase at 2 Hz
        }
        wprintf(L"done. see the log.\n");
    } else if (thrust) {
        wprintf(L"thrust test: firing Y+B with hands pointing +Z; watch API vel...\n");
        callExport(h, remoteBase, localDll, "agent_set_log_api", 1);
        callExport(h, remoteBase, localDll, "agent_set_passthrough", 0);
        // 6 = Y+B (both thrusters), the confirmed thrust buttons. Hold 6s to build
        // velocity, then off. (7=B only, 8=Y only available for single-hand tests.)
        callExport(h, remoteBase, localDll, "agent_set_thrust_test", 6);
        wprintf(L"  thrust_test 6 (Y+B) ...\n");
        Sleep(6000);
        callExport(h, remoteBase, localDll, "agent_set_thrust_test", 0);
        Sleep(2000);
        wprintf(L"done. see the log (look for vel != 0 during phase 6).\n");
    } else {
        if (passthrough.set) callExport(h, remoteBase, localDll, "agent_set_passthrough",      passthrough.v);
        if (gr.set)          callExport(h, remoteBase, localDll, "agent_set_force_grip_right", gr.v);
        if (gl.set)          callExport(h, remoteBase, localDll, "agent_set_force_grip_left",  gl.v);
        if (pt.set)          callExport(h, remoteBase, localDll, "agent_set_pose_test",        pt.v);
        if (la.set)          callExport(h, remoteBase, localDll, "agent_set_log_api",          la.v);
    }
    FreeLibrary(localDll);

    wchar_t prof[MAX_PATH]; DWORD m = GetEnvironmentVariableW(L"USERPROFILE", prof, MAX_PATH);
    if (m > 0 && m < MAX_PATH) wprintf(L"log: %ls\\echo_agent.log\n", prof);
    CloseHandle(h);
    return 0;
}
