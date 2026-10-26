"""
The setup app's window: an HTML page (ui/) shown through pywebview on Edge WebView2.

The page calls `Api` methods (each call runs on its own pywebview thread, so slow ones like the
firewall lookup don't freeze the page); long tasks (install, uninstall, firewall) run on a worker
and report back through `app.onLog` / `app.onTaskDone` in the page. gui.py (Tkinter) is the
fallback for PCs without the WebView2 runtime.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import winreg
from pathlib import Path

import catalog
import core
import shared

HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
UI_DIR = HERE / "ui"

# The in-game keys shown on the mod cards
MOD_KEYS = {
    "bl4_direct": "F6",
    "bl4_mod_menu": "F7",
    "bl4_weapondex": "F8",
    "bl4_minimap": "N",
    "bl4_third_person": "H",
    "bl4_uvh_teleport": "T",
    "bl4_reroll": "T / G",
}

# WebView2 runtime client ids (stable, beta, dev, canary), under EdgeUpdate\Clients
_WEBVIEW2_CLIENTS = (
    "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
    "{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}",
    "{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}",
    "{65C35B14-6C1D-4122-AC46-7148CC9D6497}",
)


def webview2_available() -> bool:
    """Without the runtime pywebview silently falls back to the old IE engine: use Tk then."""
    for client in _WEBVIEW2_CLIENTS:
        for hive, path in (
            (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{client}"),
            (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{client}"),
        ):
            try:
                with winreg.OpenKey(hive, path) as key:
                    version = str(winreg.QueryValueEx(key, "pv")[0])
            except OSError:
                continue
            if version and version != "0.0.0.0" and int(version.split(".")[0]) >= 86:
                return True
    return False



class Api:
    def __init__(self) -> None:
        self._window = None  # set by run(); not a public attribute, so pywebview doesn't walk it
        self._busy = False
        self._lock = threading.Lock()

    # region page plumbing

    def _js(self, func: str, *args: object) -> None:
        if self._window is not None:
            self._window.evaluate_js(f"window.app && app.{func}({', '.join(json.dumps(a) for a in args)})")

    def _log(self, msg: str) -> None:
        self._js("onLog", str(msg))

    def _task(self, name: str, func) -> bool:  # noqa: ANN001
        with self._lock:
            if self._busy:
                return False
            self._busy = True

        def worker() -> None:
            error = None
            try:
                func()
            except Exception as ex:  # noqa: BLE001
                error = str(ex)
                self._log(f"Error: {ex}")
            finally:
                self._busy = False
                self._js("onTaskDone", name, error)

        threading.Thread(target=worker, daemon=True).start()
        return True

    def drag(self) -> None:
        """
        Mousedown on the title bar: hand the drag to Windows, as if the native title bar was grabbed.
        pywebview's own drag (.pywebview-drag-region) moves the window from the page's screenX, which
        WebView2 keeps computing from where the window started, so a second drag jumped back.
        """
        from System import Action  # noqa: PLC0415  # pythonnet, loaded by pywebview's WinForms backend

        form = self._window.native

        def start() -> None:  # on the WinForms thread, which owns the window and its mouse capture
            if not _user32.GetAsyncKeyState(0x01) & 0x8000:  # VK_LBUTTON: released already
                return
            _user32.ReleaseCapture()
            _user32.SendMessageW(form.Handle.ToInt64(), 0x00A1, 2, 0)  # WM_NCLBUTTONDOWN, HTCAPTION

        form.BeginInvoke(Action(start))

    def minimize(self) -> None:
        self._window.minimize()

    def close(self) -> None:
        if not self._busy:
            self._window.destroy()

    # endregion
    # region setup

    def init(self) -> dict:
        found = core.find_game_dirs()
        return {
            "version": shared.MOD_VERSION,
            "sdk_version": shared.SDK_VERSION,
            "game_dir": str(found[0]) if found else "",
            "default_port": shared.DEFAULT_PORT,
            "lan_ip": core.lan_ip() or "",
            "mods": [
                {
                    "id": m.id,
                    "name": m.name,
                    "blurb": m.blurb,
                    "source": m.source,
                    "required": m.required,
                    "default": m.default,
                    "key": MOD_KEYS.get(m.id, ""),
                }
                for m in catalog.MODS
            ],
        }

    def status(self, game_dir: str) -> dict:
        path = Path(game_dir)
        if not game_dir or not core.is_game_dir(path):
            return {"valid": False}
        st = core.status(path)
        config = core.load_config(path)
        vc = ".".join(map(str, st.vcredist)) if st.vcredist else None
        need_vc = ".".join(map(str, core.VCREDIST_MIN))
        return {
            "valid": True,
            "ready": st.ready,
            "running": core.game_running(),
            "installed": sorted(st.mods),
            "chosen": sorted(core.choose_mods(path)),
            "dev_mods": sorted(st.dev_mods),
            "conflicts": st.conflicts,
            "compat": shared.compat_code(path),
            "checks": [
                {"key": "vc", "label": "VC++ Runtime", "ok": st.vcredist_ok,
                 "value": vc or "missing", "hint": f"needs {need_vc}+ (installed for you)"},
                {"key": "sdk", "label": "Python SDK", "ok": st.sdk_ok,
                 "value": st.sdk or "missing", "hint": f"needs {shared.SDK_VERSION}"},
                {"key": "mod", "label": "BL4 Direct Connect", "ok": st.mod_ok,
                 "value": st.mod or "missing", "hint": f"this app: {shared.MOD_VERSION}"},
                {"key": "fw", "label": "Firewall", "ok": st.firewall is not None and config.firewall_up_to_date(),
                 "value": "locked" if st.firewall else "no rule",
                 "hint": st.firewall or "hosted games reachable by anyone who can reach the port"},
                {"key": "intro", "label": "Skip Intro", "ok": True,
                 "value": st._intro_line(), "hint": ""},  # noqa: SLF001
            ],
        }

    def running(self) -> bool:
        return core.game_running()

    def browse(self) -> str | None:
        import webview  # noqa: PLC0415

        picked = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not picked:
            return None
        return str(picked[0] if isinstance(picked, (list, tuple)) else picked)

    def is_game_dir(self, game_dir: str) -> bool:
        return bool(game_dir) and core.is_game_dir(Path(game_dir))

    def launch_game(self, game_dir: str) -> str:
        """
        Starts Borderlands 4: through Steam for a Steam install, else its exe. This app runs
        elevated, and Steam started from here would run as admin too, so explorer.exe opens it:
        the running (unelevated) shell takes the request over. Returns an error, or "".
        """
        path = Path(game_dir)
        if not game_dir or not core.is_game_dir(path):
            return "Pick the Borderlands 4 folder first."
        if core.game_running():
            return "Borderlands 4 is already running."
        steam = any(part.lower() == "steamapps" for part in path.parts)
        target = f"steam://rungameid/{shared.STEAM_APP_ID}" if steam else str(path / core.GAME_EXE_REL)
        subprocess.Popen(["explorer.exe", target], creationflags=subprocess.CREATE_NO_WINDOW)  # noqa: S603, S607
        return ""

    def open_folder(self, game_dir: str) -> None:
        if game_dir and Path(game_dir).is_dir():
            os.startfile(game_dir)  # noqa: S606

    def install(self, game_dir: str, mods: list[str]) -> bool:
        path = Path(game_dir)
        return self._task("install", lambda: core.install(path, self._log, mods=set(mods)))

    def uninstall(self, game_dir: str, remove_sdk: bool) -> bool:
        path = Path(game_dir)
        return self._task("uninstall", lambda: core.uninstall(path, bool(remove_sdk), self._log))

    # endregion
    # region friends

    def public_ip(self) -> str:
        try:
            return core.public_ip()
        except RuntimeError:
            return ""

    def load_friends(self, game_dir: str) -> dict:
        config = core.load_config(Path(game_dir)) if self.is_game_dir(game_dir) else shared.Config()
        return {
            "host_port": config.host_port,
            "allow_lan": config.allow_lan,
            "friends": [{"name": f.name, "address": f.address, "port": f.port} for f in config.friends],
            "firewall_current": config.firewall_up_to_date(),
        }

    def normalize_ip(self, text: str) -> str:
        return shared.normalize_ipv4(text or "") or ""

    def save_friends(self, game_dir: str, data: dict) -> bool:
        path = Path(game_dir)
        friends = []
        for f in data.get("friends", []):
            address = shared.normalize_ipv4(str(f.get("address", "")))
            if f.get("name") and address:
                friends.append(shared.Friend(str(f["name"]).strip(), address, int(f.get("port") or shared.DEFAULT_PORT)))
        config = shared.Config(
            host_port=max(1024, min(65535, int(data.get("host_port") or shared.DEFAULT_PORT))),
            allow_lan=bool(data.get("allow_lan", True)),
            friends=friends,
        )

        def save() -> None:
            config.save(core.friends_file(path))
            self._log("Saved friends list.")
            core.apply_firewall(path, self._log)

        return self._task("friends", save)

    # endregion


# Our own user32 instance, so these prototypes don't change the shared ctypes.windll one
_user32 = ctypes.WinDLL("user32")
_user32.SendMessageW.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)
_user32.SendMessageW.restype = ctypes.c_ssize_t
_user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
_user32.GetAsyncKeyState.restype = ctypes.c_short


def _round_corners(window) -> None:  # noqa: ANN001
    """Windows 11 rounds and shadows a frameless window when asked (DWMWA_WINDOW_CORNER_PREFERENCE)."""
    try:
        hwnd = window.native.Handle.ToInt64()
        pref = ctypes.c_int(2)  # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 33, ctypes.byref(pref), 4)
    except Exception:  # noqa: BLE001 - Windows 10 has no such attribute; square corners are fine
        pass


def _window_size() -> tuple[int, int]:
    """
    16:9 like the game, so its layouts (sized in rem = 1/108 of the height) fit: about 80% of the
    work area, at most 1600 wide. Read before pywebview makes the process DPI aware, so these are
    the logical pixels it expects.
    """

    class Rect(ctypes.Structure):
        _fields_ = (("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long))

    area = Rect()
    if not ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(area), 0):  # SPI_GETWORKAREA
        return 1280, 720
    width = min(1600, int((area.right - area.left) * 0.82), int((area.bottom - area.top) * 0.85 * 16 / 9))
    width = max(960, width)
    return width, width * 9 // 16


def run() -> None:
    import webview  # noqa: PLC0415

    api = Api()
    width, height = _window_size()
    window = webview.create_window(
        f"BL4 Fix {shared.MOD_VERSION}",
        url=str(UI_DIR / "index.html"),
        js_api=api,
        width=width,
        height=height,
        resizable=False,
        frameless=True,
        easy_drag=False,
        background_color="#00030a",
    )
    api._window = window  # noqa: SLF001
    window.events.shown += lambda: _round_corners(window)
    debug = bool(os.environ.get("BL4_SETUP_DEBUG"))
    webview.start(gui="edgechromium", http_server=True, debug=debug, private_mode=True)
