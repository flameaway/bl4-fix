"""
Network-experiment helpers, exec'd into a bridge namespace:
    python tools/bridge.py A "exec(open(r'dev/netlab.py').read())"

Host side:   start_fps() / fps_report(),  start_spin(deg_per_frame) / stop_spin()
Client side: start_updates() / updates_report()   (replicated movement arrivals per actor)
Both:        set_cvar(name, value), get_cvar(name), conn_report()
"""

# ruff: noqa: F821 - unrealsdk, hooks, get_pc, world, KSL come from the bridge namespace

import math
import statistics
import time

from unrealsdk.unreal import WeakPointer

TICK = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
GS = unrealsdk.find_class("GameplayStatics").ClassDefaultObject

NL = globals().setdefault("NL", {"frames": [], "last": None, "updates": {}, "spin": 0.0, "rot": 0.0})


# region host frame times


def _frame_cb(*_):
    now = time.perf_counter()
    if NL["last"] is not None:
        NL["frames"].append(now - NL["last"])
    NL["last"] = now
    if NL["spin"]:
        pc = get_pc()
        if pc is not None and pc.Pawn is not None and now - NL.get("spin_last", 0) > 0.0015:
            NL["spin_last"] = now
            NL["rot"] = (NL["rot"] + NL["spin"]) % 360
            if NL.get("walk"):
                # Walk in a circle through the normal movement input, like a real player
                yaw = math.radians(NL["rot"])
                pc.Pawn.AddMovementInput(
                    unrealsdk.make_struct("Vector", X=math.cos(yaw), Y=math.sin(yaw), Z=0.0), 1.0, True,
                )
            else:
                # Rotate the character itself (it doesn't follow the view while the window is unfocused)
                pc.Pawn.K2_SetActorRotation(
                    unrealsdk.make_struct("Rotator", Pitch=0.0, Yaw=NL["rot"], Roll=0.0), False,
                )


def _ensure_frame_hook():
    # Replace, don't add: re-exec'ing this file makes new callback objects
    hooks.remove_hook(TICK, hooks.Type.POST, "netlab.frames")
    hooks.add_hook(TICK, hooks.Type.POST, "netlab.frames", _frame_cb)


def start_fps():
    """Camera-modifier hooks fire several times per frame, so dedupe by perf_counter gaps."""
    NL["frames"].clear()
    NL["last"] = None
    _ensure_frame_hook()


def fps_report():
    gaps = [g for g in NL["frames"] if g > 0.0015]  # drop same-frame repeats
    if not gaps:
        return "no frames"
    gaps.sort()
    avg = statistics.mean(gaps)
    p99 = gaps[int(len(gaps) * 0.99) - 1]
    return f"fps avg {1 / avg:.0f}  frame avg {avg * 1000:.1f} ms  p99 {p99 * 1000:.1f} ms  max {gaps[-1] * 1000:.1f} ms  (n={len(gaps)})"


def start_spin(deg_per_frame=3.0, walk=False):
    """
    Keep the host's character moving so there's always movement to replicate: either spin it in
    place, or (walk=True) walk it in a circle through normal movement input.
    """
    NL["spin"] = float(deg_per_frame)
    NL["walk"] = walk
    _ensure_frame_hook()


def stop_spin():
    NL["spin"] = 0.0


# endregion
# region client replicated-movement arrivals


def _rep_cb(obj, *_):
    NL["updates"].setdefault(obj.Name, []).append(time.perf_counter())


def start_updates():
    NL["updates"].clear()
    hooks.add_hook("/Script/Engine.Actor:OnRep_ReplicatedMovement", hooks.Type.POST, "netlab.rep", _rep_cb)


def stop_updates():
    hooks.remove_hook("/Script/Engine.Actor:OnRep_ReplicatedMovement", hooks.Type.POST, "netlab.rep")


