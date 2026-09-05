cmake_minimum_required(VERSION 3.15)
project(echovr_agent CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# 64-bit only; echovr.exe is x86_64 and the DLL must match the target process.
if(NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
  message(FATAL_ERROR "Build 64-bit: echovr.exe is x64 and the DLL bitness must match.")
endif()

add_library(echovr_agent SHARED dllmain.cpp)
set_target_properties(echovr_agent PROPERTIES PREFIX "")   # echovr_agent.dll, not libechovr_agent.dll

add_executable(inject inject.cpp)

# Static-link the MinGW runtime so neither artifact needs libgcc/libstdc++ DLLs
# present on the target machine.
if(MINGW)
  target_link_options(echovr_agent PRIVATE -static -static-libgcc -static-libstdc++)
  target_link_options(inject       PRIVATE -static -static-libgcc -static-libstdc++)
endif()
