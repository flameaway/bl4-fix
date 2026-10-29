# Borderlands 4: what causes the hitches and lag (2026-10-24)

An investigation of frame-time hitches in a long-running game session, done with an external
sampling profiler written for it (`dev/perf/`). The game was build 26007962 (v1.11, after the
Denuvo removal), on an RTX 3080 Ti with 32 GB of RAM, windowed at 2090x1306. Settings were DLSS
Balanced, frame generation on and Reflex on. The session had been running about 13 h, with our
mods loaded and several other test sessions using the same game.

**Confidence labels:**
- **[measured]** comes from samples or timings in this session.
- **[verified]** is a fix tested live, before and after.
- **[inferred]** fits the evidence but isn't proven.

## TL;DR, ranked by frozen time

| # | Cause | Typical cost | Fix | Status |
|---|---|---|---|---|
| 1 | Frame generation re-created by the third-party **DLSSG SM86** enabler (`version.dll`, DLSS-FG on RTX 30) on every frame-gen off→on: closing a menu (the game turns FG off in menus) and the window regaining focus. Each rebuild recreates its CUDA kernels on the RHI thread inside Present. | 120-960 ms freeze, ~1-1.5 per minute of active play. 307 in this session. | `bl4_perf` "Frame generation stays on in menus" (`gbx.GbxFrameGenDisableMode 0`) removes the menu-close rebuilds; focus changes remain. Or turn frame generation off. Set the enabler's `[Logging] Level=1` (it was at 3, trace level). | [verified] trigger; option tested on 1 unpause |
| 2 | **Windows input calls stalling** while the desktop is streamed (Sunshine + MTT Virtual Display Driver, whose WUDFHost spun a full core) | `GetAsyncKeyState` / `GetKeyState` / `GetCursorPos` up to ~100 ms each. 55% of the game thread, ~13 fps for 30+ min. | Mods no longer poll keys on the game thread: one worker thread, only while the game is in front (done by the mod-perf session). Environment: stop the stream / virtual display when not needed. | [verified] 13 → 50.7 fps under the same streaming |
| 3 | **Python thread state built and torn down per hook call** (pyunrealsdk / pybind11 on the game thread) | ~1-2 ms every frame (VirtualAlloc/Free, `PyThreadState_Clear`). 113k thread states in 13 h. | `bl4_perf` "Faster Python hooks": pin one thread state | [verified] |
| 4 | **GIL convoys**: the game thread waiting for Python's lock held by mod worker threads (thread-per-request web server, GIL-releasing ctypes polls) | 1.4-2.1 ms/frame average with mod pages open; single stalls of 190+ ms | Mod changes (persistent server threads, poll less) plus a 0.5 ms switch interval | [verified] 2.1 → 0.3 ms/frame |
| 5 | **Hard page faults** after Windows trims the game's working set. Game commit is 25-27 GB on 32 GB of RAM; ~15 GB is held by other programs. | Up to 274 ms (game thread parked on a non-resident code page). ~0.5-1.1 s per 15 min. | Close RAM-heavy programs. Optional `bl4_perf` "Keep game memory in RAM". | [measured] Fix untested |
| 6 | **Memory growth**: commit +2.1 GB in 48 min. One identified part: copies of the Mod Menu's state JSON kept by the page side (~24 per minute, ~620 MB per session). | Feeds #5 | Mod Menu answers "same" when unchanged (done by the mod-perf session) | [verified] new JSON copies 1,280 per 45 min → 2 per 12 min; all new 64 KB blocks ~4.7× fewer |
| 7 | Our mods' per-frame Python cost | ~2.65 ms/frame of the game thread in steady play (before the gating fixes) | Per-frame gates, etc. (mod-perf session) | [measured] |
| 8 | Small items: Steam overlay `PeekMessage` hook, Gearbox `bifrost` telemetry walking the object array | 33-56 ms and ~37 ms, rare | none | [measured] |

**Ruled out:**
- **Garbage collection.** A full GC runs every ~61 s, with incremental reachability off. Each one is one sample inside a normal-length frame (parallel GC is fast here).
- **The DLSS upscaler.** It was rebuilt only once in 15 minutes.
- **Shader/PSO compilation.** Precaching is on, and no PSO compile showed up on the game, render or RHI threads in any slow frame. The community's #1 cause doesn't show in this session (the shader cache is warm after 13 h).

