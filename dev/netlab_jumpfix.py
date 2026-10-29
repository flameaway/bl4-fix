"""
Jump-at-touchdown fix, test version (dev only). exec into the CLIENT's bridge.

The bug (docs/networking-performance.md 4.12): a jump press that started in the air and is still
held at touchdown. The host jumps again from the client's move, the client doesn't, then a
correction throws the client up and its jump/glide state stays out of step until it lands.

The tell: the frame after landing, the client is on the ground with bPressedJump set, jump count 0
and bWasJumping off. In 23 of 23 timed cycles this state appeared exactly when the host jumped.
The flag is set during input, before the move is built, so it can't be cleared in time from Python.
Instead the client makes the jump the host just made: falling, the jump goal's take-off speed, one
frame of that motion, jump count 1, bMayWantToGlide.

    jf_start() / jf_stop() / jf_report()
"""

# ruff: noqa: F821 - unrealsdk, hooks, get_pc come from the bridge namespace

import ctypes
import math
import time

from unrealsdk.unreal import WrappedStruct

_JF_TICK = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
MOVE_WALKING, MOVE_FALLING = 1, 3
ROLE_AUTONOMOUS = 2
LANDED_WINDOW_S = 0.2  # only right after a landing
WORLD_GRAVITY = -980.0
JF = globals().setdefault("JF", {})


def _jf_gate(st, now):
    gap = now - st.get("last_call", 0.0)
    st["last_call"] = now
    if gap < 0.004 and now - st.get("last_frame", 0.0) < 0.012:
        return False
    st["last_frame"] = now
    return True


def _jf_set_byte_bool(obj, prop, value):
    """Whole-byte bools: the SDK writes 0xFF, native code may test == 1."""
    setattr(obj, prop, value)
    field = obj.Class._find_prop(prop)
    if field.Class.Name == "BoolProperty" and field.FieldMask == 0xFF:  # noqa: PLR2004
        ctypes.c_uint8.from_address(obj._get_address() + field.Offset_Internal).value = int(bool(value))


def _jf_takeoff_speed(cmc):
    goal = cmc.CurrentJump.JumpGoal
    g = cmc.GetGravityZ()
    if g > -500:  # gliding or odd gravity: use the world's
        g = WORLD_GRAVITY * cmc.GravityScale
    if goal is not None and goal.bUseGoalHeight and goal.GoalHeight > 0:
        return math.sqrt(2 * -g * goal.GoalHeight), g
    if goal is not None and goal.bUseInitialZVelocity and goal.InitialZVelocity > 0:
        return goal.InitialZVelocity, g
    return cmc.JumpZVelocity, g


def _jf_launch(pawn, cmc, z, vz):
    """Puts the client where the host's copy is during its jump: falling, at height z, rising at vz."""
    loc = pawn.K2_GetActorLocation()
    loc.Z = z
    cmc.SetMovementMode(MOVE_FALLING, 0)
    set_loc = pawn.K2_SetActorLocation
    args = WrappedStruct(set_loc.func)  # its out param can't be passed as None
    args.NewLocation = loc
    args.bSweep = False
    args.bTeleport = True
    set_loc(args)
    cmc.Velocity.Z = vz
    pawn.JumpCurrentCount = 1
    pawn.JumpCurrentCountPreJump = 1


SNAP_CM = 8.0  # a frame's height change this far off its velocity = a correction, not movement


