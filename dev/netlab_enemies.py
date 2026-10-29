"""
Client-side enemy smoothness probe for co-op tests. Run it on the JOINING instance (B):

    python -I tools/bridge.py B "exec(open(r'C:/Users/Josh/Documents/projects/bl4-direct/dev/netlab_enemies.py').read())"
    python -I tools/bridge.py B "enemies_start()"
    ... fight for a while ...
    python -I tools/bridge.py B "print(enemies_report())"
    python -I tools/bridge.py B "enemies_stop()"

Once per client frame it samples every enemy within RADIUS_CM of the local player:
  - the mesh location: what is drawn, after the engine's network smoothing
  - the actor location: where the client's own simulation puts it; it jumps when a host update
    disagrees with the path the client was following
Counted, per enemy-minute (the number of enemies near you varies):
  - teleports: the mesh moves more than JUMP_CM in one frame, and faster than MAX_SPEED_CM_S
    (no enemy runs that fast); frames after a stall (menu, hitch) are skipped
  - freezes: the mesh stood still for FREEZE_S or more, then jumped FREEZE_JUMP_CM or more
  - snaps: the actor moves more than JUMP_CM in one frame (corrections, even ones smoothing hid)
Jumps while the mesh simulates physics (ragdolls) are counted apart and not as teleports.
"""

# ruff: noqa: F821 - unrealsdk, hooks, get_pc, ENGINE come from the bridge namespace

import math
import statistics
import time

from unrealsdk.unreal import WeakPointer

_NLE_TICK = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
_NLE_HOOK_ID = "netlab.enemies"
RADIUS_CM = 5000.0
JUMP_CM = 150.0
FREEZE_S = 0.15
FREEZE_JUMP_CM = 100.0
MAX_SPEED_CM_S = 1500.0  # faster than any enemy runs: a jump must also beat this over the frame
STALL_S = 0.25  # a frame this long (menu, loading, hitch): start over instead of comparing
RESCAN_S = 1.0

NLE = globals().setdefault("NLE", {})


def _nle_reset():
    NLE.clear()
    NLE.update(
        ptrs={},  # name -> WeakPointer
        prev={},  # name -> (t, ax, ay, az, mx, my, mz, still_since)
        last_call=0.0,
        last_frame=0.0,
        last_scan=0.0,
        frames=[],
        enemy_s=0.0,
        seen=set(),
        teleports=[],  # (t, cm, name)
        freezes=[],  # (t, still_s, cm, name)
        snaps=[],  # (t, cm, name)
        ragdoll=0,
        errors=0,
        t0=time.perf_counter(),
    )


