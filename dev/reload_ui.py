# Bridge script: hot-reload bl4_direct.ui inside a running game (dev only).
#   python tools/bridge.py A -f dev/reload_ui.py
import importlib
import re

import bl4_direct
import bl4_direct.ui as old_ui

was_open = old_ui.S.is_open
old_ui.uninstall()


# Functions added to __init__ since this game started (a full mod reload is riskier than this)
def stop_hosting() -> None:
    world = bl4_direct.ENGINE.GameViewport.World if bl4_direct.ENGINE.GameViewport else None
    bl4_direct.disable_direct()
    if world is None:
        return
    options = ""
    if (game_mode := world.AuthorityGameMode) is not None:
        options = re.sub(r"\?(listen|bIsLanMatch|port=\d+)", "", str(game_mode.OptionsString), flags=re.I)
    bl4_direct.console(f"open {world.Name}{options}")


def leave() -> None:
    bl4_direct.disable_direct()
    bl4_direct.console("disconnect")


if not hasattr(bl4_direct.PanelApi, "leave"):
    bl4_direct.PanelApi.leave = staticmethod(leave)
if not hasattr(bl4_direct.PanelApi, "stop_hosting"):
    bl4_direct.PanelApi.stop_hosting = staticmethod(stop_hosting)

ui = importlib.reload(old_ui)
ui.api = bl4_direct.PanelApi
bl4_direct.ui = ui
ui.install()
if was_open:
    ui.open_panel()
print("ui reloaded")
