// Echo VR renderer-hook research - the real native D3D12 draw DLL.
// NOT connected to Echo VR by itself (validated against host.exe first -
// see host_visual.cpp / test_draw_dll_visual.py).
//
// This is the actual draw-implementation module the whole
// "compiled-DLL-injected-via-Frida" pivot (see CLAUDE.md, 2026-08-17) was
// building toward. Two exports:
//
//   InitDraw(ID3D12Device* device) - called ONCE, creates the root
//   signature + PSO (genuine compiled C++ SetPipelineState-adjacent calls,
//   proven reliable via test_dll_d3d12.cpp's shared-device test - 0/30
//   failures). Uses whatever real, already-existing device is handed to
//   it - never creates one itself.
//
//   DrawFrame(ID3D12GraphicsCommandList* cmdList, float width, float
//   height, float x0, float y0, float x1, float y1) - records
//   SetPipelineState/SetGraphicsRootSignature/IASetPrimitiveTopology/
//   RSSetViewports/RSSetScissorRects/SetGraphicsRoot32BitConstants/
//   DrawInstanced(2,1,0,0) onto an ALREADY-OPEN command list. Deliberately
//   does NOT touch resource barriers, render-target binding, Close(), or
//   ExecuteCommandLists - the CALLER owns the command list's lifecycle
//   (matches the "splice a few calls onto someone else's already-recording
//   list" shape needed for the eventual live-game use case, and lets this
//   exact same function be used identically for isolated testing on our
//   own owned list).
//
// (x0,y0)-(x1,y1) are NDC coordinates (-1..1), matching every other native
// D3D12 test this session.
//
// Build: cl /LD /EHsc /std:c++17 draw_dll.cpp d3d12.lib d3dcompiler.lib /Fe:draw_dll.dll

#include <windows.h>
#include <d3d12.h>
#include <d3dcompiler.h>
#include <wrl/client.h>
#include <cstring>
#include <cmath>

#pragma comment(lib, "d3d12.lib")
#pragma comment(lib, "d3dcompiler.lib")

using Microsoft::WRL::ComPtr;

static const char* VS_SRC =
    "cbuffer RootConstants : register(b0) { float4 endpoints; };"
    "struct VSOutput { float4 pos : SV_POSITION; float4 color : COLOR; };"
    "VSOutput main(uint vid : SV_VertexID) { VSOutput o;"
    "  if (vid == 0) o.pos = float4(endpoints.x, endpoints.y, 0.0, 1.0);"
    "  else o.pos = float4(endpoints.z, endpoints.w, 0.0, 1.0);"
    "  o.color = float4(0.0, 1.0, 0.0, 1.0);"
    "  return o; }";

static const char* PS_SRC =
    "struct PSInput { float4 pos : SV_POSITION; float4 color : COLOR; };"
    "float4 main(PSInput input) : SV_TARGET { return input.color; }";

// D3D12 hardware-rasterized LINELIST is effectively a 1-pixel hairline on
// most drivers (line width state is not reliably honored) - too thin to be
// a usable trainer overlay. This second shader/PSO draws an actual filled
// quad (2 triangles) instead, with the 4 corner positions computed on the
// CPU side (draw_dll.cpp, not the shader) - see DrawThickLineStereo below.
// Passing 4 already-computed corners as 2x float4 root constants avoids any
// perpendicular/aspect-ratio math in HLSL, keeping the shader trivial.
static const char* VS_QUAD_SRC =
    "cbuffer RootConstants : register(b0) { float4 corner01; float4 corner23; float4 colorConst; };"
    "struct VSOutput { float4 pos : SV_POSITION; float4 color : COLOR; };"
    "VSOutput main(uint vid : SV_VertexID) { VSOutput o; float2 p;"
    "  uint idx[6] = { 0, 1, 2, 1, 3, 2 };"
    "  uint c = idx[vid];"
    "  if (c == 0) p = corner01.xy;"
    "  else if (c == 1) p = corner01.zw;"
    "  else if (c == 2) p = corner23.xy;"
    "  else p = corner23.zw;"
    "  o.pos = float4(p, 0.0, 1.0);"
    "  o.color = colorConst;"
    "  return o; }";

