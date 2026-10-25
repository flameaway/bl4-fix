# Bridge script: hot-reload BL4 Direct's F6 page (page.py, keyboard.py) and panel (ui.py) inside a
# running game (dev only). Reload the Mod Menu first if its menu.py changed (dev/reload_mod_menu.py).
#   python tools/bridge.py <id> -f dev/reload_direct_page.py
# __init__ (hosting state, hooks) is left alone; what the page needs from it that a game started
# with an older copy lacks is patched onto PanelApi below.
import importlib
import sys

import bl4_direct
import bl4_direct.ui as old_ui
from bl4_direct.shared import DEFAULT_PORT, Config, Friend, normalize_ipv4

old_ui.uninstall()
if (old_page := sys.modules.get("bl4_direct.page")) is not None:
    old_page.unregister()

for name in ("bl4_direct.keyboard", "bl4_direct.page"):
    if name in sys.modules:
        importlib.reload(sys.modules[name])
    else:
        importlib.import_module(name)

api = bl4_direct.PanelApi
if not hasattr(api, "config"):
    api.config = staticmethod(lambda: Config.load(bl4_direct.FRIENDS_FILE))

    def add_friend(name: str, address: str, port: int = DEFAULT_PORT) -> None:
        config = Config.load(bl4_direct.FRIENDS_FILE)
        config.friends = [f for f in config.friends if f.name.lower() != name.lower()]
        config.friends.append(Friend(name, normalize_ipv4(address) or address, port))
        config.save(bl4_direct.FRIENDS_FILE)
        bl4_direct.refresh_friends()

    api.add_friend = staticmethod(add_friend)

ui = importlib.reload(old_ui)
ui.api = api
# The "Open key" option, for a game started before it existed
key_option = getattr(bl4_direct, "open_key", None)
if key_option is None:
    from mods_base import KeybindOption

    key_option = bl4_direct.open_key = KeybindOption("Open key", ui.DEFAULT_KEY, description=(
        "Opens and closes the BL4 Direct page (host, join and add friends). Works in game, in the"
        " game's own menus and at the title screen. Unbound: open it from the Mods screen."
    ))
    bl4_direct.mod.options.insert(0, key_option)
    bl4_direct.panel_button.display_name = bl4_direct.panel_button.identifier = "Open the basic panel"
ui.open_key = lambda: key_option.value
bl4_direct.ui = ui
bl4_direct.page = sys.modules["bl4_direct.page"]
ui.install()
print("bl4 direct page + panel reloaded")