def updates_report(top=6):
    rows = []
    for name, ts in NL["updates"].items():
        if len(ts) < 3:
            continue
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        span = ts[-1] - ts[0]
        rows.append((len(ts) / span if span else 0, name, statistics.mean(gaps) * 1000, max(gaps) * 1000, len(ts)))
    rows.sort(reverse=True)
    if not rows:
        return "no replicated movement seen"
    return "\n".join(
        f"  {name[:40]:40} {hz:6.1f} Hz  gap avg {avg:6.1f} ms  max {mx:7.1f} ms  (n={n})"
        for hz, name, avg, mx, n in rows[:top]
    )


def _track_cb(*_):
    """Each client frame: has the other player's character visibly moved since last frame?"""
    now = time.perf_counter()
    if NL.get("track_last_frame") and now - NL["track_last_frame"] < 0.0015:
        return  # same frame (camera-modifier hooks fire several times per frame)
    NL["track_last_frame"] = now
    NL["client_frames"].append(now)
    for ptr in NL["track_pawns"]:
        pawn = ptr()
        if pawn is None:
            continue
        try:
            rot = pawn.K2_GetActorRotation()
        except Exception:
            continue
        key = pawn.Name
        prev = NL["track_prev"].get(key)
        loc = pawn.K2_GetActorLocation()
        cur = (round(rot.Yaw, 2), round(loc.X, 1), round(loc.Y, 1))
        if prev is None or cur != prev:
            NL["track"].setdefault(key, []).append(now)
        NL["track_prev"][key] = cur
        NL["track_frames"] = NL.get("track_frames", 0) + 1


def start_track():
    """Finds the other players' characters once; a find_all every frame tanks the fps (measured)."""
    me = get_pc().Pawn if get_pc() else None
    # WeakPointers: if the other player disconnects, their character is destroyed, and touching a
    # cached raw reference on the next frame crashes the game (it did - both instances)
    NL["track_pawns"] = [
        WeakPointer(p) for p in unrealsdk.find_all("OakCharacter", exact=False)
        if not p.Name.startswith("Default__") and p != me
    ]
    NL["track"] = {}
    NL["track_prev"] = {}
    NL["track_frames"] = 0
    NL["client_frames"] = []
    hooks.remove_hook(TICK, hooks.Type.POST, "netlab.track")
    hooks.add_hook(TICK, hooks.Type.POST, "netlab.track", _track_cb)


def stop_track():
    hooks.remove_hook(TICK, hooks.Type.POST, "netlab.track")


def track_report():
    rows = []
    for name, ts in NL.get("track", {}).items():
        if len(ts) < 3:
            continue
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        span = ts[-1] - ts[0]
        gaps.sort()
        rows.append(f"  {name[:28]:28} visible changes {len(ts) / span:5.1f}/s  gap avg {statistics.mean(gaps) * 1000:5.1f} ms"
                    f"  p95 {gaps[int(len(gaps) * 0.95) - 1] * 1000:6.1f} ms  max {gaps[-1] * 1000:6.1f} ms")
    frames = NL.get("client_frames", [])
    if len(frames) > 2:
        gaps = sorted(b - a for a, b in zip(frames, frames[1:]))
        rows.append(f"  (client fps avg {len(frames) / (frames[-1] - frames[0]):.0f}, worst frame {gaps[-1] * 1000:.0f} ms)")
    return "\n".join(rows) or "  other player not moving / not found"


def freeze_report(threshold_ms=100):
    """Client: list visible freezes of the other player's character longer than threshold."""
    out = []
    for name, ts in NL.get("track", {}).items():
        t0 = ts[0] if ts else 0
        for a, b in zip(ts, ts[1:]):
            if (b - a) * 1000 > threshold_ms:
                out.append(f"  {name[:20]} frozen {1000 * (b - a):5.0f} ms at +{a - t0:5.2f}s")
    return "\n".join(out) or "  no freezes"


