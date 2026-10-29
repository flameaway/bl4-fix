"""
A message channel between our mods on the host and on co-op clients, for features a client can't
do itself (its position, items, currency and loot belong to the host).

Both directions use RPCs the game already has, each carrying a string:

- client -> host: GbxPlayerController.ServerSendTextChat(Text), the text chat request.
- host -> client: PlayerController.ClientMessage(S, Type, Life), a console message the game doesn't
  show anywhere (checked on screen).

Received RPCs run through ProcessEvent, so a PRE hook on the receiving side reads ours (they start
with PREFIX) and Blocks them: the host never broadcasts them as chat, the client never prints
them. Anything without our prefix passes untouched.

Who has what: a modded host says hello (with its feature list) to every connected player, again
every few seconds until they answer, and again when its features change. A modded client answers
with its own list. A client never sends anything until it has heard the host's hello, so a host
without our mods never sees a stray chat message.

Mods use:

    coop.feature("ground_loot", lambda: mod.is_enabled)    # advertise a feature
    coop.handle("ground_loot.bring", fn)                     # host side: fn(req) -> dict | None
    coop.run("ground_loot.bring", data, on_reply)            # do it here, or ask the host
    coop.role(), coop.host_has(name), coop.everyone_has(name)

A handler gets a Request (req.pc is the asking player's controller, valid during the call) and
returns a reply dict, or None and calls req.reply(...) later. Handlers run on the game thread from
our tick, never inside the RPC itself.

Client -> host messages are split into CHUNK-character parts: the chat RPC has a validate step on
the host whose limits we don't know, and failing it would drop the connection.

No hooks in the mod's build_mod: `install()` adds ours with fixed identifiers, so importing this
module again (hot reload) replaces them.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import unrealsdk
from mods_base import ENGINE, get_pc
from unrealsdk import logging
from unrealsdk.hooks import Block, Type, add_hook, remove_hook
from unrealsdk.unreal import UObject, WeakPointer

from . import stand

PREFIX = "~bl4m~"
VERSION = 1
CHUNK = 100  # characters of payload per client -> host message
MESSAGE_TYPE = "BL4Mods"

CHAT_FUNC = "/Script/GbxGame.GbxPlayerController:ServerSendTextChat"
MESSAGE_FUNC = "/Script/Engine.PlayerController:ClientMessage"
TICK_FUNC = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
HOOK_ID = "bl4_coop"

HELLO_EVERY_S = 5.0  # host: re-send hello to a player who hasn't answered
HELLO_TRIES = 12  # then give up on them (an unmodded player): one minute
HOUSEKEEPING_S = 0.5
PART_TIMEOUT_S = 10.0
MAX_MESSAGE = 16384  # characters, reassembled
MAX_PENDING = 32

_kismet = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject


def log(msg: str) -> None:
    logging.info(f"[Co-op] {msg}")


# region Roles


def _world() -> Any:
    return ENGINE.GameViewport.World if ENGINE.GameViewport else None


def _server_connection() -> Any:
    world = _world()
    driver = world.NetDriver if world is not None else None
    return driver.ServerConnection if driver is not None else None


def _remote_controllers() -> list[UObject]:
    """The other players' controllers (on the host)."""
    world = _world()
    driver = world.NetDriver if world is not None else None
    if driver is None or driver.ServerConnection is not None:
        return []
    out = []
    for conn in driver.ClientConnections:
        pc = conn.PlayerController if conn is not None else None
        if pc is not None:
            out.append(pc)
    return out


def remote_controllers() -> list[UObject]:
    """As host: the other players' controllers (empty in solo and on a client)."""
    return _remote_controllers()


class _Role:
    """The role, worked out at most every ROLE_TTL_S: mods ask several times a frame (~30 us each)."""

    at = -1e9
    client = False
    name = "solo"


ROLE_TTL_S = 0.25


def _role() -> type[_Role]:
    now = time.monotonic()
    if now - _Role.at >= ROLE_TTL_S:
        _Role.at = now
        _Role.client = _server_connection() is not None
        _Role.name = "client" if _Role.client else ("host" if _remote_controllers() else "solo")
    return _Role


def is_client() -> bool:
    """Whether we joined someone else's game (up to ROLE_TTL_S old)."""
    return _role().client


def is_server() -> bool:
    world = _world()
    return world is not None and _server_connection() is None and bool(_kismet.IsServer(world))


