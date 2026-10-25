"""
Engine settings (console variables) we hold while the mod is on, and put back when it's off.

Found with dev/perf/sampler.py (game build 26007962):
- r.NGX.FramesUntilFeatureDestruction (game: 3). The DLSS plugin destroys an NGX feature after it
  goes unused for 3 frames. Over 15 min of menu use the DLSS upscaler was rebuilt once with the
  game's value (a ~400 ms frame) and never with 100000: weak evidence, hence experimental.
- r.Streamline.DLSSG.RetainResourcesWhenOff (game: 0). Asks Streamline to keep frame generation's
  resources while it's off. Measured: no effect on the frame-generation rebuilds (15 -> 21 in two
  15-minute windows), which happen inside the frame-generation runtime itself. Harmless, kept so a
  native (RTX 40/50) DLSS-G gets the same request; unverified there.

Each value is read before we first set it and written back on disable. The game may apply its own
values again (settings menu, map load), so the tick re-checks them every few seconds.
"""

from __future__ import annotations

from mods_base import get_pc
import unrealsdk

# cvar -> value we want, per option
DLSS: dict[str, int] = {
    "r.NGX.FramesUntilFeatureDestruction": 100000,
    "r.Streamline.DLSSG.RetainResourcesWhenOff": 1,
}
# Gearbox's switch: 0 always allow, 1 off in menus, 2 off in cinematics, 3 off in both (game's value).
# Every off -> on rebuilds frame generation; with the DlssgSm86 enabler (RTX 30) that is a
# 120-540 ms freeze each time a menu closes. Measured: unpause in front with 3 -> rebuild 0.1 s
# later; with 0 -> none. Focus changes still rebuild (frame generation stops while the window is
# in the background).
FRAMEGEN: dict[str, int] = {"gbx.GbxFrameGenDisableMode": 0}


class _State:
    ksl = None
    original: dict[str, int] = {}  # cvar -> the game's value before we changed it


def _ksl():
    if _State.ksl is None:
        _State.ksl = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject
    return _State.ksl


def _set(name: str, value: int) -> bool:
    pc = get_pc()
    if pc is None:
        return False
    _ksl().ExecuteConsoleCommand(pc, f"{name} {value}", pc)
    return True


def enforce(wanted: dict[str, int]) -> None:
    """Sets every wanted cvar that differs (~10 us per cvar when nothing changes)."""
    ksl = _ksl()
    for name, value in wanted.items():
        cur = ksl.GetConsoleVariableIntValue(name)
        if cur == value:
            continue
        _State.original.setdefault(name, cur)
        _set(name, value)


def restore(names: list[str] | None = None) -> None:
    """Puts back the game's values (all, or the given cvars)."""
    for name in list(_State.original) if names is None else names:
        if name in _State.original and _set(name, _State.original[name]):
            del _State.original[name]