static ID3D12RootSignature* g_rootSignature = nullptr;
static ID3D12PipelineState* g_pipelineState = nullptr;
static ID3D12RootSignature* g_quadRootSignature = nullptr;
static ID3D12PipelineState* g_quadPipelineState = nullptr;

static volatile LONG g_lastStage = 0;
extern "C" __declspec(dllexport) int __stdcall GetDrawLastStage() { return g_lastStage; }

// Returns 0 on success, negative on failure (see stage comments). Safe to
// call multiple times - re-initializes if called again.
extern "C" __declspec(dllexport) int __stdcall InitDraw(ID3D12Device* device) {
    if (!device) return -100;
    g_lastStage = 1;

    D3D12_ROOT_PARAMETER rootParam = {};
    rootParam.ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    rootParam.Constants.ShaderRegister = 0;
    rootParam.Constants.RegisterSpace = 0;
    rootParam.Constants.Num32BitValues = 4;
    rootParam.ShaderVisibility = D3D12_SHADER_VISIBILITY_VERTEX;

    D3D12_ROOT_SIGNATURE_DESC rsDesc = {};
    rsDesc.NumParameters = 1;
    rsDesc.pParameters = &rootParam;
    rsDesc.Flags = D3D12_ROOT_SIGNATURE_FLAG_NONE;

    g_lastStage = 2;
    ComPtr<ID3DBlob> rsBlob, rsError;
    if (FAILED(D3D12SerializeRootSignature(&rsDesc, D3D_ROOT_SIGNATURE_VERSION_1, &rsBlob, &rsError)))
        return -1;

    g_lastStage = 3;
    ComPtr<ID3D12RootSignature> rootSignature;
    if (FAILED(device->CreateRootSignature(0, rsBlob->GetBufferPointer(), rsBlob->GetBufferSize(),
                                            IID_PPV_ARGS(&rootSignature))))
        return -2;

    g_lastStage = 4;
    ComPtr<ID3DBlob> vsBlob, psBlob, err;
    if (FAILED(D3DCompile(VS_SRC, strlen(VS_SRC), nullptr, nullptr, nullptr, "main", "vs_5_0", 0, 0, &vsBlob, &err)))
        return -3;
    if (FAILED(D3DCompile(PS_SRC, strlen(PS_SRC), nullptr, nullptr, nullptr, "main", "ps_5_0", 0, 0, &psBlob, &err)))
        return -4;
    g_lastStage = 5;

    D3D12_GRAPHICS_PIPELINE_STATE_DESC psoDesc = {};
    psoDesc.pRootSignature = rootSignature.Get();
    psoDesc.VS = { vsBlob->GetBufferPointer(), vsBlob->GetBufferSize() };
    psoDesc.PS = { psBlob->GetBufferPointer(), psBlob->GetBufferSize() };
    psoDesc.BlendState.RenderTarget[0].SrcBlend = D3D12_BLEND_ONE;
    psoDesc.BlendState.RenderTarget[0].DestBlend = D3D12_BLEND_ZERO;
    psoDesc.BlendState.RenderTarget[0].BlendOp = D3D12_BLEND_OP_ADD;
    psoDesc.BlendState.RenderTarget[0].SrcBlendAlpha = D3D12_BLEND_ONE;
    psoDesc.BlendState.RenderTarget[0].DestBlendAlpha = D3D12_BLEND_ZERO;
    psoDesc.BlendState.RenderTarget[0].BlendOpAlpha = D3D12_BLEND_OP_ADD;
    psoDesc.BlendState.RenderTarget[0].RenderTargetWriteMask = D3D12_COLOR_WRITE_ENABLE_ALL;
    psoDesc.SampleMask = UINT_MAX;
    psoDesc.RasterizerState.FillMode = D3D12_FILL_MODE_SOLID;
    psoDesc.RasterizerState.CullMode = D3D12_CULL_MODE_NONE;
    psoDesc.RasterizerState.DepthClipEnable = TRUE;
    psoDesc.DepthStencilState.DepthEnable = FALSE;
    psoDesc.DepthStencilState.StencilEnable = FALSE;
    psoDesc.DepthStencilState.DepthFunc = D3D12_COMPARISON_FUNC_ALWAYS;
    psoDesc.DepthStencilState.FrontFace = { D3D12_STENCIL_OP_KEEP, D3D12_STENCIL_OP_KEEP, D3D12_STENCIL_OP_KEEP, D3D12_COMPARISON_FUNC_ALWAYS };
    psoDesc.DepthStencilState.BackFace = psoDesc.DepthStencilState.FrontFace;
    psoDesc.InputLayout = { nullptr, 0 };
    psoDesc.PrimitiveTopologyType = D3D12_PRIMITIVE_TOPOLOGY_TYPE_LINE;
    psoDesc.NumRenderTargets = 1;
    psoDesc.RTVFormats[0] = DXGI_FORMAT_R8G8B8A8_UNORM;
    psoDesc.SampleDesc.Count = 1;

    g_lastStage = 6;
    ComPtr<ID3D12PipelineState> pso;
    if (FAILED(device->CreateGraphicsPipelineState(&psoDesc, IID_PPV_ARGS(&pso))))
        return -5;
    g_lastStage = 7;

    // Deliberately leak these two refs for the DLL's lifetime (simplest
    // correct pattern for a "create once, use forever" global - matches
    // the pattern used throughout this whole project of not bothering
    // with explicit Release() for objects that live as long as the
    // process/injection does).
    if (g_rootSignature) g_rootSignature->Release();
    if (g_pipelineState) g_pipelineState->Release();
    rootSignature->AddRef();
    pso->AddRef();
    g_rootSignature = rootSignature.Get();
    g_pipelineState = pso.Get();

    g_lastStage = 8;

    // Second pipeline: filled quad (thick line), TRIANGLELIST, 12 root
    // constants (corner01 float4 + corner23 float4 + colorConst float4).
    D3D12_ROOT_PARAMETER quadRootParam = {};
    quadRootParam.ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    quadRootParam.Constants.ShaderRegister = 0;
    quadRootParam.Constants.RegisterSpace = 0;
    quadRootParam.Constants.Num32BitValues = 12;
    quadRootParam.ShaderVisibility = D3D12_SHADER_VISIBILITY_VERTEX;

    D3D12_ROOT_SIGNATURE_DESC quadRsDesc = {};
    quadRsDesc.NumParameters = 1;
    quadRsDesc.pParameters = &quadRootParam;
    quadRsDesc.Flags = D3D12_ROOT_SIGNATURE_FLAG_NONE;

    g_lastStage = 9;
    ComPtr<ID3DBlob> quadRsBlob, quadRsError;
    if (FAILED(D3D12SerializeRootSignature(&quadRsDesc, D3D_ROOT_SIGNATURE_VERSION_1, &quadRsBlob, &quadRsError)))
        return -6;

    g_lastStage = 10;
    ComPtr<ID3D12RootSignature> quadRootSignature;
    if (FAILED(device->CreateRootSignature(0, quadRsBlob->GetBufferPointer(), quadRsBlob->GetBufferSize(),
                                            IID_PPV_ARGS(&quadRootSignature))))
        return -7;

    g_lastStage = 11;
    ComPtr<ID3DBlob> quadVsBlob, quadErr;
    if (FAILED(D3DCompile(VS_QUAD_SRC, strlen(VS_QUAD_SRC), nullptr, nullptr, nullptr, "main", "vs_5_0", 0, 0, &quadVsBlob, &quadErr)))
        return -8;
    g_lastStage = 12;

    D3D12_GRAPHICS_PIPELINE_STATE_DESC quadPsoDesc = {};
    quadPsoDesc.pRootSignature = quadRootSignature.Get();
    quadPsoDesc.VS = { quadVsBlob->GetBufferPointer(), quadVsBlob->GetBufferSize() };
    quadPsoDesc.PS = { psBlob->GetBufferPointer(), psBlob->GetBufferSize() };  // reuse the same trivial pixel shader
    quadPsoDesc.BlendState.RenderTarget[0].SrcBlend = D3D12_BLEND_ONE;
    quadPsoDesc.BlendState.RenderTarget[0].DestBlend = D3D12_BLEND_ZERO;
    quadPsoDesc.BlendState.RenderTarget[0].BlendOp = D3D12_BLEND_OP_ADD;
    quadPsoDesc.BlendState.RenderTarget[0].SrcBlendAlpha = D3D12_BLEND_ONE;
    quadPsoDesc.BlendState.RenderTarget[0].DestBlendAlpha = D3D12_BLEND_ZERO;
    quadPsoDesc.BlendState.RenderTarget[0].BlendOpAlpha = D3D12_BLEND_OP_ADD;
    quadPsoDesc.BlendState.RenderTarget[0].RenderTargetWriteMask = D3D12_COLOR_WRITE_ENABLE_ALL;
    quadPsoDesc.SampleMask = UINT_MAX;
    quadPsoDesc.RasterizerState.FillMode = D3D12_FILL_MODE_SOLID;
    quadPsoDesc.RasterizerState.CullMode = D3D12_CULL_MODE_NONE;
    quadPsoDesc.RasterizerState.DepthClipEnable = TRUE;
    quadPsoDesc.DepthStencilState.DepthEnable = FALSE;
    quadPsoDesc.DepthStencilState.StencilEnable = FALSE;
    quadPsoDesc.DepthStencilState.DepthFunc = D3D12_COMPARISON_FUNC_ALWAYS;
    quadPsoDesc.DepthStencilState.FrontFace = { D3D12_STENCIL_OP_KEEP, D3D12_STENCIL_OP_KEEP, D3D12_STENCIL_OP_KEEP, D3D12_COMPARISON_FUNC_ALWAYS };
    quadPsoDesc.DepthStencilState.BackFace = quadPsoDesc.DepthStencilState.FrontFace;
    quadPsoDesc.InputLayout = { nullptr, 0 };
    quadPsoDesc.PrimitiveTopologyType = D3D12_PRIMITIVE_TOPOLOGY_TYPE_TRIANGLE;
    quadPsoDesc.NumRenderTargets = 1;
    quadPsoDesc.RTVFormats[0] = DXGI_FORMAT_R8G8B8A8_UNORM;
    quadPsoDesc.SampleDesc.Count = 1;

    g_lastStage = 13;
    ComPtr<ID3D12PipelineState> quadPso;
    if (FAILED(device->CreateGraphicsPipelineState(&quadPsoDesc, IID_PPV_ARGS(&quadPso))))
        return -9;
    g_lastStage = 14;

    if (g_quadRootSignature) g_quadRootSignature->Release();
    if (g_quadPipelineState) g_quadPipelineState->Release();
    quadRootSignature->AddRef();
    quadPso->AddRef();
    g_quadRootSignature = quadRootSignature.Get();
    g_quadPipelineState = quadPso.Get();

    g_lastStage = 15;
    return 0;
}

