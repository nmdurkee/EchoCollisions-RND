// inject — LoadLibrary injector + control poker for echovr_agent.dll.
//
// Loads the DLL into echovr.exe (nothing dropped in the game folder), and can
// then call the DLL's exported control knobs in-process so stage 1 is a command
// line rather than hand-written code.
//
//   inject.exe <dll-path> [--target proc.exe] [controls...]
//
// Controls (each calls the matching export via a remote thread):
//   --active            passthrough OFF — hooks may modify input
//   --passthrough       passthrough ON  — change nothing (stage 0, default)
//   --grip-right [0|1]  force right grip (default 1)
//   --grip-left  [0|1]  force left grip  (default 1)
//
// With no controls it just injects (stage 0). If the DLL is already loaded,
// re-running with only controls still works — LoadLibrary is idempotent and
// returns the existing module.
//
//   inject.exe echovr_agent.dll                     # stage 0: inject + log
//   inject.exe echovr_agent.dll --active --grip-right 1   # stage 1
//   inject.exe echovr_agent.dll --grip-right 0            # release

#include <windows.h>
#include <tlhelp32.h>
#include <cstdio>
#include <cwchar>
#include <cstring>

static DWORD findPid(const wchar_t* name) {
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return 0;
    PROCESSENTRY32W pe{}; pe.dwSize = sizeof(pe);
    DWORD pid = 0;
    if (Process32FirstW(snap, &pe))
        do {
            if (_wcsicmp(pe.szExeFile, name) == 0) { pid = pe.th32ProcessID; break; }
        } while (Process32NextW(snap, &pe));
    CloseHandle(snap);
    return pid;
}

// Full 64-bit base of a module inside the target (thread exit codes truncate to
// 32 bits, so we cannot use those).
static uintptr_t findModuleBase(DWORD pid, const wchar_t* dllName) {
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid);
    if (snap == INVALID_HANDLE_VALUE) return 0;
    MODULEENTRY32W me{}; me.dwSize = sizeof(me);
    uintptr_t base = 0;
    if (Module32FirstW(snap, &me))
        do {
            if (_wcsicmp(me.szModule, dllName) == 0) { base = (uintptr_t)me.modBaseAddr; break; }
        } while (Module32NextW(snap, &me));
    CloseHandle(snap);
    return base;
}

// Call one exported void(long) in the target. Resolves the remote address by
// rebasing our own GetProcAddress result onto the target's module base.
static bool callExport(HANDLE hProc, uintptr_t remoteBase, HMODULE localDll,
                       const char* fn, long arg) {
    FARPROC local = GetProcAddress(localDll, fn);
    if (!local) { wprintf(L"  export not found: %hs\n", fn); return false; }
    uintptr_t off = (uintptr_t)local - (uintptr_t)localDll;
    auto remote = (LPTHREAD_START_ROUTINE)(remoteBase + off);
    HANDLE th = CreateRemoteThread(hProc, nullptr, 0, remote, (LPVOID)(intptr_t)arg, 0, nullptr);
    if (!th) { wprintf(L"  CreateRemoteThread(%hs) failed (%lu)\n", fn, GetLastError()); return false; }
    WaitForSingleObject(th, INFINITE);
    CloseHandle(th);
    wprintf(L"  called %hs(%ld)\n", fn, arg);
    return true;
}

