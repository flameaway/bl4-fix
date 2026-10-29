"""
Who dealt how much damage, with what. Runs where the damage is dealt: solo, or on the host for
every player (a client gets the host's board through the co-op channel, see share.py).

Per hit (`record`, called from the caused-damage hook, see native.py): one read of the hook's
parameters block (offsets taken from reflection once, `native.struct_offsets`), the player from
the instigator (their character), the source from the causer:

- a hit from an equipped weapon / shield / repair kit actor: that item (naming.Equipped);
- a grenade, or any projectile whose owner chain reaches an equipped item: that item;
- a damage-over-time tick (`bIsDamageOverTime`): the element's status ("Bleed", "Burning"...);
- the character itself: a ground slam (`IsPerformingAnyTypeOfGroundSlam`, or a kinetic splash),
  melee (a single-target hit within MELEE_RANGE of the character), otherwise its skills;
- any other pawn the player owns: companions; vehicles; the world (burning ground): environment;
- a hit whose instigator isn't a player but something a player owns (a summon: its own character
  instigates its hits): that player's companions.

Only damage to hostile targets counts, minus the overkill (the last hit's damage past the target's
health). Causers are classified once each (`S.causers`, keyed by address + object index) since
projectiles die before the next frame; everything else is dict lookups.

Scopes: "fight" (team-wide: hits more than FIGHT_GAP_S apart start a new one, unless a boss this
fight has hit is still up; a boss's death ends it; the last fight is kept until the next begins) and "session" (since the game started or
the last reset). Bosses: every player's boss bars (`BossState.VisibleBosses`) are watched; damage
to a watched boss is also counted per boss, and when it dies `S.report` holds the boss report
(page.py opens it). The board (`board()`) is built on demand: a mod menu poll or a co-op request.
"""

from __future__ import annotations

import ctypes
import itertools
import struct
import sys
import time
from pathlib import Path
from typing import Any

from mods_base import ENGINE, get_pc
from unrealsdk import logging
from unrealsdk.unreal import WeakPointer

from . import naming, native

FIGHT_GAP_S = 10.0
MELEE_RANGE = 320.0  # unreal units (cm) between the character and a melee hit
HOSTILE = 2  # ETeamAttitude
TOP = 6  # sources per player in the board
REFRESH_S = 0.5  # players, bindings and equipped items
BOSS_SCAN_S = 0.25  # boss bars and boss deaths
MAX_CAUSERS = 4096
MAX_PENDING = 20000  # hits queued between two ticks (never near it: a tick runs every frame)
GAME_DIR = Path()  # set by __init__ (Kairosdex boss pictures live under it)

_U64 = struct.Struct("<Q").unpack_from
_I32 = struct.Struct("<i").unpack_from
_F32 = struct.Struct("<f").unpack_from
_VEC = struct.Struct("<3d").unpack_from


class Tally:
    __slots__ = ("total", "crit", "hits", "kills", "big", "sources")

    def __init__(self) -> None:
        self.total = 0.0
        self.crit = 0.0
        self.hits = 0
        self.kills = 0
        self.big = 0.0
        # source key -> [damage, hits, element bits]
        self.sources: dict[str, list[Any]] = {}

    def add(self, src: str, dmg: float, crit: bool, kill: bool, elem_bit: int) -> None:
        self.total += dmg
        self.hits += 1
        if crit:
            self.crit += dmg
        if kill:
            self.kills += 1
        if dmg > self.big:
            self.big = dmg
        entry = self.sources.get(src)
        if entry is None:
            self.sources[src] = [dmg, 1, elem_bit]
        else:
            entry[0] += dmg
            entry[1] += 1
            entry[2] |= elem_bit


class Player:
    __slots__ = ("key", "name", "char", "pawn", "pawn_addr", "equipped", "fight", "session", "is_local", "seen",
                 "controller")  # fmt: skip

    def __init__(self, key: int) -> None:
        self.key = key  # PlayerState address
        self.name = ""
        self.char = ""
        self.pawn: Any = None  # WeakPointer
        self.pawn_addr = 0
        self.controller: Any = None  # WeakPointer (its boss bars)
        self.equipped = naming.Equipped()
        self.fight = Tally()
        self.session = Tally()
        self.is_local = False
        self.seen = 0.0


