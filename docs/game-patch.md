# A game patch came out

What a Borderlands 4 patch can break in our mods, how each mod reacts on players' machines until
we ship an update, and the steps to update everything. The tool is `tools/game_patch.py`; this is
its runbook.

## What happens on players' machines meanwhile

Nothing we do should crash a patched game or undo the patch. Mods compare what they depend on and
step aside when it changed:

- **Page scripts** (BL4 UI Link scripts; until 2026-10-29 shim paks 4242 to 4250) are pinned by
  the SHA1 of the vanilla page they were written against (each mod's `vanilla.json`). A changed
  page means the script isn't registered and the feature pauses (logged: "... paused: this game
  version changed ..."; "Needs update" in the mod list). The link's own pak pins debug.js.
- **Generated data** with a runtime check switches its feature off or warns: Item Cards'
  numbers/firmware/drops, Reroll (all rerolls blocked), Instant Fast Travel (normal portal),
  Kairosdex ("chances may be off"), Carnage Meter (plain item names).
- **Native code**: every finder checks its bytes and turns that part off on a mismatch:
  - ECHO Anywhere, Instant Tabs (per site), Reroll (sell + mail fallback), Build Presets, Crisp
    Numbers, BL4 Direct's arrival fix, Minimap (falls back to the ECHO map copy), Carnage Meter,
    Kairosdex rolls, Backpack Size's Lost Loot.
  - Third Person gives up after 3 empty camera scans in a session.
- **Not checked** (stale data stays in use; the first things to regenerate):
  - Item Cards' `encore.json` (Big Encore travel spots).
  - Kairosdex's `missions.json` (save backfill) and `travel.json` (Waypoint / Travel).
  - UVH Teleport's `bosses.json`.
  - Minimap's `maps.json`.
  - Build Presets' `trees.json` (display only).
- **A renamed UFunction** means its hook silently never fires. A renamed class used at import
  (`find_class(...)` at module level, in 9 mods) logs "Failed to import mod" and the mod is gone.
  `live` finds the first kind, `log` the second.
- The SDK itself (unrealsdk 3.2.0) finds engine globals by byte pattern. If a patch breaks it,
  every mod breaks: check the SDK log first.

`python tools/game_patch.py log <their unrealsdk.log>` lists these symptoms from a player's log.

## Steps

1. **See what changed, before starting the game** (7 s, read only):

       python tools/game_patch.py check

   It compares the Steam build id, the exe (size + PE stamp) and every game file each generator
   read last time (`tools/game_pins.json`: 18 generators, 218 game files, plus the NCS chunk
   paths they probed that didn't exist, so a new chunk shows up) against the install. It lists:
   - the stale generators, with what players get meanwhile;
   - the mods' own runtime pins that no longer match (those mods have stepped aside);
   - whether the logo movies Skip Intro renames still exist.

2. **Start the game the safe way** (`python tools/scenario.py up <id>`, never from the shell),
   then read the SDK log:

       python tools/game_patch.py log

   A "Failed to import mod" or an SDK init error comes first: nothing else is meaningful until
   the SDK and every mod load.

3. **Regenerate the stale data**:

       python tools/game_patch.py regen          (the stale ones and what reads their output)
       python tools/game_patch.py regen --all    (everything, ~90 s)
       python tools/game_patch.py regen gen_item_cards

   - It runs them in dependency order. For example, `gen_weapondex` comes before missions /
     travel / reroll legendaries.
   - It runs them with a fixed hash seed, so unchanged data comes out byte for byte.
   - It records their new inputs in `tools/game_pins.json` and lists the mods whose files
     changed.

   A generator that fails means an anchor or a record it expects is gone: fix that generator
   (the page generators all fail loudly on a missing anchor). Then read `git diff` on the
   outputs. A changed page or NCS record usually needs a look at the code that uses it, not
   just new data. Paks only load at startup, so new shim pages need a restart.

4. **Check native code and hooks in the running game**:

       python tools/game_patch.py live <bridge id>

   - Per mod, it reports each native part (`ok`, `OFF` with the reason, or `pending` if not
     looked up yet this session) and the data pins the mods check.
   - Every `/Script/` hook target of every loaded mod must exist; blueprint ones only exist
     while loaded.
   - Then it scans the log.
   - ~1 ms on the game thread (30 ms the first time); it only reads state.
   - `pending` parts resolve on first use (Crisp Numbers on its first tick, ECHO Anywhere on
     its key). Use them once, then run `live` again.

5. **Fix each `OFF` part.** Dump the running image (`dev/re/ext_dump.py <pid> <dir>`), then
   follow the mod's notes (table below). Every pattern must match exactly once and check itself
   before it's used. Update the build-dated RVAs in that mod's notes.

6. **Fix each `MISSING` hook**: find the new name with `find_all("Function")` filtered by a word
   of the old one, or `unrealsdk.hooks.log_all_calls(True)` around the action.

7. **Test in game** what changed, then bump the
   version of every mod whose files changed (`pyproject.toml`, and BL4 Direct's `MOD_VERSION`)
   and release (`tools/release.py`). Finally, `check` again: it should be clean.

## Per mod: what a patch can break, and where the fix is

| Mod | Depends on | Shows up as | Fix |
|---|---|---|---|
| Mod Menu | the UI Link's `debug.js` (4251, shared; its old paks 4245/4247 are retired); the game's option templates | check: `gen_uilink_page` stale | `regen gen_uilink_page`; menu.js selectors if the pause / main menu DOM changed |
| Item Cards | `item_card.html` (pin; link script) + skills/firmware/drops/encore NCS; `ui_script_backpack` / `ui_script_hud_skills` hooks | check; live (page + data, hooks) | `regen gen_item_cards`; JS anchors listed in `tools/gen_item_cards.py` |
| Easy Respec | `menu_skills.html` (pin; link script); `ui_script_skills:RemovePoint` | check; live hooks | `regen gen_respec_page` |
| Instant Tabs | 2 pages (pins; link script); 6 code patch sites (byte patterns, cached per build in `settings/bl4_instant_tabs_sites.json`) | check; live "code patches: not found X" | `regen gen_instant_tabs_pages`; patterns in `mods/bl4_instant_tabs/patch.py` docstring |
| Kairosdex | `celebratory_notifications.html` (pin; link script), the UI Link's debug.js (4251, shared); dex/stats/rolls/missions/travel data; identity RVAs (shared with Reroll), save key offset (rescans itself), picture bake RVAs (dev) | check; live (identity functions, dex.json) | `regen gen_weapondex` (+ dependents); RVAs: `mods/bl4_weapondex/identity.py` |
| Carnage Meter | `loot_feed.html` (pin; link script); `items.json`; damage delegate (reflection, self-checked) | check; live "damage delegate"; screen says "Can't count damage on this game version" | `regen gen_carnage_popup gen_damage_meter`; delegate: `native.py` |
| Reroll | `parts.json`, `legendaries.json`; 7 RVAs pinned by prologue bytes | check; live (swap, data); "Reroll needs an update" | `regen gen_reroll_parts gen_reroll_legendaries`; RVAs: `native.py` "The swap" |
| Build Presets | equip call (anchor pattern + reflected layout check); `trees.json` | live "native equip" | `mods/bl4_builds/native.py`; `regen gen_builds_trees` |
| ECHO Anywhere | call chain from `CanOpenStatusMenu` (follows code, checks against reflection) | live; log "this game version isn't supported" | `native.py` docstring |
| Third Person | camera mode defs found by heap scan (vanilla values, vtables); mode names from NCS `camera_mode` | live "camera defs"; log "doesn't work on this game version" | its `camera_defs.py` "Verified"; `tools/ncs_parse.py --find camMode_exo_heavyarms` for the values |
| Backpack Size | Lost Loot subsystem offsets (unreflected, self-checked) | live; log "Lost Loot left alone" | `limits.py` (Lost Loot offsets) |
| Crisp Numbers | console manager helper RVA (byte-checked) | live "cvar lookup"; log "[Crisp Damage Numbers] off" | `cvars.py` docstring; `dev/perf/crisp_bench.py` |
| BL4 Direct | arrival fix (2 exe patterns); `SteamNetDriver`; coop RPCs `ServerSendTextChat` / `ClientMessage` (their `Text` / `S` params) | live "arrival fix", hooks; log "can't read the RPC's ..." | `arrival.py` + `docs/fast-travel-investigation.md` |
| Minimap | `maps.json`; GUObjectArray code RVAs + giver vtable; fog grid, waypoint and route struct offsets | check (`gen_minimap`, not runtime-checked); live "live objectives / route" | `regen gen_minimap`; `objectives.py` (the giver vtable has no self-check: re-derive it with the RVAs) |
| Instant Fast Travel | `stations.json` (checked) | check; live; log "goes through the portal" | `regen gen_instant_travel` |
| UVH Teleport | `bosses.json` (not runtime-checked); challenges page DOM | check | `regen gen_uvh_teleport`; `ui/challenges.js` selectors |
| Skip Intro | `Movies/2KLOGO.bk2`, `GBXLOGO.bk2` (a patch restoring them is fine: renamed again at start) | check "Loose game files" | `LOGOS` in `movies.py` |
| Performance Fixes | cvars `gbx.GbxFrameGenDisableMode`, `r.NGX.*`, `r.Streamline.DLSSG.*` (not checked for existence) | none (a missing cvar reads 0) | `render.py`; re-measure with `dev/perf/` |
| Tidy Pause | `#def_menu_news_pause`, `#def_lobby_info_pause` in the pause page | none (does nothing silently) | `ui/pause.js` |
| Less Fog, Vehicles Anywhere, Shoot to Open, ported mods | reflected classes / properties / blueprint paths only | log ("Failed to import mod", "(logged once)" errors); live hooks | rename in code; blueprint hooks (Encore, Grapple, Slam) need their asset loaded to show in `live` |

## Keeping this working

- A new generator goes into `GENERATORS` in `tools/game_patch.py` (run order, `needs`, what
  players get while it's stale), then `regen <it>` to record its inputs.
- A new native part or step-aside state gets a probe in `dev/patch_status.py`.
- `python tools/game_patch.py baseline` regenerates everything into a scratch copy and diffs it
  against the repo, then rewrites the pins. Use it when the pins are lost, or to prove the
  shipped data is what the install gives. 2026-10-28, build 26007962: 32 of 34 files
  byte-identical. The other two were the same data in another order: `rolls.json` /
  `legendaries.json` were made before the fixed hash seed. Kairosdex's roll test passed on both
  (6,360 rolls).
- After shipping regenerated data, `check` should be clean. If it isn't, the pins and the
  shipped files have drifted.
- Gaps worth closing in each mod (not done):
  - runtime checks for the unchecked data above;
  - Minimap's giver vtable self-check;
  - Instant Tabs' DELAY site (pattern only, no string check);
  - Reroll's slot / gear offsets against reflection (Build Presets has `_check_slot_layout`);
  - Performance Fixes' cvar existence;
  - module-level `find_class` in 9 mods (a rename removes the whole mod);
  - Slam Tweaks retrying missing paths every second without saying so.
