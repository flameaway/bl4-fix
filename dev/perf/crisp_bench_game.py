# Runs inside the game through the bridge: helpers for the crisp-numbers bench (one namespace object)
import time
import unrealsdk
from unrealsdk import hooks
from unrealsdk.hooks import Type
from unrealsdk.unreal import WeakPointer
from mods_base import get_pc, ENGINE


class _CrispBench:
    TICK = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
    LOAD_ID = "crisp_bench_load"

    def __init__(self):
        self.nfl = unrealsdk.find_class("NiagaraFunctionLibrary").ClassDefaultObject
        self.kml = unrealsdk.find_class("KismetMathLibrary").ClassDefaultObject
        self.ksl = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject
        self.numbers = unrealsdk.find_object("NiagaraSystem", "/Game/Common/Effects/Systems/Damage/NS_Damage_Numbers.NS_Damage_Numbers")
        self.fx = {}
        self.load_state = {"next": 0.0, "nextnum": 0.0, "n": 0}

    def system(self, path):
        if path not in self.fx:
            name = path.rsplit("/", 1)[1]
            self.fx[path] = unrealsdk.find_object("NiagaraSystem", f"{path}.{name}")
        return self.fx[path]

    def point(self, dist, right=0.0, up=0.0):
        cm = get_pc().PlayerCameraManager
        loc, rot = cm.GetCameraLocation(), cm.GetCameraRotation()
        f, r, u = self.kml.GetForwardVector(rot), self.kml.GetRightVector(rot), self.kml.GetUpVector(rot)
        return unrealsdk.make_struct("Vector", X=loc.X + f.X * dist + r.X * right + u.X * up,
                                     Y=loc.Y + f.Y * dist + r.Y * right + u.Y * up,
                                     Z=loc.Z + f.Z * dist + r.Z * right + u.Z * up)

    def _spawn(self, system, p):
        return self.nfl.SpawnSystemAtLocation(ENGINE.GameViewport.World, system, p, unrealsdk.make_struct("Rotator"),
                                              unrealsdk.make_struct("Vector", X=1, Y=1, Z=1), True, False, 0, True)

    def number(self, value=12345, dist=400.0, right=0.0, up=0.0, crit=False, element=0, life=2.0, stop_after=0.02):
        """One damage number: the system keeps emitting while active, so emission is stopped right
        after the first particle (Deactivate lets live particles finish)."""
        comp = self._spawn(self.numbers, self.point(dist, right, up))
        if comp is None:
            return False
        comp.SetVariableInt("User.DamageNumber", int(value))
        comp.SetVariableFloat("User.DamageAmount", float(value))
        comp.SetVariableInt("User.Crit", 1 if crit else 0)
        comp.SetVariableInt("User.Element", element)
        comp.SetVariableFloat("User.Lifetime", life)
        comp.Activate(True)
        self._stops.append((time.perf_counter() + stop_after, WeakPointer(comp)))
        return True

    def after(self, delay, fn):
        """Runs fn on the game thread `delay` seconds from now (from the stop tick)."""
        self._stops.append((time.perf_counter() + delay, fn))

    def _stop_tick(self, *_a):
        try:
            if not self._stops:
                return
            now, keep = time.perf_counter(), []
            for t, w in self._stops:
                if now >= t:
                    if isinstance(w, WeakPointer):
                        c = w()
                        if c is not None:
                            c.Deactivate()
                    else:
                        w()
                else:
                    keep.append((t, w))
            self._stops = keep
        except Exception as ex:  # noqa: BLE001
            self.load_state["stop_err"] = repr(ex)

    def stops_on(self):
        self._stops = []
        hooks.add_hook(self.TICK, Type.POST, "crisp_bench_stop", self._stop_tick)

    def stops_off(self):
        hooks.remove_hook(self.TICK, Type.POST, "crisp_bench_stop")

    def effect(self, path, dist=800.0, right=0.0, up=0.0):
        comp = self._spawn(self.system(path), self.point(dist, right, up))
        if comp is None:
            return False
        comp.Activate(True)
        return True

    def cvar(self, name, value):
        self.ksl.ExecuteConsoleCommand(ENGINE.GameViewport.World, f"{name} {value}", None)
        return float(self.ksl.GetConsoleVariableFloatValue(name))

    def get(self, name):
        return float(self.ksl.GetConsoleVariableFloatValue(name))

    # steady effects load for the GPU measurements: explosions + smoke + numbers in view
    def load_on(self, fx_period=0.5, num_period=0.15):
        st = self.load_state
        bench = self
        paths = ["/Game/Common/Effects/Systems/Explosions/Fire/NS_Explosion_Fire",
                 "/Game/Common/Effects/Systems/Smoke/NS_Smoke_Heavy_02",
                 "/Game/Common/Effects/Systems/Explosions/Shock/NS_Explosion_Shock"]

        def cb(*_a):
            try:
                now = time.perf_counter()
                if now >= st["next"]:
                    st["next"] = now + fx_period
                    st["n"] += 1
                    k = st["n"]
                    bench.effect(paths[k % len(paths)], 700.0 + (k % 3) * 150, ((k * 137) % 600) - 300, ((k * 71) % 200) - 100)
                if now >= st["nextnum"]:
                    st["nextnum"] = now + num_period
                    k = st["n"]
                    bench.number(1000 + k * 37, 450.0, ((k * 53) % 400) - 200, 0, crit=(k % 4 == 0))
            except Exception as ex:  # noqa: BLE001
                st["err"] = repr(ex)

        hooks.add_hook(self.TICK, Type.POST, self.LOAD_ID, cb)

    def load_off(self):
        hooks.remove_hook(self.TICK, Type.POST, self.LOAD_ID)


