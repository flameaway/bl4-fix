"""
The mods and their options as the menus see them, and the changes the menus can make.

Options are addressed by a path of identifiers from a mod's display options (iter_display_options),
through groups and nested pages. Those lists are rebuilt on every call (mods_base makes fresh
KeybindOption proxies each time), so identity can't be kept between frames, but identifiers can.

Plain Python on mods_base objects; call it from the game thread (option callbacks may touch the game).
"""

from __future__ import annotations

import functools
import importlib
from collections.abc import Iterable, Sequence
from typing import Any

from mods_base import (
    BaseOption,
    BoolOption,
    ButtonOption,
    CoopSupport,
    DropdownOption,
    GroupedOption,
    KeybindOption,
    Mod,
    NestedOption,
    SliderOption,
    SpinnerOption,
    ValueOption,
    get_ordered_mod_list,
    html_to_plain_text,
)
from unrealsdk import logging

from . import keys

COOP_TEXT = {
    CoopSupport.Incompatible: "Co-op: incompatible",
    CoopSupport.RequiresAllPlayers: "Co-op: every player needs it",
    CoopSupport.ClientSide: "Co-op: works for just you",
    CoopSupport.HostOnly: "Co-op: host only",
}


# get_ordered_mod_list() sorts with a comparator that parses both names as HTML on every
# comparison (100+ parsers for ~25 mods), so keep its result until the registered mods change
_sorted: tuple[tuple[Any, ...], list[Mod]] = ((), [])


def mods() -> list[Mod]:
    """The mods in display order (cached; re-sorted when one is added, removed or renamed)."""
    global _sorted  # noqa: PLW0603
    try:
        registry: list[Mod] = importlib.import_module("mods_base.mod_list").mod_list
    except Exception:  # noqa: BLE001 - mods_base moved it: just sort every time
        return list(get_ordered_mod_list())
    key = tuple((id(m), m.name, m.mod_type) for m in registry)
    if key != _sorted[0]:
        _sorted = (key, list(get_ordered_mod_list()))
    return list(_sorted[1])


def find_mod(name: str | None) -> Mod | None:
    return next((m for m in mods() if m.name == name), None)


@functools.lru_cache(maxsize=1024)
def _plain(html: str) -> str:
    try:
        return html_to_plain_text(html).strip()
    except Exception:  # noqa: BLE001
        return html


def plain(html: str) -> str:
    """HTML to plain text (memoized: the same descriptions come up on every snapshot)."""
    if not isinstance(html, str):
        return html
    return _plain(html)


def visible(option: BaseOption) -> bool:
    if option.is_hidden:
        return False
    if isinstance(option, GroupedOption | NestedOption):
        return any(visible(child) for child in option.children)
    return True


def children(option: BaseOption) -> Sequence[BaseOption]:
    return option.children if isinstance(option, GroupedOption | NestedOption) else ()


def resolve(mod: Mod, path: Sequence[str]) -> BaseOption | None:
    """The option at this identifier path in the mod's display options."""
    options: Iterable[BaseOption] = mod.iter_display_options()
    found: BaseOption | None = None
    for ident in path:
        found = next((o for o in options if o.identifier == ident), None)
        if found is None:
            return None
        options = children(found)
    return found


def status(mod: Mod) -> tuple[str, str]:
    """(status text, kind): kind is on / off / warn."""
    text = plain(mod.get_status())
    # "Needs update" / "Conflict": set by the shared health package (a game update or another mod's
    # file broke part of it; its description says what)
    if any(word in text for word in ("ncompatible", "estart", "eeds update", "onflict")):
        return text, "warn"
    return text, "on" if mod.is_enabled else "off"


