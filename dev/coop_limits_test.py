"""
Offline test of the co-op channel's host-side limits (mods/bl4_direct/coop.py, no game): fake
unrealsdk + mods_base, fake controllers, messages fed straight into the receive functions.

    python dev/coop_limits_test.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "mods" / "bl4_direct" / "coop.py"


class Clock:
    t = 1000.0


placed: list[dict] = []


def _fake_sdk() -> None:
    unrealsdk = types.ModuleType("unrealsdk")
    hooks = types.ModuleType("unrealsdk.hooks")
    unreal = types.ModuleType("unrealsdk.unreal")
    log = types.ModuleType("unrealsdk.logging")

    class Type:
        PRE, POST = "PRE", "POST"

    hooks.Type = Type
    hooks.Block = object()
    hooks.add_hook = lambda *a: None
    hooks.remove_hook = lambda *a: None
    unreal.UObject = object
    unreal.WeakPointer = lambda obj=None: (lambda: obj)
    for name in ("info", "error", "warning", "misc", "dev_warning"):
        setattr(log, name, lambda msg: None)
    unrealsdk.find_class = lambda name: types.SimpleNamespace(ClassDefaultObject=types.SimpleNamespace(IsServer=lambda w: True))
    unrealsdk.logging, unrealsdk.hooks, unrealsdk.unreal = log, hooks, unreal
    sys.modules.update({"unrealsdk": unrealsdk, "unrealsdk.hooks": hooks, "unrealsdk.unreal": unreal,
                        "unrealsdk.logging": log})  # fmt: skip

    mods_base = types.ModuleType("mods_base")
    mods_base.ENGINE = types.SimpleNamespace(GameViewport=None)
    mods_base.get_pc = lambda: Sender.local
    sys.modules["mods_base"] = mods_base

    pkg = types.ModuleType("bl4_direct")
    pkg.__path__ = []
    frametick = types.ModuleType("bl4_direct.frametick")
    frametick.add = lambda *a, **k: None
    frametick.remove = lambda *a, **k: None
    stand = types.ModuleType("bl4_direct.stand")
    stand.tick = lambda: None
    stand.place = lambda pc, target: placed.append(target) or {"ok": True}
    pkg.frametick, pkg.stand = frametick, stand
    sys.modules.update({"bl4_direct": pkg, "bl4_direct.frametick": frametick, "bl4_direct.stand": stand})


class Sender:
    """A fake player controller."""

    local: Sender | None = None
    _next = 1

    def __init__(self) -> None:
        self.addr = Sender._next
        Sender._next += 1
        self.NetConnection = types.SimpleNamespace(_get_address=lambda: self.addr)
        self.PlayerState = types.SimpleNamespace(PlayerNamePrivate=f"p{self.addr}", Name="ps")
        self.Name = "pc"
        self.sent: list[str] = []
        self.got: list[str] = []

    def _get_address(self) -> int:
        return self.addr

    def ServerSendTextChat(self, text: str) -> None:  # noqa: N802
        self.sent.append(text)

    def ClientMessage(self, s: str, kind: str, life: float) -> None:  # noqa: N802
        self.got.append(s)


def _load() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("bl4_direct.coop", SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bl4_direct.coop"] = mod
    spec.loader.exec_module(mod)
    mod.time = types.SimpleNamespace(monotonic=lambda: Clock.t)
    return mod


passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {name} {detail}")


def main() -> int:
    _fake_sdk()
    coop = _load()
    ran: list[dict] = []
    coop.handle("t.x", lambda req: ran.append(req.data) or {"ok": True})

    def q(pc: Sender, rid: int, body: str = '{"a":1}') -> None:
        coop._on_host_message(pc, f"q|t.x|{rid}|{body}")

    # No rate limits (the user's call): a burst from one player and from several all run, in the
    # next tick
    a, b = Sender(), Sender()
    for i in range(50):
        q(a, i)
    for i in range(30):
        q(b, i)
    check("no rate limit: all 80 queued", len(coop._State.queue) == 80, str(len(coop._State.queue)))
    check("no rate-limit code left", not any(hasattr(coop, n) for n in ("MAX_REQUESTS_PER_S", "MAX_QUEUE", "MAX_PER_TICK")))
    coop._tick_hook()
    check("all run in one tick", len(ran) == 80 and not coop._State.queue, str(len(ran)))
    ran.clear()

    # NaN / Infinity / 1e400 refused, non-dict bodies dropped
    Clock.t += 2
    c = Sender()
    for body in ('{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', '{"x":1e400}'):
        try:
            q(c, 1, body)
            check(f"refused {body}", False, "no error")
        except ValueError:
            check(f"refused {body}", True)
    q(c, 2, "[1,2]")
    check("non-dict body dropped", not coop._State.queue)
    check("finite floats still parse", coop._loads('{"x":1.5e3}') == {"x": 1500.0})

    # Hellos: a list of strings only
    for bad in ('{"f":"abc"}', '{"f":[1,2]}', '[1]', json.dumps({"f": ["x" * 65]})):
        try:
            coop._on_host_message(c, f"h|{bad}")
            check(f"hello {bad[:20]} refused", False)
        except ValueError:
            check(f"hello {bad[:20]} refused", True)
    coop._on_host_message(c, 'h|{"v":1,"f":["t.x","ground_loot"]}')
    check("good hello", coop._State.peers[c.addr].features == {"t.x", "ground_loot"})

    # Parts: at most MAX_PARTS_OPEN open per player (a memory bound), pieces no longer than CHUNK
    d = Sender()
    for rid in range(coop.MAX_PARTS_OPEN + 2):
        coop._on_host_message(d, f"p|{rid}|0|3|q|t.x|")
    check("parts open capped", len(coop._State.peers[d.addr].parts) == coop.MAX_PARTS_OPEN,
          str(len(coop._State.peers[d.addr].parts)))  # fmt: skip
    e = Sender()
    coop._on_host_message(e, f"p|1|0|2|{'x' * (coop.CHUNK + 1)}")
    check("long piece refused", not coop._State.peers[e.addr].parts)
    # A part that reassembles into another part is not unpacked again
    coop._on_host_message(e, "p|5|0|1|p|6|0|1|q|t.x|1|{}")
    check("nested part ignored", not coop._State.queue and not coop._State.peers[e.addr].parts)

    # A real chunked request (as _send_to_host splits it) still reassembles and runs
    Clock.t += 2
    f = Sender()
    Sender.local = f
    data = {"gear": {str(i): "@Ug" + "a" * 120 for i in range(10)}, "skills": {"n": 3}}
    coop._send_to_host("q", f"t.x|7|{coop._dump(data)}")
    check("sent in parts", len(f.sent) > 1, str(len(f.sent)))
    prefix = coop.PREFIX + "|"
    for text in f.sent:
        coop._on_host_message(f, text[len(prefix) :])
    check("chunked request queued once", len(coop._State.queue) == 1)
    ran.clear()
    coop._tick_hook()
    check("chunked request intact", ran == [data])
    check("parts cleared", not coop._State.peers.get(f.addr) or not coop._State.peers[f.addr].parts)

    # Carnage-style 1/s poll and Grapple-style pairs over 10 s: nothing dropped
    g = Sender()
    sent_n = 0
    for s in range(10):
        q(g, 1000 + sent_n)
        q(g, 1001 + sent_n)
        q(g, 1002 + sent_n)
        sent_n += 3
        coop._tick_hook()
        Clock.t += 1.0
    check("legit rate passes", len(ran) == 1 + 30, str(len(ran)))

    # Teleport bounds
    req = coop.Request(g, coop.TELEPORT, {}, 1, local=lambda r: None)
    for d_ in ({"x": float("nan"), "y": 0, "z": 0}, {"x": 1e9, "y": 0, "z": 0}, {"x": 0, "y": 0, "z": 0, "yaw": 2e6},
               {"x": "a", "y": 0, "z": 0}, {"y": 0, "z": 0}, {"x": True, "y": 0, "z": 0}):  # fmt: skip
        req.data = d_
        check(f"teleport {d_} refused", coop._teleport_near(req).get("ok") is False and not placed)
    req.data = {"x": 1.0, "y": -2.0, "z": 3.0, "yaw": 90, "location": "L" * 500}
    out = coop._teleport_near(req)
    check("teleport ok", out == {"ok": True} and len(placed) == 1 and len(placed[0]["location"]) == 200)

    print(f"{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
