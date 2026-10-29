# BL4 Direct: co-op latency and smoothness

Investigation into whether BL4 Direct's co-op (game net driver swapped to `SteamNetDriver`, plain UDP, listen server) can be made less laggy. Game build `26007962`, UE 5.5.4. Values were read on 2026-10-23 from a running instance (bridge `A`) that was hosting `VaultoftheDamned_P` with `?listen?bIsLanMatch?port=7777` and **no client connected**. The area was quiet, with no enemies. Nothing was changed in the game.

Confidence labels used below:
- **[read]**: value read from the live game or a CDO through the bridge.
- **[exe]**: string found in `Borderlands4.exe`. The UTF-16 strings were extracted with a small scanner. Cvar help texts sit next to the cvar names.
- **[UE]**: inferred from stock Unreal Engine source behaviour. Gearbox may have changed it.
- **[community]**: from web reports.
- **[unverified]**: a guess that still needs a test.

---

## TL;DR

1. **`NetServerMaxTickRate = 30` is almost certainly *not* limiting us.** In stock UE that value only caps the frame rate of dedicated servers, or of listen servers when `bClampListenServerTickRate` is true. Here it is false [read]. A listen server replicates once per host frame, gated by each actor's `NetUpdateFrequency`; the player pawn's is 100 Hz [read]. **The host's frame rate is the replication rate,** and a host hitch is a lag spike for every client. Test 1 confirms this cheaply.
2. **Gearbox caps server replication time:** `gbx.net.ReplicationBudgetSeconds = 0.0025` (2.5 ms per tick), with `gbx.net.BudgetSkippedPriorityBoost = 100` [read, exe]. Generic replication is in use: Iris is off and there is no ReplicationGraph [read]. In big fights, a 2.5 ms budget plausibly skips actor updates, which would show up as choppy or teleporting enemies. This is the most promising knob. Its meaning is inferred from the name; the exe contains no help text for it.
3. **`?bIsLanMatch` matters and must stay.** Without it, stock `AGameNetworkManager::UpdateNetSpeeds` throttles every client of a listen server to `clamp(TotalNetBandwidth / players, 4000, 7000)` bytes/s. BL4's values are `TotalNetBandwidth=32000`, `MaxDynamicBandwidth=7000` [read], which is about 56 kbit/s per client. The live manager shows `AdjustedNetSpeed=7000` and `LastNetSpeedUpdateTime=0.0`, meaning it has never run, which is consistent with the LAN flag suppressing it [read]. Verify this with a client connected (Test 0).
4. **SHiFT and Steam drivers are tuned identically.** Both inherit the same IpNetDriver config: rates 262144, tick 30/120, buffers 131072/32768. Only `ConnectionTimeout` differs (Shift 30, Steam 60) [read]. Our path loses nothing compared with the stock SHiFT co-op tuning. SteamNetDriver with an IP address is IpNetDriver code (`SteamNetDriver → IpNetDriver → NetDriver`) [read], so it adds no overhead [UE].
5. Secondary knobs:
   - Movement-correction tolerance on the host (`GameNetworkManager.MAXPOSITIONERRORSQUARED=3`, `ClientAuthorativePosition=False`), for rubber-banding of the client's own character.
   - `Engine.NetClientTicksPerSecond=200`, which only matters with 3–4 players or a host running above 200 fps.
   - The network path itself: VPN relays such as Tailscale DERP add a lot of latency.

---

## 1. What BL4 uses today

### 1.1 Net driver: live SteamNetDriver vs class defaults

Read with a script that walks `cls._properties()` and `getattr`s each one on:
- the live driver: `world().NetDriver`, which is `SteamNetDriver_2147477967`, a listen server with 0 clients;
- the `ClassDefaultObject` of `SteamNetDriver`, `ShiftNetDriver`, `IpNetDriver` and `NetDriver`.

| Property | Live Steam | Steam CDO | Shift CDO | IpNetDriver CDO | NetDriver CDO |
|---|---|---|---|---|---|
| `NetServerMaxTickRate` | 30 | 30 | 30 | 30 | 0 |
| `MaxNetTickRate` | 120 | 120 | 120 | 120 | 0 |
| `bClampListenServerTickRate` | False | False | False | False | False |
| `MaxClientRate` | 262144 | 262144 | 262144 | 262144 | 15000 |
| `MaxInternetClientRate` | 262144 | 262144 | 262144 | 262144 | 10000 |
| `ServerDesiredSocketReceive/SendBufferBytes` | 131072 | 131072 | 131072 | 131072 | n/a |
| `ClientDesiredSocketReceive/SendBufferBytes` | 32768 | 32768 | 32768 | 32768 | n/a |
| `KeepAliveTime` | 0.2 | 0.2 | 0.2 | 0.2 | 0 |
| `InitialConnectTimeout` | 30 | 30 | 30 | 30 | 0 |
| **`ConnectionTimeout`** | **60** | **60** | **30** | **60** | 0 |
| `RelevantTimeout` | 2.0 | 2.0 | 2.0 | 2.0 | 0 |
| `NetConnectionClassName` | SteamNetConnection | SteamNetConnection | ShiftNetConnection | IpConnection | (none) |
| `ReplicationDriverClassName` | '' | '' | '' | '' | '' |
| `ReplicationBridgeClassName` | '' | '' | '' | '' | '' |
| `ReplicationSystemConfigServer/Client` | all zeros | all zeros | all zeros | all zeros | all zeros |
| `bIsPassthrough` / `LagThresholdSeconds` | n/a | n/a | False / 5.0 | n/a | n/a |

Notes:
- The task brief said `ConnectionTimeout = 30` on the Steam driver. I read **60** on both the live SteamNetDriver and its CDO. 30 is the ShiftNetDriver value. It doesn't affect latency.
- No ReplicationGraph is in use (`ReplicationDriverClassName=''`, `ReplicationDriver=None`) [read].
- Inheritance chains [read]:
  - `SteamNetDriver → IpNetDriver → NetDriver`
  - `ShiftNetDriver → IpNetDriver → NetDriver`
  - `ShiftNetConnection → IpConnection → NetConnection`

### 1.2 Connection and player net speed

These come from the CDOs of `SteamNetConnection`, `ShiftNetConnection`, `IpConnection`, `NetConnection`, `Player`, `LocalPlayer` and `OakLocalPlayer`, and from the live `OakLocalPlayer`.

| Property | Value | Notes |
|---|---|---|
| `ConfiguredInternetSpeed` | 1048576 | Same on all of the above [read] |
| `ConfiguredLanSpeed` | 1048576 | Same, so `?LAN` in a URL doesn't change the client's requested speed [read] |
| `DefaultMaxChannelSize` | 32767 | [read] |
| `CurrentNetSpeed` | 0 | Live local player on the host; it has no connection of its own [read] |

