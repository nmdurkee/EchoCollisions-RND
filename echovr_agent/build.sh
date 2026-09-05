#!/usr/bin/env bash
# Build the single self-contained echo_agent.exe (DLL embedded inside).
# Requires MinGW-w64 g++ on PATH and python.
set -e
cd "$(dirname "$0")"

echo "[1/3] compiling DLL..."
g++ -std=c++17 -O2 -shared dllmain.cpp -o echovr_agent.dll \
    -static -static-libgcc -static-libstdc++ -lwinhttp

echo "[2/3] embedding DLL..."
python -c "d=open('echovr_agent.dll','rb').read(); \
f=open('embedded_dll.h','w'); \
f.write('#pragma once\nstatic const unsigned int g_dll_len=%d;\nstatic const unsigned char g_dll[]={'%len(d)); \
f.write(','.join(str(b) for b in d)); f.write('};\n'); f.close(); \
print('  embedded', len(d), 'bytes')"

echo "[3/3] compiling exe..."
g++ -std=c++17 -O2 -municode echo_agent.cpp -o echo_agent.exe \
    -static -static-libgcc -static-libstdc++

rm -f echovr_agent.dll embedded_dll.h        # intermediates; exe is self-contained
echo "done -> echo_agent.exe"
