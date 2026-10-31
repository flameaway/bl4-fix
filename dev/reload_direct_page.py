# Bridge script: hot-reload BL4 Direct's F6 page (page.py, clipboard.py) and panel (ui.py) inside a
# running game (dev only). Reload the Mod Menu first if its menu.py changed (dev/reload_mod_menu.py).
#   python tools/bridge.py <id> -f dev/reload_direct_page.py
# __init__ (hosting state, hooks) is left alone; what the page needs from it that a game started
# with an older copy lacks is patched onto PanelApi below.
import importlib
import sys

import bl4_direct
import bl4_direct.ui as old_ui

# shared first: page and ui import from it (UAC_HINT, the netsh firewall rule)
shared = importlib.reload(sys.modules["bl4_direct.shared"])
DEFAULT_PORT, Config, Friend, normalize_ipv4 = shared.DEFAULT_PORT, shared.Config, shared.Friend, shared.normalize_ipv4

old_ui.uninstall()
if (old_page := sys.modules.get("bl4_direct.page")) is not None:
    old_page.unregister()

for name in ("bl4_direct.clipboard", "bl4_direct.page"):
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

if not hasattr(bl4_direct, "netsh_command"):
    # A game started before the netsh firewall update (2026-10-30): same as PanelApi.update_firewall now
    def update_firewall() -> str | None:
        config = Config.load(bl4_direct.FRIENDS_FILE)
        signature, spec = config.firewall_signature(), config.firewall_spec()

        def args() -> str | None:
            return shared.netsh_command(spec, [name for name, _ in shared.find_rules()])

        def applied() -> None:
            latest = Config.load(bl4_direct.FRIENDS_FILE)
            latest.firewall_applied = signature
            latest.save(bl4_direct.FRIENDS_FILE)
            bl4_direct.log(f"Firewall rule updated ({signature}).")

        refused = "Firewall update cancelled - they can't join your game until you allow it."
        failed = "The firewall update failed - try Update again, or re-run BL4 Fix Setup."
        if not bl4_direct.ui.run_elevated(str(shared.NETSH), args, refused, applied, failed):
            return "Windows is already asking about the firewall - answer that prompt first."
        return None

    api.update_firewall = staticmethod(update_firewall)

if "update_firewall" in api.host.__code__.co_names:
    # A game started before hosting stopped asking about the firewall (2026-10-30)
    def host_only() -> str | None:
        bl4_direct.host()
        return None

    api.host = staticmethod(host_only)
bl4_direct.ui = ui
bl4_direct.page = sys.modules["bl4_direct.page"]
ui.install()
print("bl4 direct page + panel reloaded")