class Boss:
    __slots__ = ("addr", "actor", "name", "picture", "first", "last", "damage")

    def __init__(self, addr: int, actor: Any) -> None:
        self.addr = addr
        self.actor = WeakPointer(actor)
        self.name, self.picture = _boss_identity(actor)
        self.first = 0.0  # first and last hit on it by a player
        self.last = 0.0
        self.damage: dict[int, float] = {}  # player key -> damage to this boss


ELEMENTS = ("normal", "fire", "shock", "corrosive", "cryo", "radiation", "sonic")
ELEMENT_BIT = {name: 1 << i for i, name in enumerate(ELEMENTS)}
# The status names the game's skill tooltips use ("apply Bleed, causing Kinetic Status Effect damage")
DOT_NAMES = {"normal": "Bleed", "fire": "Burning", "shock": "Shock", "corrosive": "Corrosion", "cryo": "Frozen",
             "radiation": "Irradiated", "sonic": "Sonic"}  # fmt: skip
ELEMENT_NAMES = {"normal": "Kinetic", "fire": "Fire", "shock": "Shock", "corrosive": "Corrosive", "cryo": "Cryo",
                 "radiation": "Radiation", "sonic": "Sonic"}  # fmt: skip


class S:
    binder = native.Binder()
    off: dict[str, int] = {}  # noqa: RUF012
    masks: dict[str, tuple[int, int]] = {}  # noqa: RUF012 - bool field -> (offset, mask)
    block = 0  # bytes of the parameters block read per hit
    players: dict[int, Player] = {}  # noqa: RUF012 - PlayerState address -> player
    by_pawn: dict[int, Player] = {}  # noqa: RUF012 - character address -> player
    order: list[int] = []  # noqa: RUF012 - players in join order
    types: dict[bytes, tuple[str, bool]] = {}  # noqa: RUF012 - DamageType handle bytes -> (element, DoT)
    causers: dict[tuple[int, int], tuple[str, str | int | None]] = {}  # noqa: RUF012
    # Instigators that aren't players (summons) -> the owning player's character address, or 0
    minions: dict[tuple[int, int], int] = {}  # noqa: RUF012
    direct: set[int] = set()  # noqa: RUF012 - characters and equipped gear actors (no classifying)
    # Hits for drain(): (raw block, causer key, minion key)
    pending: list[tuple[bytes, tuple[int, int] | None, tuple[int, int] | None]] = []  # noqa: RUF012
    # Offsets the hook reads (from S.off)
    attitude_at = causer_at = instigator_at = type_at = 0
    bosses: dict[int, Boss] = {}  # noqa: RUF012 - watched boss address -> boss
    report: dict[str, Any] | None = None  # the last boss report
    report_ids = itertools.count(int(time.time() * 1000))
    fight_start = 0.0
    fight_last = 0.0
    fight_no = 0  # bumped when a fight starts (a boss report belongs to one)
    fight_closed = False  # a boss died: the next hit starts a new fight
    session_start = 0.0
    session_last = 0.0
    refreshed = 0.0
    boss_scanned = 0.0
    revision = 0  # bumped on every counted hit: the board changed
    # Diagnostics (the `carnage stats` command)
    events = 0
    counted = 0
    unknown = 0
    errors = 0
    busy = 0.0  # in the hook
    worst = 0.0
    drains = 0
    drain_busy = 0.0  # counting in the tick
    drain_worst = 0.0
    busy_seen = 0.0
    frame_worst = 0.0  # the most hook time between two ticks
    ok = False  # bound and listening


