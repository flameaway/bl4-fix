# BL4 Direct Connect

Direct IP co-op for Borderlands 4, without SHiFT matchmaking or Steam invites. One player hosts and
friends join by IP address, all from an in-game panel (press **F6**).

## How it works

BL4 routes co-op through Gearbox's SHiFT networking (`ShiftNetDriver`). The game also ships
Unreal's `SteamNetDriver`, which talks plain UDP whenever it's given an IP address rather than a
Steam ID. The mod swaps the game's net driver to that one, then uses Unreal's stock travel
commands: the host reloads their map as a listen server, and joiners `open <ip>:<port>`.
Stopping hosting or leaving puts the SHiFT driver back.

## Install

Run `BL4DirectSetup.exe` (both players). It:

- finds your Borderlands 4 install (Steam or Epic)
- installs the Microsoft VC++ runtime if needed
- installs the pinned Python SDK version (0.3), the BL4 Direct mod and the Mod Menu, already
  enabled
- installs the optional mods you tick (Detailed Item Cards is ticked by default; the rest, from
  ECHO Anywhere to the Minimap and the ports of Yeti's mods, are up to you)
- adds a firewall rule so only friends you've added can reach a game you host

Close the game first. Run it again any time (Start menu > **BL4 Mods Setup**) to add or remove
mods, repair, uninstall, or manage friends. Every mod's settings and keys are in game: **F7**, or
**MODS** in the pause menu and on the title screen (under Continue).

### Updates

When the game starts, the Mod Menu asks GitHub once whether newer versions of your mods have been
released (nothing is sent but the request itself; turn it off with "Check for updates at launch"
in the Mod Menu's own settings). If there are, **MODS** shows a **!**. Open it, look at the
Updates page, and press **Update all**: the new versions download and are checked, then install
when the game closes. **Restart now** closes the game, installs them and starts it again (Steam).
Mods you didn't install are listed there too, with an Install button.

## Play

In game, press **F6**:

| Who | Do |
|---|---|
| Both | Add each other: type a name and the other person's public IP, then **ADD**. Your own public IP is shown at the top with a **COPY** button. |
| Host | Load into your game, press **HOST A GAME**. The panel shows the address to share and how many friends are connected. |
| Joiner | From the main menu, press **JOIN** next to the host's name. |
| Done | **STOP HOSTING** / **LEAVE GAME** returns to normal online play. |

The panel also shows a compatibility line (`game <build> / sdk <ver> / mod <ver>`). Both players
should see the same values.

### Firewall and ports

- Only the **host** needs anything opened. Joining is an outgoing connection.
- Internet play: the host forwards **UDP 7777** on their router to their PC. Joiners need nothing.
- The setup app adds a Windows Firewall rule that blocks UDP 7777 from everyone except your
  friends (and, optionally, your local network/Tailscale). Steam's own rule would otherwise let
  anyone reach it. When you host after changing your friend list, Windows asks once for
  permission to update that rule.
- Co-op uses very little bandwidth: about 2-4 KB/s each way in testing.

## Detailed item cards

A separate mod, **BL4 Detailed Item Cards** (`bl4_item_cards`), installed alongside BL4 Direct by
the setup app. It makes item cards more informative. These changes are local to your game, so the
other player doesn't need them, and it works without BL4 Direct:

- **Class mods** list each boosted skill with its name, points and full description, coloured by
  skill tree, instead of only icons and "+2".
- **Secondary weapon stats** (the small row, e.g. `35cm`) show the stat's name ("Splash Radius").
- **Banner and red flavour text** render the game's colour markup.
- **Legendaries** show which bosses drop them and how often, with keys (J/K by default) to set a
  waypoint to, or travel to, that boss's Moxxi's Big Encore machine.

The game's menus are HTML pages packed in its `.pak` files. The mod writes
`OakGame/Content/Paks/~mods/pakchunk0-Windows_4242_P.pak`, which holds an edited copy of the
item card page and a new stylesheet. The game reads paks at startup, so changes apply after a
restart. To avoid undoing game updates:

- The pak is only installed while the game's own item card page is exactly the version ours was
  built from. If a patch changes it, the mod removes its pak and says so in the console, and the
  game's normal cards come back until the mod is updated.
- If another mod in `~mods` also changes item cards, ours steps aside.

Turn it off by disabling **BL4 Detailed Item Cards** in the mod menu (`mods` in the console). Its
options there set the Big Encore keys.

## Known limitations

- Players connected this way aren't in a SHiFT party, so SHiFT-only features may not work.
- The host's map reloads when hosting starts or stops.
- If the host gets signed out of SHiFT mid-session (e.g. the same SHiFT account signs in
  elsewhere), the game drops the hosted session.
- Joining your own public IP from inside your own network needs router "hairpin" support. Use
  your LAN IP, or test with a friend.

## Development

See [BL4_MODDING_GUIDE.md](BL4_MODDING_GUIDE.md) for everything learned building this (SDK
quirks, networking, runtime UMG UI, and the two-instance test harness).

- `mods/bl4_direct/`: the mod (`__init__.py` networking and commands, `ui.py` the F6 panel,
  `shared.py` config shared with the app)
- `mods/bl4_item_cards/`: the detailed item cards mod (`tools/gen_item_cards.py` regenerates the
  edited page and game data in `card_page/` after a game patch; `tools/ui_live.py` restyles cards
  live while the game runs)
- `ported/`: our GPL-3.0 rewrites of Yeti's mods (see `ported/README.md`)
- `installer/`: the setup app (`python installer/build.py` produces `dist/BL4DirectSetup.exe`;
  `installer/catalog.py` lists the mods it can install)
- `dev/bl4_bridge/` + `tools/`: test harness (run Python inside a game instance, drive game
  windows from the desktop session, UDP relay for packet inspection)