The client asks for 1048576 bytes/s, and the server clamps that to `MaxClientRate = 262144` (about 2 Mbit/s per client) [UE]. The quiet-area traffic was 2–4 KB/s, about 1% of the cap. **Bandwidth caps are not the bottleneck unless combat traffic is about 100× higher.** Test 0 measures this.

### 1.3 Replication system and cvars

All read with `KismetSystemLibrary.GetConsoleVariableInt/Float/StringValue` [read].

**Replication system**

| Cvar | Value | Meaning |
|---|---|---|
| `net.Iris.UseIrisReplication` | 0 | **Iris is off.** Generic (legacy) replication; the Iris cvars exist but don't apply. |
| `net.IsPushModelEnabled` | 1 | Push model on |
| `net.Iris.PushModelMode` | 2 | Push model on |
| `net.UseAdaptiveNetUpdateFrequency` | 0 | Actors update at their full `NetUpdateFrequency` |
| `net.DormancyEnable` | 1 | |
| `net.DisableRandomNetUpdateDelay` | 0 | |

**Gearbox replication budget**

| Cvar | Value | Meaning |
|---|---|---|
| **`gbx.net.ReplicationBudgetSeconds`** | **0.0025** | Gearbox-specific. Help text not found in the exe [exe]. |
| `gbx.net.BudgetSkippedPriorityBoost` | 100 | Gearbox-specific |

**Transport**

| Cvar | Value | Meaning |
|---|---|---|
| `net.EnableCongestionControl` | 0 | |
| `net.OodleClientEnableMode` / `net.OodleServerEnableMode` | '' | No Oodle packet compression |
| `net.AllowEncryption` | 1 | Encryption is only required when this is 2 [UE]. |
| `net.RequiredEncryptionNetDriverDefNames` | 'all' | Only enforced when `net.AllowEncryption` is 2. |
| `net.IpNetDriverUseReceiveThread` | 0 | Packets are received on the game thread |
| `net.UseRecvMulti` | 0 | |
| `net.DoPacketOrderCorrection` | 1 | |
| `net.PacketOrderMaxCachedPackets` | 32 | |
| `net.PingExcludeFrameTime` | 0 | Reported ping includes the remote's frame time [UE]. This explains "localhost ping ≈ frame time". |
| `net.MaxRPCPerNetUpdate` | 2 | |
| `net.ProcessQueuedBunchesMillisecondLimit` | 30 | |

**Character movement**

| Cvar | Value |
|---|---|
| `p.NetUsePackedMovementRPCs` | 1 |
| `p.NetEnableMoveCombining` | 1 |
| `p.NetEnableListenServerSmoothing` | 1 |
| `p.NetPingExtrapolation` | -1 (engine default) |
| `p.NetPingLimit` | -1 (engine default) |
| `p.NetServerMaxMoveDeltaTimeScalar` | 1.75 |
| `p.NetUseClientTimestampForReplicatedTransform` | 1 |

**Gearbox AI navigation replication**

All four have help texts that read "Once a new X is sent to clients, it will not send another X for this long (in seconds)" [exe].

| Cvar | Value |
|---|---|
| `gbx.nav.net.PathPeriod` | 0.25 |
| `gbx.nav.net.TransformPeriod` | 0.25 |
| `gbx.nav.net.DesiredYawPeriod` | 0.25 |
| `gbx.nav.net.AckPeriod` | 0.25 |

**Gearbox lag compensation**

| Cvar | Value | Meaning |
|---|---|---|
| `gbx.LagComp.ProjectileScalingEnabled` | 1 | Hit help for laggy clients is already on |
| `gbx.LightProjectile.EnableLagCompMagnetism` | 1 | Hit help for laggy clients is already on |

**Frame rate**

| Setting | Value |
|---|---|
| `t.MaxFPS` | 0 |
| `r.VSync` | 0 |
| `GameUserSettings.ini` | `LimitFrameRate=False`, `FrameRateLimit=60` |

On the host, `GameplayStatics.GetWorldDeltaSeconds` was 0.0095 s, about 105 fps.

### 1.4 Engine, GameNetworkManager and actors

**Engine (`OakGameEngine`)** [read]

| Property | Value |
|---|---|
| `NetClientTicksPerSecond` | 200 |
| `bSmoothFrameRate` | False |
| `bUseFixedFrameRate` | False |
| `MinDesiredFrameRate` | 35 |

**`GameNetworkManager`** (live: `world().NetworkManager`) [read]

| Group | Values |
|---|---|
| Dynamic bandwidth | `TotalNetBandwidth=32000`, `MinDynamicBandwidth=4000`, `MaxDynamicBandwidth=7000`, `AdjustedNetSpeed=7000`, `LastNetSpeedUpdateTime=0.0` |
| Position correction | `MAXPOSITIONERRORSQUARED=3.0`, `ClientAuthorativePosition=False`, `MAXCLIENTUPDATEINTERVAL=0.25`, `MaxMoveDeltaTime=0.125` |
| Client move sending | `ClientNetSendMoveDeltaTime=0.0166` (the client sends moves at 60 Hz), `ClientNetSendMoveThrottleAtNetSpeed=10000` |
| Hitch handling | `ServerForcedUpdateHitchThreshold=0.15` |
| Relevancy | `bUseDistanceBasedRelevancy=True` |

**Game session** [read]: `OakGameSession`. `bIsLanMatch` isn't a UPROPERTY so it can't be read directly, but the game mode's `OptionsString` is `?listen?bIsLanMatch?port=7777?travelstation=...`.

**Actor update rates.** Live, quiet area, 59 replicated actors in total [read].

| Actor | `NetUpdateFrequency` | `MinNetUpdateFrequency` | Other |
|---|---|---|---|
| `OakCharacter` (player pawn) | 100 | 2 | `NetCullDistanceSquared=4e8` (200 m) |
| `OakPlayerController` | 100 | 2 | |
| `OakPlayerState` | 1 | 2 | |
| `OakGameState` | 10 | 2 | |
| `LightProjectileManager` | 30 | 30 | Always relevant; bullets and tracers go through managers |
| `LightBeamManager` | 30 | 30 | Always relevant |
| Most interactables | 100 | 2 | |
| `OakWeapon` | 1 | 1 | Dormant |
| `GrapplePoint` | 1 | 1 | Dormant |

No enemies were present, so enemy rates are unread. Enemies are presumably `OakCharacter`; the pawn chain is `OakCharacter → GbxCharacter → GbxAICharacter → GbxNavCharacter → … → Character`.