CORRECTION_FUNCS = (
    "/Script/Engine.Character:ClientAdjustPosition",
    "/Script/Engine.Character:ClientVeryShortAdjustPosition",
    "/Script/Engine.Character:ClientAdjustRootMotionPosition",
    "/Script/Engine.Character:ClientAdjustRootMotionSourcePosition",
    "/Script/Engine.Character:ClientAckGoodMove",
    "/Script/Engine.Character:ClientMoveResponsePacked",
)


def _corr_cb(_obj, _args, _ret, func):
    name = func.func.Name
    NL["corr"][name] = NL["corr"].get(name, 0) + 1


def start_corrections():
    """Client: count the host's movement corrections (rubber-banding) vs acknowledgements."""
    NL["corr"] = {}
    NL["corr_t0"] = time.perf_counter()
    for f in CORRECTION_FUNCS:
        try:
            hooks.remove_hook(f, hooks.Type.PRE, "netlab.corr")
            hooks.add_hook(f, hooks.Type.PRE, "netlab.corr", _corr_cb)
        except ValueError:
            pass


def corrections_report():
    span = time.perf_counter() - NL.get("corr_t0", time.perf_counter())
    counts = NL.get("corr", {})
    if not counts:
        return "  no movement RPCs seen"
    return "  " + ", ".join(f"{k} {v / span * 60:.0f}/min" for k, v in sorted(counts.items()))


def _server_corr_cb(*_):
    now = time.perf_counter()
    if now - NL.get("sc_last", 0) < 0.0015:
        return
    NL["sc_last"] = now
    for ptr in NL["sc_pawns"]:
        pawn = ptr()
        if pawn is None:
            continue
        cmc = pawn.CharacterMovement
        adj, ack = cmc.ServerLastClientAdjustmentTime, cmc.ServerLastClientGoodMoveAckTime
        prev = NL["sc_prev"].get(pawn.Name)
        if prev is not None:
            if adj != prev[0]:
                NL["sc_adj"] += 1
            if ack != prev[1]:
                NL["sc_ack"] += 1
        NL["sc_prev"][pawn.Name] = (adj, ack)


def start_server_corr():
    """Host: count corrections vs good-move acks it sends to remote players."""
    me = get_pc().Pawn
    NL["sc_pawns"] = [
        WeakPointer(p) for p in unrealsdk.find_all("OakCharacter", exact=False)
        if not p.Name.startswith("Default__") and p != me
    ]
    NL["sc_prev"], NL["sc_adj"], NL["sc_ack"], NL["sc_t0"] = {}, 0, 0, time.perf_counter()
    hooks.remove_hook(TICK, hooks.Type.POST, "netlab.sc")
    hooks.add_hook(TICK, hooks.Type.POST, "netlab.sc", _server_corr_cb)


def server_corr_report():
    span = time.perf_counter() - NL["sc_t0"]
    return f"  corrections {NL['sc_adj'] / span * 60:.0f}/min, good-move acks {NL['sc_ack'] / span * 60:.0f}/min"


def _host_rot_cb(*_):
    now = time.perf_counter()
    if now - NL.get("hr_last", 0) < 0.0015:
        return
    NL["hr_last"] = now
    pawn = get_pc().Pawn if get_pc() else None
    if pawn is None:
        return
    yaw = round(pawn.K2_GetActorRotation().Yaw, 2)
    if yaw != NL.get("hr_prev"):
        NL["hr"].append(now)
    NL["hr_prev"] = yaw


def start_host_rot():
    """Host: record when its own character's rotation actually changes."""
    NL["hr"] = []
    hooks.remove_hook(TICK, hooks.Type.POST, "netlab.hostrot")
    hooks.add_hook(TICK, hooks.Type.POST, "netlab.hostrot", _host_rot_cb)