extern "C" __declspec(dllexport) int __stdcall DrawFrame(ID3D12GraphicsCommandList* cmdList,
                                                            float width, float height,
                                                            float x0, float y0, float x1, float y1) {
    if (!cmdList || !g_rootSignature || !g_pipelineState) return -1;

    cmdList->SetPipelineState(g_pipelineState);
    cmdList->SetGraphicsRootSignature(g_rootSignature);
    cmdList->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_LINELIST);

    D3D12_VIEWPORT viewport = { 0, 0, width, height, 0.0f, 1.0f };
    cmdList->RSSetViewports(1, &viewport);
    D3D12_RECT scissor = { 0, 0, (LONG)width, (LONG)height };
    cmdList->RSSetScissorRects(1, &scissor);

    float endpoints[4] = { x0, y0, x1, y1 };
    cmdList->SetGraphicsRoot32BitConstants(0, 4, endpoints, 0);

    cmdList->DrawInstanced(2, 1, 0, 0);
    return 0;
}

// Stereo-aware draw: the confirmed real headset-visible target
// (2688x1600, DXGI_FORMAT_R8G8B8A8_UNORM_SRGB, found 2026-08-17 via
// recon_echovr_correlate_rtv.py + confirmed live) is a SIDE-BY-SIDE
// stereo buffer - left eye in the left half, right eye in the right
// half. Plain DrawFrame() draws one line across the FULL width in NDC
// space, which straddles the eye-split boundary - each eye then shows a
// different, independently lens-warped fragment of that single line,
// so the two eyes see non-corresponding images (confirmed live: "2 lines,
// not matching"). This draws the SAME line, in LOCAL per-eye NDC space
// (-1..1 within each eye's own half), into both halves separately via
// two separate viewports/scissors - gives both eyes an identical
// relative screen position (zero disparity / infinite-convergence HUD
// style), which is what actually fuses into one line perceptually.
// (x0,y0)-(x1,y1) are NDC coordinates local to ONE eye's half, not the
// full buffer.
// eyeOffsetNDC: empirical convergence tuning (2026-08-17) - identical
// zero-disparity content in each eye's local NDC space did NOT visually
// fuse live (user report: "one slightly to the right and one slightly to
// the left", not crossed - i.e. a genuine disparity/convergence mismatch,
// not a left/right eye swap). Positive eyeOffsetNDC shifts the LEFT eye's
// content rightward (toward the shared boundary) and the RIGHT eye's
// content leftward by the same amount - i.e. converging/crossing
// disparity. Negative diverges. Units are local per-eye NDC (-1..1 spans
// one eye's own half-width), so 0.1 is roughly 5% of one eye's width.
extern "C" __declspec(dllexport) int __stdcall DrawFrameStereo(ID3D12GraphicsCommandList* cmdList,
                                                                   float fullWidth, float fullHeight,
                                                                   float x0, float y0, float x1, float y1,
                                                                   float eyeOffsetNDC) {
    if (!cmdList || !g_rootSignature || !g_pipelineState) return -1;

    cmdList->SetPipelineState(g_pipelineState);
    cmdList->SetGraphicsRootSignature(g_rootSignature);
    cmdList->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_LINELIST);

    float eyeWidth = fullWidth / 2.0f;

    float leftEndpoints[4] = { x0 + eyeOffsetNDC, y0, x1 + eyeOffsetNDC, y1 };
    D3D12_VIEWPORT vpLeft = { 0.0f, 0.0f, eyeWidth, fullHeight, 0.0f, 1.0f };
    cmdList->RSSetViewports(1, &vpLeft);
    D3D12_RECT scLeft = { 0, 0, (LONG)eyeWidth, (LONG)fullHeight };
    cmdList->RSSetScissorRects(1, &scLeft);
    cmdList->SetGraphicsRoot32BitConstants(0, 4, leftEndpoints, 0);
    cmdList->DrawInstanced(2, 1, 0, 0);

    float rightEndpoints[4] = { x0 - eyeOffsetNDC, y0, x1 - eyeOffsetNDC, y1 };
    D3D12_VIEWPORT vpRight = { eyeWidth, 0.0f, eyeWidth, fullHeight, 0.0f, 1.0f };
    cmdList->RSSetViewports(1, &vpRight);
    D3D12_RECT scRight = { (LONG)eyeWidth, 0, (LONG)fullWidth, (LONG)fullHeight };
    cmdList->RSSetScissorRects(1, &scRight);
    cmdList->SetGraphicsRoot32BitConstants(0, 4, rightEndpoints, 0);
    cmdList->DrawInstanced(2, 1, 0, 0);

    return 0;
}