int wmain(int argc, wchar_t** argv) {
    if (argc < 2) {
        wprintf(L"usage: inject <dll-path> [--target proc.exe] "
                L"[--active|--passthrough] [--grip-right N] [--grip-left N]\n");
        return 2;
    }
    const wchar_t* dll = argv[1];
    const wchar_t* proc = L"echovr.exe";

    // Parse controls.
    struct { bool set; long v; } passthrough{false,0}, gr{false,1}, gl{false,1},
                                 ip{false,1}, pt{false,0}, la{false,1};
    for (int i = 2; i < argc; ++i) {
        if (!wcscmp(argv[i], L"--target") && i + 1 < argc) proc = argv[++i];
        else if (!wcscmp(argv[i], L"--active"))      { passthrough.set = true; passthrough.v = 0; }
        else if (!wcscmp(argv[i], L"--passthrough")) { passthrough.set = true; passthrough.v = 1; }
        else if (!wcscmp(argv[i], L"--grip-right"))  { gr.set = true; if (i+1<argc && iswdigit(argv[i+1][0])) gr.v = _wtol(argv[++i]); }
        else if (!wcscmp(argv[i], L"--grip-left"))   { gl.set = true; if (i+1<argc && iswdigit(argv[i+1][0])) gl.v = _wtol(argv[++i]); }
        else if (!wcscmp(argv[i], L"--inject-pose")) { ip.set = true; if (i+1<argc && iswdigit(argv[i+1][0])) ip.v = _wtol(argv[++i]); }
        else if (!wcscmp(argv[i], L"--pose-test"))   { pt.set = true; if (i+1<argc && iswdigit(argv[i+1][0])) pt.v = _wtol(argv[++i]); }
        else if (!wcscmp(argv[i], L"--log-api"))     { la.set = true; if (i+1<argc && iswdigit(argv[i+1][0])) la.v = _wtol(argv[++i]); }
        else { wprintf(L"unknown arg: %ls\n", argv[i]); return 2; }
    }

    wchar_t full[MAX_PATH];
    if (!GetFullPathNameW(dll, MAX_PATH, full, nullptr) ||
        GetFileAttributesW(full) == INVALID_FILE_ATTRIBUTES) {
        wprintf(L"dll not found: %ls\n", dll); return 1;
    }
    // Bare filename, for the module lookup in the target.
    const wchar_t* dllName = wcsrchr(full, L'\\');
    dllName = dllName ? dllName + 1 : full;

    DWORD pid = findPid(proc);
    if (!pid) { wprintf(L"process not found: %ls (is the game running?)\n", proc); return 1; }
    wprintf(L"target %ls pid=%lu\n", proc, pid);

    HANDLE h = OpenProcess(PROCESS_CREATE_THREAD | PROCESS_VM_OPERATION |
                           PROCESS_VM_WRITE | PROCESS_VM_READ | PROCESS_QUERY_INFORMATION,
                           FALSE, pid);
    if (!h) { wprintf(L"OpenProcess failed (%lu). Try an elevated shell.\n", GetLastError()); return 1; }

    // Inject (idempotent: if already loaded, LoadLibraryW returns the existing module).
    SIZE_T bytes = (wcslen(full) + 1) * sizeof(wchar_t);
    void* remote = VirtualAllocEx(h, nullptr, bytes, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    WriteProcessMemory(h, remote, full, bytes, nullptr);
    auto loadLib = (LPTHREAD_START_ROUTINE)GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "LoadLibraryW");
    HANDLE th = CreateRemoteThread(h, nullptr, 0, loadLib, remote, 0, nullptr);
    if (!th) { wprintf(L"injection failed (%lu)\n", GetLastError()); VirtualFreeEx(h, remote, 0, MEM_RELEASE); CloseHandle(h); return 1; }
    WaitForSingleObject(th, INFINITE);
    CloseHandle(th);
    VirtualFreeEx(h, remote, 0, MEM_RELEASE);
    wprintf(L"injected %ls\n", dllName);

    // Apply controls, if any.
    if (passthrough.set || gr.set || gl.set || ip.set || pt.set || la.set) {
        uintptr_t remoteBase = findModuleBase(pid, dllName);
        // Load our own copy only to read export offsets. DONT_RESOLVE_DLL_REFERENCES
        // means DllMain does NOT run here, so the injector never pollutes the log
        // (which lives next to the DLL) with its own load/unload events.
        HMODULE localDll = LoadLibraryExW(full, nullptr, DONT_RESOLVE_DLL_REFERENCES);
        if (!remoteBase || !localDll) {
            wprintf(L"could not locate the DLL for control calls "
                    L"(base=%p local=%p)\n", (void*)remoteBase, (void*)localDll);
        } else {
            if (passthrough.set) callExport(h, remoteBase, localDll, "agent_set_passthrough",      passthrough.v);
            if (gr.set)          callExport(h, remoteBase, localDll, "agent_set_force_grip_right", gr.v);
            if (gl.set)          callExport(h, remoteBase, localDll, "agent_set_force_grip_left",  gl.v);
            if (ip.set)          callExport(h, remoteBase, localDll, "agent_set_inject_pose",      ip.v);
            if (pt.set)          callExport(h, remoteBase, localDll, "agent_set_pose_test",        pt.v);
            if (la.set)          callExport(h, remoteBase, localDll, "agent_set_log_api",          la.v);
        }
        if (localDll) FreeLibrary(localDll);
    }

    // The DLL writes its log to the first writable of: its own folder, the user
    // profile, then TEMP. Program Files usually is not writable, so the profile
    // is the common landing spot. The log's own first line states its real path.
    wchar_t logdir[MAX_PATH]; wcscpy(logdir, full);
    wchar_t* s = wcsrchr(logdir, L'\\'); if (s) *(s + 1) = 0;
    wchar_t prof[MAX_PATH]; DWORD m = GetEnvironmentVariableW(L"USERPROFILE", prof, MAX_PATH);
    wprintf(L"log is one of (see its first line for the real path):\n");
    wprintf(L"  %lsecho_agent.log\n", logdir);
    if (m > 0 && m < MAX_PATH) wprintf(L"  %ls\\echo_agent.log\n", prof);
    CloseHandle(h);
    return 0;
}