**`OakCharacterMovementComponent`** [read]. All are engine defaults.

| Property | Value |
|---|---|
| `NetworkSmoothingMode` | Exponential |
| `NetworkSimulatedSmoothLocationTime` | 0.1 |
| `ListenServerNetworkSimulatedSmoothLocationTime` | 0.04 |
| `NetworkMaxSmoothUpdateDistance` | 256 |
| `NetworkNoSmoothUpdateDistance` | 384 |
| `NetworkMinTimeBetweenClientAdjustments` | 0.1 |
| `bServerAcceptClientAuthoritativePosition` | False |

### 1.5 How a listen server actually paces replication [UE, partly exe]

- **Frame rate.** `UGameEngine::GetMaxTickRate` only applies `NetServerMaxTickRate` for dedicated servers, or for listen servers when `bClampListenServerTickRate` is true. Here it's false, so the host runs at its normal frame rate.
- **Once per frame.** `UNetDriver::TickFlush → ServerReplicateActors` runs every host frame. Each actor is reconsidered when `1/NetUpdateFrequency` has elapsed, and the pawn's frequency is 100 Hz.
- **`MaxNetTickRate=120`.** It caps the replication rate when the frame rate is higher (UE docs: "Limit tick rate of replication to allow very high frame rates to still replicate data"). It only matters when the host runs above 120 fps.
- **Client throttle (listen server only).** `ServerReplicateActors_PrepConnections` ticks at most `trunc(NetClientTicksPerSecond × DeltaSeconds)` clients per frame, rotating through them. The exe contains the strings `limitclientticks` and `lanplay` from this exact function [exe], so the code is present. With 200:
  - At 105 fps that's 2 clients per frame, so a 2-player game is unaffected.
  - At 144 fps with 3 clients, it's 1 client per frame, so each client gets about 48 Hz.
  - Above 200 fps, frames alternate between 0 and 1 clients.
- **Ping.** UE ping is measured from acks, which the remote only sends at its own tick, and `net.PingExcludeFrameTime=0`. The reported ping therefore always includes about one remote frame.

**Conclusion:** the replication rate seen by clients is roughly min(host fps, 120, actor `NetUpdateFrequency`). The latency is roughly network RTT plus about one frame on each side. **Host frame time, and especially host hitches, is the dominant game-side factor,** not the 30 Hz setting. Test 1 confirms this with a packet count.

---

## 2. Ranked changes

Most changes take effect on the **host only**, which is the side that replicates. "Runtime" means Python run on the host's game thread after the map has loaded with `?listen`, for example from the mod's tick after it detects a new PlayerController.

### #1: Host frame-rate stability (settings and advice, no code)

- **What:** have the host cap fps to a value it can hold in combat, using the in-game limiter (`LimitFrameRate=True`, `FrameRateLimit=60` or `90`) or `t.MaxFPS`. The host should also lower CPU-heavy settings. Players should avoid hosting from the weaker PC.
- **Why:** replication, packet sends and acks all happen once per host frame on the game thread (§1.5). A 100 ms host hitch means clients get nothing for 100 ms, then a burst followed by corrections. That is exactly "big issues sometimes". BL4's PC performance is widely reported as uneven [community] ([Top Gear review](https://www.topgear.com/car-news/gaming/borderlands-4-review-brilliant-co-op-action-runs-a-less-brilliant-pace), [allthings.how](https://allthings.how/borderlands-4-pc-practical-fixes-for-stutter-and-low-fps/)).
- **Where:** in-game options, or `Engine.ini` / runtime `t.MaxFPS`.
- **Risk:** none.
- **Confidence:** the mechanism is [UE] plus [read] (no listen-server clamp, ping ≈ frame time). The size of the effect is [unverified].
- **Do not** set `bClampListenServerTickRate=True`. That would cap the host's *whole game* at `NetServerMaxTickRate=30` fps.

### #2: Raise Gearbox's replication time budget

- **What:** `gbx.net.ReplicationBudgetSeconds` from `0.0025` to `0.005`, or up to `0.008` if the host has CPU headroom.
- **Where:**
  - Runtime on the host: `ExecuteConsoleCommand(pc, "gbx.net.ReplicationBudgetSeconds 0.005", pc)`, then read it back with `GetConsoleVariableFloatValue` to confirm it took. Shipping builds can reject cheat-flagged cvars; this is [unverified].
  - Or the host's `Engine.ini`:
    ```ini
    [SystemSettings]
    gbx.net.ReplicationBudgetSeconds=0.005
    ```
    Community UE tweak packs commonly use `[SystemSettings]` for `r.*` cvars [community]. It is [unverified] for `gbx.*`.
- **Expected effect:** in heavy fights with many enemies, projectiles, loot and effects, fewer actors are skipped per replication pass. Clients would see smoother enemy movement and fewer delayed hits or loot pops. There is no effect in quiet areas.
- **Cost and risk:**
  - The host's game thread can spend up to the new budget per tick on replication: +2.5–5.5 ms, but only when it was previously over budget.
  - At 60 fps (16.7 ms frames) that's significant. If the host is already CPU-bound it may lower host fps, which hurts #1.
  - Trivially reversible.
- **Confidence:** the value is [read] and the name is [exe]. The semantics are inferred from the name and the companion `BudgetSkippedPriorityBoost`; no help text was found. The effect is [unverified]. This is the best candidate for "enemies stutter in big fights".

### #3: Confirm and keep `?bIsLanMatch` (already done; verify only)

- **What:** nothing to change. Verify that the client's connection on the host has `CurrentNetSpeed ≈ 262144`, not ≤ 7000.
  - Host: `world().NetDriver.ClientConnections[0].CurrentNetSpeed`.
  - Client: `world().NetDriver.ServerConnection.CurrentNetSpeed`.
- **Why:** stock `AGameNetworkManager::UpdateNetSpeeds` skips throttling only for LAN matches, which `AGameSession` reads from `?bIsLanMatch`. Otherwise it caps each client at `clamp(32000 / players, 4000, 7000)` B/s, which would starve combat traffic.
- **Fallback:** if a future change drops `?bIsLanMatch`, or BL4's `OakGameSession` ignores it, set this at runtime on the host:
  ```
  world().NetworkManager.TotalNetBandwidth = 1048576
  world().NetworkManager.MinDynamicBandwidth = 262144
  world().NetworkManager.MaxDynamicBandwidth = 262144
  ```
  The same keys can go in `Engine.ini` under `[/Script/Engine.GameNetworkManager]`.
- **Confidence:** the values are [read]. The throttle logic is [UE]. `LastNetSpeedUpdateTime=0` is consistent with the throttle being skipped, but that was observed with 0 clients.