In a steady scene the game thread is GPU-bound. At 65 fps (15.2 ms per frame) it spends:
- 6.2 ms in the engine tick;
- 2.65 ms in our mods' hooks (part of the engine tick);
- 6.7 ms idle in NVIDIA Reflex pacing.

## 1. Tools written for this (`dev/perf/`)

None of them inject anything or write to the game. `.perf/` (gitignored) holds the captures.

| Tool | What it does |
|---|---|
| `sampler.py <pid> --seconds N --rate R --threads GameThread,RHIThread,... --out x.pkl.gz` | Suspends each chosen thread for ~0.1 ms. It copies the registers and stack, unwinds offline from each module's `.pdata`, and tags every sample with the game frame by reading `GFrameCounter` (RVA `0xC761E08`, checked against `KismetSystemLibrary.GetFrameCount`). It also records whether the sampled code page was resident, to detect hard page faults. 1 kHz costs ~5-10% of the game thread. |
| `analyze.py x.pkl.gz` | Frame-time percentiles and slow frames, and per thread what's over-represented in slow frames. Also the dominant stack of the worst frames. Options: `--share A,B` (ms per frame in functions matching each name), `--callers` / `--callees NAME`, `--events events.json` (timeline), `--folded` (flame graph). |
| `symbols.py` + `dump_uobjects.py` + `names.json` | Names for addresses in a symbol-less exe. Sources: `UFunction::Func` thunks (`Class::execFoo`), class vtables (named after the most basic class with that pointer, e.g. `GameEngine::vt[92]`), strings referenced by the function, hand-named engine loop functions, and exports for DLLs. |
| `frame_stacks.py x.pkl.gz RHIThread --min-ms 300` | Dominant stacks of one thread in the big frames. |
| `events.py` (bridge) | Logs pause / cursor / HUD / world changes with QPC timestamps, so hitches line up with what the player did. |
| `vmmap.py <pid> [--save a.json] [--diff a.json] [--resident]` | Committed-memory map: by type and allocation size, what's resident, and what's new since a snapshot. |
| `memlog.py <pid> out.csv` | Commit, working set, page faults, handles and system commit every N seconds. |
| `trim_watch.py <pid>` | On a sudden working-set drop, captures every game thread to see whether a game thread did it. |
| `who_holds.py <pid>` | Every process holding a handle to the game, with its access rights. |
| `sm86_watch.py <log>` | Timestamps DlssgSm86 frame-generation re-creations as they're logged. |
| `pin_tstate.py` (bridge) | Dev version of the Python thread-state pin. |

UE Trace / Unreal Insights is compiled out of this shipping build: there's no `CpuProfiler` channel, and no `Trace.Start` / `csvprofile` / `startfpschart` commands. That's why we sample from outside.

## 2. Python thread-state churn (fixed, `bl4_perf`)

- **What the samples show.** 8.7% of game-thread samples were in `PyThread_detach_thread` / `PyThreadState_Clear` under `pyunrealsdk`, and 6.7% + 3.2% were in `NtFreeVirtualMemory` / `NtAllocateVirtualMemory` called from them. At 65 fps that's ~1.3-2 ms per frame.
- **Proof.** `PyThreadState_Get()` returned a different address on every bridge call made on the same OS thread (40260). After 13 h the game thread's `threading` name was `Dummy-113366`: a new `_DummyThread` for every fresh thread state.
- **Cause.** pybind11's `gil_scoped_acquire` finds no thread state for the game thread, creates one, and deletes it when the outermost scope exits, on every hook call.
- **Fix.** One `PyGILState_Ensure()` on the game thread raises the state's `gilstate_counter`, so it is never deleted. pybind11 still releases the GIL after each call.
- **Result** [verified]:
  - The thread state is stable.
  - Thread-state churn fell to 0% of samples.
  - pyunrealsdk fell from 3.16 to 2.11 ms/frame, python314 from 2.91 to 1.86.
  - Disable/enable restores and re-pins correctly, from the game thread and from a worker thread (deferred to the next tick).