def jf_start(enabled=True, hold=True):
    st = {"enabled": enabled, "enabled_hold": hold, "prev_mode": MOVE_WALKING, "landed_at": -1.0, "done_for": -1.0, "prev_t": 0.0,
          "fixes": [], "seen": 0, "errors": []}

    def tick(*_):
        try:
            now = time.perf_counter()
            if not _jf_gate(st, now):
                return
            dt, st["prev_t"] = now - st["prev_t"], now
            pc = get_pc()
            pawn = pc.Pawn if pc is not None else None
            if pawn is None or pawn.Role != ROLE_AUTONOMOUS:  # clients only: the host's own moves are local
                return
            cmc = pawn.CharacterMovement
            mode = getattr(cmc.MovementMode, "value", cmc.MovementMode)
            z = pawn.K2_GetActorLocation().Z
            prev = st.get("prev_z")
            snapped = prev is not None and abs(z - (prev[0] + prev[1] * dt)) > SNAP_CM
            st["prev_z"] = (z, cmc.Velocity.Z)
            traj = st.get("traj")
            if traj is not None:
                t = now - traj["t0"]
                zt = traj["z0"] + traj["v0"] * t + 0.5 * traj["g"] * t * t
                if zt < traj["z0"] or t > 2.0:
                    st["traj"] = None
                elif mode == MOVE_WALKING and snapped and abs(z - traj["z0"]) < 5 and zt > traj["z0"] + 10:
                    # A correction for a move from before the jump put the client back on the ground, and its
                    # replay refused the jump again (same local check). Put it back on the host's jump arc.
                    _jf_launch(pawn, cmc, zt, traj["v0"] + traj["g"] * t)
                    st["prev_mode"] = MOVE_FALLING
                    st["prev_z"] = (zt, traj["v0"] + traj["g"] * t)
                    st["relaunches"] = st.get("relaunches", 0) + 1
                    if pawn.bPressedJump:
                        _jf_set_byte_bool(cmc, "bMayWantToGlide", True)
                        st["hold"] = st["enabled_hold"]
                    return
            if st["prev_mode"] == MOVE_FALLING and mode == MOVE_WALKING:
                st["landed_at"] = now
                st["hold"] = False
            st["prev_mode"] = mode
            if st.get("hold"):
                # The host's copy keeps bMayWantToGlide through the jump while the key is held (and then
                # glides at the apex); the client clears it after our jump. Keep it until release or landing.
                if mode != MOVE_FALLING or not pawn.bPressedJump:
                    st["hold"] = False
                elif cmc.bWantsToGlide:
                    st["hold"] = False
                elif cmc.Velocity.Z <= 0:
                    # Past the apex with the key held the host's copy starts gliding (may 1 -> wants 1,
                    # ready 1); the client's input doesn't, since it treats the press as stale.
                    _jf_set_byte_bool(cmc, "bWantsToGlide", True)
                    _jf_set_byte_bool(cmc, "bMayWantToGlide", False)
                    _jf_set_byte_bool(cmc, "bIsGlideReady", True)
                    st["glides"] = st.get("glides", 0) + 1
                    st["hold"] = False
                elif not cmc.bMayWantToGlide:
                    _jf_set_byte_bool(cmc, "bMayWantToGlide", True)
                    st["holds"] = st.get("holds", 0) + 1
                return
            # Jump count is usually 0 here, but 2 when a correction arrived at touchdown (the replay keeps it)
            if (mode != MOVE_WALKING or not pawn.bPressedJump or pawn.bWasJumping
                    or now - st["landed_at"] > LANDED_WINDOW_S or st["done_for"] == st["landed_at"]):
                return
            st["seen"] += 1
            st["done_for"] = st["landed_at"]
            if not st["enabled"]:
                st["fixes"].append((round(now, 4), "seen only"))
                return
            v0, g = _jf_takeoff_speed(cmc)
            dt = min(max(dt, 0.004), 0.05)
            # The host jumped at the start of this frame's move: one frame of that jump
            _jf_launch(pawn, cmc, z + v0 * dt + 0.5 * g * dt * dt, v0 + g * dt)
            st["traj"] = {"t0": now - dt, "z0": z, "v0": v0, "g": g}
            st["prev_z"] = (z + v0 * dt + 0.5 * g * dt * dt, v0 + g * dt)
            _jf_set_byte_bool(cmc, "bMayWantToGlide", True)
            st["hold"] = st["enabled_hold"]
            st["fixes"].append((round(now, 4), round(v0), round(dt * 1000, 1)))
        except Exception as ex:
            if len(st["errors"]) < 5:
                st["errors"].append(repr(ex))

    JF["st"] = st
    hooks.remove_hook(_JF_TICK, hooks.Type.POST, "netlab.jumpfix")
    hooks.add_hook(_JF_TICK, hooks.Type.POST, "netlab.jumpfix", tick)
    return f"jumpfix on (enabled={enabled})"


def jf_stop():
    hooks.remove_hook(_JF_TICK, hooks.Type.POST, "netlab.jumpfix")
    return "jumpfix off"


def jf_report():
    st = JF["st"]
    return {"seen": st["seen"], "holds": st.get("holds", 0), "glides": st.get("glides", 0), "relaunches": st.get("relaunches", 0), "fixes": st["fixes"][-10:], "errors": st["errors"]}


print("netlab_jumpfix loaded")
