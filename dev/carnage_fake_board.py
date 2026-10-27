# Dev only: fills the Carnage Meter with three made-up co-op players next to the real one, to judge
# the screen's layout without a co-op session. `carnage reset` (or the Reset button) clears it.
#   python tools/bridge.py <id> -f dev/carnage_fake_board.py
import random
import time

import bl4_damage_meter.tracker as T

FAKES = [
    (0x10001, "Moxxi's Favorite", "Char_DarkSiren", [("item:13:73", 2_400_000, 1), ("dot:fire", 1_150_000, 0), ("skill", 900_000, 0), ("item:321:7", 120_000, 0)]),
    (0x10002, "TorgueFan42", "Char_ExoSoldier", [("item:6:57", 3_900_000, 1), ("grenade", 1_600_000, 0), ("slam", 400_000, 0), ("melee", 210_000, 0), ("dot:shock", 90_000, 0)]),
    (0x10003, "ClaptrapEnjoyer", "Char_Gravitar", [("item:20:65", 820_000, 4), ("dot:corrosive", 640_000, 0), ("skill", 410_000, 0)]),
]

now = time.monotonic()
for key, name, char, sources in FAKES:
    p = T.S.players.get(key) or T.Player(key)
    T.S.players[key] = p
    if key not in T.S.order:
        T.S.order.append(key)
    p.name, p.char, p.seen = name, char, now + 3600
    for tally in (p.fight, p.session):
        for src, dmg, elem in sources:
            hits = random.randint(20, 300)
            for _ in range(3):
                tally.add(src, dmg / 3, random.random() < 0.3, random.random() < 0.1, elem)
            tally.sources[src][1] = hits
        tally.kills = random.randint(4, 30)
real = next((p for p in T.S.players.values() if p.is_local), None)
if real is not None and real.fight.total == 0:
    for tally in (real.fight, real.session):
        for src, dmg, elem in [("item:10:61", 3_100_000, 4), ("dot:normal", 1_900_000, 0), ("item:13:73", 950_000, 2), ("melee", 300_000, 0)]:
            tally.add(src, dmg, False, True, elem)
T.S.fight_start, T.S.fight_last = now - 83, now
T.S.session_start, T.S.session_last = now - 1460, now
T.S.revision += 1
print("fake board:", [(p.name, round(p.fight.total)) for p in T.S.players.values()])