- **Note.** This belongs in every SDK setup; it would also be worth reporting upstream to pyunrealsdk.

## 3. Frame generation re-creation (the biggest freezes)

**Evidence** [measured]:
- In 21 frames of 300-960 ms over 15 min, 57% of RHI samples were inside `NVSDK_NGX_D3D12_CreateFeature`.
- The path is `FD3D12Viewport::PresentInternal` → `sl.interposer` → Streamline's `sl.dlss_g` plugin (`ProgramData\NVIDIA\NGX\models\sl_dlss_g_0\...\170_E658703.dll`) → `nvngx_dlssg.dll` + `sm86_backend.dll` → `nvcuda64`.
- Those two DLLs come from `AppData\Local\DlssgSm86\bundles`, deployed by `OakGame\Binaries\Win64\version.dll` + `dlssg_sm86.ini`. That is a third-party enabler that runs DLSS frame generation on Ampere.
- The enabler's own log (`dlssg_sm86\logs\backend_10068.jsonl`, 42 MB) counts 307 `feature_create` events in this session. Each one rebuilds ~128 runtime kernels: `create_span_ms` ~290, runtime create median 44 ms, max 237 ms.

**What we tried** [measured]:

| Change | Rebuilds in 15 min |
|---|---|
| none | 15 |
| `r.Streamline.DLSSG.RetainResourcesWhenOff=1` + `r.NGX.FramesUntilFeatureDestruction=100000` | 21 |
| `gbx.GbxFrameGenDisableMode=0` (FG never turned off in menus) | 9, but no menus were used in that window (see below for the focused test) |

- The retain cvars are not a fix.
- Only 16 of the 307 re-creations went through the game's NGX `CreateFeature`, all early in the session. The rest happen inside the frame-generation runtime.

**The trigger** [measured with `sm86_watch.py` + `events.py`, which logs pause state and whether the game window is in front]: every frame-generation off→on transition rebuilds the feature.
- Unpause with the game in front: in mode 3, a rebuild 0.1 s later (14:57:57); in mode 0, none (14:59:46).
- The game window regaining focus: a rebuild 0.4-0.7 s later (14:57:46, 14:58:43, 14:59:10), in either mode. Frame generation stops while the window is in the background.
- With the game *not* in front, pause/unpause causes no rebuild, because frame generation is already off. That's why the first controlled tests showed none (Esc ×4, the dex page ×3, roll pictures ×3).
- `r.Streamline.DLSSG.RetainResourcesWhenOff=1` doesn't help. The enabler's DLSS-G runtime rebuilds its kernel set for every feature regardless; its own log says so ("runtime_creates is the kernel set the runtime rebuilds per feature either way").

**Recommendation for the player:**
- If the freezes bother you, turn frame generation off in-game. RTX 30 cards have no official DLSS-FG.
- Otherwise lower `dlssg_sm86.ini` `[Logging] Level` from 3 to 1. Level 3 writes kernel and evaluation traces, 106 MB of logs so far.