def role() -> str:
    """'client', 'host' (with players connected) or 'solo' (up to ROLE_TTL_S old)."""
    return _role().name


# endregion
# region Features


# Kept across a reload of this module (importlib.reload reuses its namespace): other mods
# registered these and won't do it again
_features: dict[str, Callable[[], bool]] = globals().get("_features", {})
_handlers: dict[str, Callable[[Request], dict[str, Any] | None]] = globals().get("_handlers", {})
_push_handlers: dict[str, Callable[[dict[str, Any]], None]] = globals().get("_push_handlers", {})


def feature(name: str, enabled: Callable[[], bool]) -> None:
    """Advertise a feature to the other side while enabled() says so (checked twice a second)."""
    _features[name] = enabled


def handle(kind: str, fn: Callable[[Request], dict[str, Any] | None]) -> None:
    """Host side: run fn(request) for a `kind` request (from a client, or our own via run())."""
    _handlers[kind] = fn


def on_push(kind: str, fn: Callable[[dict[str, Any]], None]) -> None:
    """Client side: run fn(data) when the host pushes a `kind` message."""
    _push_handlers[kind] = fn


def my_features() -> list[str]:
    out = []
    for name, enabled in _features.items():
        try:
            if enabled():
                out.append(name)
        except Exception:  # noqa: BLE001 - a mod being reloaded
            pass
    return sorted(out)


class _Peer:
    """A connected player, on the host."""

    def __init__(self, pc: UObject) -> None:
        self.pc = WeakPointer(pc)
        self.features: set[str] | None = None  # None until they answer
        self.hello_at = 0.0
        self.tries = 0
        self.told: list[str] | None = None  # the feature list we last said hello with
        self.parts: dict[int, tuple[float, str, list[str | None]]] = {}


class _State:
    peers: dict[int, _Peer] = {}  # host: connection address -> peer
    host_features: set[str] | None = None  # client: what the host said, None = not heard
    host_connection = 0  # client: address of the connection host_features came over
    told_host: list[str] | None = None
    next_id = 1
    pending: dict[int, tuple[Callable[[dict[str, Any]], None], float]] = {}
    queue: list[tuple[Request, Callable[..., Any]]] = []
    housekeeping_at = 0.0
    unreadable: set[str] = set()  # RPC parameters that couldn't be read (logged once each)


def host_has(name: str) -> bool:
    """As a client: whether the host said it runs `name` (False until it says hello)."""
    return _State.host_features is not None and name in _State.host_features


def host_known() -> bool:
    """As a client: whether a modded host has said hello."""
    return _State.host_features is not None


def players_without(name: str) -> list[UObject]:
    """As host: connected players not known to run `name` (unmodded, or not answered yet)."""
    out = []
    for pc in _remote_controllers():
        peer = _State.peers.get(_conn_key(pc))
        if peer is None or peer.features is None or name not in peer.features:
            out.append(pc)
    return out


def players_with(name: str) -> list[UObject]:
    """As host: connected players who said they run `name`."""
    return [pc for pc in _remote_controllers() if player_has(pc, name)]


def player_features(pc: UObject) -> set[str] | None:
    """As host: what this connected player said they run; None = no answer (no mods, or not yet)."""
    peer = _State.peers.get(_conn_key(pc))
    return None if peer is None else peer.features


def player_has(pc: UObject, name: str) -> bool:
    """As host: whether this connected player said they run `name`."""
    peer = _State.peers.get(_conn_key(pc))
    return peer is not None and peer.features is not None and name in peer.features


def everyone_has(name: str) -> bool:
    """True if every player in this game runs `name` (solo: always)."""
    if is_client():
        return host_has(name)
    return not players_without(name)


def _conn_key(pc: UObject) -> int:
    conn = pc.NetConnection if hasattr(pc, "NetConnection") else None
    return conn._get_address() if conn is not None else pc._get_address()


# endregion
# region Requests


class Request:
    """One request, being handled on the host."""

    def __init__(self, pc: UObject, kind: str, data: dict[str, Any], rid: int,
                 local: Callable[[dict[str, Any]], None] | None = None) -> None:  # fmt: skip
        self.pc = pc
        self._pc = WeakPointer(pc)
        self.kind = kind
        self.data = data
        self.id = rid
        self.remote = local is None
        self._local = local
        self.done = False

    def controller(self) -> UObject | None:
        """The asking player's controller now (None if they left)."""
        return self._pc()

    def reply(self, data: dict[str, Any]) -> None:
        if self.done:
            return
        self.done = True
        if self._local is not None:
            _call(self._local, data)
            return
        pc = self._pc()
        if pc is not None:
            _send_to_client(pc, f"r|{self.id}|{_dump(data)}")