def fmt_number(option: SliderOption, value: float) -> str:
    if option.is_integer:
        return str(round(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def value_text(option: ValueOption[Any], value: Any) -> str:
    if isinstance(option, BoolOption):
        return (option.true_text or "On") if value else (option.false_text or "Off")
    if isinstance(option, SliderOption):
        return fmt_number(option, value)
    if isinstance(option, KeybindOption):
        return keys.display_name(value)
    return str(value)


def slider_value(option: SliderOption, value: float) -> float:
    """Snaps to the slider's steps and range."""
    lo, hi = option.min_value, option.max_value
    if option.step > 0:
        value = lo + round((value - lo) / option.step) * option.step
    value = min(hi, max(lo, value))
    return round(value) if option.is_integer else value


def is_default(option: BaseOption) -> bool:
    return not isinstance(option, ValueOption) or not hasattr(option, "default_value") or (
        option.value == option.default_value  # pyright: ignore[reportUnknownMemberType]
    )


# region JSON for the HTML menu


def option_json(option: BaseOption) -> dict[str, Any] | None:
    if not visible(option):
        return None
    node: dict[str, Any] = {
        "id": option.identifier,
        "name": option.display_name,
        "title": option.description_title or option.display_name,
        "desc": plain(option.description),
    }
    match option:
        case GroupedOption() | NestedOption():
            node["kind"] = "group" if isinstance(option, GroupedOption) else "nested"
            node["children"] = [n for c in option.children if (n := option_json(c)) is not None]
            return node
        case BoolOption():
            node.update(kind="bool", value=bool(option.value),
                        off=option.false_text or "Off", on=option.true_text or "On")  # fmt: skip
        case SliderOption():
            node.update(kind="slider", value=option.value, min=option.min_value, max=option.max_value,
                        step=option.step, int=option.is_integer, text=fmt_number(option, option.value))  # fmt: skip
        case DropdownOption() | SpinnerOption():
            node.update(kind="choice", value=option.value, choices=list(option.choices),
                        wrap=isinstance(option, DropdownOption) or option.wrap_enabled)  # fmt: skip
        case KeybindOption():
            node.update(kind="keybind", value=option.value, text=keys.display_name(option.value),
                        rebindable=option.is_rebindable)  # fmt: skip
        case ButtonOption():
            node["kind"] = "button"
        case ValueOption():
            node.update(kind="value", text=str(option.value))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        case _:
            return None
    if isinstance(option, ValueOption) and hasattr(option, "default_value"):
        node["default"] = value_text(option, option.default_value)  # pyright: ignore[reportUnknownMemberType]
        node["changed"] = not is_default(option)
    return node


def mod_json(mod: Mod) -> dict[str, Any]:
    text, kind = status(mod)
    return {
        "name": mod.name,
        "author": mod.author or "",
        "version": mod.version or "",
        "desc": plain(mod.description),
        "status": text,
        "status_kind": kind,
        "enabled": mod.is_enabled,
        "can_toggle": not mod.enabling_locked,
        "coop": COOP_TEXT.get(mod.coop_support, ""),
        "options": [n for o in mod.iter_display_options() if (n := option_json(o)) is not None],
    }


# endregion
# region Changes


class ChangeError(Exception):
    pass


def set_value(mod: Mod, option: ValueOption[Any], value: Any) -> None:
    """Sets an option (running its on_change) and saves the mod's settings."""
    try:
        option.value = value
        mod.save_settings()
    except Exception as ex:
        logging.exception(ex)
        raise ChangeError(f"Couldn't change {option.display_name}: {ex}") from ex


def set_from_json(mod: Mod, option: BaseOption, value: Any) -> None:
    """Applies a value sent by the HTML menu, checked and converted for the option's type."""
    match option:
        case BoolOption():
            set_value(mod, option, bool(value))
        case SliderOption():
            set_value(mod, option, slider_value(option, float(value)))
        case DropdownOption() | SpinnerOption():
            if str(value) not in option.choices:
                raise ChangeError(f"{value} isn't a choice for {option.display_name}")
            set_value(mod, option, str(value))
        case KeybindOption():
            if value is not None and str(value) not in keys.VIRTUAL_KEYS:
                raise ChangeError(f"Unknown key {value}")
            set_value(mod, option, None if value is None else str(value))
        case _:
            raise ChangeError(f"{option.display_name} can't be set")


def reset(mod: Mod, option: BaseOption) -> None:
    if isinstance(option, ValueOption) and hasattr(option, "default_value"):
        set_value(mod, option, option.default_value)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]


def press(option: BaseOption) -> None:
    if not isinstance(option, ButtonOption) or option.on_press is None:
        return
    try:
        option.on_press(option)
    except Exception as ex:
        logging.exception(ex)
        raise ChangeError(f"{option.display_name} failed: {ex}") from ex


def set_enabled(mod: Mod, enabled: bool) -> str:
    try:
        if enabled:
            mod.enable()
        else:
            mod.disable()
    except Exception as ex:
        logging.exception(ex)
        raise ChangeError(f"Couldn't {'enable' if enabled else 'disable'} {mod.name}: {ex}") from ex
    return f"{mod.name} {'enabled' if mod.is_enabled else 'disabled'}."


# endregion
