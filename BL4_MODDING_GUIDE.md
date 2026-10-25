# Borderlands 4 Modding Guide (learned building BL4 Direct)

Use this as the system prompt / briefing for building Borderlands 4 mods with the Python SDK. It covers the setup, the APIs that work, the traps that cost hours, and a test harness for automating two game instances on one PC. Items marked **(verified)** were confirmed in game during this project. Items marked **(observed)** were seen once and may vary.

Game build at time of writing: Steam build `26007962`, Unreal Engine 5.5.4, oak2-mod-manager SDK `v0.3` (unrealsdk 3.2.0, pyunrealsdk 1.10.0, Python 3.14).

---

## 1. Ground rules

- **Mods are Python, running inside the game,** through the bl-sdk Python SDK (`oak2-mod-manager`). Reference: <https://github.com/bl-sdk>. Mod database: <https://bl-sdk.github.io/oak2-mod-db/>.
- **Hooks only see UFunctions.** The SDK hooks `ProcessEvent`/`CallFunction`. Native C++ functions that aren't UFunctions (e.g. `UNetDriver::InitConnect`, `PreLogin`, Slate input handling) can't be hooked. You work around them by changing data (config structs, CDO properties) or by finding a UFunction that fires nearby. The only alternative is a native signature scan, which is fragile (see the split-screen mod in §10).
- **Never touch unreal objects off the game thread.** Background Python threads may do file and network I/O only. Run unreal calls from a hook (a per-frame tick hook works well).
- **Never keep a raw UObject reference across frames.** Store `unrealsdk.unreal.WeakPointer(obj)` and call it (`ptr()`) to get the object, or None once the engine has destroyed it.
  - Reading anything from a freed object, even `.Name` as an "is it alive?" check, is an access violation that kills the game.
  - Verified the hard way: two instances cached each other's player characters for per-frame measurement. A dropped connection destroyed those characters, and both games crashed on the next frame inside `pyunrealsdk.dll`.
  - A descriptor that wraps attributes in WeakPointers (see `_Weak` in `ui.py`) makes this painless.
- **Prefer data over code paths.** Many big wins in BL4 come from flipping a definition at runtime (e.g. `ENGINE.NetDriverDefinitions`), not from reverse-engineering functions.
- **Read existing mods first.** The mod DB has MIT/GPL sources that already solved UI, input, entitlements, travel and more (§10).

## 2. SDK install and layout

- Release zip extracts into the game root: `OakGame/Binaries/Win64/dsound.dll` (proxy loader), `Win64/Plugins/*` (unrealsdk, pyunrealsdk, Python 3.14), and `sdk_mods/` (mods_base, console_mod_menu, keybinds).
- **Config:** `Win64/Plugins/unrealsdk.toml` (shipped) plus `Win64/Plugins/unrealsdk.user.toml` (yours, overrides).
  - `[mod_manager] extra_folders = ["C:/path/to/dev/mods"]` loads mods from a repo with no copying.
  - `[unrealsdk] log_file`, `console_log_level`, `external_console` are available. See `supported_settings.toml` in the unrealsdk repo.
- **Log:** `Win64/Plugins/unrealsdk.log`. Console output, including the console mod menu's text, is mirrored there.
  - **Every game instance writes the same file,** and a newly launched instance truncates it. Use your own per-instance log for automation.
- **Mod loading rules** (from `sdk_mods/__main__.py`):
  - A mod is either a folder with `__init__.py` or a `.sdkmod` zip containing exactly one root folder named like the zip.
  - **A folder beats a `.sdkmod` with the same module name,** and the log warns "Found multiple versions". Good for dev: junction your source folder into an `extra_folders` dir to override an installed copy.
  - Entries starting with `.` are ignored, so put helper tools in `sdk_mods/.yourmod/`.
  - Folder names containing `.` are rejected.
- **Settings** live in `sdk_mods/settings/<mod>.json`. `{"enabled": true}` makes a mod auto-enable at startup; an installer can write it.
- **Mods are disabled by default.** Options, commands and hooks only register while enabled. Commands are not registered until `mod.enable()`.
- **VC++ runtime:** the SDK needs a recent x64 redist; 14.44+ is a safe minimum. Registry: `HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64` (`Installed`, `Major`, `Minor`).
- **SDK Python is 3.14,** so the new syntax is fine inside mods. Tooling (installers etc.) may run an older Python, e.g. 3.11 has no `type X = ...` alias statement.

