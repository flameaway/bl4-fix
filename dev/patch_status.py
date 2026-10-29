# After a game patch: does each loaded mod still work on this build? Through the bridge (dev only):
#   python tools/game_patch.py live <id>        (or: python tools/bridge.py <id> -f dev/patch_status.py)
# Reads state only. Three lazy native finders that only compare code bytes (Build Presets' equip
# call, Reroll's and Kairosdex's identity functions) are run if they haven't yet this session; the
# rest report what their mod found when it last looked. Everything lives in one function so the
# shared bridge namespace keeps nothing.
# Add a probe here when a mod gets a new native part or step-aside state (docs/game-patch.md).
# ruff: noqa: F821, BLE001, SLF001


def _bl4_patch_status() -> None:
    import sys
    import time

    import mods_base
    import unrealsdk
    from mods_base import get_pc

    started = time.perf_counter()
    rows: list[tuple[str, str, str, str]] = []  # (mod, part, status, detail)

    def mod(name: str):  # noqa: ANN202
        return sys.modules.get(name)

    def probe(mod_name: str, part: str, fn) -> None:  # noqa: ANN001
        if mod(mod_name) is None:
            rows.append((mod_name, part, "-", "not loaded"))
            return
        try:
            status, detail = fn(mod(mod_name))
        except Exception as ex:
            status, detail = "PROBE?", f"the probe broke ({ex!r}): update dev/patch_status.py"
        rows.append((mod_name, part, status, str(detail)[:160]))

    def ok_if(cond, good: str = "", bad: str = "") -> tuple[str, str]:  # noqa: ANN001
        if cond is None:
            return "pending", "not looked yet this session"
        return ("ok", good) if cond else ("OFF", bad)

    # region Native parts

    def echo(m):  # noqa: ANN001, ANN202
        st = m.State
        if st.failed:
            return "OFF", "finder failed: see the log line 'this game version isn't supported'"
        return ok_if(True if st.menu is not None else None, "status menu call found")

    probe("bl4_echo_anywhere", "status menu call", echo)

    def tabs(m):  # noqa: ANN001, ANN202
        found = sys.modules["bl4_instant_tabs.patch"]._found
        if not found:
            return "pending", "no sites looked up yet"
        missing = [k for k, v in found.items() if v is None]
        return ("OFF", f"not found: {', '.join(missing)}") if missing else ("ok", f"{len(found)} sites")

    probe("bl4_instant_tabs", "code patches", tabs)

    def reroll_native(m):  # noqa: ANN001, ANN202
        native = sys.modules["bl4_reroll.native"]
        return ok_if(native.available(), "in-place swap", f"fallback (sell + mail): {native._api_error}")

    probe("bl4_reroll", "in-place swap", reroll_native)
    probe("bl4_reroll", "parts / legendaries data",
          lambda m: ok_if(m.data_ok(), "current", "changed: every reroll is blocked"))

    def builds(m):  # noqa: ANN001, ANN202
        game = sys.modules["bl4_builds.game"]
        try:
            game.equipper()
        except Exception as ex:
            return "OFF", repr(ex)
        return "ok", "equip call found"

    probe("bl4_builds", "native equip", builds)

    def third(m):  # noqa: ANN001, ANN202
        f = sys.modules["bl4_third_person.camera_defs"].Finder
        if f.defs is not None:
            return "ok", "camera defs found"
        if getattr(f, "gave_up", lambda: False)():
            return "OFF", "camera defs not found (gave up for the session)"
        return ("pending", "scan running") if f._thread is not None and f._thread.is_alive() else \
            ("pending" if not f._failed_at else "OFF", "not scanned yet" if not f._failed_at else "last scan found nothing")

    probe("bl4_third_person", "camera defs", third)

    def lost_loot(m):  # noqa: ANN001, ANN202
        bad = sys.modules["bl4_backpack_size.limits"]._lost_loot_bad
        return ("OFF", "Lost Loot memory doesn't match (left alone)") if bad else ("ok", "no mismatch seen")

    probe("bl4_backpack_size", "Lost Loot offsets", lost_loot)

    def crisp(m):  # noqa: ANN001, ANN202
        s = m._S
        if s.broken:
            return "OFF", "cvar lookup failed: the mod stays out of the way"
        return ok_if(True if s.render_cvar is not None else None, "console cvar lookup found")

    probe("bl4_crisp_numbers", "cvar lookup", crisp)

    def arrival(m):  # noqa: ANN001, ANN202
        a = sys.modules["bl4_direct.arrival"]
        if a.scan_error:
            return "OFF", f"scan raised: {a.scan_error}"
        if not a.scan_done():
            return "pending", "exe scan not done (runs at enable)"
        return ok_if(a.setter() is not None, "fast-travel arrival fix found", "not found: friends can't fast travel")

    probe("bl4_direct", "arrival fix", arrival)

    def minimap(m):  # noqa: ANN001, ANN202
        obj = sys.modules["bl4_minimap.objectives"].Objectives.broken
        route = sys.modules["bl4_minimap.breadcrumbs"].Breadcrumbs.broken
        bad = [f"objectives: {obj}" for _ in [0] if obj] + [f"route: {route}" for _ in [0] if route]
        return ("OFF", "; ".join(bad) + " (falls back to the ECHO map copy)") if bad else ("ok", "live objectives + route")

    probe("bl4_minimap", "live objectives / route", minimap)

    def carnage(m):  # noqa: ANN001, ANN202
        b = sys.modules["bl4_damage_meter.tracker"].S.binder
        return ("OFF", b.error) if b.error else ("ok", "damage delegate bound or bindable")

    probe("bl4_damage_meter", "damage delegate", carnage)

    def dex_native(m):  # noqa: ANN001, ANN202
        ident = sys.modules["bl4_weapondex.identity"]
        try:
            ident.api()
        except Exception as ex:
            return "OFF", f"rolling is off: {ex}"
        return "ok", "identity functions"

    probe("bl4_weapondex", "identity functions", dex_native)

    # endregion
    # region Data pins (checked by the mods themselves)

    def dex_data(m):  # noqa: ANN001, ANN202
        return ok_if(sys.modules["bl4_weapondex.data"]._data_ok, "dex.json current", "dex says 'chances may be off'")

    probe("bl4_weapondex", "dex.json", dex_data)
    probe("bl4_instant_travel", "stations.json",
          lambda m: ok_if(sys.modules["bl4_instant_travel.stations"]._current, "current", "portal used instead"))

    def cards(m):  # noqa: ANN001, ANN202
        page = sys.modules["bl4_item_cards.card_page"]
        bad = [k for k, v in page._data_ok.items() if not v]
        status = m._status if isinstance(getattr(m, "_status", None), str) else ""
        if "paused" in status.lower():
            return "OFF", status
        return ("OFF", f"stale: {', '.join(bad)}") if bad else ("ok", status or "page + data current")

    probe("bl4_item_cards", "page + data", cards)

    # endregion
    # region Hook targets

    missing: list[str] = []
    unloaded: list[str] = []
    checked = 0
    paths: set[tuple[str, str]] = set()
    for m in mods_base.mod_list.mod_list:
        for h in getattr(m, "hooks", ()):
            for func, _type in getattr(h, "hook_funcs", ()):
                paths.add((m.name, func))
    coop = mod("bl4_direct.coop")
    if coop is not None:
        paths |= {("BL4 Direct (coop)", coop.CHAT_FUNC), ("BL4 Direct (coop)", coop.MESSAGE_FUNC)}
    for owner, func in sorted(paths):
        checked += 1
        try:
            unrealsdk.find_object("Function", func)
        except Exception:
            # Blueprint functions only exist once their blueprint loaded (an Encore machine, a grapple point)
            (unloaded if func.startswith("/Game/") else missing).append(f"{owner}: {func}")

    # endregion

    pc = get_pc()
    print(f"== Mods on this build ({'in a world' if pc is not None else 'no player yet: some finders run later'})")
    width = max(len(r[0]) for r in rows) if rows else 10
    for name, part, status, detail in rows:
        if status == "-":
            continue
        print(f"  {status:8} {name:{width}}  {part}: {detail}")
    absent = sorted({r[0] for r in rows if r[2] == "-"})
    if absent:
        print(f"  (not loaded: {', '.join(absent)})")
    print(f"\n== Hook targets ({checked} checked)")
    for line in missing:
        print(f"  MISSING  {line}   <- renamed by the patch? this hook never fires")
    for line in unloaded:
        print(f"  unloaded {line}   (blueprint not loaded now: can't tell)")
    if not missing:
        print("  every /Script/ hook target exists")
    print(f"\n({(time.perf_counter() - started) * 1000:.0f} ms on the game thread)")


try:
    _bl4_patch_status()
finally:
    del _bl4_patch_status
