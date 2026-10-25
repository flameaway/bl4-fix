"""
Exploration helpers, exec'd into the bridge namespace (re-run `reload_helpers()` after editing).
Everything here runs on the game thread.
"""

# ruff: noqa: F821  - `event`, `unrealsdk`, `hooks` come from the bridge namespace

import re

from mods_base import ENGINE, get_pc

KSL = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject


def cmd(command: str) -> None:
    """Runs a console command as the local player."""
    pc = get_pc()
    KSL.ExecuteConsoleCommand(pc, command, pc)


def world():
    return ENGINE.GameViewport.World if ENGINE.GameViewport else None


def dump(cls_or_name, pattern: str = "", *, deep: bool = False, funcs: bool = True) -> None:
    """Prints a class's properties (and functions), optionally filtered by a regex."""
    cls = unrealsdk.find_class(cls_or_name) if isinstance(cls_or_name, str) else cls_or_name
    pat = re.compile(pattern, re.I)
    structs = list(cls._superfields()) if deep else [cls]
    for struct in structs:
        if struct.Name in ("Object", "Actor") and cls.Name not in ("Object", "Actor"):
            break
        lines = []
        for prop in struct._properties():
            if prop.Owner == struct and pat.search(prop.Name):
                lines.append(f"    {type(prop).__name__[1:]} {prop.Name}")
        if funcs:
            for field in struct._fields():
                if field.Class.Name == "Function" and field.Outer == struct and pat.search(field.Name):
                    lines.append(f"    fn {field.Name}")
        if lines:
            print(f"== {struct._path_name()}")
            print("\n".join(lines))


def props(obj, pattern: str = "") -> None:
    """Prints an object's current property values, optionally filtered by a regex."""
    pat = re.compile(pattern, re.I)
    for prop in obj.Class._properties():
        if pat.search(prop.Name):
            try:
                value = getattr(obj, prop.Name)
            except Exception as ex:  # noqa: BLE001
                value = f"<{ex}>"
            print(f"  {prop.Name} = {value!r}"[:300])


def _format_args(args) -> str:
    try:
        return ", ".join(f"{p.Name}={getattr(args, p.Name)!r}" for p in args._type._properties())
    except Exception as ex:  # noqa: BLE001
        return f"(args unavailable: {ex})"


def trace(func: str, ident: str = "bl4_bridge.trace") -> bool:
    """Logs every call to an unreal function, with its args, to the events log."""

    def _cb(obj, args, _ret, bound):
        event(f"{bound.func.Name} on {obj.Name}: {_format_args(args)}"[:2000])

    return hooks.add_hook(func, hooks.Type.PRE, ident, _cb)


def untrace(func: str, ident: str = "bl4_bridge.trace") -> None:
    hooks.remove_hook(func, hooks.Type.PRE, ident)