// Computes a screen-space-correct perpendicular offset for (x0,y0)-(x1,y1)
// given an eye's pixel dimensions (eyeWidthPx, heightPx), so the resulting
// quad has a uniform on-screen thickness regardless of the line's angle or
// the buffer's aspect ratio (NDC x and y do NOT have the same pixel scale
// for a non-square viewport, so a naive NDC-space perpendicular would look
// stretched). Writes 4 corners (c0..c3, each x,y) into outCorners[8].
static void ComputeQuadCorners(float x0, float y0, float x1, float y1,
                                float eyeWidthPx, float heightPx,
                                float thicknessPixels, float outCorners[8]) {
    float halfW = eyeWidthPx / 2.0f;
    float halfH = heightPx / 2.0f;

    // Direction in real pixel space.
    float pdx = (x1 - x0) * halfW;
    float pdy = (y1 - y0) * halfH;
    float len = sqrtf(pdx * pdx + pdy * pdy);
    if (len < 0.0001f) { pdx = 1.0f; pdy = 0.0f; len = 1.0f; }
    pdx /= len; pdy /= len;

    // Perpendicular in pixel space, scaled to half the desired thickness.
    float perpPx = -pdy * (thicknessPixels / 2.0f);
    float perpPy = pdx * (thicknessPixels / 2.0f);

    // Convert perpendicular offset back to NDC units (divide by half-extent).
    float perpNdcX = perpPx / halfW;
    float perpNdcY = perpPy / halfH;

    outCorners[0] = x0 + perpNdcX; outCorners[1] = y0 + perpNdcY;  // c0
    outCorners[2] = x0 - perpNdcX; outCorners[3] = y0 - perpNdcY;  // c1
    outCorners[4] = x1 + perpNdcX; outCorners[5] = y1 + perpNdcY;  // c2
    outCorners[6] = x1 - perpNdcX; outCorners[7] = y1 - perpNdcY;  // c3
}

