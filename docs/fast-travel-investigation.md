# Co-op fast travel: why players got stuck in the portal

Investigated 2026-10-23 on the game build of 2026-10-22 (`Borderlands4.exe`, 647,338,264 bytes).
Offsets and RVAs below are for that build only.

## Short answer

There were two separate problems.

1. **The friend got dropped (a real bug, fixed in mod 0.4.2).** When anyone requests a fast
   travel, the host checks every player's native "client ready" flag (`PlayerController+0x32B9`).
   A player whose flag is unset is removed from the session on the spot, and the host then waits
   in the portal for a player that no longer exists. The game only sets that flag after it has
   verified the player's entitlements through the online layer. That never happens for a friend
   connected by direct IP. This was confirmed even with the shared-identity artifact below
   neutralized. 0.4.2 calls the game's own setter for friends who have already arrived, on
   `RequestTeleportToStation` / `RequestTeleportToPlayer` (PRE).

2. **The host never finished arriving (a test-setup artifact, not a mod bug).** Testing used two
   copies of the game on **one** Steam/SHiFT account. The host's online layer keeps a "local user
   0" slot that points at the host's own PlayerController. When a client on the *same* account
   joins, the game re-binds that slot to the client's controller. From then on the host can't find
   its own player, its arrival readiness check fails, and it sits in the portal forever.
   - Re-pointing the slot at the host's controller released the stuck host immediately.
   - With the slot kept correct, an in-map co-op teleport finished in 5 s and a cross-map
     co-op travel (World_P to Harmonica_P, both players) finished in about 14 s.
   - Friends on their own accounts never hit this. `dev/re/same_account_guard.py` works around it
     for one-account testing. Validated: with the guard running on the host, a cross-map co-op
     travel (Harmonica_P to World_P) finished for both players with no manual help.
   - The profile.sav wipes seen during one-account testing are a separate problem. Both copies
     write the same save folder: the host writes the full profile (~81 KB), the client writes a
     ~9 KB one, and the last write wins. The guard doesn't change that. Back up saves and restore
     after testing.

## How a fast travel completes (server side)

Each World owns a native teleport manager: a tickable whose vtable is at RVA `0xB968FC0` and
whose `Tick` is at `0x423229A`. It isn't reflected, so find it by scanning the heap for that
vtable pointer (`dev/re/heapscan.py`). It keeps one 0x98-byte record per traveling player at
`this+0x210` (TArray, count at `+0x218`):

| Record field | Meaning (observed) |
|---|---|
| `+0x10` | weak pointer {index, serial} to the player's controller |
| `+0x18` | destination station handle (FGameDataHandle, type 24576) |
| `+0x80`, `+0x84` | timestamp and minimum duration (2.5 s) |
| `+0x88` | phase |
| `+0x8C` | goes 1 to 0 about 1 s in |
| `+0x93` | 1 once the destination is ready |

The record is created on request. When `+0x93 == 1`, the tick sends the completion notice
(`ClientNotifyTeleporting` with a `None` station) and removes the record once the player has
confirmed (`controller+0x23BB == 1`).

**Group barrier:** with two or more records, nobody is released until **every** traveler has
`+0x93 == 1` and `controller+0x23BB == 1`. A "hold everyone" flag is also set while any
GbxLevelSequenceManager sequence is active (`manager+0x518`, entry `+0x39`).

## How a local player finishes the teleport screen

On the host's own controller:

| Field | Meaning |
|---|---|
| `+0x23B8` | "teleport screen active". Set by `0x4200B2A` at travel start, cleared by `0x1427FE4`. |
| `+0x23BB` | "ready for teleport". Set by `0xE1A75C`. |
| `+0x3250` / `+0x3251` | client-side "teleport in progress" / "waiting on ready flag" |

Each frame `0xE19A28` runs while `+0x23B8` is set. After a minimum screen time it asks
`0xE19C60(pc)` whether the player can finish. For the authority's own player that check calls
`0x10D53F0(GameInstance)` and then looks the controller up in the online user subsystem
(`0x47210F0` → `0xF6F32` → `+0x68`, then `0xD27DF6(sub, &handle, pc)`, which matches by
controller pointer). It needs status `0xB` from `0x2D8968`.

On one account, that lookup returned **-1 for the host's own controller** and **slot 0 / status
0xB for the client's controller**. The subsystem's user list had a single entry, and it pointed
at the client.

## How the "client ready" flag gets set

- The setter is `0x41EDCBC`. It sets `+0x32B9`, registers the player with the online user
  subsystem, applies checkpoint stations, and, on clients, retries a pending arrival
  (`0x14DC7B2`, which runs only for NetMode == Client).
- The setter's only caller is `0x4109BEE` (an entitlement refresh). That is reached only through
  a callback object (thunk `0x4116908`, vtable `0xB968890`) owned by the online entitlement
  service.
- A joining client does send `ServerRefreshPlayerEntitlementFacts`. The host stores it
  (`0x149A73` → `0xED30B4(sub, pc)` → vtable +0x68), but that path never marks the player
  ready. Observed: the flag stayed 0 even with the host's identity kept correct.

## Method / tools (`dev/re/`)

| Tool | What it does |
|---|---|
| `ext_dump.py PID OUTDIR` | Dumps the running game's decrypted image from outside the process. The exe on disk is protected. |
| `re_tool.py` | capstone helpers over the dump: `.pdata` function bounds, disassembly, displacement and call-site search |
| `heapscan.py PID VALUE` | Finds heap objects by vtable pointer |
| `gate.py`, `gichk.py`, `online_chk.py` | Read flags and call the game's own getters live (exec through the bridge) |
| `recorder.py`, `recrec.py`, `stacks.py` | Per-frame change logs of the flags and teleport records, plus native call stacks captured inside UFunction hooks (`RtlCaptureStackBackTrace`, game-module frames only) |
| `same_account_guard.py` | The dev-only fix for one-account testing |

Useful technique: a native stack captured inside a pyunrealsdk hook shows exactly which native
function sent an RPC. That's how the completion notice was traced to the teleport manager tick.