def setup() -> bool:
    """Reflection lookups and binding checks, once (game thread)."""
    if S.ok:
        return True
    if not S.binder.ready():
        return False
    try:
        S.off = native.struct_offsets()
        details = S.binder.func._find_prop("Details").Struct
        base = S.binder.func._find_prop("Details").Offset_Internal
        for name in ("bWasCrit", "bIsDamageOverTime", "bCausedDeath", "bWasSplashDamage"):
            prop = details._find_prop(name)
            S.masks[name] = (base + prop.Offset_Internal, int(getattr(prop, "FieldMask", 0xFF)) or 0xFF)
        # Field -> bytes we read from it. The block read per hit ends at the last of them, never
        # past the parameters block (304 bytes in this build)
        sizes = {"damagedactor": 8, "damage": 4, "overkilldamage": 4, "damagetype": 8, "damageinstigator": 8,
                 "damagecauser": 8, "hitlocation": 24, "attitudetowards": 1}  # fmt: skip
        for name in sizes:
            if name not in S.off:
                raise KeyError(name)
        end = max(S.off[n] + size for n, size in sizes.items())
        S.block = max(end, *(offset + 1 for offset, _ in S.masks.values()))
        S.attitude_at, S.causer_at = S.off["attitudetowards"], S.off["damagecauser"]
        S.instigator_at, S.type_at = S.off["damageinstigator"], S.off["damagetype"]
        if S.block > S.binder.func.ParamsSize:
            raise ValueError(f"fields end at {S.block}, past the {S.binder.func.ParamsSize}-byte parameters")
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Carnage Meter] the damage details changed shape: {ex!r}")
        return False
    S.ok = True
    return True


# region Per hit


def _flag(raw: bytes, name: str) -> bool:
    offset, mask = S.masks[name]
    return bool(raw[offset] & mask)


def _element(raw: bytes, args: Any) -> tuple[str, bool]:
    """(element, damage over time) of a damage type, named from the live hit the first time it's seen."""
    at = S.off["damagetype"]
    key = raw[at : at + 8]
    found = S.types.get(key)
    if found is None:
        try:
            name = str(args.Details.DamageType._name)
        except Exception:  # noqa: BLE001
            name = "Normal"
        low = name.lower()
        elem = low.removesuffix("_status")
        found = S.types[key] = (elem if elem in ELEMENT_BIT else "normal", low.endswith("_status"))
    return found


def _classify_causer(player: Player, causer: Any) -> tuple[str, str | int | None]:
    """(kind, detail) for a causer we haven't seen: "item" + actor address, "grenade", "skill"..."""
    if causer is None:
        return ("env", None)
    if causer._get_address() == player.pawn_addr:
        return ("self", None)
    names = []
    cls = causer.Class
    while cls is not None and len(names) < 12:
        names.append(cls.Name)
        cls = cls.SuperField
    if "ModularGrenade" in names:
        return ("grenade", None)
    if "WorldSettings" in names:
        return ("env", None)
    # Projectiles, beams, spawned helpers: follow the owner up to a gear actor or the character
    actor, depth = causer, 0
    while actor is not None and depth < 5:
        address = actor._get_address()
        if address in player.equipped.by_actor:
            return ("item", address)
        if address == player.pawn_addr:
            return ("skill", None) if depth else ("self", None)
        try:
            actor = actor.Owner or actor.Instigator
        except AttributeError:
            break
        depth += 1
    if "WheeledVehiclePawn" in names or any("Vehicle" in n for n in names[:3]):
        return ("vehicle", None)
    if "Pawn" in names:
        return ("companion", None)
    return ("other", names[0])


def _minion_owner(actor: Any) -> int:
    """The character address of the player who owns a non-player instigator (a summon), else 0."""
    depth = 0
    while actor is not None and depth < 5:
        try:
            actor = actor.Owner or actor.Instigator
        except AttributeError:
            return 0
        if actor is not None and actor._get_address() in S.by_pawn:
            return actor._get_address()
        depth += 1
    return 0


def record(args: Any) -> None:
    """
    One hit, inside the hook: copy the hit's details and queue them (drain() counts them on the
    next tick). The only object reads here are for what may be gone by then: a causer we haven't
    classified yet (projectiles die fast) and a damage type's name the first time it's seen.
    Each hit runs with the CPU caches cold (the game's own work in between), so every step here
    costs ~10x what it does in a loop: keep it short.
    """
    start = time.perf_counter()
    S.events += 1
    try:
        raw = ctypes.string_at(args._get_address(), S.block)
        if raw[S.attitude_at] != HOSTILE:
            return
        (causer,) = _U64(raw, S.causer_at)
        (instigator,) = _U64(raw, S.instigator_at)
        key = minion = None
        if instigator and instigator not in S.direct:
            # Not a player's character: a summon? Its owner is read now (it may die before the tick)
            minion = (instigator, ctypes.c_int32.from_address(instigator + native.INDEX_OFFSET).value)
            if minion not in S.minions:
                S.minions[minion] = _minion_owner(args.Details.DamageInstigator)
        if causer and causer not in S.direct:
            # A live actor during the broadcast: its object index is safe to read
            key = (causer, ctypes.c_int32.from_address(causer + native.INDEX_OFFSET).value)
            if key not in S.causers:
                player = S.by_pawn.get(instigator)
                if player is not None:
                    S.causers[key] = _classify_causer(player, args.Details.DamageCauser)
        if raw[S.type_at : S.type_at + 8] not in S.types:
            _element(raw, args)
        if len(S.pending) < MAX_PENDING:
            S.pending.append((raw, key, minion))
    except Exception as ex:  # noqa: BLE001 - never break the game's damage
        S.errors += 1
        if S.errors <= 5:
            logging.error(f"[Carnage Meter] {ex!r}")
    finally:
        took = time.perf_counter() - start
        S.busy += took
        if took > S.worst:
            S.worst = took