// Thick, stereo-fused line - the real overlay draw call. Same per-eye
// local-NDC + eyeOffsetNDC convergence model as DrawFrameStereo (see its
// comment above), but renders an actual filled quad (via g_quadPipelineState)
// instead of a 1px hairline, and takes an explicit color (so different
// lines/markers can be visually distinguished later, e.g. the predicted
// path vs. a save-point marker).
extern "C" __declspec(dllexport) int __stdcall DrawThickLineStereo(
        ID3D12GraphicsCommandList* cmdList,
        float fullWidth, float fullHeight,
        float x0, float y0, float x1, float y1,
        float thicknessPixels, float eyeOffsetNDC,
        float r, float g, float b) {
    if (!cmdList || !g_quadRootSignature || !g_quadPipelineState) return -1;

    cmdList->SetPipelineState(g_quadPipelineState);
    cmdList->SetGraphicsRootSignature(g_quadRootSignature);
    cmdList->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);

    float eyeWidth = fullWidth / 2.0f;
    float colorConst[4] = { r, g, b, 1.0f };

    float leftCorners[8];
    ComputeQuadCorners(x0 + eyeOffsetNDC, y0, x1 + eyeOffsetNDC, y1, eyeWidth, fullHeight, thicknessPixels, leftCorners);
    D3D12_VIEWPORT vpLeft = { 0.0f, 0.0f, eyeWidth, fullHeight, 0.0f, 1.0f };
    cmdList->RSSetViewports(1, &vpLeft);
    D3D12_RECT scLeft = { 0, 0, (LONG)eyeWidth, (LONG)fullHeight };
    cmdList->RSSetScissorRects(1, &scLeft);
    cmdList->SetGraphicsRoot32BitConstants(0, 8, leftCorners, 0);
    cmdList->SetGraphicsRoot32BitConstants(0, 4, colorConst, 8);
    cmdList->DrawInstanced(6, 1, 0, 0);

    float rightCorners[8];
    ComputeQuadCorners(x0 - eyeOffsetNDC, y0, x1 - eyeOffsetNDC, y1, eyeWidth, fullHeight, thicknessPixels, rightCorners);
    D3D12_VIEWPORT vpRight = { eyeWidth, 0.0f, eyeWidth, fullHeight, 0.0f, 1.0f };
    cmdList->RSSetViewports(1, &vpRight);
    D3D12_RECT scRight = { (LONG)eyeWidth, 0, (LONG)fullWidth, (LONG)fullHeight };
    cmdList->RSSetScissorRects(1, &scRight);
    cmdList->SetGraphicsRoot32BitConstants(0, 8, rightCorners, 0);
    cmdList->SetGraphicsRoot32BitConstants(0, 4, colorConst, 8);
    cmdList->DrawInstanced(6, 1, 0, 0);

    return 0;
}