`gbx.GbxFrameGenDisableMode` (Gearbox):
- 0 = always allow
- 1 = disable in menus
- 2 = disable in cinematics
- 3 = disable in both (the game's value)

## 4. Input calls stalling under desktop streaming

- **When.** From 14:32 the game thread spent 12.9 ms/frame, and later 55% of its time, in `NtUserGetAsyncKeyState`, called from mods' per-frame key polls. Single frames reached 7.5 s, and fps fell to ~13.
- **Measured from a worker thread inside the game:**

  | Call | p90 | p99 | Cap |
  |---|---|---|---|
  | `GetAsyncKeyState` | 3 ms | 95 ms | ~100 ms |
  | `GetKeyState` | same | same | ~100 ms |
  | `GetCursorPos` | 54 ms | 97 ms | ~100 ms |
  | `GetForegroundWindow` | 0.003 ms | 0.03 ms | (unaffected) |

- **What changed at the same time:**
  - `WUDFHost` (DeviceGroupId `MttVDDGroup`: the MTT Virtual Display Driver) went from idle (1,150 CPU-s in 35 h) to a full core.
  - Sunshine (started 14:49) and a Moonlight client (14:40) were running.
  - The desktop was being streamed through the virtual display.
- **Cause** [inferred]: input-state calls wait on input processing that the streaming stack slows down.
- **Mitigation in mods:** check `GetForegroundWindow` first (unaffected), and share one key snapshot per frame. Handed to the mod-perf session.
- **Side finding.** Holding the GIL across these calls (`ctypes.PyDLL`) briefly made it worse, because Python worker threads couldn't run while they blocked. Reverted to `WinDLL`.

## 5. GIL convoys (fixed in the mods)

**Before**, with the Weapondex page open:
- The game thread averaged 2.1 ms/frame in `PyEval_RestoreThread` / `PyGILState_Ensure` waits.
- One single 190 ms stall was the game thread in `_Py_HandlePending` → `PyEval_RestoreThread`, with `bl4_mod_menu.web` / `publish` also queued on the GIL.

**Causes:**
- The Mod Menu's `ThreadingHTTPServer` started a thread per request.
- Every GIL-releasing ctypes call on the game thread let a waking worker take the GIL.
- 12 leaked `weapondex-thumbs` threads each woke up periodically.

**Fixes:**
- The mod-perf and Weapondex sessions: a persistent thread pool for the web server, the thumbs threads ended, and fewer page redraws.
- `bl4_perf`: a 0.5 ms switch interval. A/B: 1.4 → 1.0 ms/frame.

**After:** 0.3 ms/frame [verified]. Focus wasn't recorded for the before/after captures; the event logger only gained its `front` field at 14:55 local. The game was unfocused for stretches that day (peer screenshots: 17:29-17:30, 18:00, ~19:2x UTC). Windows gives the foreground process longer scheduler quanta, so GIL handoff numbers can differ between focused and unfocused. Treat this before/after as indicative, and re-measure focused.

**Caveat:** these "after" numbers, and the mod-perf session's "no stalls" checks of sliced worker loops, were taken with `bl4_perf`'s 0.5 ms switch interval active. Most players won't install `bl4_perf`, so mod code has to be designed for the default 5 ms:
- Worker loops that slice with `time.sleep(0)` starve the game thread at 5 ms. Offline repro: the game thread got 21 turns in 2 s with `sleep(0)` vs 796 with `sleep(0.001)`.
- Use `sleep(0.001)`. Changed in Weapondex thumbs, BL4 Direct arrival and Third Person camera_defs.

## 6. Memory: commit growth, trimming, page faults

**Commit:**
- Game commit was 25.6 GB at 13:41 and 27.7 GB at 14:29 (`memlog.py`). System commit was 60-64 GB of a 63.5 GB limit; the pagefile was growing.
- In 45 min there were +11,434 new private allocations (2.55 GB). 8,392 of them are 64 KB blocks, the size UE's allocator and cohtml get straight from VirtualAlloc.
- Classifying the content: ~1,280 new blocks are full copies of the Mod Menu state snapshot (`{"menus":true,"state":{...}}` for Reroll, Weapondex and BL4 Direct). That's ~24 per minute, never freed, kept by the page or Coherent side.
- Fixed at the source: unchanged state now answers 36 bytes. Re-diff after menu use to confirm.
- Another ~7k new blocks are binary and unidentified. A lead: dex roll pictures load under a new URL each roll, which Coherent may cache.
- 74 private allocations of 64-256 MB are all zeros and non-resident: committed, never touched.

**Trimming:**
- The working set dropped 3.17 → 1.96 GB in one 30 s step, with commit unchanged, while 10-12 GB showed as available. Later drops: 8.2 → 4.6, 7.7 → 2.9 GB.
- No third-party memory cleaner holds a handle to the game: only System and csrss hold `SET_QUOTA` (`who_holds.py`). No game thread was inside a trim call at any drop (`trim_watch.py`). So it's the Windows memory manager.
- **Effect:**
  - The game thread was parked 274 ms on `sub_3ef3d84` = `mov al,1; ret`, a code page that wasn't resident.
  - Page-fault stalls per 15 min: 0.46-1.13 s on the game thread, 0.12-0.32 s on the RHI thread.
  - Only 0.14 of the exe's 0.61 GB was resident at one point.
- **A trap found while testing.** Writing back the process's default working-set limits (min 200 KB, max 1.3 MB) took the game from 6.3 GB to 0.9 GB resident in one call, soft limits or not. `SetProcessWorkingSetSizeEx` trims down to the maximum at once. `bl4_perf` now never sets a maximum below the current working set.
- **Remedies:**
  - Free RAM: ~20 dev tool processes held ~15 GB here.
  - Optionally, `bl4_perf`'s soft minimum working set. Off by default; not measured yet.

## 7. Engine settings read live (for reference)

| Group | Settings |
|---|---|
| GC | `gc.TimeBetweenPurgingPendingKillObjects` 61.1, `gc.AllowIncrementalReachability` 0, `gc.AllowParallelGC` 1, `gc.CreateGCClusters` 1, `gc.MultithreadedDestructionEnabled` 0, `gc.IncrementalBeginDestroyEnabled` 1 |
| Streaming | `s.AsyncLoadingTimeLimit` 3, `s.LevelStreamingActorsUpdateTimeLimit` 2, `s.UseUnifiedTimeBudgetForStreaming` 1, `wp.Runtime.BlockOnSlowStreaming` 1 |
| Texture pool | `r.Streaming.PoolSize` 3000, `r.Streaming.LimitPoolSizeToVRAM` 0 |
| PSO | `r.PSOPrecaching` 1, `r.PSOPrecache.ProxyCreationWhenPSOReady` 1, `r.ShaderPipelineCache.Enabled` 1, `D3D12.PSO.DiskCache` 0 |
| Frame pacing | `r.GTSyncType` 2, `r.OneFrameThreadLag` 1, `t.Streamline.Reflex.Enable` 1, `t.MaxFPS` 0, `r.VSync` 0 |
| DLSS | `r.ScreenPercentage` 58.8, `r.NGX.FramesUntilFeatureDestruction` 3, `r.Streamline.DLSSG.RetainResourcesWhenOff` 0, `gbx.GbxFrameGenDisableMode` 3 |

All were set back to the game's values after the tests.

## 8. Community / web research (summary)

- **Mods.** The popular "performance mods" are Engine.ini files or paks, none with frame-time captures:
  - Ultimate Engine Tweaks (Nexus 13, now hidden)
  - Optimax (17)
  - Ultra Plus (32)
  - BL4 Optimizer (91)
  - Smoothlands 4 (9): reported crashes and no gain
  - The KirbbZ tweaks (268, hidden): disable GC clusters and virtual shadow maps, likely harmful
- **Ini handling.** Many of their cvars are startup-only or blocked without a console unlocker, and the game rewrites the ini unless it's read-only.
- **Known causes.** Gearbox and Digital Foundry blame shader/PSO compilation (one material set per gun part). The big fixes were:
  - v1.2 (Dec 2025): PGO build, leak fixes, PSO work.
  - v1.5 (Mar 2026): HLOD, VSM caching, about +20% average.
  - v1.11 (Oct 22 2026): removed Denuvo.
- **Long sessions.** Console performance degrades over long sessions; on PC that's anecdotal only.
- **monokrome/bl4** (Rust toolkit: saves, serials, NCS, a GUObjectArray memory reader) has nothing on performance.
- **Profiles.** Nobody has published Insights, PIX or PresentMon profiles of BL4.

## 9. Verified / untested / next

**Verified live:**
- The Python pin, its restore, and its effect.
- The GIL fixes' effect.
- That the retain cvars don't stop frame-gen rebuilds.
- Input-call latency under streaming.
- The JSON-copy leak, and that it stopped.
- The frame-gen triggers (menu close in front, focus regained).
- The key-poll move off the game thread (13 → 50.7 fps under streaming).
- `bl4_perf`, functionally: it loads and pins, and applies and restores every option. Disabling from the game thread or from a worker thread both restore everything: unpinned, switch interval 5 ms, cvars back to the game's values, working-set limits back.

**Untested:**
- The *benefit* of "Keep game memory in RAM" (no before/after).
- "Frame generation stays on in menus" beyond one unpause, and how menus and cutscenes look with it.
- "Keep DLSS loaded" on a native RTX 40/50 DLSS-G, and whether native DLSS-G rebuilds as slowly as the enabler.
- A cold start with `bl4_perf` installed from its .sdkmod (so far only hot-loaded through `dev/reload_perf.py`).
- The unidentified binary 64 KB blocks.

**Next:**
- A clean session (no streaming, mods on vs off, FG on vs off) with `sampler.py` at 300 Hz for 10 min each, in a busy fight in World_P.
- PresentMon for GPU-side frame times (not used here).

## 10. GPU cost of our mods and game settings (measured)

Measured 2026-10-24, 15:30-16:10 local time, game 10068 (build 26007962), solo, in Cowbell_P (The Speakeasy, an interior).
- Character parked and the window in front.
- Frame generation on (the DlssgSm86 enabler), DLSS Balanced, Reflex on. Display 2560x1440 @ 240 Hz.
- Sunshine was running. All 24 mods were enabled at the start.

### Method and tools

- **`dev/perf/gpu_time.py <pid>`** reads the engine's own per-frame times from outside, read-only. The RVAs are u32 FPlatformTime cycles:

  | RVA | Global |
  |---|---|
  | `0xCA84AC0` | `GGPUFrameTime` |
  | `0xCA9A348` | `GGameThreadTime` |
  | `0xCA9A350` | `GRenderThreadTime` |
  | `0xCA9A358` | `GRHIThreadTime` |
  | `0xC83F420` | `SecondsPerCycle` (double, 1e-7; QPC at 10 MHz) |

  - **How they were found:** snapshots of `.data` gave u32s in the 5-60 ms range that change every frame. The function at RVA `0x3421A8C` (the stat-unit "bound by" update) loads exactly these four, scales each by `SecondsPerCycle*1000`, and picks the largest as the bottleneck (2 game, 3 render, 4 RHI, 5 GPU).
  - **Cross-check:** the first read gave 13.34 ms, while the game's own FPS/GPU overlay showed "GPU: 13.33".
  - Each game frame (a `GFrameCounter` change, polled at ~2 kHz) records all four values.
- **`dev/perf/gpu_util.py`** is a PDH reader for Windows' "GPU Engine" utilization counters, the ones Task Manager uses. `gpu_time.py` adds them to each run:
  - **3d%:** the game's 3D-engine utilization.
  - **pdh ms:** 3d% × frame time, i.e. 3D-engine busy ms per game frame.
  - **Unaccounted work:** this GPU exposes no Compute/CUDA engine in PDH, so the enabler's CUDA frame-gen work is either counted in 3D or not visible at all.
- **`dev/perf/gpu_ab.py <pid> --a <code> --b <code> --restore <code>`** runs interleaved A/B/A/B through the bridge:
  - 15 s per run, after a 4-6 s settle.
  - Before and after each run, the bridge checks that the game is in front, unpaused and shows no cursor. Runs that fail are flagged `BAD STATE` and dropped.
  - `--shot` screenshots each side. Results are appended to `.perf/gpu.csv`.
- **Limits of the method:**
  - **GGPUFrameTime is a span**, from the frame's first to last GPU timestamp. It includes in-frame bubbles, so it reads higher than the PDH busy time (13.0 vs 11.3 ms).
  - **What counts as a game frame:** fps here means game frames (`GFrameCounter`); with FG on, the presented rate is about twice that.
  - **Focus:** the focus check must run through the bridge. From the agent shell (session 0), `GetForegroundWindow` returns nothing.

### Stability (what's noise)

- **Null A/B** (nothing changed between A and B), 4 × 15 s:
  - GPU 13.46-13.80 ms per run.
  - A-vs-B difference: +0.02 ms (pdh +0.02, fps −0.03).
  - A second null at a new spot: +0.08 ms.
- **Noise floor:** about ±0.1 ms GPU and ±0.5 fps for a 2-pair A/B. Anything inside that is "no measurable change".
- **Positive control:** `r.Lumen.ScreenProbeGather.DownsampleFactor` 32 → 16 (twice the GI probes) gave GPU +1.29 ms (pdh +0.59) and fps 58.5 → 54.3. The method does see render changes.
- **Disturbances:** the player moved mid-session, and the window lost focus during the first Third Person, Weapondex and Item Cards runs. Those runs were redone at the new parked spot (stable baseline 13.0 ms); only the redone ones are below.

### Per-mod GPU cost (gameplay, no menu open, parked)

| Mod (on → off) | GPU ms, on | GPU ms, off | Δ GPU (off − on) | Δ pdh ms | fps on / off | Verdict |
|---|---|---|---|---|---|---|
| Minimap (idle; no re-film while parked) | 13.68 [13.67-13.69] | 13.70 [13.70-13.70] | +0.02 | +0.00 | 54.4 / 54.6 | 0 |
| Less Fog (Very clear) | 13.35 | 13.42 | +0.08 | +0.04 | 56.2 / 56.1 | 0 here (interior; fog is small indoors) |
| Third Person (mod on, view off) | 13.06 | 13.08 | +0.02 | ~0 | 57.1 / 57.2 | 0 |
| Weapondex | 13.04 | 13.00 | −0.04 | ~0 | 57.2 / 57.5 | 0 |
| Item Cards | 13.06 | 12.98 | −0.08 | ~0 | 57.2 / 57.6 | 0 GPU; game thread −0.4 ms when off |
| Mod Menu | 12.97 | 13.07 | +0.09 | ~0 | 57.8 / 57.2 | 0 |
| **All mods** (23 off; Mod Menu refused to disable) | 12.98 | 13.03 | +0.05 | ~0 | 57.7 / 57.8 | 0 GPU; game thread **−1.72 ms** when off (10.8 → 9.1), fps unchanged |

[x-y] is the range over 2 runs. Each row is 2 interleaved pairs of 15 s runs.

**The one real GPU cost: the Minimap's capture when it films.**
- **The test:** a temporary hook called the mod's own `CaptureComponent2D.CaptureScene()` once per frame, on the existing 1024² target with unchanged settings.

  | | Capture off | Capture every frame | Δ |
  |---|---|---|---|
  | GPU (GGPUFrameTime) | 13.44 | 18.28 | **+4.85 ms** |
  | PDH busy | 11.33 | 15.37 | +4.04 ms |
  | fps | 55.6 | 38.1 | −17.5 |
  | Game thread | | | +2.2 ms |
  | Render thread | | | +3.2 ms |

  Both pairs agreed within 0.05 ms.
- **In real use:** one new picture = 10 captures over 1 s (`SETTLE_S` / `SETTLE_EVERY_S`). That's ~48 ms of GPU and 10 frames each ~5 ms longer. In a vehicle a new picture comes every 3-4 s.
- **The capture is already trimmed:** Lumen GI and reflections, lighting, shadows, fog, AO and most post effects are off, with 3 show-only actors. Left on: Nanite, PostProcessing, AntiAliasing.
- **Why it still costs:** 1024² = 1.05 MP, more than the main view's DLSS internal resolution (58.8% of 2090x1306 ≈ 0.94 MP), and it runs a full scene render without upscaling.
- **Levers for the Minimap's owner** (none tested):
  - a smaller target (currently `RT_SIZE = 1024` in `scene.py`);
  - fewer settle captures;
  - re-filming only once the virtual-texture mips have arrived.

### Frame generation (DlssgSm86) cost

`r.Streamline.DLSSG.Enable` 1/0, 2 interleaved pairs:

| | FG on | FG off | Δ |
|---|---|---|---|
| Game fps | 57.80 [57.67-57.93] (~115.6 presented) | 72.07 [71.93-72.20] | −14.3 fps (−20% rendered frames) |
| GPU (GGPUFrameTime) | 12.94 | 11.69 | **+1.25 ms** per rendered frame |
| PDH 3D busy | 11.29 ms (65.3%) | 9.95 ms (71.8%) | +1.34 ms |
| Frame time | 17.3 ms | 13.9 ms | +3.4 ms |
| Game thread | 10.46 | 11.61 | −1.15 (more Reflex idle) |

- **The cost:** FG on costs about 3.4 ms per rendered frame and 20% of the rendered frame rate. In exchange the presented rate goes 72 → ~116.
- **Where it goes:** only ~1.3 ms of that shows as 3D-engine work. The rest is either the enabler's CUDA work, which PDH doesn't show on this GPU, or serialization. 3D utilization *drops* with FG on, so the GPU's 3D queue is not saturated in either mode.
- **Rebuilds:** each off→on rebuilt the feature: 119 ms and 136 ms (`sm86_watch.py`). Only 2 such toggles were done.

**Not GPU-saturated:**
- In every steady run, 3D utilization was 63-66%, with the frame at ~17.3 ms against a 13 ms GPU span.
- The limiter is the FG/Reflex pipeline (the game thread idles in Reflex), not raw GPU work. That's why removing 1.7 ms of game-thread work (all mods off) didn't change fps.

### Game cvars tried (each A/B, 2 pairs; all restored)

| Cvar (game value → tried) | Δ GPU ms | Δ fps | Visible? | Keep? |
|---|---|---|---|---|
| `r.GTSyncType` 2 → 0 | −0.03 | −0.2 | no | no (no effect; Reflex owns pacing) |
| `t.Streamline.Reflex.Mode` 1 → 2 (Boost) | −0.07 | −0.5 | no | no |
| `r.Lumen.Reflections.DownsampleFactor` 1 → 2 | −0.02 | +0.1 | not in this scene | no |
| `r.Shadow.Virtual.SMRT.RayCountLocal` 8 → 4 + `SamplesPerRayLocal` 4 → 2 | −0.02 | −0.2 | no | no |
| `r.Lumen.TraceMeshSDFs` 1 → 0 | −0.04 | +0.1 | no | no |
| `r.ContactShadows` 0, `r.CapsuleShadows` 0, `r.Shadow.Virtual.ResolutionLodBiasDirectional` −0.5 → 0, `r.BloomQuality` 5 → 4 | −0.03 | +0.2 | no | no |
| (control) `r.Lumen.ScreenProbeGather.DownsampleFactor` 32 → 16 | **+1.29** | −4.2 | better GI | n/a: shows the game already runs coarse GI (32; UE default 16) |

Screenshots of each side: `.perf/shot_<label>_A|B.png`.
- **"Visible?"** means no difference by eye at screenshot scale (checked: the lowvis pair).
- **Pixel diffs:** the A/B mean pixel differences (3.2-5.6 of 255, with 4.8-8% of pixels over 20) are the same order for every pair, including the probes control. The animated weapon and the NPCs dominate them, so they can't rule out subtle changes (shadow softness, reflection sharpness up close).

**Not changed, on purpose:**
- **Texture pool** (`r.Streaming.PoolSize` 3000, `LimitPoolSizeToVRAM` 0). The game holds 9.25 GB dedicated VRAM (10.75 of 12 GB for the adapter). There's no headroom to grow the pool, and shrinking it can't save GPU time in a parked scene.
- **`r.OneFrameThreadLag` 0.** It would only serialize the threads more.
- **`s.*` / `gc.*`.** No hitches occurred while parked (frame p99 21-23 ms, max ≤ 27 ms apart from the disturbances), and GC was already ruled out (§TL;DR). With no hitch to measure, a change can't be shown to help.

**Conclusion:**
- None of the tried low-visibility render settings is worth keeping.
- At these settings the game's GPU time goes to the base passes, Lumen at coarse settings, DLSS and frame generation.
- The real levers are the user-facing ones:
  - DLSS mode;
  - FG on/off: −20% rendered frames and ~3.4 ms latency per rendered frame, for ~1.6× presented fps;
  - the Minimap's capture size and count.

### Caveats

- **Console priority.** Cvars set with a console command stay at SetByConsole priority until restart, even with the value put back. In UE, a later lower-priority write (scalability groups from the Graphics menu, `SetByCode`) is then ignored with a warning.
  - Affected this session: the 12 cvars above (including `r.Streamline.DLSSG.Enable`) are at the game's values.
  - The effect: changing graphics quality or FG in the menu may not apply to them until the game restarts.
  - The same applies to §3/§7's earlier tests and to `bl4_perf`'s cvar options.
  - Not verified: the shipping build has no engine log to check.
- **One scene.** Everything is from one interior, parked spot. The Minimap re-film cost, Less Fog and the volumetric/shadow settings would differ outdoors (World_P) and while moving.
- **Third Person's active view** (the character model on screen) was not measured, only the mod idle.
- **Side note:** `Plugins/unrealsdk.log` is 229 MB this session.