def _own_state(player: Player, own: dict[int, tuple[bool, Any]]) -> tuple[bool, Any]:
    """(slamming, location) of a player's character, read once per drain and only if needed."""
    found = own.get(player.key)
    if found is None:
        slam, loc = False, None
        pawn = player.pawn() if player.pawn is not None else None
        if pawn is not None:
            try:
                slam = bool(pawn.IsPerformingAnyTypeOfGroundSlam())
            except Exception:  # noqa: BLE001
                pass
            try:
                v = pawn.K2_GetActorLocation()
                loc = (v.X, v.Y, v.Z)
            except Exception:  # noqa: BLE001
                pass
        found = own[player.key] = (slam, loc)
    return found


def _own_attack(player: Player, raw: bytes, elem: str, own: dict[int, tuple[bool, Any]]) -> str:
    slam, loc = _own_state(player, own)
    if slam:
        return "slam"
    if _flag(raw, "bWasSplashDamage"):
        return "slam" if elem == "normal" else "skill"
    if loc is None:
        return "skill"
    hx, hy, hz = _VEC(raw, S.off["hitlocation"])
    dx, dy, dz = hx - loc[0], hy - loc[1], hz - loc[2]
    return "melee" if dx * dx + dy * dy + dz * dz < MELEE_RANGE * MELEE_RANGE else "skill"


def _source(player: Player, raw: bytes, key: tuple[int, int] | None, dot: bool, elem: str,
            own: dict[int, tuple[bool, Any]]) -> str:  # fmt: skip
    if dot:
        return "dot:" + elem
    (causer,) = _U64(raw, S.causer_at)
    item = player.equipped.by_actor.get(causer)
    if item is not None:
        return item
    if not causer:
        return "env"
    if causer == player.pawn_addr:
        return _own_attack(player, raw, elem, own)
    kind, detail = S.causers.get(key, ("other", "?")) if key is not None else ("other", "?")
    if kind == "item":
        return player.equipped.by_actor.get(detail, "gear")  # type: ignore[arg-type]
    if kind == "grenade":
        return player.equipped.grenade or "grenade"
    if kind == "self":
        return _own_attack(player, raw, elem, own)
    if kind == "other":
        return f"other:{detail}"
    return kind


def _boss_engaged() -> bool:
    """A watched boss was hit during this fight (its fight goes on through pauses, e.g. immune phases)."""
    return any(boss.last and boss.last >= S.fight_start for boss in S.bosses.values())


def _count(raw: bytes, key: tuple[int, int] | None, minion: tuple[int, int] | None, now: float,
           own: dict[int, tuple[bool, Any]]) -> bool:  # fmt: skip
    player = S.by_pawn.get(_U64(raw, S.instigator_at)[0])
    if player is None and minion is not None:
        player = S.by_pawn.get(S.minions.get(minion, 0))
    if player is None:
        S.unknown += 1
        return False
    dmg = _F32(raw, S.off["damage"])[0]
    over = _F32(raw, S.off["overkilldamage"])[0]
    if over > 0:
        dmg = max(0.0, dmg - over)
    if not dmg > 0:  # also NaN
        return False
    elem, dot = S.types.get(raw[S.type_at : S.type_at + 8], ("normal", False))
    if minion is not None:
        src = "dot:" + elem if dot else "companion"
    else:
        src = _source(player, raw, key, dot, elem, own)
    crit = _flag(raw, "bWasCrit")
    kill = _flag(raw, "bCausedDeath")
    # A boss bar alone doesn't hold a fight open: in boss runs the next boss's bar is up before
    # the first shot, and every run piled into one fight
    if S.fight_closed or (now - S.fight_last > FIGHT_GAP_S and not _boss_engaged()):
        _new_fight(now)
    S.fight_last = S.session_last = now
    if not S.session_start:
        S.session_start = now
    bit = 0 if dot else ELEMENT_BIT.get(elem, 1)
    player.fight.add(src, dmg, crit, kill, bit)
    player.session.add(src, dmg, crit, kill, bit)
    if S.bosses:
        boss = S.bosses.get(_U64(raw, S.off["damagedactor"])[0])
        if boss is not None:
            boss.damage[player.key] = boss.damage.get(player.key, 0.0) + dmg
            boss.first = boss.first or now
            boss.last = now
    return True