// True per-eye stereo: unlike DrawThickLineStereo (one shared NDC line +
// hand-tuned eyeOffsetNDC convergence, which only looks right at the one
// depth it was tuned against), this takes ALREADY-INDEPENDENTLY-PROJECTED
// NDC endpoints for EACH eye (computed on the Python side from two real,
// separate eye camera positions - head position offset by +-IPD/2 along
// the head's own "left" vector). Real per-eye disparity varies correctly
// with depth, which is what actually makes an object read as embedded in
// 3D space instead of a flat HUD overlay. See test_live_3d_trajectory.py.
extern "C" __declspec(dllexport) int __stdcall DrawThickLineStereoIndependent(
        ID3D12GraphicsCommandList* cmdList,
        float fullWidth, float fullHeight,
        float leftX0, float leftY0, float leftX1, float leftY1,
        float rightX0, float rightY0, float rightX1, float rightY1,
        float thicknessPixels,
        float r, float g, float b) {
    if (!cmdList || !g_quadRootSignature || !g_quadPipelineState) return -1;

    cmdList->SetPipelineState(g_quadPipelineState);
    cmdList->SetGraphicsRootSignature(g_quadRootSignature);
    cmdList->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);

    float eyeWidth = fullWidth / 2.0f;
    float colorConst[4] = { r, g, b, 1.0f };

    float leftCorners[8];
    ComputeQuadCorners(leftX0, leftY0, leftX1, leftY1, eyeWidth, fullHeight, thicknessPixels, leftCorners);
    D3D12_VIEWPORT vpLeft = { 0.0f, 0.0f, eyeWidth, fullHeight, 0.0f, 1.0f };
    cmdList->RSSetViewports(1, &vpLeft);
    D3D12_RECT scLeft = { 0, 0, (LONG)eyeWidth, (LONG)fullHeight };
    cmdList->RSSetScissorRects(1, &scLeft);
    cmdList->SetGraphicsRoot32BitConstants(0, 8, leftCorners, 0);
    cmdList->SetGraphicsRoot32BitConstants(0, 4, colorConst, 8);
    cmdList->DrawInstanced(6, 1, 0, 0);

    float rightCorners[8];
    ComputeQuadCorners(rightX0, rightY0, rightX1, rightY1, eyeWidth, fullHeight, thicknessPixels, rightCorners);
    D3D12_VIEWPORT vpRight = { eyeWidth, 0.0f, eyeWidth, fullHeight, 0.0f, 1.0f };
    cmdList->RSSetViewports(1, &vpRight);
    D3D12_RECT scRight = { (LONG)eyeWidth, 0, (LONG)fullWidth, (LONG)fullHeight };
    cmdList->RSSetScissorRects(1, &scRight);
    cmdList->SetGraphicsRoot32BitConstants(0, 8, rightCorners, 0);
    cmdList->SetGraphicsRoot32BitConstants(0, 4, colorConst, 8);
    cmdList->DrawInstanced(6, 1, 0, 0);

    return 0;
}

BOOL APIENTRY DllMain(HMODULE, DWORD, LPVOID) {
    return TRUE;
}