def _dump(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=True)


def _call(fn: Callable[..., Any], *args: Any) -> None:
    try:
        fn(*args)
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Co-op] {ex!r}")
        logging.exception(ex)


def run(kind: str, data: dict[str, Any], on_reply: Callable[[dict[str, Any]], None], *,
        feature_name: str | None = None, timeout: float = 6.0) -> bool:  # fmt: skip
    """
    Does `kind` for the local player: right here in solo or as host, else asks the host. on_reply
    gets the handler's reply dict, or {"ok": False, "error": ...} if it couldn't happen. Returns
    False (and calls nothing) if we're a client and the host doesn't run feature_name.
    """
    if not is_client():
        fn = _handlers.get(kind)
        pc = get_pc()
        if fn is None or pc is None:
            on_reply({"ok": False, "error": "not available"})
            return True
        _run_handler(Request(pc, kind, data, 0, on_reply), fn)
        return True
    if feature_name is not None and not host_has(feature_name):
        return False
    request(kind, data, on_reply, timeout=timeout)
    return True


def request(kind: str, data: dict[str, Any], on_reply: Callable[[dict[str, Any]], None], *,
            timeout: float = 6.0) -> None:  # fmt: skip
    """As a client: asks the host. on_reply gets the answer, or {"ok": False, "error": "timeout"}."""
    install()
    rid = _State.next_id
    _State.next_id += 1
    _State.pending[rid] = (on_reply, time.monotonic() + timeout)
    while len(_State.pending) > MAX_PENDING:
        old = next(iter(_State.pending))
        cb, _ = _State.pending.pop(old)
        _call(cb, {"ok": False, "error": "too many requests"})
    _send_to_host("q", f"{kind}|{rid}|{_dump(data)}")


def push(pc: UObject, kind: str, data: dict[str, Any]) -> None:
    """As host: sends a message to one connected player (only if they run our mods)."""
    peer = _State.peers.get(_conn_key(pc))
    if peer is not None and peer.features is not None:
        _send_to_client(pc, f"s|{kind}|{_dump(data)}")


def push_all(kind: str, data: dict[str, Any], feature_name: str | None = None) -> None:
    """As host: sends a message to every modded player (running feature_name, if given)."""
    for pc in _remote_controllers():
        peer = _State.peers.get(_conn_key(pc))
        if peer is not None and peer.features is not None and (feature_name is None or feature_name in peer.features):
            _send_to_client(pc, f"s|{kind}|{_dump(data)}")


def _run_handler(req: Request, fn: Callable[[Request], dict[str, Any] | None]) -> None:
    try:
        result = fn(req)
    except Exception as ex:  # noqa: BLE001 - tell the asker instead of leaving them waiting
        logging.error(f"[Co-op] {req.kind}: {ex!r}")
        logging.exception(ex)
        result = {"ok": False, "error": f"the host's mod failed ({type(ex).__name__})"}
    if result is not None:
        req.reply(result)


# endregion
# region Sending


def _send_to_host(op: str, body: str) -> None:
    pc = get_pc()
    if pc is None:
        return
    text = f"{op}|{body}"
    if len(text) <= CHUNK:
        pc.ServerSendTextChat(f"{PREFIX}|{text}")
        return
    rid = _State.next_id
    _State.next_id += 1
    pieces = [text[i : i + CHUNK] for i in range(0, len(text), CHUNK)]
    for i, piece in enumerate(pieces):
        pc.ServerSendTextChat(f"{PREFIX}|p|{rid}|{i}|{len(pieces)}|{piece}")


def _send_to_client(pc: UObject, text: str) -> None:
    pc.ClientMessage(f"{PREFIX}|{text}", MESSAGE_TYPE, 0.0)


# endregion
# region Receiving