### #4: Looser movement-correction tolerance (client rubber-banding)

- **What:** on the host's live `world().NetworkManager`:
  - `MAXPOSITIONERRORSQUARED`: 3.0 → 25–100 (about 5–10 cm of allowed error).
  - Optionally `ClientAuthorativePosition = True`, so the server adopts the client's position when the error is within tolerance instead of correcting it.
- **Where:**
  - Runtime on the host. Reapply after every map load, because the manager is a per-world actor.
  - Or `Engine.ini` `[/Script/Engine.GameNetworkManager]` with `MAXPOSITIONERRORSQUARED=25` and `ClientAuthorativePosition=True`. Whether the user ini reaches this class's defaults is [unverified].
- **Expected effect:** fewer server corrections ("snap back") for the *client's own* character under jitter or small packet loss. It does nothing for enemy smoothness.
- **Risk:** low between friends. Clients get slightly more authority, so a hacked client could teleport a little. BL4's `OakCharacterMovementComponent` may override `ServerCheckClientError`, in which case there is no effect.
- **Confidence:** [UE]; Gearbox overrides are [unverified].

### #5: `NetClientTicksPerSecond` for 3–4 player sessions

- **What:** `ENGINE.NetClientTicksPerSecond` from 200 to 1000 (runtime on the host; it's a UPROPERTY read every frame). Alternatively, add `-lanplay` to the host's launch options, which doubles the rate.
- **Expected effect:** every client is updated every host frame. It only matters with 2+ clients and a host above about 100 fps, or 1 client with the host above 200 fps. In 2-player co-op at normal fps there's no change.
- **Risk:** low. Replication CPU per frame rises proportionally to the clients actually served; compare #2.
- **Confidence:** value [read], code path [UE] plus [exe] strings.

### #6: Network path (not a game setting)

- If friends connect over Tailscale, ZeroTier or Hamachi, check that the path is direct and not relayed. For Tailscale, `tailscale ping <host>` says `via DERP(...)` when relayed, which adds tens to hundreds of ms and jitter. Prefer port forwarding UDP 7777 or a direct VPN path.
- Wi-Fi on the host adds jitter for everyone; wired is preferable.
- **Confidence:** general networking knowledge. A possible mod/app feature is showing RTT and a "relay detected" hint; this is not designed yet.

### #7: Experimental, low confidence

These come from Gearbox-specific cvars; their visible effect is [unverified].
- `gbx.nav.net.TransformPeriod` / `PathPeriod` / `DesiredYawPeriod` from 0.25 to 0.1 (host). AI navigation state is sent at most 4 Hz and clients follow paths locally. Lowering the periods might reduce AI position drift, at a bandwidth cost.
- `LightProjectileManager.NetUpdateFrequency` from 30 to 60 on the host's live managers. This may smooth other players' bullets and tracers.

### Not worth doing

| Change | Why not |
|---|---|
| Changing `NetServerMaxTickRate` | No effect on a listen server without the clamp (§1.5). |
| `bClampListenServerTickRate=True` | Harmful: it caps the host's whole game at 30 fps. |
| Raising `MaxClientRate` / `MaxInternetClientRate` | 262144 B/s is far above the measured 2–4 KB/s. Revisit only if Test 0 shows combat traffic near 200 KB/s per client. |
| Iris | Off, and the replication system configs are all zero. Enabling it needs engine and project setup, not a runtime toggle. |
| Oodle compression | Saves bandwidth, not latency, and needs a packet handler component and dictionaries. |
| `net.IpNetDriverUseReceiveThread` | Received packets are still processed at the next tick, so latency doesn't change. |
| Switching to plain IpNetDriver | Already the same code path; it also fails to init in BL4 (guide §6). |
| Socket buffer sizes | No sign of drops at these data rates. |

---

## 3. Test plan (two-instance harness)

### Setup common to all tests

1. **Instances.** Instance `A` hosts, instance `B` joins, with the same build and mod. Note that both instances share one CPU, so host fps is lower than on a real setup. Compare A/B within the same run, never across machines.
2. **Relay.** Put the logging relay between them. Start `python tools/udp_proxy.py 7778 127.0.0.1 7777 <scratch>/proxy.log` and have B run `bl4join 127.0.0.1:7778`.
3. **Delay and loss.** For the jitter and loss variants, make a *scratchpad copy* of the proxy (don't edit `tools/`). It should delay each forwarded packet by `base ± jitter` ms, for example 40 ± 15, and drop p% of packets, for example 1–2%, using a heap of `(send_time, data, dest)` flushed in the select loop.
4. **Per-second summary of the proxy log.** Columns 2 and 3 are the direction and size:
   ```sh
   awk '{split($1,t,"."); k=t[1]" "$2; n[k]++; b[k]+=$3} END{for(k in n) print k, n[k]" pkt/s", b[k]" B/s"}' proxy.log | sort
   ```
5. **Sampler.** A bridge sampler on each instance, run via `--thread`-less code. It registers a tick hook on `CameraModifier:BlueprintModifyCamera` POST, throttled to one sample per frame, and appends to a list. After 30 s, dump:
   - **Host (A):**
     - `GameplayStatics.GetWorldDeltaSeconds(world())`, giving the fps and hitch histogram;
     - `world().NetDriver.ClientConnections[0].CurrentNetSpeed`;
     - the client PlayerState's `GetPingInMilliseconds()`;
     - the client pawn's `CharacterMovement.ServerLastClientAdjustmentTime`. Each change is one correction sent to the client.
   - **Client (B):**
     - `GetWorldDeltaSeconds`;
     - its own `PlayerState.GetPingInMilliseconds()`;
     - for enemy smoothness, the number of distinct `ReplicatedMovement.Location` values per second on nearby enemy `OakCharacter` actors. This is the replication rate actually received. If Gbx nav characters don't use `ReplicatedMovement`, use the actor location change rate instead.
6. **Scenarios.**
   - **(Q) Quiet:** host walks in circles for 30 s.
   - **(C) Combat:** both players in a populated enemy area with a fixed route or spawn, 60 s.
   - Repeat each arm 3 times.

### Test 0: Baseline and the `bIsLanMatch` check (do first, about 10 min)

- **Measure:**
  - S→C and C→S pkt/s and B/s in Q and C;
  - host fps and hitches (frames > 50 ms);
  - client ping;
  - `CurrentNetSpeed` on both ends.
- **Pass:**
  - `CurrentNetSpeed` is about 262144 on the host's client connection. If it's ≤ 7000, apply the #3 fallback immediately: that would be a real bug.
  - Combat B/s is recorded. If it's anywhere near 100–250 KB/s, revisit the bandwidth caps.

### Test 1: Is replication paced by host fps, not by 30 Hz? (supports #1)

1. In Q with the host moving, record S→C pkt/s alongside host fps.
2. Cap the host to 30, then 60, then uncapped (`t.MaxFPS 30` / `60` / `0` via `ExecuteConsoleCommand` on A).
3. In a separate run, set `world().NetDriver.NetServerMaxTickRate = 120` on the host while uncapped.
- **Expect:** S→C pkt/s tracks host fps (about 30 / 60 / ~100 or more), and changing `NetServerMaxTickRate` makes no difference. If pkt/s instead stays near 30 regardless of fps, Gearbox has changed pacing. Then setting `NetServerMaxTickRate` to 60 or 120 on the live host driver becomes change #1. Retest the client-ping deltas in that case.
- **Also record** client ping at each cap: it should rise by about 1 frame of host time (33 ms at 30 fps).

### Test 2: Replication budget (#2)

1. In C, run arm a with `gbx.net.ReplicationBudgetSeconds` at 0.0025 and arm b at 0.006.
2. Set the value on the host with `ExecuteConsoleCommand`, then confirm it with `GetConsoleVariableFloatValue`. If the read-back is unchanged, the cvar is locked; retry through `[SystemSettings]` in `Engine.ini` with a game restart.
- **Measure:**
  - client-side enemy replicated-location updates per second (mean, and p10 of the per-enemy rate);
  - S→C B/s;
  - host frame time (mean and p95).
- **Pass:** a higher enemy update rate or fewer long gaps (> 200 ms) in arm b, with host p95 frame time not noticeably worse. Also screenshot or record the client view for a subjective check.
- **If there is no difference in C,** the budget probably isn't being hit on this hardware. It may still matter on a weaker host, so keep the result as "no effect here".

### Test 3: Correction tolerance (#4) under simulated jitter and loss

1. Use the delaying, dropping proxy at 40 ± 15 ms and 2% loss.
2. Have the client strafe and jump along a fixed path for 60 s. A key-press script via `tools/input.ps1` gives a repeatable pattern.
3. Run arm a with defaults. In arm b, set on the host's `world().NetworkManager`: `MAXPOSITIONERRORSQUARED = 25` and `ClientAuthorativePosition = True`.
- **Measure:**
  - corrections per minute, from changes to `ServerLastClientAdjustmentTime` on the host-side client pawn;
  - client-visible snaps (subjective, or position-delta spikes on the client's own pawn above about 30 cm in one frame).
- **Pass:** corrections per minute drop substantially with no gameplay oddities (doors, elevators, grapples).

### Optional: Test 4, 3+ clients and `NetClientTicksPerSecond` (#5)

This needs more than two instances, so it's likely impractical on one PC. Skip it unless 4-player sessions are reported as worse than 2-player.

---

## 4. Not verified / open questions

- The exact semantics of `gbx.net.ReplicationBudgetSeconds`, and whether `gbx.*` cvars can be set at runtime or through `[SystemSettings]` in this shipping build.
- Whether Gearbox altered stock listen-server pacing (`GetMaxTickRate`, `PrepConnections`) or `ServerCheckClientError` in `OakCharacterMovementComponent`. Tests 1 and 3 answer this.
- Enemy actor update rates and the replicated actor count in combat. Only a quiet area was inspected; there were no enemies to read.
- Whether BL4's SHiFT co-op adds packet-handler encryption that ours lacks. No impact on latency is expected either way.
- `bIsLanMatch` on `OakGameSession` can't be read directly, because it isn't a UPROPERTY. Test 0 settles it indirectly through `CurrentNetSpeed`.
- Community sources specific to BL4 co-op netcode are thin. Web searches found only vendor blogs ([ExitLag](https://www.exitlag.com/blog/borderlands-4-desync-fix/)) and performance-focused `Engine.ini` mods ([Nexus](https://www.nexusmods.com/borderlands4/mods/96)). I found no verified BL4 network `Engine.ini` tweaks.

---

## 4. Measured results (2026-10-23, two instances on one PC)

Setup: host A and client B on one PC, B joined through `tools/netlab_proxy.py`, which simulates delay, jitter, loss and reordering and counts traffic. Smoothness was measured on B by sampling the host character's transform every frame (`dev/netlab.py`, cached through WeakPointers). Rubber-banding was measured on A by watching B's pawn's `ServerLastClientAdjustmentTime` (corrections sent) and `ServerLastClientGoodMoveAckTime` (acks). Movement came from `AddMovementInput` every frame. Snapping rotation instead produced fake freezes, so it wasn't used. Scripts: `dev/netlab_run.py`, `dev/netlab_corrections.py`.

### 4.1 Host frame rate drives everything (TL;DR #1 confirmed)

| Host fps | Host→client packets/s | Packet gap avg | Client ping |
|---|---|---|---|
| ~55 (uncapped / 60 cap) | ~50 | 20 ms | 13–15 ms |
| 32 (30 cap) | **30** | **33 ms** | **21.5 ms** |

`NetServerMaxTickRate=30` doesn't limit a listen server; the host's frame rate does.

### 4.2 Remote players stay smooth on bad links

The host character, walking, as seen by the client:

| Link | Visible updates | Freezes > 120 ms |
|---|---|---|
| clean | ~51/s | none |
| 40 ms one-way | ~51/s | none |
| 40 ms ± 20 ms jitter | ~50/s | none |
| 40 ms ± 20 ms + 3% loss | ~51/s | none |

### 4.3 The joiner's own rubber-banding comes from loss and reordering, not latency

The client walked in circles while the host counted the corrections it sent:

| Link | Corrections/min |
|---|---|
| clean | 0 |
| 40 ms each way | 0 |
| 40 ms ± 20 ms jitter (in order) | 0 |
| 40 ms ± 20 ms jitter **with reordering** | 83 |
| 40 ms + **3% loss** | 47 |
| 40 ms ± 20 ms (in order) + 3% loss | 122–173 |
| same, `ClientAuthorativePosition=True` | 126 |
| same, `MAXPOSITIONERRORSQUARED` 64 / 225 (default 3) | 110 / 205 |

- Pure latency, even about 100 ms of ping, causes no corrections.
- Lost or reordered packets do, and loss combined with jitter is worst.
- Neither the position tolerance nor client-authoritative position meaningfully reduced corrections, so these aren't small position disagreements and the game settings don't fix them.
- The fix is a cleaner link: wired instead of Wi-Fi, no VPN relay hop, and a direct path, which BL4 Direct already gives versus a SHiFT relay.

### 4.4 Replication budget (`gbx.net.ReplicationBudgetSeconds`)

The value can be set and read back at runtime. In a quiet area with only the two players:

| Budget | Host→client data | Host character visible updates | Host fps |
|---|---|---|---|
| 2.5 ms (default) | 3.1 KB/s | 46/s, no freezes | 49 |
| 0.1 ms (starved) | **2.3 KB/s (−25%)** | 46/s, no freezes | 48 |
| 10 ms | 3.1 KB/s | 46/s, no freezes | 47 |

The budget really does throttle replication: starving it cut a quarter of the traffic. Player characters weren't affected, so they're prioritized, and what gets dropped is lower-priority actors. In big fights those are the enemies. A modest raise (about 5 ms) is a plausible fix for choppy enemies in big fights, but that needs a test with many enemies (not done; no enemies near the test spot).

### 4.5 Reference: SHiFT servers

TCP round trip from the test PC to SHiFT's AWS us-east-1 (Ashburn, Virginia) endpoints: 26–35 ms (nearest Cloudflare: 20–25 ms). Relay locations weren't observed.

### 4.6 Recommendations

1. **The host's frame rate is the update rate,** so steady frame pacing on the host helps everyone. A stable 60 beats a jumpy 50–110, and the host should lower graphics before big fights if their fps dips.
2. **For rubber-banding, fix the link:** wired over Wi-Fi, no relays (check `tailscale status` shows "direct"), and avoid congested uploads on the host.
3. **Optional:** expose `gbx.net.ReplicationBudgetSeconds` (2.5 ms → 5 ms) as a host setting for big fights, after a combat test confirms it.
4. **Keep `?bIsLanMatch`.** Don't enable `bClampListenServerTickRate`.

### 4.7 What the replication budget does (best current understanding)

- **Where it lives (verified):** `gbx.net.ReplicationBudgetSeconds` and `gbx.net.BudgetSkippedPriorityBoost` are registered among the engine's own NetDriver cvars in the exe (next to `net.ActorChannelPool` and the actor `NextUpdateTime` options). So it's part of the host's per-frame "which actors do I send this frame" pass. No help text ships for it.
- **How that pass works (stock UE):** each host frame, actors that are due (by `NetUpdateFrequency`) are sorted by priority (`NetPriority`, scaled by how long since they were last sent and by distance), then serialized and sent in that order.
- **What Gearbox adds (inferred from names + 4.4):** stop the pass once 2.5 ms of CPU has been spent. Anything left over waits a frame and gets its priority boosted by 100 (`BudgetSkippedPriorityBoost`), so it goes first next time and isn't starved. Starving the budget (0.1 ms) cut 25% of traffic while the player character was untouched, which fits.
- **Who goes first:** priorities read from the live host world (quiet area, 59 replicated actors):

| NetPriority | Updates/s | What |
|---|---|---|
| 10 | 10–30 | `LightProjectileManager`, `LightBeamManager` (Gearbox batches bullets/beams into managers), `OakGameState` |
| 3 | 100 | player character, player controller, their weapons, shield, grenade, class mod |
| 1 | up to 100 | world objects: vending machines, grapple points, carryables, interactables, player state |

  Enemies weren't present to read. Their priority decides how much they suffer when the budget runs out.
- **Practical meaning:** the budget is a cap, not a cost. Raising it changes nothing in quiet scenes (10 ms showed no fps change). In big fights it lets the host send more of the lower-priority backlog per frame, at the price of more host CPU per frame. Too much could lower host fps, which lowers everyone's update rate (4.1).

### 4.8 Disconnect seen after the tests

The client dropped exactly 60 s after the test relay was stopped (`ConnectionLost` / `ConnectionTimeout`). 60 s is `SteamNetDriver.ConnectionTimeout`, so this was the test setup, not the game. Side finding: the game calls `HandleNetworkError` repeatedly while tearing a connection down (29 calls in 0.4 s). The mod now reports each failure type at most once per 5 s.

### 4.9 Enemies teleporting on a client: AI update periods + budget (2026-10-28)

A friend reported enemies teleporting on their screen (client) in co-op. Test: host A and client B on one PC (same account), B joined through `tools/netlab_proxy.py` at 40 ± 15 ms one way and 1% loss. In an open area of Idolator's Noose, a Pangolin boss and 10 adds were spawned (`dev/netlab_arena.py`, Kairosdex's latent-safe spawner). Both characters wandered randomly (walk, strafe, jump), and B was teleported to a random valid arena spot every 20 s. `dev/netlab_enemies.py` on B sampled every enemy within 50 m each frame. It counted mesh jumps of more than 150 cm in one frame that were also faster than 15 m/s ("teleports", what the player sees) and actor-location jumps ("snaps", corrections the smoothing may hide). `dev/netlab_enemy_ab.py` alternated the host settings every 2 minutes, 6 halves in all:

- **default:** `gbx.net.ReplicationBudgetSeconds` 0.0025, `gbx.nav.net.TransformPeriod` / `PathPeriod` 0.25
- **tuned:** 0.005 / 0.1 / 0.1

| Half | Settings | Enemy-min | Teleports /enemy-min | Snaps /enemy-min | Host fps | Host→client |
|---|---|---|---|---|---|---|
| 1 | default | 13.0 | 0.8 | 4.1 | 70 | 9.0 KB/s |
| 2 | tuned | 12.1 | 0.1 | 1.8 | 73 | 10.4 KB/s |
| 3 | default | 12.1 | 0.6 | 4.0 | 72 | 8.6 KB/s |
| 4 | tuned | 12.1 | 0.7 | 2.3 | 73 | 9.9 KB/s |
| 5 | default | 12.1 | 1.7 | 4.1 | 73 | 8.4 KB/s |
| 6 | tuned | 12.0 | 0.4 | 1.8 | 71 | 9.7 KB/s |
| **all** | **default** | 37.2 | **1.0** (37) | **4.0** (150) | 72 | 8.7 KB/s |
| **all** | **tuned** | 36.2 | **0.4** (15) | **2.0** (72) | 72 | 10.0 KB/s |

- **Snaps halve with the tuned settings in every pair** (4.1→1.8, 4.0→2.3, 4.1→1.8). Of everything measured, this is the clearest effect.
- **Visible teleports drop about 60% overall** (1.0 → 0.4 per enemy-minute), but the pairs are noisy (half 4 > half 3). The typical jump was 2–4.5 m.
- **No measurable host cost.** Fps was equal across the halves, and traffic was +15% (about 10 KB/s).
- In the default halves, the proxy's worst host→client gaps (687, 328, 594 ms) lined up with host frame hitches (99, 332, 584 ms). Host hitches are a separate source of visible jumps (see 4.1), and these settings don't address them.
- An earlier run (3–7 enemies, the user fighting by hand) saw 9.4 teleports per enemy-minute in one default half and 2.3 in the other. Uncontrolled fights swamp the effect, so use the automated arena.
- Caveats: one PC (both copies share the CPU), a simulated link, only 6–11 enemies, and one area. The size of the gain on a real friend's link is unverified.

Two-instance (one account) pitfalls hit during this test:
- Teleporting the host's own character starts Gearbox's arrival hold (gravity 0, move input ignored), which never releases on one account. Big Encore travel (`stand.place`) on the host hit it too (2026-10-28). `CharacterMovement.GravityScale = 1` brings the character down. The move block doesn't clear: `IsMoveInputIgnored()` stayed True after `ResetIgnoreMoveInput`, 10x `SetIgnoreMoveInput(False)`, `ResetIgnoreInputFlags` and `ClientIgnoreMoveInput(False)`, so Gearbox's override checks something else. A same-map fast travel doesn't clear it (it starts it again). The override (`OakPlayerController` vtable +0x740, RVA 0x3c9f366 in this build) also returns True while an unreflected byte at `OakCharacterMovementComponent+0x11F0` is set. Writing 0 there, plus `GravityScale = 1`, released A, and it stayed released (checked 3 s later). No direct store to that offset exists in the exe, so the game's own setter and release path are still unknown.
- A client joining while the host is still behind its loading screen breaks the host's arrival.
- A host crash let the client write a 10 KB `profile.sav`. It was restored from a backup.

### 4.10 No nav throttle (periods 0) vs 0.1 vs the game's 0.25 (2026-10-28)

The same arena setup as 4.9, with 8–12 enemies. Three variants, 2 rounds each, alternating every 2 minutes. All of them used budget 0.005 except "0.25", which is the game's full default.

| Nav periods | Snaps /enemy-min (round 1 / 2 / all) | Teleports /enemy-min (all) | Host→client | Host fps (round 2) |
|---|---|---|---|---|
| 0.25 (game) | 3.3 / 3.1 / **3.2** | 0.8 | 8.9 KB/s | 46 |
| 0.1 | 2.3 / 1.2 / **1.7** | 0.56 | 9.1 KB/s | 47 |
| **0** (no throttle) | 1.2 / 0.7 / **1.0** | 0.43 | **16.5 KB/s** | 47 |

- Snaps fall at each step in both rounds. Visible teleports fall overall but are noisy (round 2: 0.5 / 0.1 / 0.5).
- No fps cost in round 2. Round 1 had host hitches of up to 1.5 s in the 0.25 and 0.1 halves, which don't look caused by the settings. B's `t.MaxFPS 30` cap reset after the first half; host fps was equal across halves 2–6.
- The cost is upload: about 1.8x, roughly 17 KB/s per friend.
- BL4 Direct's "Smoother enemies for friends" option (`mods/bl4_direct/smooth.py`) therefore uses 0 / 0 / 0.005.

### 4.11 Client hit registration and lag-compensation cvars (2026-10-28)

- **Nothing about hits travels from the client.** There is no fire or hit RPC. The client sends trigger and aim (`Weapon:ServerSetPatternSeed`, `OakPlayerController:ServerSetBestTarget`, `ServerSetFullyAimingAtTarget`, ...), and the host simulates the client's bullets against its own enemy positions. A friend at 120 ms ping aims at where an enemy was on their screen, and the host tests that against where the enemy is now.
- **Gearbox's compensation** (exe help texts, read from the dump):
  - `gbx.LagComp.ProjectileScaling{Enabled=1, MinPing=0, MaxPing=0.25 s, MinRadius=0.2, MaxRadius=4.0}`: the bullet's hit radius grows with the shooter's ping.
  - `gbx.LightProjectile.{EnableLagCompMagnetism=1, LagCompMagnetismMinAngle=0, MaxAngle=5°, MinPing=30, MaxPing=200 ms, ...}`: aim magnetism for laggy shooters.
  - A rewind compensator exists but is off. RVA 0x6196754 reads Engine-ini section `[LagCompensation]` keys `FrameHistoryTime`, `SampleRate` and `RewindTimeBias`. RVA 0x3f95b11 creates it (0x80 bytes at `object+0x938`) only on a listen or dedicated server, without `-nolagcomp`, when `[LagCompensation] Enabled` is true (default false). Which class gets it is unknown: every `OakCharacter` read 0 at +0x938. The game rewrites the user's `Engine.ini` at startup and drops unknown sections, so it would need a launch argument (`-ini:Engine:[LagCompensation]:Enabled=True`). Untested.
- **Test** (`dev/netlab_suite.py` + `dev/netlab_suite_run.py`; rows in `.bridge/netlab/suite.jsonl`): a target dummy 8 m from B, moved ±3 m on a 4 s sine by the host and healed every second. B aims perfectly at what its own screen shows (camera input, posted left clicks). The link was ~120 ms ping. Shots were counted on the host from the client weapon's `RemoteShotState` (2 changes per shot, checked against ammo), and hits from a second hook on Carnage Meter's damage callback.

| Arm (host cvars) | Hits on the moving dummy |
|---|---|
| default | 12.7%, 13.8%, 13.9% |
| radius only (`ProjectileScalingMaxRadius` 20, `MaxPing` 0.15) | 22.6% |
| magnet only (`MaxAngle` 8, `MaxPing` 150) | 13.1% (no effect) |
| both | 24.7–26.1% |
| **strong** (radius 40, `MaxPing` 0.10, magnet 12° / 100 ms) | **28.3%, 29.5%** |
| still dummy, default | 100% (204/204) |

- The radius is what helps, and it doubles hits on a moving target at this ping. Magnetism does nothing here.
- It only applies to laggy shooters (scaled by their ping), so the host's own shots are unchanged. Set too large, it reads as aim assist.
- The enemy-smoothness settings (4.9–4.10) and movement trust didn't change hits.

### 4.12 Jump rubber-banding: a held jump at touchdown (2026-10-28)

The user saw their own character (client B, 120 ms lab link) rubber-band on some jumps.

**Method.** `dev/netlab_jumplog.py` logs B's character every frame on both copies with `time.perf_counter()`, which on Windows is one clock (QPC) for both processes. B logs its own character. A logs its copy of B, plus each correction it sends (`ServerLastClientAdjustmentTime` changes). `dev/netlab_jumplog_analyze.py` lines the two up: the host's copy trails by 5–50 ms, found by best fit. It classifies each correction and prints frame-by-frame windows (`--window`).

**Findings**

1. **Walking corrections earlier were a test artifact.** B had drifted into the town's wandering NPC cats (`Char_CatElder`, `Char_CatYoung`), and each copy has them in different places (one copy had one 1.6 m from B, the other 5 m). Bumping into them gave 58–117 corrections/min while walking. On open flat ground 15 m from any NPC, a smooth circle gave **0 corrections in 36 s** (320 acks), as in 4.3. The same goes in combat: running into enemies whose positions differ between host and client causes corrections. Smoother enemies (4.10) make the positions agree better.
2. **On open ground with a clean link, every correction came from one jump case:** the jump key down at the moment of touchdown, with that press started in the air (with no jumps left it starts a glide). Reproduced with scripted presses (jump, double jump, then a tap timed around touchdown):

| Tap (relative to touchdown, held for) | Host jumps again on landing | Client jumps again | Corrections after |
|---|---|---|---|
| released before touchdown (-210 to -66 ms, 50 or 120 ms taps) | no | no | 0 |
| **pressed in the air, still down at touchdown** (-98 to -21 ms) | **yes, 14–30 ms after landing** | **no**: it stays on the ground with `bPressedJump=1`, jump count 0 | 1–13 |
| pressed at or after touchdown (-2 to +77 ms) | yes | yes | 0 |

   Frame by frame (t+41.18 in `jl_*_tapland2.json`): both copies land and clear `bPressedJump`. On the next frame B sets it again because the key is held, but doesn't jump. The host gets the same move with the jump flag and jumps (`JumpCurrentCount` 1, `bWasJumping` 1, vz 601). B stays grounded for 3 frames, then the correction throws it up ~45 cm over two frames: the visible snap. B is now airborne without having jumped (jump count 0, no glide flags) while the host's copy has a real jump and starts gliding because the key is held. **B falls at normal gravity while the host glides, so a correction pulls B up every 100 ms** (`NetworkMinTimeBetweenClientAdjustments`) until it lands, ~1.2 s of sawtooth. A press during that time makes B double jump while the host keeps gliding, the biggest gap (33–45 cm).
3. **The client refuses a jump that the host accepts from the same move.** The refusal is local to the client: an input or glide gate that the host, which only sees the move flags, doesn't have. `CanJumpInternal`, `Jump`, `OnJumped`, `OnLanded` and `StopJumping` don't go through ProcessEvent, so they can't be hooked to show which check it is. It's a Gearbox bug, independent of the link: it happens on a clean 0 ms link and gets worse with delay (more frames until the correction).
4. **The same correction can also land during a normal jump or double jump** (seen on the lab link with 1% loss, `jl_*_lab1.json`). B's replay then drops the jump for one or two frames (down 36 cm, then up 82 cm) and loses Gearbox's `bMayWantToGlide`, which starts the same glide sawtooth.
5. **Vault power costs differ between host and client.** On the host every character has `VaultPowerCost_Dash` 25 / `VaultPowerCost_Glide` 7.5 (`BaseValue` 50 / 15). On the client both its own character and the host's show 50 / 15. The properties are replicated (`CPF_Net`) but the modified `Value` isn't on the client. The source of the host's -50% isn't found yet: not god mode (`UnSetGodLock` didn't change it), not our mods (no code touches these attributes), and the `vaultpower_vaultreward_upgrades` graph has no points. Not yet tested whether this desyncs long glides or dashes (vault power runs out at different times on each copy).

**What doesn't help:** movement trust (`bIgnoreClientMovementErrorChecksAndCorrection` + `bServerAcceptClientAuthoritativePosition`) skips the position check, but `ServerCheckClientError` still corrects on a movement-mode mismatch [UE], and this bug is one (client walking, host falling). A better link or a UDP tunnel doesn't help either: it happens at 0 ms.

**Rates in these runs:** random running and jumping on open ground (B uncapped ~60 fps, clean link): 17.7 corrections/min, all from case 2. With the old random mover, corrections came in episodes about 8.5 s apart, each from one held-at-touchdown press.

#### 4.12.1 A client-side fix for the held-jump case (`dev/netlab_jumpfix.py`, tested 2026-10-28)

Why the obvious fix doesn't work: B sets the jump flag once, during input processing in the frame after touchdown, and the move with the flag is built in the same frame. No hookable UFunction runs between those two steps (traced: ServerMovePacked comes after the move has captured the flag), so Python can't clear it in time. Clearing it at camera time lands one frame too late: the host still jumps.

What works: the stuck state itself is an exact signal. The frame after touchdown, B is walking with `bPressedJump` set and `bWasJumping` off. In 23/23 timed cycles that happened exactly when the host jumped. B then makes the host's jump itself:
- falling mode;
- the jump goal's take-off speed (`sqrt(2·g·GoalHeight)` = 623 cm/s for `JumpGoal_Default`), with one frame of that motion applied;
- `JumpCurrentCount` 1;
- `bMayWantToGlide` kept on while the key is held, then the glide start at the apex (`bWantsToGlide` / `bIsGlideReady`), as the host's copy does.

Two corrections to that rule:
- If a correction for a move from before the jump puts B back on the ground (its replay refuses the jump again), B is put back on the host's jump arc.
- The jump-count condition was dropped: a correction at touchdown can leave it at 2.

Results (same scripted scenario, B uncapped ~60 fps; "affected" = cycles where the host jumped from a pre-touchdown press):

| Link | Fix | Affected cycles | Corrections / cycle | Largest snap per cycle (median / max) | Snaps > 50 cm at touchdown | Sawtooth cycles (≥ 5 corrections) |
|---|---|---|---|---|---|---|
| clean | off | 15 | 2.5 (max 12) | 44 / 57 cm | 6 | 2 |
| clean | on (2 runs) | 12 + 12 | 1.2 / 1.7 (max 2) | 14 / 36 cm and 16 / 21 cm | 0 + 0 | 0 |
| 40 ± 15 ms, 1% loss | off | 18 | 3.0 (max 9) | 84 / 115 cm | 16 | 3 |
| 40 ± 15 ms, 1% loss | on (final) | 22 | 2.4 (max 6) | 16 / 33 cm | 0 | 4 (small: max 33 cm) |

- With the fix, the corrections left are the ones for moves already in flight when the host jumped. They are small and land where B already is. The ~14–16 cm "largest snap" is mostly the fix's own take-off frame, the same as any jump starting.
- 22 of 22 fixes in the final lagged run lined up with a host jump: no false triggers.
- Not fixed: a correction landing in the middle of a double jump on a lossy link (4.12 finding 4). It showed up in one lagged run only (2 events, up to 205 cm), not in the other two.
- Untested: a real player and keyboard (all presses were posted Space keys); gamepads; jumps changed by gear, skills or low gravity (the fix uses the current jump goal's height); other jump types (ladder, water, slide jumps); the host's own character; anything but the default jump.
