"""
Arena automation for co-op enemy-smoothness tests (dev only). exec it into a bridge namespace:

    python -I tools/bridge.py <host> "exec(open(r'C:/Users/Josh/Documents/projects/bl4-direct/dev/netlab_arena.py').read())"

Host:
    arena_setup(radius_m=20)        centre = where the host stands; finds valid spots in the radius
    arena_spawn('Char_PangolinBasic', 4)   more enemies on valid spots (Kairosdex's safe spawner)
    arena_hops_start(every_s=20)    every N s, teleport every client to a valid spot (not the host)
    arena_hops_stop(), arena_despawn()
Any instance (each moves its own character; a client's movement has to come from the client):
    wander_start(cx, cy, cz, radius_cm)   random walk / strafe / jump, steering back inside the radius
    wander_stop()

A spot is valid when it has a walkable floor (stand._floor), room for the capsule there
(stand._capsule_free) and a clear line from the arena centre at eye height, so nobody lands behind a
wall or outside the arena. Spawns keep their params alive for the session: GbxSpawn* is latent and
writes Result into the params block later (see bl4_weapondex/bake.py).
"""

# ruff: noqa: F821 - unrealsdk, hooks, get_pc, ENGINE come from the bridge namespace

import math
import random
import time

from unrealsdk.unreal import FGameDataHandle, WeakPointer, WrappedStruct

_AR_TICK = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
ARENA = globals().setdefault("ARENA", {"spots": [], "keep": [], "spawned": [], "center": None})
# Spawns whose Result hasn't been read yet: (args, WeakPointer(world), time)
ARENA.setdefault("pending", [])
HARVEST_S = 60.0


def _ar_stand():
    import bl4_direct.stand as stand  # noqa: PLC0415

    return stand


def _ar_players(include_host=True):
    """(pc, pawn) for the host (optional) and every connected client."""
    out = []
    pc = get_pc()
    if include_host and pc is not None and pc.Pawn is not None:
        out.append((pc, pc.Pawn))
    nd = ENGINE.GameViewport.World.NetDriver
    for c in nd.ClientConnections if nd else []:
        cpc = c.PlayerController
        if cpc is not None and cpc.Pawn is not None:
            out.append((cpc, cpc.Pawn))
    return out


def arena_setup(radius_m=20.0, tries=80):
    stand = _ar_stand()
    me = get_pc().Pawn
    c = me.K2_GetActorLocation()
    cx, cy = c.X, c.Y
    ignore = [p for _, p in _ar_players()]
    floor0 = stand._floor(cx, cy, c.Z, ignore)
    cz = floor0 if floor0 is not None else c.Z - me.CapsuleComponent.GetScaledCapsuleHalfHeight()
    half = me.CapsuleComponent.GetScaledCapsuleHalfHeight()
    eye = stand._vec(cx, cy, cz + stand.EYE)
    spots = []
    t0 = time.perf_counter()
    for _ in range(tries):
        r = radius_m * 100 * math.sqrt(random.uniform(0.05, 1.0))
        a = random.uniform(0, 2 * math.pi)
        x, y = cx + math.cos(a) * r, cy + math.sin(a) * r
        fz = stand._floor(x, y, cz, ignore)
        if fz is None:
            continue
        z = fz + half + stand.LIFT
        if not stand._capsule_free(me, x, y, z, ignore):
            continue
        if stand._line(eye, stand._vec(x, y, fz + stand.EYE), ignore) is not None:
            continue  # behind something, seen from the centre
        spots.append((x, y, z))
    ARENA.update(spots=spots, center=(cx, cy, cz), radius=radius_m * 100)
    return f"centre {cx:.0f} {cy:.0f} {cz:.0f}, {len(spots)}/{tries} valid spots, {1000 * (time.perf_counter() - t0):.0f} ms"


def arena_spawn(char, count=4):
    from bl4_weapondex import bake  # noqa: PLC0415 - its ProcessEvent caller

    if not ARENA["spots"]:
        return "run arena_setup first"
    pc = get_pc()
    lib = unrealsdk.find_class("SpawnBlueprintLibrary").ClassDefaultObject
    fn = lib.GbxSpawnActorAtTransform
    cx, cy, _ = ARENA["center"]
    for x, y, z in random.sample(ARENA["spots"], min(count, len(ARENA["spots"]))):
        face = math.atan2(cy - y, cx - x)
        args = WrappedStruct(fn.func)
        args.Context = pc
        args.GbxActorDef = FGameDataHandle(bake.GBX_ACTOR_TYPE, char)
        t = args.Transform
        t.Translation.X, t.Translation.Y, t.Translation.Z = x, y, z + 50
        t.Rotation.Z, t.Rotation.W = math.sin(face / 2), math.cos(face / 2)
        t.Scale3D.X = t.Scale3D.Y = t.Scale3D.Z = 1
        args.bIsCritical = True
        args.SpawnDetails.bCritical = True
        args.SpawnDetails.bOverrideCritical = True
        args.LatentInfo.CallbackTarget = pc
        args.LatentInfo.UUID = random.randint(100000, 2_000_000_000)
        args.LatentInfo.Linkage = -1
        args.LatentInfo.ExecutionFunction = "None"
        ARENA["keep"].append(args)  # the late Result write lands here: never free it
        ARENA["pending"].append((args, WeakPointer(ENGINE.GameViewport.World), time.monotonic()))
        bake._process_event(lib, fn.func, args)
    return f"spawning {count} x {char}"