def _on_host_message(pc: UObject, text: str) -> None:
    """A message from a client, on the host (inside the RPC: only parse and queue here)."""
    key = _conn_key(pc)
    peer = _State.peers.get(key)
    if peer is None:
        peer = _State.peers[key] = _Peer(pc)
    op, _, rest = text.partition("|")
    if op == "p":
        rid_s, i_s, n_s, piece = rest.split("|", 3)
        rid, i, n = int(rid_s), int(i_s), int(n_s)
        if not 0 <= i < n <= MAX_MESSAGE // CHUNK + 1:
            return
        _, prefix, parts = peer.parts.get(rid, (0.0, "", [None] * n))
        if len(parts) != n:
            return
        parts[i] = piece
        peer.parts[rid] = (time.monotonic(), prefix, parts)
        if all(p is not None for p in parts):
            del peer.parts[rid]
            _on_host_message(pc, "".join(p for p in parts if p is not None))
        return
    if op == "h":
        info = json.loads(rest)
        features = set(info.get("f", []))
        if peer.features != features:
            log(f"{_name(pc)} runs {', '.join(sorted(features)) or 'no co-op features'}")
        peer.features = features
        return
    if op == "q":
        kind, rid_s, body = rest.split("|", 2)
        fn = _handlers.get(kind)
        req = Request(pc, kind, json.loads(body), int(rid_s))
        if fn is None:
            req.reply({"ok": False, "error": "the host doesn't run that"})
            return
        _State.queue.append((req, fn))


def _on_client_message(text: str) -> None:
    """A message from the host, on a client."""
    op, _, rest = text.partition("|")
    conn = _server_connection()
    if op == "h":
        info = json.loads(rest)
        features = set(info.get("f", []))
        if _State.host_features != features:
            log(f"the host runs {', '.join(sorted(features)) or 'no co-op features'}")
        _State.host_features = features
        _State.host_connection = conn._get_address() if conn is not None else 0
        _State.told_host = None  # answer (again): the host may have just restarted the mod
        _say_hello_to_host()
    elif op == "r":
        rid_s, body = rest.split("|", 1)
        entry = _State.pending.pop(int(rid_s), None)
        if entry is not None:
            _call(entry[0], json.loads(body))
    elif op == "s":
        kind, body = rest.split("|", 1)
        fn = _push_handlers.get(kind)
        if fn is not None:
            _call(fn, json.loads(body))


def _name(pc: UObject) -> str:
    ps = pc.PlayerState
    return str(ps.PlayerNamePrivate or ps.Name) if ps is not None else str(pc.Name)


def _rpc_text(args: Any, name: str) -> str | None:
    """
    The RPC's text parameter, or None if it can't be read (a game patch renamed it). Then the hooks let
    every call through: blocking on an error would swallow all of the players' real chat.
    """
    try:
        return str(getattr(args, name))
    except Exception as ex:  # noqa: BLE001
        if name not in _State.unreadable:
            _State.unreadable.add(name)
            logging.error(f"[Co-op] can't read the RPC's {name} parameter (a game patch?): co-op messages are off: {ex!r}")
        return None


def _chat_hook(obj: UObject, args: Any, _ret: Any, _func: Any) -> Any:
    text = _rpc_text(args, "Text")
    if text is None or not text.startswith(PREFIX + "|"):
        return None
    try:
        if not is_server() or obj.IsLocalController():
            return None  # our own outgoing request (as a client): let it go out
        _on_host_message(obj, text[len(PREFIX) + 1 :])
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Co-op] bad message: {ex!r}")
    return Block


def _message_hook(obj: UObject, args: Any, _ret: Any, _func: Any) -> Any:
    text = _rpc_text(args, "S")
    if text is None or not text.startswith(PREFIX + "|"):
        return None
    try:
        if not obj.IsLocalController():
            return None  # the host sending to a client: let it go out
        if is_client():
            _on_client_message(text[len(PREFIX) + 1 :])
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Co-op] bad message: {ex!r}")
    return Block


# endregion
# region Tick


def _say_hello_to_host() -> None:
    mine = my_features()
    if mine != _State.told_host:
        _State.told_host = mine
        _send_to_host("h", _dump({"v": VERSION, "f": mine}))