import ctypes
import ctypes.wintypes as _wt
import os
import threading


class _Grab:
    """Full-resolution window captures (PrintWindow, PW_RENDERFULLCONTENT) on worker threads, raw
    BGRA + a small header, so the game thread only starts them. Win32 only, no unreal objects."""

    def __init__(self):
        u, g, k = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32"), ctypes.WinDLL("kernel32")
        self.u, self.g = u, g
        u.GetForegroundWindow.restype = ctypes.c_void_p
        self.EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        u.EnumWindows.argtypes = (self.EnumProc, ctypes.c_void_p)
        u.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        u.IsWindowVisible.argtypes = (ctypes.c_void_p,)
        u.GetWindowRect.argtypes = (ctypes.c_void_p, ctypes.POINTER(_wt.RECT))
        u.GetWindowDC.argtypes = (ctypes.c_void_p,)
        u.GetWindowDC.restype = ctypes.c_void_p
        u.ReleaseDC.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        u.PrintWindow.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint)
        g.CreateCompatibleDC.argtypes = (ctypes.c_void_p,)
        g.CreateCompatibleDC.restype = ctypes.c_void_p
        g.CreateCompatibleBitmap.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_int)
        g.CreateCompatibleBitmap.restype = ctypes.c_void_p
        g.SelectObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        g.SelectObject.restype = ctypes.c_void_p
        g.GetDIBits.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint)
        g.DeleteObject.argtypes = (ctypes.c_void_p,)
        g.DeleteDC.argtypes = (ctypes.c_void_p,)
        self.hwnd = self._find(k.GetCurrentProcessId())

    def _find(self, pid):
        best = [None, 0]

        def cb(h, _l):
            owner = ctypes.c_ulong()
            self.u.GetWindowThreadProcessId(h, ctypes.byref(owner))
            r = _wt.RECT()
            if owner.value == pid and self.u.IsWindowVisible(h) and self.u.GetWindowRect(h, ctypes.byref(r)):
                area = (r.right - r.left) * (r.bottom - r.top)
                if area > best[1]:
                    best[0], best[1] = h, area
            return True

        self._cb = self.EnumProc(cb)
        self.u.EnumWindows(self._cb, None)
        return best[0]

    def grab(self, path):
        u, g = self.u, self.g
        r = _wt.RECT()
        u.GetWindowRect(self.hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        wdc = u.GetWindowDC(self.hwnd)
        mdc = g.CreateCompatibleDC(wdc)
        bmp = g.CreateCompatibleBitmap(wdc, w, h)
        old = g.SelectObject(mdc, bmp)
        u.PrintWindow(self.hwnd, mdc, 2)

        class BIH(ctypes.Structure):
            _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                        ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                        ("biSizeImage", ctypes.c_uint32), ("a", ctypes.c_int32), ("b", ctypes.c_int32),
                        ("c", ctypes.c_uint32), ("d", ctypes.c_uint32)]

        bih = BIH(ctypes.sizeof(BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        g.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bih), 0)
        g.SelectObject(mdc, old)
        g.DeleteObject(bmp)
        g.DeleteDC(mdc)
        u.ReleaseDC(self.hwnd, wdc)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(w.to_bytes(4, "little") + h.to_bytes(4, "little") + buf.raw)
        os.replace(tmp, path)

    def later(self, path, delay):
        def run():
            time.sleep(delay)
            try:
                self.grab(path)
            except Exception as ex:  # noqa: BLE001
                with open(path + ".err", "w") as f:
                    f.write(repr(ex))
        threading.Thread(target=run, daemon=True).start()


crisp_bench = _CrispBench()
crisp_grab = _Grab()
print("bench ready")