def arena_spawned():
    """
    WeakPointers to what our spawns produced so far (Result is written frames later).

    Result is a raw pointer: read it only in the world the spawn was made in, only until it's
    found, and for at most HARVEST_S. Reading old blocks after a map reload dereferenced actors of
    the destroyed world and crashed the host (2026-10-28, AV inside pyunrealsdk).
    """
    world = ENGINE.GameViewport.World
    now = time.monotonic()
    still = []
    for args, made_in, t in ARENA["pending"]:
        if made_in() != world or now - t > HARVEST_S:
            continue  # another world, or it never came: forget it (the block itself stays in keep)
        actor = args.Result
        if actor is None:
            still.append((args, made_in, t))
        else:
            ARENA["spawned"].append(WeakPointer(actor))
    ARENA["pending"] = still
    alive = [w() for w in ARENA["spawned"]]
    return f"{sum(1 for a in alive if a is not None)} of ours alive, {len(ARENA['keep'])} spawn calls"


def arena_despawn():
    arena_spawned()
    n = 0
    for w in ARENA["spawned"]:
        a = w()
        if a is not None:
            a.K2_DestroyActor()
            n += 1
    ARENA["spawned"].clear()
    return f"destroyed {n}"


def arena_hop(include_host=False):
    # Not the host by default: with two copies on one account, teleporting the host's own character
    # starts Gearbox's arrival hold (gravity 0, move input ignored) and it never releases (2026-10-28)
    spots = ARENA["spots"]
    players = _ar_players(include_host)
    if len(spots) < len(players):
        return "not enough spots"
    cx, cy, _ = ARENA["center"]
    for (_, pawn), (x, y, z) in zip(players, random.sample(spots, len(players))):
        yaw = math.degrees(math.atan2(cy - y, cx - x))
        pawn.K2_TeleportTo(unrealsdk.make_struct("Vector", X=x, Y=y, Z=z),
                           unrealsdk.make_struct("Rotator", Pitch=0.0, Yaw=yaw, Roll=0.0))
    return f"hopped {len(players)}"


def _ar_make_hops(every_s):
    st = {"next": time.perf_counter() + every_s, "hops": 0, "errors": 0}

    def tick(*_):
        try:
            now = time.perf_counter()
            if now < st["next"]:
                return
            st["next"] = now + every_s
            arena_hop()
            st["hops"] += 1
        except Exception:
            st["errors"] += 1

    tick.st = st
    return tick


def arena_hops_start(every_s=20.0):
    ARENA["hops"] = _ar_make_hops(every_s)
    hooks.remove_hook(_AR_TICK, hooks.Type.POST, "netlab.arena_hops")
    hooks.add_hook(_AR_TICK, hooks.Type.POST, "netlab.arena_hops", ARENA["hops"])
    return f"hops every {every_s:.0f} s"


def arena_hops_stop():
    hooks.remove_hook(_AR_TICK, hooks.Type.POST, "netlab.arena_hops")
    return f"hops off {ARENA.get('hops').st if ARENA.get('hops') else ''}"


def _ar_make_wander(cx, cy, radius, _get_pc=get_pc, _u=unrealsdk):
    st = {"dir": 0.0, "next_turn": 0.0, "last_call": 0.0, "last_frame": 0.0, "jumps": 0, "errors": 0}

    def tick(*_):
        try:
            now = time.perf_counter()
            gap = now - st["last_call"]
            st["last_call"] = now
            if gap < 0.004 and now - st["last_frame"] < 0.012:
                return  # once a frame
            st["last_frame"] = now
            pc = _get_pc()
            pawn = pc.Pawn if pc is not None else None
            if pawn is None:
                return
            loc = pawn.K2_GetActorLocation()
            if math.hypot(loc.X - cx, loc.Y - cy) > radius * 0.8:
                st["dir"] = math.atan2(cy - loc.Y, cx - loc.X) + random.uniform(-0.6, 0.6)
                st["next_turn"] = now + random.uniform(0.8, 1.5)
            elif now >= st["next_turn"]:
                st["dir"] = random.uniform(0, 2 * math.pi)
                st["next_turn"] = now + random.uniform(0.6, 2.5)
                if random.random() < 0.3:
                    pawn.Jump()
                    st["jumps"] += 1
            pawn.AddMovementInput(_u.make_struct("Vector", X=math.cos(st["dir"]), Y=math.sin(st["dir"]), Z=0.0), 1.0, False)
        except Exception:
            st["errors"] += 1

    tick.st = st
    return tick


def wander_start(cx, cy, cz, radius_cm):
    globals()["WANDER"] = _ar_make_wander(cx, cy, radius_cm)
    hooks.remove_hook(_AR_TICK, hooks.Type.POST, "netlab.wander")
    hooks.add_hook(_AR_TICK, hooks.Type.POST, "netlab.wander", WANDER)
    return "wander on"


def wander_stop():
    hooks.remove_hook(_AR_TICK, hooks.Type.POST, "netlab.wander")
    w = globals().get("WANDER")
    return f"wander off {w.st if w else ''}"


print("netlab_arena loaded")