## 3. Writing a mod (mods_base)

```python
from mods_base import build_mod, command, hook, get_pc, ENGINE, ButtonOption, DropdownOption, SETTINGS_DIR
from unrealsdk.hooks import Type, Block

@hook("/Script/Engine.GameInstance:HandleNetworkError", Type.PRE)
def on_net_error(obj, args, ret, func): ...

@command(description="...")
def mycmd(args): ...          # argparse Namespace
mycmd.add_argument("target")

mod = build_mod(options=[...], hooks=[on_net_error], commands=[mycmd],
                on_enable=..., on_disable=...)
```

- Name, version and description come from `pyproject.toml` next to `__init__.py` (`[project]` + `[tool.sdkmod] supported_games = ["BL4"]`). Keep one version source of truth and assert they match in your build.
- `ENGINE` is the `OakGameEngine`, `get_pc(possibly_loading=True)` is the local `OakPlayerController`, and the world is `ENGINE.GameViewport.World`.
- **Console mod menu** (`mods` command): supports Button, Bool, Dropdown/Spinner, Slider, Keybind, Grouped and Nested options. **It has no text input.**
- **The console:** `~` once opens the one-line console, which shows no output. `~` twice opens the full console, the only place menu output is visible. Users get confused by this (verified); don't make the console your primary UI.

## 4. Introspection toolbox (run these through a bridge or `pyexec`)

```python
cls = unrealsdk.find_class("ShiftNetDriver")             # native class by name
cls._path_name()                                          # /Script/SocketSubsystemShift.ShiftNetDriver
for prop in cls._properties(): ...                        # FProperties (UE5 keeps these off Children)
[f for f in cls._fields() if f.Class.Name == "Function" and f.Outer == cls]  # own UFunctions only
list(cls._superfields())                                  # inheritance chain
unrealsdk.find_all("Function", exact=True)                # grep every UFunction name
unrealsdk.find_all("SomeClass", exact=False)              # live instances (filter out Default__)
fn = unrealsdk.find_object("Function", "/Script/X.Y:Z")
fn.FunctionFlags & 0x2000                                  # FUNC_Static
[(p.Name, type(p).__name__) for p in fn._properties()]    # parameter list
```

- **Blueprint classes** aren't found by `find_class("x_C")`. Use `unrealsdk.find_object("BlueprintGeneratedClass", "/Game/.../x.x_C").ClassDefaultObject`.
- **Functions that take a `GbxDefPtr`** can't take `None` positionally. Build the args struct instead: `WrappedStruct(func, Arg=...)`, then `bound_fn(args_struct)`. Unset fields stay default.
- **Struct values:** `unrealsdk.make_struct("Vector2D", X=.., Y=..)`, `make_struct("LinearColor", ...)`, `make_struct("Key", KeyName="F6")`, `make_struct("SlateColor", SpecifiedColor=..., ColorUseRule=0)`.
- **Enum names:** `unrealsdk.find_enum("EGbxUIPopupType")`.
- **Assigning to nested struct fields of a UObject writes through** (e.g. `box.WidgetStyle.BackgroundColor = ...`) (verified).
- **`unrealsdk.hooks.log_all_calls(True)`** for a few seconds writes every ProcessEvent call to `Plugins/unrealsdk.calls.tsv`. Rank by frequency to find tick candidates, and diff "before vs after an action" to find what an action triggers.
- **Exe string mining:** scan `Borderlands4.exe` for UTF-16LE strings. Class names, cvars, config keys and log formats are all there.
  - This found `UShiftNetDriver`, `bIsPassthrough`, `[ShiftSockets] bUseIPSockets`, `SteamNetDriver`, `OnlineSubsystemSteam`, and the SHiFT error texts.

## 5. Tick and timing

