"""
Jump rubber-band logger (dev only). exec into BOTH bridges (host A, client B on one PC). Each side
logs the client character's movement state every frame with time.perf_counter() (QPC: one clock
for every process on the machine), so the two logs line up without any sync.

Client (B, its own character):   jl_client_start() / jl_client_stop()
Host   (A, B's character there): jl_host_start()   / jl_host_stop()
    The host log marks each correction it sends (ServerLastClientAdjustmentTime changes) and each
    good-move ack.
jl_dump(path) writes the log as JSON; dev/netlab_jumplog_analyze.py joins the two.

Pair it with netlab_suite's suite_move_start(..., jump_key=True) on B for the movement.
"""

# ruff: noqa: F821 - unrealsdk, hooks, get_pc, ENGINE come from the bridge namespace

import collections
import json
import time

from unrealsdk.unreal import WeakPointer

_JL_TICK = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
_JL_RESP = "/Script/Engine.Character:ClientMoveResponsePacked"
JL = globals().setdefault("JL", {})


def _jl_gate(st, now):
    gap = now - st.get("last_call", 0.0)
    st["last_call"] = now
    if gap < 0.004 and now - st.get("last_frame", 0.0) < 0.012:
        return False
    st["last_frame"] = now
    return True


def _jl_row(now, pawn):
    cmc = pawn.CharacterMovement
    loc = pawn.K2_GetActorLocation()
    v = cmc.Velocity
    return [
        round(now, 5), round(loc.X, 1), round(loc.Y, 1), round(loc.Z, 1), round(v.X, 1), round(v.Y, 1), round(v.Z, 1),
        getattr(cmc.MovementMode, "value", cmc.MovementMode), cmc.CustomMovementMode, pawn.JumpCurrentCount, int(pawn.bPressedJump),
        int(cmc.bWantsToGlide), int(cmc.bMayWantToGlide), int(cmc.bIsGlideReady), cmc.JumpedCount,
        int(cmc.bRepInDelayedFall), cmc.ReplicatedMantleState.Counter, int(cmc.bIsSprinting), int(cmc.bWantsToSlide),
        round(cmc.ServerLastClientAdjustmentTime, 4), round(cmc.ServerLastClientGoodMoveAckTime, 4),
        round(pawn.JumpKeyHoldTime, 3), int(pawn.bWasJumping), pawn.JumpCurrentCountPreJump,
        str(cmc.CurrentJump.JumpType.TagName), round(pawn.JumpForceTimeRemaining, 3),
        int(cmc.bStopGlidingOnButtonRelease),
    ]


COLS = ["t", "x", "y", "z", "vx", "vy", "vz", "mode", "cmode", "jcount", "pressed", "glide", "may_glide",
        "glide_ready", "jumped", "delayed_fall", "mantle_ctr", "sprint", "slide", "adj_t", "ack_t", "jhold", "was_jumping",
        "jcount_pre", "jtype", "jforce", "stop_glide_rel"]


def _jl_start(side, get_pawn):
    st = {"side": side, "rows": collections.deque(maxlen=30000), "events": collections.deque(maxlen=20000),
          "errors": [], "pawn": None, "t0": time.perf_counter()}

    def tick(*_):
        try:
            now = time.perf_counter()
            if not _jl_gate(st, now):
                return
            pawn = st["pawn"]() if st["pawn"] else None
            if pawn is None:
                p = get_pawn()
                if p is None:
                    return
                st["pawn"] = WeakPointer(p)
                pawn = p
            st["rows"].append(_jl_row(now, pawn))
            if side == "client":
                c = globals().get("SUITE", {}).get("client")
                if c is not None and c.get("jumps", 0) != st.get("jumps"):
                    if st.get("jumps") is not None:
                        st["events"].append([round(now, 5), "space"])
                    st["jumps"] = c.get("jumps", 0)
        except Exception as ex:
            if len(st["errors"]) < 5:
                st["errors"].append(repr(ex))

    def resp(obj, args, ret, func):
        try:
            p = st["pawn"]() if st["pawn"] else None
            if p is not None and obj == p:
                st["events"].append([round(time.perf_counter(), 5), "resp"])
        except Exception as ex:
            if len(st["errors"]) < 5:
                st["errors"].append(repr(ex))

    JL[side] = st
    hooks.remove_hook(_JL_TICK, hooks.Type.POST, "netlab.jumplog")
    hooks.add_hook(_JL_TICK, hooks.Type.POST, "netlab.jumplog", tick)
    if side == "client":
        hooks.remove_hook(_JL_RESP, hooks.Type.PRE, "netlab.jumplog_resp")
        hooks.add_hook(_JL_RESP, hooks.Type.PRE, "netlab.jumplog_resp", resp)
    return f"jumplog {side} on"


def jl_client_start():
    return _jl_start("client", lambda: get_pc().Pawn if get_pc() is not None else None)


def jl_host_start():
    def client_pawn():
        nd = ENGINE.GameViewport.World.NetDriver
        conns = list(nd.ClientConnections) if nd else []
        return conns[0].PlayerController.Pawn if conns and conns[0].PlayerController else None

    return _jl_start("host", client_pawn)


def jl_stop():
    hooks.remove_hook(_JL_TICK, hooks.Type.POST, "netlab.jumplog")
    hooks.remove_hook(_JL_RESP, hooks.Type.PRE, "netlab.jumplog_resp")
    return "jumplog off"


jl_client_stop = jl_host_stop = jl_stop


def jl_dump(path):
    side = "client" if "client" in JL else "host"
    st = JL[side]
    data = {"side": side, "cols": COLS, "rows": list(st["rows"]), "events": list(st["events"]), "errors": st["errors"]}
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    import os  # noqa: PLC0415

    os.replace(tmp, path)
    return f"{side}: {len(data['rows'])} rows, {len(data['events'])} events, errors {st['errors']}"


print("netlab_jumplog loaded")
