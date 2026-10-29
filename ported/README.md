# Ported mods

Our own rewrites of community mods from the [BL4 SDK mod database](https://bl-sdk.github.io/oak2-mod-db/),
fitted to this repo's rules (game-thread only, no per-frame cost, everything restored on disable,
options and keys in the Mod Menu).

The originals are by **Yeti** ([RedxYeti/yeti-bl4-sdk](https://github.com/RedxYeti/yeti-bl4-sdk))
and are licensed GPL-3.0, so these ports are GPL-3.0 too (see `LICENSE`), like the rest of the
repo. They are modified versions (rewritten 2026-10-24 from the originals as of
commit af729fe, 2026-06-27); the table below says what changed.

| Port | Original | What changed |
|---|---|---|
| `bl4_dialog_skipper` | Dialog Skipper | Skips run on the game thread (the original used `threading.Timer`), no `find_all` per line, per-speaker filter, speed reset on disable. |
| `bl4_grapple_anywhere` | Grapple Anywhere | No pak (vanilla grapple range), one helper point at a time, cleaned up on detach / timeout / map change, vanilla points restored on disable. |
| `bl4_ground_loot` | Ground Loot Helpers | `GetAllActorsOfClass` instead of `find_all`, range option, pickups / gear switches. |
| `bl4_slam_tweaks` | TPS Style Slams | Remembers and restores every value it changes, damage as a % of the game's own value. |
| `bl4_encore_tweaks` | Encore Tweaks | Host check, per-refill switches, the helper vending machine is destroyed right after use (the original leaked one per use). |

Falling Menus was not ported: ECHO Anywhere already opens the menu while falling (the refusal is
`CanOpenStatusMenu`, which it bypasses) without forcing the walking movement mode mid-air.