def drain(now: float) -> None:
    """Counts the hits queued since the last tick, in one go (warm caches, one slam check per player)."""
    if not S.pending:
        return
    start = time.perf_counter()
    batch, S.pending = S.pending, []
    own: dict[int, tuple[bool, Any]] = {}
    counted = 0
    for raw, key, minion in batch:
        try:
            counted += _count(raw, key, minion, now, own)
        except Exception as ex:  # noqa: BLE001
            S.errors += 1
            if S.errors <= 5:
                logging.error(f"[Carnage Meter] {ex!r}")
    if counted:
        S.counted += counted
        S.revision += 1
    if len(S.causers) > MAX_CAUSERS:
        S.causers.clear()
    if len(S.minions) > MAX_CAUSERS:
        S.minions.clear()
    took = time.perf_counter() - start
    S.drains += 1
    S.drain_busy += took
    if took > S.drain_worst:
        S.drain_worst = took


def _new_fight(now: float) -> None:
    S.fight_start = now
    S.fight_no += 1
    S.fight_closed = False
    for player in S.players.values():
        player.fight = Tally()


def reset() -> None:
    """Starts both scopes over; players who have left the game go from the board."""
    S.fight_start = S.fight_last = S.session_start = S.session_last = 0.0
    S.fight_no += 1
    S.fight_closed = False
    for key in [k for k, p in S.players.items() if not p.pawn_addr]:
        del S.players[key]
    S.order = [k for k in S.order if k in S.players]
    for player in S.players.values():
        player.fight = Tally()
        player.session = Tally()
    for boss in S.bosses.values():
        boss.damage.clear()
        boss.first = boss.last = 0.0
    S.revision += 1


def game_session() -> tuple[int, int, int, int] | None:
    """
    The game's session id (`GbxGameState.GameSessionGuid`, replicated, so a client reads the
    host's), None when there's no game (title screen, loading).
    """
    world = ENGINE.GameViewport.World if ENGINE.GameViewport else None
    game_state = world.GameState if world is not None else None
    if game_state is None:
        return None
    try:
        guid = game_state.GameSessionGuid
    except AttributeError:
        return None
    found = (guid.A, guid.B, guid.C, guid.D)
    return found if any(found) else None


def new_session() -> None:
    """A new game session (back from the main menu, another character): a clean board."""
    unbind_all()
    S.players.clear()
    S.order = []
    S.report = None
    reset()


# endregion
# region Bosses


def _words(def_name: str) -> str:
    """"Name_OldThrash" -> "Old Thrash" (the fallback when the Kairosdex doesn't know a boss)."""
    text = def_name.removeprefix("Name_").replace("_", " ")
    out = ""
    for i, ch in enumerate(text):
        if i and ch.isupper() and text[i - 1].islower():
            out += " "
        out += ch
    return out.strip() or "Boss"


def _boss_identity(actor: Any) -> tuple[str, str]:
    """(name, picture URL or "") for a boss: the Kairosdex's name and baked portrait if it's loaded."""
    display = ""
    try:
        display = str(actor.UXNameRepData.DisplayDef._name)
    except (AttributeError, ValueError):
        pass
    dex_tracker = sys.modules.get("bl4_weapondex.tracker")
    dex_data = sys.modules.get("bl4_weapondex.data")
    if dex_tracker is not None and dex_data is not None:
        try:
            boss_id = dex_tracker.identify(actor)
            if boss_id:
                folder = GAME_DIR / "OakGame/Content/uiresources/bl4weapondex/bosses"
                picture = ""
                for sub in ("small/", ""):
                    if (folder / sub / f"{boss_id}.png").is_file():
                        picture = f"coui://uiresources/bl4weapondex/bosses/{sub}{boss_id}.png"
                        break
                return str(dex_data.boss_name(boss_id)), picture
        except Exception:  # noqa: BLE001 - the Kairosdex changed: use the fallback
            pass
    return _words(display), ""


