@echo off
call "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat"
cd /d "%~dp0"
cl /LD /EHsc /std:c++17 draw_dll.cpp d3d12.lib d3dcompiler.lib /Fe:draw_dll_v5.dll
