"""
Co-op feel test suite (dev only): client hit registration, the client's own rubber-banding, and
(with dev/netlab_enemies.py) enemy smoothness. exec it into BOTH bridge namespaces; the runner is
dev/netlab_suite_run.py.

Host side (the game that hosts; one client connected):
    suite_host_start()          counters: B's shots (its weapon's RemoteShotState: 2 changes per
                                shot, checked against clicks), B's hits on anything (a second hook on
                                Carnage Meter's damage callback), corrections sent to B
                                (ServerLastClientAdjustmentTime changes), host frame times
    suite_host_report() / suite_host_stop()
    suite_mover_start(near=(x, y, z)) / suite_mover_stop()
                                moves the Char_TargetDummy nearest to `near` side to side
                                (sine, MOVER_AMP_CM, MOVER_PERIOD_S): a moving target that can't die
Client side (B; its own character, its own screen):
    suite_shoot_start(target_xyz)   aims at the character nearest target_xyz each frame (mouse-look
                                input: AddYawInput / AddPitchInput; SetControlRotation is overwritten
                                by the game's camera) and posts left clicks to its own window at
                                CLICK_HZ while the aim error is under AIM_TOLERANCE_DEG
                                (Enhanced Input's InjectInput for Action_Fire doesn't fire here)
    suite_move_start(cx, cy, radius_cm)   runs and jumps a lot (JUMP_EVERY_S)
    suite_client_report() / suite_client_stop()
                                self snaps: the own character moved more than SNAP_CM beyond what its
                                velocity explains in one frame (a correction from the host)
"""

# ruff: noqa: F821 - unrealsdk, hooks, get_pc, ENGINE come from the bridge namespace

import ctypes
import math
import os
import random
import time
from ctypes import wintypes

from unrealsdk.unreal import WeakPointer

_SU_TICK = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
MOVER_AMP_CM = 300.0  # +-3 m
MOVER_PERIOD_S = 4.0  # peak speed 2*pi*A/T = ~4.7 m/s
CLICK_HZ = 3.0
RELOAD_EVERY_S = 3.0  # posted R: reloading a full magazine does nothing
CLICK_HOLD_S = 0.05
AIM_TOLERANCE_DEG = 0.6
AIM_HEIGHT = 1.2  # x capsule half height above the feet: chest
JUMP_EVERY_S = 1.2
SNAP_CM = 50.0
STALL_S = 0.25

SUITE = globals().setdefault("SUITE", {})


def _su_gate(st, now):
    """True once per frame (the camera hook fires ~6 times a frame)."""
    gap = now - st.get("last_call", 0.0)
    st["last_call"] = now
    if gap < 0.004 and now - st.get("last_frame", 0.0) < 0.012:
        return False
    st["last_frame"] = now
    return True


def _su_hook(name, fn):
    hooks.remove_hook(_SU_TICK, hooks.Type.POST, name)
    if fn is not None:
        hooks.add_hook(_SU_TICK, hooks.Type.POST, name, fn)


# region host


def _su_client_pc():
    nd = ENGINE.GameViewport.World.NetDriver
    conns = list(nd.ClientConnections) if nd else []
    return conns[0].PlayerController if conns else None