- **Reliable per-frame UFunctions:** `/Script/Engine.CameraModifier:BlueprintModifyCamera` and `:BlueprintModifyPostProcess`. They fire in the main menu and in gameplay, several times per frame (once per modifier) (verified). Use a POST hook and throttle yourself.
- **Never travel from inside UI callbacks.** Close your UI, then run the travel a few ticks later. Map loads destroy the PlayerController and everything outered to it.
- **Detect map changes** by comparing `get_pc()` to the PC you stored. On change, drop widget refs without calling anything on them; they belong to the dead world.

## 6. Networking (what made direct-IP co-op work)

- **Net driver definitions:** `ENGINE.NetDriverDefinitions`. `GameNetDriver` (and Beacon/Honcho/Experience) defaults to `/Script/SocketSubsystemShift.ShiftNetDriver`, with fallback `IpNetDriver`. You can edit `DriverClassName` at runtime. It affects net drivers created afterwards (verified).
- **ShiftNetDriver** (subclass of IpNetDriver) has `bIsPassthrough`.
  - As host it listens on a real UDP socket, but on a random port that ignores `?port=` (observed).
  - As client, while logged into SHiFT, it forces `bIsPassthrough=False` during `InitConnect` and routes even raw IPs through SHiFT's sockets, so no packets reach the IP (verified with a UDP relay). Setting the CDO's flag doesn't help.