def _nle_dist(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _nle_make_tick(_unrealsdk=unrealsdk, _get_pc=get_pc, _engine=ENGINE, _nle=NLE):
    gs = _unrealsdk.find_class("GameplayStatics").ClassDefaultObject
    oak_char = _unrealsdk.find_class("OakCharacter")

    def rescan(now):
        _nle["last_scan"] = now
        world = _engine.GameViewport.World
        _, actors = gs.GetAllActorsOfClass(world, oak_char, [])
        ptrs = {}
        for a in actors:
            if a.PlayerState is not None:  # a player, not an enemy
                continue
            ptrs[a.Name] = WeakPointer(a)
        _nle["ptrs"] = ptrs
        for name in list(_nle["prev"]):
            if name not in ptrs:
                del _nle["prev"][name]

    def tick(*_):
        try:
            now = time.perf_counter()
            # This hook fires ~6 times a frame: act on the first call after a gap, or every 12 ms
            gap = now - _nle["last_call"]
            _nle["last_call"] = now
            if gap < 0.004 and now - _nle["last_frame"] < 0.012:
                return
            stalled = now - _nle["last_frame"] > STALL_S
            _nle["last_frame"] = now
            _nle["frames"].append(now)
            if stalled:
                _nle["prev"].clear()
            if now - _nle["last_scan"] > RESCAN_S:
                rescan(now)
            pc = _get_pc()
            me = pc.Pawn if pc is not None else None
            if me is None:
                return
            ml = me.K2_GetActorLocation()
            my = (ml.X, ml.Y, ml.Z)
            prev_all = _nle["prev"]
            for name, ptr in list(_nle["ptrs"].items()):
                a = ptr()
                if a is None:
                    _nle["ptrs"].pop(name, None)
                    prev_all.pop(name, None)
                    continue
                al = a.K2_GetActorLocation()
                act = (al.X, al.Y, al.Z)
                if _nle_dist(act, my) > RADIUS_CM:
                    prev_all.pop(name, None)  # re-entering range isn't a jump
                    continue
                mesh = a.Mesh
                if mesh is None:
                    continue
                ml2 = mesh.K2_GetComponentLocation()
                msh = (ml2.X, ml2.Y, ml2.Z)
                _nle["seen"].add(name)
                prev = prev_all.get(name)
                if prev is None:
                    prev_all[name] = (now, *act, *msh, now)
                    continue
                pt, still_since = prev[0], prev[7]
                dt = now - pt
                _nle["enemy_s"] += dt
                dm = _nle_dist(msh, prev[4:7])
                da = _nle_dist(act, prev[1:4])
                run = MAX_SPEED_CM_S * dt
                jump, fjump = max(JUMP_CM, run), max(FREEZE_JUMP_CM, run)
                if da > jump:
                    _nle["snaps"].append((now, da, name))
                if dm > jump or (dm > fjump and now - still_since >= FREEZE_S):
                    ragdoll = False
                    try:
                        ragdoll = bool(mesh.IsSimulatingPhysics("None"))
                    except Exception:
                        pass
                    if ragdoll:
                        _nle["ragdoll"] += 1
                    else:
                        if dm > jump:
                            _nle["teleports"].append((now, dm, name))
                        if now - still_since >= FREEZE_S and dm > fjump:
                            _nle["freezes"].append((now, now - still_since, dm, name))
                if dm > 0.5:
                    still_since = now
                prev_all[name] = (now, *act, *msh, still_since)
        except Exception:
            _nle["errors"] += 1

    return tick


def enemies_start():
    _nle_reset()
    hooks.remove_hook(_NLE_TICK, hooks.Type.POST, _NLE_HOOK_ID)
    hooks.add_hook(_NLE_TICK, hooks.Type.POST, _NLE_HOOK_ID, _nle_make_tick())
    return "enemy probe on"


def enemies_stop():
    hooks.remove_hook(_NLE_TICK, hooks.Type.POST, _NLE_HOOK_ID)
    return "enemy probe off"


def _nle_pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else 0.0


def enemies_report(detail=False):
    span = time.perf_counter() - NLE["t0"]
    em = NLE["enemy_s"] / 60.0
    per = (lambda n: n / em) if em > 0 else (lambda n: 0.0)
    tp, fz, sn = NLE["teleports"], NLE["freezes"], NLE["snaps"]
    frames = NLE["frames"]
    gaps = sorted(b - a for a, b in zip(frames, frames[1:]))
    lines = [
        f"  {span:.0f} s, {len(NLE['seen'])} enemies seen, {em:.1f} enemy-minutes in range",
        f"  teleports {len(tp)} ({per(len(tp)):.1f}/enemy-min)  sizes p50 {_nle_pct([t[1] for t in tp], .5):.0f}"
        f" p90 {_nle_pct([t[1] for t in tp], .9):.0f} max {max((t[1] for t in tp), default=0):.0f} cm",
        f"  freezes>=150ms then jump {len(fz)} ({per(len(fz)):.1f}/enemy-min)"
        f"  still p50 {_nle_pct([f[1] for f in fz], .5) * 1000:.0f} ms max {max((f[1] for f in fz), default=0) * 1000:.0f} ms",
        f"  actor snaps {len(sn)} ({per(len(sn)):.1f}/enemy-min)  ragdoll jumps skipped {NLE['ragdoll']}  errors {NLE['errors']}",
    ]
    if len(gaps) > 2:
        lines.append(f"  client fps avg {len(frames) / (frames[-1] - frames[0]):.0f}, p99 frame {_nle_pct(gaps, .99) * 1000:.0f} ms,"
                     f" worst {gaps[-1] * 1000:.0f} ms")
    if detail:
        t0 = NLE["t0"]
        for t, cm, name in tp[-15:]:
            lines.append(f"    +{t - t0:6.2f}s teleport {cm:6.0f} cm  {name}")
    return "\n".join(lines)


print("netlab_enemies loaded")