def host_rot_report(threshold_ms=100):
    ts = NL.get("hr", [])
    if len(ts) < 3:
        return "  host rotation didn't change"
    gaps = [(b - a, a - ts[0]) for a, b in zip(ts, ts[1:])]
    big = [f"  host pawn unchanged {g * 1000:5.0f} ms at +{t:5.2f}s" for g, t in gaps if g * 1000 > threshold_ms]
    head = f"  host pawn changes {len(ts) / (ts[-1] - ts[0]):.0f}/s"
    return head + ("\n" + "\n".join(big) if big else "  (no host-side stalls)")


# endregion
# region cvars / connections


def set_cvar(name, value):
    KSL.ExecuteConsoleCommand(get_pc(), f"{name} {value}", get_pc())


def get_cvar(name):
    v = KSL.GetConsoleVariableFloatValue(name)
    return v


def conn_report():
    nd = world().NetDriver
    out = []
    conns = list(nd.ClientConnections) if nd else []
    if nd and nd.ServerConnection:
        conns.append(nd.ServerConnection)
    for c in conns:
        pc = c.PlayerController
        ps = pc.PlayerState if pc else None
        ping = ps.GetPingInMilliseconds() if ps else None
        out.append(f"  {c.Name}: CurrentNetSpeed={getattr(c, 'CurrentNetSpeed', '?')} ping={ping}")
    return "\n".join(out) or "  no connections"


# endregion
print("netlab loaded")


# region test combat: god mode + enemy spawning (host only)


def god(on=True):
    """Invulnerability for the local player via Gearbox's own god lock. Re-apply after travel."""
    pawn = get_pc().Pawn
    ods = unrealsdk.find_class("OakDamageStatics").ClassDefaultObject
    (ods.SetGodLock if on else ods.UnSetGodLock)(pawn)
    return unrealsdk.find_class("DamageStatics").ClassDefaultObject.IsInGodMode(pawn)


def spawn_enemies(code="Char_GunToterPistol4Arm_Male", count=10, radius_m=12.0):
    """Spawns enemies in a ring around the host using GbxSpawnActorAtTransform (a latent call)."""
    import random  # noqa: PLC0415

    from unrealsdk.unreal import FGameDataHandle, WrappedStruct  # noqa: PLC0415

    pc = get_pc()
    pawn = pc.Pawn
    fn = unrealsdk.find_object("Function", "/Script/GbxSpawn.SpawnBlueprintLibrary:GbxSpawnActorAtTransform")
    type_handle = fn._find_prop("GbxActorDef").TypeHandle
    lib = unrealsdk.find_class("SpawnBlueprintLibrary").ClassDefaultObject
    loc = pawn.K2_GetActorLocation()
    for i in range(count):
        angle = 2 * math.pi * i / count
        spot = unrealsdk.make_struct(
            "Vector",
            X=loc.X + math.cos(angle) * radius_m * 100,
            Y=loc.Y + math.sin(angle) * radius_m * 100,
            Z=loc.Z + 50,
        )
        args = WrappedStruct(fn)
        args.Context = pawn
        args.GbxActorDef = FGameDataHandle(type_handle, code)
        args.Transform = unrealsdk.make_struct(
            "Transform", Translation=spot, Scale3D=unrealsdk.make_struct("Vector", X=1.0, Y=1.0, Z=1.0),
        )
        # A real callback target with a unique id: latent actions are keyed by (target, UUID)
        args.LatentInfo = unrealsdk.make_struct(
            "LatentActionInfo", Linkage=-1, UUID=random.randint(1, 2**30), CallbackTarget=pc,
        )
        lib.GbxSpawnActorAtTransform(args)
    return count


def count_enemies(radius_m=60.0):
    me = get_pc().Pawn
    loc = me.K2_GetActorLocation()
    n = 0
    for a in unrealsdk.find_all("OakCharacter", exact=False):
        if a.Name.startswith("Default__") or a == me or a.Name.startswith("OakCharacter"):
            continue
        l = a.K2_GetActorLocation()
        if ((l.X - loc.X) ** 2 + (l.Y - loc.Y) ** 2) ** 0.5 < radius_m * 100:
            n += 1
    return n


# endregion