def _is_dead(actor: Any) -> bool:
    try:
        return bool(actor.IsDead())
    except (AttributeError, ValueError, TypeError):
        return False


def _scan_bosses(now: float) -> None:
    """Every BOSS_SCAN_S: new boss bars on any player's screen, and watched bosses that died."""
    for player in S.players.values():
        pc = player.controller() if player.controller is not None else None
        if pc is None:
            continue
        try:
            visible = list(pc.BossState.VisibleBosses)
        except AttributeError:
            continue
        for actor in visible:
            if actor is None:
                continue
            address = actor._get_address()
            known = S.bosses.get(address)
            if known is not None and known.actor() is actor:
                continue
            if not _is_dead(actor):
                S.bosses[address] = Boss(address, actor)
    for address, boss in list(S.bosses.items()):
        actor = boss.actor()
        if actor is None:
            del S.bosses[address]  # despawned or left behind (no report: nobody saw it die)
        elif _is_dead(actor):
            del S.bosses[address]
            if boss.damage:
                _make_report(boss, now)


def _player_entry(player: Player, now: float) -> dict[str, Any]:
    info = naming.character(player.char)
    return {
        "id": player.key & 0xFFFFFFFF,
        "name": player.name or "Vault Hunter",
        "hero": info["hero"],
        "class": info["class"],
        "portrait": info.get("portrait", ""),
        "trait": info.get("trait", ""),
        "local": player.is_local,
        "here": player.pawn_addr != 0 or now - player.seen < 2.0,
    }


def _make_report(boss: Boss, now: float) -> None:
    seconds = max(1.0, boss.last - boss.first)
    fight_dur = max(0.0, S.fight_last - S.fight_start)
    players = []
    for key in S.order:
        player = S.players.get(key)
        if player is None or (player.fight.total <= 0 and key not in boss.damage):
            continue
        entry = _player_entry(player, now)
        tally = _tally(player, player.fight, fight_dur)
        tally["boss"] = round(boss.damage.get(key, 0.0))
        tally["boss_dps"] = round(boss.damage.get(key, 0.0) / seconds)
        entry["report"] = tally
        players.append(entry)
    S.report = {
        "id": next(S.report_ids),
        "boss": boss.name,
        "picture": boss.picture,
        "seconds": round(seconds, 1),
        "players": players,
        "fight_no": S.fight_no,
    }
    # The boss fight ends with the boss (unless another boss bar is still up)
    S.fight_closed = not S.bosses
    S.revision += 1
    logging.info(f"[Carnage Meter] {boss.name} defeated in {seconds:.0f} s: "
                 + ", ".join(f"{p['name']} {p['report']['boss']:,}" for p in players))  # fmt: skip


# endregion
# region Upkeep


def _player_states() -> list[Any]:
    world = ENGINE.GameViewport.World if ENGINE.GameViewport else None
    game_state = world.GameState if world is not None else None
    return list(game_state.PlayerArray) if game_state is not None else []


_causer_classes: dict[int, bool] = {}


def _has_causer_data(pawn: Any) -> bool:
    """A character with the caused-damage delegate (players are OakCharacters; vehicles aren't)."""
    key = pawn.Class._get_address()
    found = _causer_classes.get(key)
    if found is None:
        try:
            pawn.Class._find_prop(native.CAUSER_PROP)
            found = True
        except ValueError:
            found = False
        _causer_classes[key] = found
    return found