def _host_housekeeping(now: float) -> None:
    mine = my_features()
    live: set[int] = set()
    for pc in _remote_controllers():
        key = _conn_key(pc)
        live.add(key)
        peer = _State.peers.get(key)
        if peer is None or peer.pc() is None or peer.pc() != pc:
            peer = _State.peers[key] = _Peer(pc)
        for rid, (at, _, _) in list(peer.parts.items()):
            if now - at > PART_TIMEOUT_S:
                del peer.parts[rid]
        if peer.told == mine and (peer.features is not None or peer.tries >= HELLO_TRIES):
            continue
        if peer.told == mine and now - peer.hello_at < HELLO_EVERY_S:
            continue
        if peer.told != mine:
            peer.tries = 0
        peer.told = mine
        peer.hello_at = now
        peer.tries += 1
        _send_to_client(pc, f"h|{_dump({'v': VERSION, 'f': mine})}")
    for key in [k for k in _State.peers if k not in live]:
        del _State.peers[key]


def _client_housekeeping(now: float) -> None:
    conn = _server_connection()
    address = conn._get_address() if conn is not None else 0
    if address != _State.host_connection:
        # Left, or joined another game: whatever we knew about the host is gone
        _State.host_features = None
        _State.host_connection = address
        _State.told_host = None
    if _State.host_features is not None:
        _say_hello_to_host()
    for rid, (cb, deadline) in list(_State.pending.items()):
        if now > deadline:
            del _State.pending[rid]
            _call(cb, {"ok": False, "error": "the host didn't answer"})


def _tick_hook(*_: Any) -> None:
    try:
        stand.tick()
        if _State.queue:
            queue, _State.queue = _State.queue, []
            for req, fn in queue:
                if req.controller() is not None:
                    _run_handler(req, fn)
        now = time.monotonic()
        if now < _State.housekeeping_at:
            return
        _State.housekeeping_at = now + HOUSEKEEPING_S
        if is_client():
            _client_housekeeping(now)
        else:
            if _State.host_features is not None or _State.pending:
                _client_housekeeping(now)  # just left a game: fail what's still waiting
            if _State.peers or _remote_controllers():
                _host_housekeeping(now)
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        logging.error(f"[Co-op] {ex!r}")


# endregion
# region Built-in services

TELEPORT = "coop.teleport"


def _teleport_near(req: Request) -> dict[str, Any]:
    """
    Puts the asking player in front of a Big Encore machine at (x, y, z), facing it, never in a
    wall (stand.py: the machine's `yaw` and its marker name `location` find its front; without
    them, spots all around it). The host's teleport moves anyone; a client's own would be undone
    by the host.
    """
    d = req.data
    target = {"x": float(d["x"]), "y": float(d["y"]), "z": float(d["z"]),
              "yaw": None if d.get("yaw") is None else float(d["yaw"]), "location": str(d.get("location") or "")}  # fmt: skip
    return stand.place(req.pc, target)


def teleport_near(x: float, y: float, z: float, on_done: Callable[[dict[str, Any]], None], *,
                  yaw: float | None = None, location: str | None = None, **_old: Any) -> bool:  # fmt: skip
    """
    Teleports the local player in front of the Big Encore machine at (x, y, z) (`yaw` = its facing,
    `location` = its marker name): at once in solo or as host, through the host's mod as a client.
    on_done gets {"ok": True} or {"ok": False, "error": ...}. False if we're a client and the host
    doesn't run our mods (then nothing happens). `radii` / `height` from older callers are ignored.
    """
    return run(TELEPORT, {"x": x, "y": y, "z": z, "yaw": yaw, "location": location}, on_done,
               feature_name=TELEPORT)  # fmt: skip


handle(TELEPORT, _teleport_near)
feature(TELEPORT, lambda: True)

# endregion


_installed = False


def install() -> None:
    """Adds our hooks (again, replacing an older copy's). Safe to call any number of times."""
    global _installed
    if _installed:
        return
    _installed = True
    for func, kind, fn, suffix in (
        (CHAT_FUNC, Type.PRE, _chat_hook, "chat"),
        (MESSAGE_FUNC, Type.PRE, _message_hook, "message"),
        (TICK_FUNC, Type.POST, _tick_hook, "tick"),
    ):
        remove_hook(func, kind, f"{HOOK_ID}_{suffix}")
        add_hook(func, kind, f"{HOOK_ID}_{suffix}", fn)


def uninstall() -> None:
    global _installed
    _installed = False
    remove_hook(CHAT_FUNC, Type.PRE, f"{HOOK_ID}_chat")
    remove_hook(MESSAGE_FUNC, Type.PRE, f"{HOOK_ID}_message")
    remove_hook(TICK_FUNC, Type.POST, f"{HOOK_ID}_tick")


install()