def suite_host_start():
    import sys  # noqa: PLC0415

    native = sys.modules["bl4_damage_meter.native"]
    tracker = sys.modules["bl4_damage_meter.tracker"]
    cpc = _su_client_pc()
    pawn = cpc.Pawn
    st = {
        "pawn": WeakPointer(pawn), "weapon": None, "weapon_check": 0.0, "shot_state": None,
        "shot_changes": 0, "hits": {}, "hit_shots": set(), "skipped": 0, "corr": 0, "adj": None, "frames": [],
        "errors": 0,
        "t0": time.perf_counter(),
    }

    def hit_hook(obj, args, _ret, _func, _tr=tracker):
        try:
            if obj._get_address() != _tr.S.binder.cdo_addr:
                return None
            b = st["pawn"]()
            d = args.Details
            if b is not None and d.DamageInstigator == b:
                w = st["weapon"]() if st["weapon"] else None
                # Bullet hits only: no damage-over-time ticks, no splash, caused by the gun itself
                if d.bIsDamageOverTime or d.bWasSplashDamage or w is None or d.DamageCauser != w:
                    st["skipped"] += 1
                    return None
                victim = args.DamagedActor
                key = victim.Name.split("_21")[0] if victim is not None else "?"
                st["hits"][key] = st["hits"].get(key, 0) + 1
                st["hit_shots"].add(st["shot_changes"] // 2)  # each shot counts once
        except Exception:
            st["errors"] += 1
        return None

    def tick(*_):
        try:
            now = time.perf_counter()
            if not _su_gate(st, now):
                return
            st["frames"].append(now)
            b = st["pawn"]()
            if b is None:
                return
            if now - st["weapon_check"] > 1.0:  # the gun may change (swap, reload)
                st["weapon_check"] = now
                slots = b.ActiveWeapons.Slots
                w = slots[0].Weapon if len(slots) else None
                st["weapon"] = WeakPointer(w) if w is not None else None
            w = st["weapon"]() if st["weapon"] else None
            if w is not None:
                v = w.RemoteShotState
                if st["shot_state"] is not None and v != st["shot_state"]:
                    st["shot_changes"] += 1
                st["shot_state"] = v
            adj = b.CharacterMovement.ServerLastClientAdjustmentTime
            if st["adj"] is not None and adj != st["adj"]:
                st["corr"] += 1
            st["adj"] = adj
        except Exception:
            st["errors"] += 1

    SUITE["host"] = st
    hooks.remove_hook(native.FUNC_PATH, hooks.Type.PRE, "netlab.suite_hits")
    hooks.add_hook(native.FUNC_PATH, hooks.Type.PRE, "netlab.suite_hits", hit_hook)
    _su_hook("netlab.suite_host", tick)
    return f"host counters on for {pawn.Name}"


def suite_host_stop():
    import sys  # noqa: PLC0415

    hooks.remove_hook(sys.modules["bl4_damage_meter.native"].FUNC_PATH, hooks.Type.PRE, "netlab.suite_hits")
    _su_hook("netlab.suite_host", None)
    return "host counters off"


def suite_host_report():
    st = SUITE["host"]
    span = time.perf_counter() - st["t0"]
    f = st["frames"]
    gaps = sorted(b - a for a, b in zip(f, f[1:]))
    fps = (len(f) / (f[-1] - f[0])) if len(f) > 2 else 0
    hits = sum(st["hits"].values())
    shots = st["shot_changes"] / 2
    return {
        "span_s": round(span, 1), "shots": shots, "hit_shots": len(st["hit_shots"]), "hits": hits,
        "hits_by": dict(st["hits"]), "skipped_hits": st["skipped"],
        "corrections": st["corr"], "corr_per_min": round(st["corr"] / span * 60, 1) if span else 0,
        "host_fps": round(fps), "host_worst_ms": round(gaps[-1] * 1000) if gaps else 0,
        "host_over_50ms": sum(1 for g in gaps if g > 0.05), "errors": st["errors"],
    }


def suite_mover_start(near, dist_m=12.0, amp_cm=None):
    """
    Takes the Char_TargetDummy nearest to `near` (the client's spot), puts it dist_m in front of the
    client and moves it side to side across the client's line of sight. The path is checked first
    (floor under both ends, a clear line between them at chest height, room for the capsule; the
    swing shrinks until it fits) so the dummy doesn't go into walls.
    """
    import bl4_direct.stand as stand  # noqa: PLC0415 - floor / capsule / line checks

    gs = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
    _, acts = gs.GetAllActorsOfClass(ENGINE.GameViewport.World, unrealsdk.find_class("OakCharacter"), [])
    dummies = [a for a in acts if a.Name.startswith("Char_TargetDummy")]
    if not dummies:
        return "no target dummy loaded", None
    nx, ny, nz = near

    def dist(a):
        l = a.K2_GetActorLocation()
        return math.dist((l.X, l.Y, l.Z), (nx, ny, nz))

    dummy = min(dummies, key=dist)
    orig = dummy.K2_GetActorLocation()
    half = dummy.CapsuleComponent.GetScaledCapsuleHalfHeight()
    away = math.atan2(orig.Y - ny, orig.X - nx)
    ignore = [dummy] + [pc_pawn for pc_pawn in (get_pc().Pawn, _su_client_pc().Pawn) if pc_pawn is not None]
    def try_path(angle, dist_cm):
        """A base dist_cm from the client at `angle`, with a clear side-to-side swing; (base, axis, amp) or None."""
        bx, by = nx + math.cos(angle) * dist_cm, ny + math.sin(angle) * dist_cm
        fz = stand._floor(bx, by, nz, ignore)
        if fz is None:
            return None
        bz = fz + half + stand.LIFT
        eye = stand._vec(nx, ny, nz + 60)
        if stand._line(eye, stand._vec(bx, by, bz + half * 0.4), ignore) is not None:
            return None  # the client can't see it there
        ax, ay = -math.sin(angle), math.cos(angle)
        amp = MOVER_AMP_CM if amp_cm is None else amp_cm
        while amp >= 150 or (amp_cm == 0 and amp == 0):
            ok = True
            for side in (-1, 1):
                x, y = bx + ax * amp * side, by + ay * amp * side
                f = stand._floor(x, y, bz - half, ignore)
                if (f is None or abs(f - fz) > 60 or not stand._capsule_free(dummy, x, y, f + half + stand.LIFT, ignore)
                        or stand._line(eye, stand._vec(x, y, f + half * 1.4), ignore) is not None):
                    ok = False
                    break
            if ok and stand._line(stand._vec(bx - ax * amp, by - ay * amp, bz + half * 0.4),
                                  stand._vec(bx + ax * amp, by + ay * amp, bz + half * 0.4), ignore) is None:
                return (bx, by, bz), (ax, ay), amp
            amp -= 50
        return None

    found = None
    for dd in (dist_m, dist_m - 2, dist_m + 2, dist_m - 4, dist_m + 4):
        for off in (0, 20, -20, 40, -40, 60, -60):
            if dd < 4:
                continue
            found = try_path(away + math.radians(off), dd * 100)
            if found:
                break
        if found:
            break
    if not found:
        return "no clear side-to-side path near the client", None
    (bx, by, bz), (ax, ay), amp = found
    st = {"dummy": WeakPointer(dummy), "base": (bx, by, bz), "orig": (orig.X, orig.Y, orig.Z), "axis": (ax, ay),
          "amp": amp, "t0": time.perf_counter(), "errors": 0, "next_heal": 0.0, "heals": 0,
          "damage_statics": unrealsdk.find_class("DamageStatics").ClassDefaultObject}

    def tick(*_):
        try:
            now = time.perf_counter()
            if not _su_gate(st, now):
                return
            d = st["dummy"]()
            if d is None:
                return
            # Full health once a second: hits on a dummy at 0 HP don't register (god mode would stop
            # the damage callback altogether, checked)
            if now >= st["next_heal"]:
                st["next_heal"] = now + 1.0
                st["damage_statics"].ResetHealthAndDamage(d)
                st["heals"] += 1
            off = st["amp"] * math.sin(2 * math.pi * (now - st["t0"]) / MOVER_PERIOD_S)
            x0, y0, z0 = st["base"]
            d.K2_SetActorLocation(unrealsdk.make_struct("Vector", X=x0 + st["axis"][0] * off, Y=y0 + st["axis"][1] * off,
                                                        Z=z0), False, unrealsdk.make_struct("HitResult"), False)
        except Exception:
            st["errors"] += 1

    SUITE["mover"] = st
    _su_hook("netlab.suite_mover", tick)
    out = math.hypot(bx - nx, by - ny) / 100
    return f"moving {dummy.Name} {out:.0f} m out, +-{amp / 100:.1f} m every {MOVER_PERIOD_S:.0f} s", st["base"]


def suite_mover_stop():
    _su_hook("netlab.suite_mover", None)
    st = SUITE.get("mover")
    if st:
        d = st["dummy"]()
        if d is not None:  # put it back where it stood
            bx, by, bz = st["orig"]
            d.K2_SetActorLocation(unrealsdk.make_struct("Vector", X=bx, Y=by, Z=bz), False,
                                  unrealsdk.make_struct("HitResult"), True)
    return "mover off"


def suite_set(values):
    """Sets host cvars; returns what they read back."""
    ksl = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject
    pc = get_pc()
    for k, v in values.items():
        ksl.ExecuteConsoleCommand(pc, f"{k} {v}", pc)
    return {k: round(ksl.GetConsoleVariableFloatValue(k), 4) for k in values}


def suite_get(names):
    ksl = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject
    return {k: ksl.GetConsoleVariableFloatValue(k) for k in names}


def suite_trust(on):
    """Movement trust for the client's character, on the host: skip the client-error check and accept
    the client's position (no corrections). Per character; set on its CharacterMovement."""
    cmc = _su_client_pc().Pawn.CharacterMovement
    cmc.bIgnoreClientMovementErrorChecksAndCorrection = bool(on)
    cmc.bServerAcceptClientAuthoritativePosition = bool(on)
    return (cmc.bIgnoreClientMovementErrorChecksAndCorrection, cmc.bServerAcceptClientAuthoritativePosition)


def suite_place_client(xyz, face_xyz):
    """Puts the client's character back on a spot, facing a point (a host-side teleport of a client is
    safe; teleporting the host's own character starts the arrival hold on one account)."""
    pawn = _su_client_pc().Pawn
    x, y, z = xyz
    fx, fy, _ = face_xyz
    yaw = math.degrees(math.atan2(fy - y, fx - x))
    pawn.K2_TeleportTo(unrealsdk.make_struct("Vector", X=x, Y=y, Z=z),
                       unrealsdk.make_struct("Rotator", Pitch=0.0, Yaw=yaw, Roll=0.0))
    return "placed"


# endregion
# region client


class _SuWin:
    user32 = None
    hwnd = None


def _su_window():
    """This game's main window (the largest visible one of our process). Own WinDLL: shared windll
    prototypes stay untouched."""
    if _SuWin.hwnd:
        return _SuWin.hwnd
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.EnumWindows.argtypes = (ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM)
    u.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    u.IsWindowVisible.argtypes = (wintypes.HWND,)
    u.GetClientRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    u.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    pid = os.getpid()
    best = [None, 0]

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        owner = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and u.IsWindowVisible(hwnd):
            r = wintypes.RECT()
            u.GetClientRect(hwnd, ctypes.byref(r))
            area = (r.right - r.left) * (r.bottom - r.top)
            if area > best[1]:
                best[0], best[1] = hwnd, area
        return True

    u.EnumWindows(each, 0)
    _SuWin.user32, _SuWin.hwnd = u, best[0]
    return _SuWin.hwnd


def _su_click(down):
    hwnd = _su_window()
    if not hwnd:
        return False
    r = wintypes.RECT()
    _SuWin.user32.GetClientRect(hwnd, ctypes.byref(r))
    lparam = ((r.bottom // 2) << 16) | (r.right // 2)
    # WM_LBUTTONDOWN (MK_LBUTTON) / WM_LBUTTONUP
    _SuWin.user32.PostMessageW(hwnd, 0x0201 if down else 0x0202, 1 if down else 0, lparam)
    return True


def _su_key(vk, down):
    hwnd = _su_window()
    if not hwnd:
        return False
    scan = ctypes.WinDLL("user32").MapVirtualKeyW(vk, 0)
    lparam = 1 | (scan << 16) | ((0xC0000000) if not down else 0)
    _SuWin.user32.PostMessageW(hwnd, 0x0100 if down else 0x0101, vk, lparam)  # WM_KEYDOWN / WM_KEYUP
    return True


def _su_self_probe(st, pawn, now):
    """Counts own-character snaps: a move in one frame larger than its velocity explains."""
    loc = pawn.K2_GetActorLocation()
    v = pawn.GetVelocity()
    cur = (loc.X, loc.Y, loc.Z, v.X, v.Y, v.Z, now)
    prev = st.get("self_prev")
    st["self_prev"] = cur
    if prev is None or now - prev[6] > STALL_S:
        return
    dt = now - prev[6]
    ex = (prev[0] + prev[3] * dt, prev[1] + prev[4] * dt, prev[2] + prev[5] * dt)
    err = math.dist(ex, cur[:3])
    if err > SNAP_CM:
        st["snaps"].append(round(err))


def suite_shoot_start(target_xyz, click_hz=CLICK_HZ, reload_every_s=RELOAD_EVERY_S):
    gs = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
    tx, ty, tz = target_xyz
    st = {"target": None, "next_find": 0.0, "next_click": 0.0, "up_at": None, "clicks": 0, "aimed_frames": 0,
          "frames": 0, "snaps": [], "errors": [], "t0": time.perf_counter(), "next_reload": time.perf_counter() + 1.0,
          "r_up_at": None, "reloads": 0}

    def find():
        _, acts = gs.GetAllActorsOfClass(ENGINE.GameViewport.World, unrealsdk.find_class("OakCharacter"), [])
        best = None
        for a in acts:
            if a.PlayerState is not None:
                continue
            l = a.K2_GetActorLocation()
            d = math.dist((l.X, l.Y, l.Z), (tx, ty, tz))
            if best is None or d < best[0]:
                best = (d, a)
        st["target"] = WeakPointer(best[1]) if best and best[0] < 800 else None

    def tick(*_):
        try:
            now = time.perf_counter()
            if st["up_at"] is not None and now >= st["up_at"]:
                _su_click(False)
                st["up_at"] = None
            if st["r_up_at"] is not None and now >= st["r_up_at"]:
                _su_key(0x52, False)
                st["r_up_at"] = None
            elif reload_every_s and now >= st["next_reload"] and st["up_at"] is None:
                st["next_reload"] = now + reload_every_s
                _su_key(0x52, True)
                st["r_up_at"] = now + 0.06
                st["reloads"] += 1
            if not _su_gate(st, now):
                return
            st["frames"] += 1
            pc = get_pc()
            pawn = pc.Pawn if pc is not None else None
            if pawn is None:
                return
            _su_self_probe(st, pawn, now)
            if now >= st["next_find"]:
                st["next_find"] = now + 1.0
                find()
            t = st["target"]() if st["target"] else None
            if t is None:
                return
            tl = t.Mesh.K2_GetComponentLocation()  # what is drawn (after smoothing): what a player aims at
            h = t.CapsuleComponent.GetScaledCapsuleHalfHeight()
            cam = pc.PlayerCameraManager.GetCameraLocation()
            cr = pc.GetControlRotation()
            dx, dy, dz = tl.X - cam.X, tl.Y - cam.Y, tl.Z + h * AIM_HEIGHT - cam.Z
            dyaw = (math.degrees(math.atan2(dy, dx)) - cr.Yaw + 180) % 360 - 180
            pitch = cr.Pitch if cr.Pitch <= 180 else cr.Pitch - 360
            dpitch = math.degrees(math.atan2(dz, math.hypot(dx, dy))) - pitch
            pc.AddYawInput(dyaw / pc.GetDeprecatedInputYawScale())
            pc.AddPitchInput(dpitch / pc.GetDeprecatedInputPitchScale())
            if abs(dyaw) < AIM_TOLERANCE_DEG and abs(dpitch) < AIM_TOLERANCE_DEG:
                st["aimed_frames"] += 1
                if now >= st["next_click"] and st["up_at"] is None:
                    st["next_click"] = now + 1.0 / click_hz
                    if _su_click(True):
                        st["clicks"] += 1
                        st["up_at"] = now + CLICK_HOLD_S
        except Exception as ex:
            if len(st["errors"]) < 3:
                st["errors"].append(repr(ex))

    SUITE["client"] = st
    _su_hook("netlab.suite_client", tick)
    return "shooting"


def suite_move_start(cx, cy, radius_cm, jump_key=False):
    st = {"dir": 0.0, "next_turn": 0.0, "next_jump": 0.0, "jumps": 0, "frames": 0, "clicks": 0, "aimed_frames": 0,
          "snaps": [], "errors": [], "t0": time.perf_counter(), "space_up_at": None}

    def tick(*_):
        try:
            now = time.perf_counter()
            if not _su_gate(st, now):
                return
            st["frames"] += 1
            pc = get_pc()
            pawn = pc.Pawn if pc is not None else None
            if pawn is None:
                return
            _su_self_probe(st, pawn, now)
            loc = pawn.K2_GetActorLocation()
            if math.hypot(loc.X - cx, loc.Y - cy) > radius_cm * 0.8:
                st["dir"] = math.atan2(cy - loc.Y, cx - loc.X) + random.uniform(-0.6, 0.6)
                st["next_turn"] = now + random.uniform(0.8, 1.5)
            elif now >= st["next_turn"]:
                st["dir"] = random.uniform(0, 2 * math.pi)
                st["next_turn"] = now + random.uniform(0.6, 2.0)
            if st["space_up_at"] is not None and now >= st["space_up_at"]:
                _su_key(0x20, False)
                st["space_up_at"] = None
            if now >= st["next_jump"]:
                st["next_jump"] = now + JUMP_EVERY_S * random.uniform(0.7, 1.3)
                if jump_key:  # a real key press through the game's input (posted Space)
                    _su_key(0x20, True)
                    st["space_up_at"] = now + 0.12
                else:
                    pawn.Jump()
                st["jumps"] += 1
            pawn.AddMovementInput(unrealsdk.make_struct("Vector", X=math.cos(st["dir"]), Y=math.sin(st["dir"]), Z=0.0),
                                  1.0, False)
        except Exception as ex:
            if len(st["errors"]) < 3:
                st["errors"].append(repr(ex))

    SUITE["client"] = st
    _su_hook("netlab.suite_client", tick)
    return "moving"


def suite_client_stop():
    _su_hook("netlab.suite_client", None)
    st = SUITE.get("client")
    if st and st.get("up_at") is not None:
        _su_click(False)
    return "client off"


def suite_client_report():
    st = SUITE["client"]
    span = time.perf_counter() - st["t0"]
    snaps = sorted(st["snaps"])
    return {
        "span_s": round(span, 1), "clicks": st["clicks"], "aimed_share": round(st["aimed_frames"] / st["frames"], 2)
        if st["frames"] else 0, "jumps": st.get("jumps", 0), "self_snaps": len(snaps),
        "self_snaps_per_min": round(len(snaps) / span * 60, 1) if span else 0,
        "snap_p50_cm": snaps[len(snaps) // 2] if snaps else 0, "snap_max_cm": snaps[-1] if snaps else 0,
        "errors": st["errors"],
    }


# endregion
print("netlab_suite loaded")