def tick(now: float) -> None:
    """Players, their characters (bind new ones) and equipped items every REFRESH_S; bosses more often."""
    if not S.ok:
        return
    # The hook's time since the last tick (about a frame): the worst frame for `carnage stats`
    spent, S.busy_seen = S.busy - S.busy_seen, S.busy
    if spent > S.frame_worst:
        S.frame_worst = spent
    drain(now)  # before the boss scan: a boss's last hits count before its report
    if now - S.boss_scanned >= BOSS_SCAN_S:
        S.boss_scanned = now
        try:
            _scan_bosses(now)
        except Exception as ex:  # noqa: BLE001
            logging.dev_warning(f"[Carnage Meter] bosses: {ex!r}")
    if now - S.refreshed < REFRESH_S:
        return
    S.refreshed = now
    local = get_pc(possibly_loading=True)
    local_ps = local.PlayerState if local is not None else None
    present: set[int] = set()
    S.by_pawn = {}
    states = _player_states()
    current = {ps._get_address() for ps in states}
    for ps in states:
        key = ps._get_address()
        present.add(key)
        player = S.players.get(key)
        if player is None:
            player = _returning(ps, current)
            if player is None:
                player = S.players[key] = Player(key)
                S.order.append(key)
            else:
                _rekey(player, key)
        player.seen = now
        player.is_local = local_ps is not None and key == local_ps._get_address()
        try:
            player.name = str(ps.GetPlayerName()) or player.name
            player.char = str(ps.ReplicatedCharacterDef) or player.char
            owner = ps.Owner  # the player's controller (on the host, for every player)
            if owner is not None and (player.controller is None or player.controller() is not owner):
                player.controller = WeakPointer(owner) if owner.Class._find_prop("BossState") else None
        except Exception:  # noqa: BLE001
            pass
        pawn = ps.PawnPrivate
        if pawn is None or not _has_causer_data(pawn):
            player.pawn, player.pawn_addr = None, 0
            continue
        address = pawn._get_address()
        if address != player.pawn_addr:
            if S.binder.bind(pawn):
                player.pawn, player.pawn_addr = WeakPointer(pawn), address
            else:
                player.pawn, player.pawn_addr = None, 0
                continue
        S.by_pawn[address] = player
        try:
            player.equipped.refresh(pawn, ps)
        except Exception as ex:  # noqa: BLE001
            logging.dev_warning(f"[Carnage Meter] equipped items: {ex!r}")
    # Players who left stay on the board for the session, without a character
    for key, player in S.players.items():
        if key not in present:
            player.pawn, player.pawn_addr, player.controller = None, 0, None
    direct = set(S.by_pawn)
    for player in S.players.values():
        direct.update(player.equipped.by_actor)
    S.direct = direct


def _returning(ps: Any, current: set[int]) -> Player | None:
    """
    The board's entry for a player whose PlayerState is new (map travel replaces it): one who is no
    longer in the game under their old one, with the same name.
    """
    try:
        name = str(ps.GetPlayerName())
    except Exception:  # noqa: BLE001
        return None
    if not name:
        return None
    return next((p for k, p in S.players.items() if k not in current and p.name == name), None)


def _rekey(player: Player, key: int) -> None:
    old = player.key
    del S.players[old]
    S.players[key] = player
    S.order = [key if k == old else k for k in S.order]
    for boss in S.bosses.values():
        if old in boss.damage:
            boss.damage[key] = boss.damage.pop(old)
    player.key = key
    player.pawn, player.pawn_addr = None, 0  # bound again below


def unbind_all() -> None:
    for player in S.players.values():
        pawn = player.pawn() if player.pawn is not None else None
        if pawn is not None:
            S.binder.unbind(pawn)
        player.pawn, player.pawn_addr = None, 0
    S.by_pawn = {}
    S.direct = set()
    S.pending = []
    S.bosses.clear()


def bound_count() -> int:
    return len(S.by_pawn)


# endregion
# region Board


def _label(player: Player, key: str) -> dict[str, Any]:
    if key.startswith("item:"):
        info = player.equipped.info.get(key)
        if info is None:
            _, type_index, comp_index = key.split(":")
            info = player.equipped.info[key] = naming.describe(int(type_index), int(comp_index))
        return {"name": info["name"], "kind": "item", "rarity": info["rarity"], "icon": info["icon"], "sub": info["kind"]}
    if key.startswith("dot:"):
        elem = key[4:]
        sub = f"{ELEMENT_NAMES.get(elem, elem.title())} damage over time"
        return {"name": DOT_NAMES.get(elem, elem.title()), "kind": "dot", "element": elem, "sub": sub}
    if key.startswith("other:"):
        return {"name": key[6:], "kind": "other", "sub": ""}
    fixed = {
        "slam": ("Ground Slam", "Slam"),
        "melee": ("Melee", "Melee"),
        "skill": ("Skills", "Abilities"),
        "companion": ("Companions", "Allies"),
        "vehicle": ("Vehicle", "Vehicle"),
        "env": ("Environment", "Hazards"),
        "grenade": ("Grenade", "Ordnance"),
        "gear": ("Gear", ""),
    }
    name, sub = fixed.get(key, (key.title(), ""))
    return {"name": name, "kind": key, "sub": sub}