- **Plain `IpNetDriver` doesn't work:** `NetDriverListenFailure` / `PendingNetGameCreateFailure` (verified). Most likely the default socket subsystem is SHiFT's.
- **`/Script/OnlineSubsystemSteam.SteamNetDriver` works** (verified, both host and client). It uses plain UDP for IP addresses.
  - Host: `open <Map>?listen?bIsLanMatch?port=7777` (keep the GameMode's own `OptionsString`, e.g. `?travelstation=...`).
  - Client: `open <ip>:<port>`.
  - Leave: `disconnect`. Stop hosting: reload the map without `?listen` after restoring the driver.
- **Run console commands from Python** with `find_class("KismetSystemLibrary").ClassDefaultObject.ExecuteConsoleCommand(pc, "open ...", pc)` (verified for engine commands like `open` and `disconnect`). Unverified whether this reaches SDK-registered commands; call your Python handler directly instead.
- **State checks:**
  - `world.NetDriver.ServerConnection` set means you're a client.
  - `len(world.NetDriver.ClientConnections)` is the number of connected players.
  - `KismetSystemLibrary.IsServer(world)` / `IsStandalone(world)`.
  - `world.AuthorityGameMode.OptionsString`.
- **Errors:** hook `GameInstance:HandleNetworkError` and `:HandleTravelError`. `args.FailureType` is an enum (`ConnectionTimeout`, `ConnectionLost`, `NetDriverListenFailure`, `PendingConnectionFailure`, `PendingNetGameCreateFailure`, ...).
- **SHiFT is one session per account.** Logging in from a second instance kicks the first one offline ("You are Offline"). On the kicked instance this **closed a hosted listen socket** while the world still reported hosting (verified, twice). Real players with separate accounts don't hit this. If you test two instances on one account, let both finish logging in before hosting.
- **Bandwidth:** two players in a quiet area used about 2–4 KB/s each way (about 30 kbit/s).
- **Ping:** localhost "ping" of about 20 ms equals frame time; the net driver ticks once per frame.
- **Measured (two instances through `tools/netlab_proxy.py`):**
  - **Host updates follow host fps** (verified). About 50 packets/s at ~55 fps, exactly 30/s with the host capped at 30 fps. `NetServerMaxTickRate=30` doesn't apply to listen servers (`bClampListenServerTickRate=False`).
  - **Remote characters stay smooth on bad links** (verified). A walking character showed no visible freezes even with 40 ms delay each way, ±20 ms jitter and 3% loss; client smoothing covers it.
  - **The joining player's own movement gets corrected on bad links** (verified). Corrections went from 0/min clean to about 120/min at 40 ms ±20 ms (reordering) with 3% loss. Raising `GameNetworkManager.MAXPOSITIONERRORSQUARED` (default 3) didn't reduce them.
  - **Counting corrections:** on the host, watch the remote pawn's `CharacterMovement.ServerLastClientAdjustmentTime` (correction sent) vs `ServerLastClientGoodMoveAckTime` (ack). On the client, UE5 packs both into `ClientMoveResponsePacked`, which Python can't read.
  - **`gbx.net.ReplicationBudgetSeconds`** (Gearbox, default 0.0025) can be changed and read back at runtime through `ExecuteConsoleCommand` / `GetConsoleVariableFloatValue`. Its effect is untested (needs a busy fight).
- **Fast travel while hosting** (verified, two instances with a monitor polling every second):
  - **With a client connected:** seamless travel through the `Loader` map. Both games move together, and the server, its net driver and the port survive. Took about 37 s.
  - **Host alone:** a hard travel. The listen socket closes the moment travel starts, the host passes through `Entry_P` standalone, and a new world loads with the GameMode options kept (`?listen?bIsLanMatch?port=7777`). With our `SteamNetDriver` configured, that new world either came up as a "server" with nothing bound to the port (no error raised) or hung on loading for a long time.
  - **Fix in BL4 Direct 0.4 (verified):** swap in our driver only while starting to host, then restore the SHiFT setting once the port is confirmed bound. The running server keeps its driver.
    - With the setting restored, a solo fast travel became an in-world teleport, and a cross-map travel (`World_P` → `Harmonica_P`) became a **seamless** travel through `Loader` in 7 s, keeping the server and port.
    - So the configured `GameNetDriver` class itself pushed BL4 onto the hard-travel path.
    - A watchdog still re-hosts after any map change if hosting is wanted but the port is free (safety net; it hasn't been needed since).
  - **Port check:** binding a UDP socket to the port fails with WSAEADDRINUSE (10048) while the game holds it.
- **Co-op fast travel with a direct-IP friend** (verified by disassembly and live tests; full write-up in `docs/fast-travel-investigation.md`):
  - When anyone requests a teleport, the host drops any player whose native "client ready" flag (`PlayerController+0x32B9`) is unset, then waits in the portal for them forever.
  - The game only sets that flag after verifying the player's entitlements online, which never happens for a direct-IP friend.
  - Fix (mod 0.4.2): call the game's own setter for friends who have already arrived, in a PRE hook on `RequestTeleportToStation` / `RequestTeleportToPlayer`. **Don't** set it while a friend is still joining: that left the joiner on a black screen.
  - **Testing with two copies on one account breaks the host's own arrival.** The client's login re-binds the host's online "local user 0" slot to the client's controller, so the host's readiness check can't find its own player. That's a test artifact, not a mod bug. Use `dev/re/same_account_guard.py` on the host when testing on one account. Start it only after the client has fully loaded in: running it while the client joined left the client with nothing equipped (2026-10-25).
  - The exe on disk is protected. Dump the running image with `dev/re/ext_dump.py` and disassemble it with `dev/re/re_tool.py`. Native call stacks captured inside UFunction hooks show which native function fired an RPC.
- **Test-movement pitfalls:**
  - Setting a standing character's rotation every frame (`K2_SetActorRotation`) produced fake 0.2–0.9 s "freezes" on the client.
  - Use `Pawn.AddMovementInput(dir, 1.0, True)` each frame to walk like a real player. `SetControlRotation` needs a second `bResetCamera` arg in BL4, and the character doesn't follow the view while its window is unfocused.
- **`[ShiftSockets] bUseIPSockets=True`** in the user `Engine.ini` changes SHiFT's socket layer (1 UDP socket instead of about 28) but wasn't needed for the SteamNetDriver approach.
- **The user `Engine.ini`** (`Documents/My Games/Borderlands 4/Saved/Config/Windows/Engine.ini`) is read by the game. The shipping build writes no UE log file (`-abslog`/`-log` did nothing).

## 7. Native in-game UI (UMG built at runtime)

The pattern is from MattsBL4ModsMenu (MIT). All of this is verified in BL4 Direct's `ui.py`.

- **Build:** `UserWidget` (outer = PC) → `WidgetTree` → root `CanvasPanel` → children (`Border`, `TextBlock`, `Button`, `EditableTextBox`). Then `overlay.AddToViewport(z)`. Construct each with `unrealsdk.construct_object(find_object("Class", "/Script/UMG.X"), outer)`.
- **Layout:** set `child.Slot` position and size (`SetPosition`/`SetSize`/`SetZOrder`/`SetAutoSize(False)`). Lay out in a 1920×1080 design space and scale by `(viewport / dpi) / 1920` and `/1080`. Get these from `WidgetLayoutLibrary.GetViewportSize(pc)` and `GetViewportScale(pc)`.
- **Text:**
  - `TextBlock.SetText(str)`, with size via `SetRenderScale`.
  - Color: `SetColorAndOpacity(SlateColor)`.
  - `SetJustification(1)` to center. TextBlocks draw from the top of their slot; give them a line-height slot centered in the box.
- **Decoration:** Borders with `SetBrushColor`, and `SetVisibility(3)` (HitTestInvisible) on anything that shouldn't eat clicks.
- **Buttons:** a real `Button` at nearly zero opacity over your own Border and TextBlock. Each tick, poll `IsPressed()`: pressed then released fires the action.
  - Bindable delegates aren't needed.
  - After firing, return immediately; the action probably rebuilt the widget list.
- **Rebuild everything on any change.** Removing and recreating the canvas is simpler and safer than patching widgets.
- **Text input:**
  - **Polling `IsInputKeyDown`** works only in game input modes. Keys also leak into BL4's own menus (Coherent UI reads Slate focus). Leaked letters opened the Options screen and Tab opened "Legal Information" (verified). Don't do this.
  - **Use `EditableTextBox`** plus `WidgetBlueprintLibrary.SetInputMode_UIOnlyEx(pc, box, 0, False)` with `box.SetUserFocus(pc)` and `box.SetKeyboardFocus()`. The box receives typed text natively, nothing leaks, and `GetText()` reads it.
  - Default style is light grey on white. Set `WidgetStyle.BackgroundColor` / `ForegroundColor` / `FocusedForegroundColor` as `SlateColor` (`ColorUseRule=0`).
  - Clicking a text box only focuses it when the window is active. Put an invisible Button over it that calls your focus function.
  - Redraws destroy the boxes, so copy their text out before redrawing, and clear the boxes themselves when you mean to clear.
- **Hotkeys while UI-only mode is active:**
  - PlayerInput sees no keys, so read `user32.GetAsyncKeyState` for F6/Esc/Enter/Tab.
  - Only do that when the foreground window belongs to the game (`GetWindowThreadProcessId(GetForegroundWindow())` equals the current process).
  - When closed, check both `pc.IsInputKeyDown(Key)` and the Windows key state. Some other UI may have left the game in UI-only mode.
- **On close:**
  - In gameplay: `SetInputMode_GameOnly(pc, True)` and hide the cursor.
  - At the title screen: `SetInputMode_GameAndUIEx(pc, None, 0, False, False)`.
  - Then `SetFocusToGameViewport()`.
- **Mouse cursor:** `pc.bShowMouseCursor = True`. Move/look input: `SetIgnoreLookInput` / `SetIgnoreMoveInput` and the matching `Reset...`.
- **Not usable (observed):**
  - `OakPlayerState:SetPrototypeToastMessage` rendered nothing at the main menu (untested in gameplay).
  - `GbxUIPopupManager` (rich dialogs with buttons, text input and list choices) exists, but no instance was found and `GetLocalPlayerSubsystem` returned None.
  - Main-menu actions (`ui_script_main_menu_C:ContinueCampaign`) are BP events that need the real menu widget. Calling them on the CDO did nothing.
- **Clipboard:** Win32 via ctypes (`OpenClipboard`/`EmptyClipboard`/`GlobalAlloc`/`SetClipboardData(CF_UNICODETEXT)`). Set `argtypes`/`restype` to pointer types, or 64-bit handles get truncated (verified).
- **Public IP:** a background thread with `urllib` (`api.ipify.org`). The SDK's Python has SSL. Redraw when the lookup state changes.
- **Users dislike permanent on-screen banners.** Keep status inside your panel unless asked.

## 8. Test automation (two instances on one PC)

**The environment trap.** On this machine the agent's shell runs elevated in Windows **session 0** (a service), not on the desktop (session 1).
- Starting `steam.exe` from there makes the user's Steam shut down and restart.
- Games launched from there crash or run invisibly.
- **Always launch through something running in the user's session.**

| Piece | What it does |
|---|---|
| `tools/desktop_agent.py` | Runs in the user's session (started once via a one-shot `schtasks /create ... /it /rl LIMITED` + `/run` + `/delete`, with the user's OK). Fixed actions: start Steam, launch the game with `-bridgeid=X`, kill the game, screenshot, click, key, type. Restarts itself when its source changes. |
| `dev/bl4_bridge` | Dev-only SDK mod. Drop `*.py` into `.bridge/<id>/in/` to run on the game thread (on the tick hook), or `*.thread.py` to run on a background thread. Output appears in `out/`. One persistent namespace per instance; `helpers.py` adds `dump()`, `props()`, `trace()`, `cmd()`, `world()`. |
| `tools/bridge.py <id> "code"` | Host-side client for the bridge (`-f file.py`, `--thread`, `--events`). |
| `tools/shot.ps1` | `PrintWindow(hwnd, PW_RENDERFULLCONTENT)` on the **largest visible window** of the process; the "main window" handle sometimes points to a tiny extra window. |
| `tools/input.ps1` | `PostMessage` mouse clicks and key presses straight to the window. Doesn't move the real cursor or steal focus. |
| `tools/udp_proxy.py` | Logging UDP relay to watch handshakes (`C->S` / `S->C` sizes and hex). |
| `tools/scenario.py` | Launch-and-wait helpers with fixed window coordinates. |

**Input automation:**
- `WM_KEYDOWN`/`WM_KEYUP` updates `IsInputKeyDown`.
- `WM_CHAR` is needed for text boxes and Slate typing.
- Synthetic messages don't update `GetAsyncKeyState`, so Windows-key-state hotkeys can't be automated this way; click your Close button instead.
- Text-box focus-by-click needs an active window, hence the focus buttons.

**⚠ Save data:** two instances on one account read and write **the same save files** (`Documents/My Games/Borderlands 4/Saved/SaveGames/<steamid>/Profiles/client/*.sav`).
- During testing, `profile.sav` (account-wide unlocks) was overwritten with an 88% smaller version and the active character's save shrank. In game, all fast-travel stations were locked.
- Most likely one instance (kicked off SHiFT by the other's login, or freshly crashed) saved an incomplete profile.
- **Always back up `SaveGames` before multi-instance tests,** and restore with the game closed. Force-kill rather than quit, since a normal exit saves again.
- Steam Cloud syncs on exit, so check `Steam/logs/cloud_log.txt` to see which version got uploaded.
- Better: keep the second instance from writing saves at all (untested idea: a separate saved-dir per instance).

**Two instances:**
- Put `steam_appid.txt` containing `1285190` next to `OakGame/Binaries/Win64/Borderlands4.exe`. Without it a direct exe launch bounces through Steam, and Steam refuses a second copy.
- Launch the Win64 exe directly with `-windowed -ResX=1600 -ResY=900` so UI coordinates are stable.
- Each instance may compile shaders on first boot after a patch (minutes).
- The photosensitivity warning appears on each fresh launch. At 1600×900, Confirm is at (640, 665) and Continue at (200, 209) in 1280-wide screenshot space.

**Iterating:**
- Junction the mod source into the dev `extra_folders` dir to override the installed copy.
- Hot-reload a submodule through the bridge: `old.uninstall(); importlib.reload(...); reassign module refs; install()` (see `dev/reload_ui.py`).
- Restart the game for changes to the mod's `__init__`.

**Firewall testing:** Windows Firewall ignores traffic from the PC to itself. WSL in `networkingMode=mirrored` sends from the host's own IPs, so it isn't a valid outside source (verified). Verify rules with `Get-NetFirewallRule -PolicyStore ActiveStore`, and do the real test from another machine.

## 9. Shipping (installer lessons)

- **PyInstaller** `--onefile --windowed --uac-admin`, with the pinned SDK zip (sha256-verified) and the built `.sdkmod` bundled as data.
  - Windowed exes have no stdout. For CLI mode, `AttachConsole(-1)` and open `CONOUT$`.
- **Install:**
  1. Refuse while `Borderlands4.exe` is running.
  2. Install or repair the VC++ runtime.
  3. Extract the SDK, but never into `sdk_mods/settings/`.
  4. Move any loose mod folder aside, since it would shadow the `.sdkmod`.
  5. Write `settings/<mod>.json {"enabled": true}`.
  6. Copy the app to `sdk_mods/.<mod>/`, so the mod can launch it elevated (`ShellExecuteW(..., "runas", ...)`) for admin-only tasks such as firewall rules.
- **Firewall:** Steam's install adds an "allow everything inbound" rule for `Borderlands4.exe`, so an allowlist must be a **block rule covering the complement** of the allowed addresses (block beats allow). Compute the complement with `ipaddress.address_exclude`, cover IPv6 too, and keep loopback allowed.
- **Compatibility code:** show `game <steam buildid> / sdk <ver> / mod <ver>` in the app and in game, so players can confirm they match. The build id is in `steamapps/appmanifest_1285190.acf`.
- **Normalize IPs before storing them.** People type `21.142.145.01`. A regex check accepts it, but Python's `ipaddress` rejects leading zeros, and that crashed the elevated firewall update (verified). Store `".".join(str(int(p)) for p in parts)`, and make the rule builder skip, not crash on, anything unparsable.
- **Tkinter isn't thread safe.** Background threads (public-IP lookup, install tasks) must not touch widgets, not even through `after()`. Queue callables and drain the queue from a recurring `after()` on the Tk thread. An update made from a worker thread silently never appeared in testing (verified).
- **Test installer logic against fake game folders** (`dev/installer_test.py`). A single dummy `OakGame/Binaries/Win64/Borderlands4.exe` is enough; stub the firewall and `game_running`. It covers the fresh-PC install, repair, uninstall, and conflict paths without touching a real install.
- **A windowed PyInstaller exe shows a raw traceback dialog on an uncaught exception.** Catch everything in CLI mode and write a log (`%LOCALAPPDATA%\BL4Direct\last_run.log`) when there's no console.
- **Only the host needs friends allowlisted** (joining is outbound; stateful firewall). Update the rule when hosting starts, or immediately if already hosting, and only when the allowed set changed (a stored "applied signature"). That keeps the admin prompt rare (verified).
- **Firewall rule verified from a second machine** (`dev/fwtest/run_test.py`, over SSH from a Tailscale/LAN box). A Steam-style allow-all rule plus our complement block rule: non-friends blocked, a friend's IP allowed, other IPs of the same machine still blocked, LAN/VPN allowance works.

## 10. Reference mods worth reading

- **MattsBL4ModsMenu** (MIT): the runtime UMG UI patterns, input polling, viewport/DPI math, pause/title launcher. Its own menu shows mods_base options; string options are editable, buttons aren't.
- **bl4-2player-splitscreen** (GPL3, TitanNav):
  - Local players without an online ID never finish "client ready"; it sig-scans the exe for the ready setter and calls it with ctypes.
  - DLC entitlements are per player (`Client_RequestDLCEntitlements` / `ServerRefreshPlayerEntitlementFacts`).
  - Shows the safe pattern for sig scans pinned to a build (PE TimeDateStamp + SizeOfImage).
- **blimgui** (MIT): a Dear ImGui library in a separate window, with native modules; may break on SDK updates.
- **P2P Teleporter / World Travel** (Squ1ggs, MIT): client-side co-op features and travel-station browsing.

## 11. Checklist for a new BL4 mod

1. Find the UFunctions involved: `log_all_calls` around the action, plus a `find_all("Function")` name grep. Check for an existing mod that touches the same system.
2. Prefer changing data (definitions, CDO defaults, config structs) over hooking flow.
3. Hook UFunctions only. Keep hooks small, guard every unreal access with try/except, and never call unreal from threads.
4. Any travel or map change happens a few ticks after UI closes. Drop refs to world-owned objects on PC change.
5. UI: runtime UMG + `EditableTextBox` + UI-only input mode. Rebuild on change and poll `IsPressed()`.
6. Test with the bridge and desktop agent. Screenshot to confirm visuals, and read state through the bridge to confirm behavior.
7. Ship as a `.sdkmod` with `pyproject.toml`, and a single version source of truth checked by the build.