GUN_ICONS = {"pistol", "smg", "assault", "shotgun", "sniper", "heavy", "turret"}
MIX = ("guns", "elemental", "skills", "melee", "gear", "other")


def _category(player: Player, key: str) -> str:
    if key.startswith("item:"):
        info = player.equipped.info.get(key) or _label(player, key)
        return "guns" if info.get("icon") in GUN_ICONS else "gear"
    if key.startswith("dot:"):
        return "elemental"
    if key in ("skill", "companion"):
        return "skills"
    if key in ("melee", "slam"):
        return "melee"
    if key in ("grenade", "gear"):
        return "gear"
    return "other"


def _tally(player: Player, tally: Tally, duration: float) -> dict[str, Any]:
    mix = dict.fromkeys(MIX, 0.0)
    for key, entry in tally.sources.items():
        mix[_category(player, key)] += entry[0]
    top = sorted(tally.sources.items(), key=lambda kv: -kv[1][0])[:TOP]
    sources = []
    for key, (dmg, hits, bits) in top:
        entry = _label(player, key)
        entry.update({"dmg": round(dmg), "hits": hits,
                      "elements": [e for e in ELEMENTS if bits & ELEMENT_BIT[e] and e != "normal"]})  # fmt: skip
        sources.append(entry)
    return {
        "total": round(tally.total),
        "crit": round(tally.crit),
        "hits": tally.hits,
        "kills": tally.kills,
        "big": round(tally.big),
        "dps": round(tally.total / max(duration, 1.0)),
        "top": sources,
        "mix": [[name, round(mix[name])] for name in MIX if mix[name] > 0],
    }


def board() -> dict[str, Any]:
    """Everything the screen shows (also what a host sends its friends)."""
    now = time.monotonic()
    fight_live = bool(S.fight_last) and not S.fight_closed and (now - S.fight_last <= FIGHT_GAP_S or _boss_engaged())
    fight_dur = max(0.0, S.fight_last - S.fight_start)
    session_dur = max(0.0, S.session_last - S.session_start)
    players = []
    for key in S.order:
        player = S.players.get(key)
        if player is None:
            continue
        entry = _player_entry(player, now)
        entry["fight"] = _tally(player, player.fight, fight_dur)
        entry["session"] = _tally(player, player.session, session_dur)
        players.append(entry)
    return {
        "rev": S.revision,
        "listening": S.ok and bool(S.by_pawn),
        "error": S.binder.error,
        "fight": {"live": fight_live, "no": S.fight_no, "seconds": round(fight_dur, 1), "ago": round(now - S.fight_last, 1) if S.fight_last else None,
                  "boss": next((b.name for b in S.bosses.values() if b.damage), None)},  # fmt: skip
        "session": {"seconds": round(session_dur, 1)},
        "players": players,
    }


def stats() -> str:
    avg = S.busy / S.events * 1e6 if S.events else 0.0
    per_hit = S.drain_busy / S.counted * 1e6 if S.counted else 0.0
    per_drain = S.drain_busy / S.drains * 1e3 if S.drains else 0.0
    return (f"listening on {bound_count()} character(s); {S.events} hits seen, {S.counted} counted,"
            f" {S.unknown} from unknown instigators, {S.errors} errors; hook {avg:.1f} us per hit,"
            f" worst {S.worst * 1e3:.2f} ms; counting {per_hit:.1f} us per hit, {per_drain:.3f} ms per tick,"
            f" worst {S.drain_worst * 1e3:.2f} ms; worst frame in the hook {S.frame_worst * 1e3:.2f} ms;"
            f" {len(S.causers)} causers cached; {len(S.bosses)} boss(es) watched")  # fmt: skip


# endregion
