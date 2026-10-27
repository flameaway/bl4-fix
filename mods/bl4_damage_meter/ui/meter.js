"use strict";

// Carnage Report: every player's damage, kills and top damage sources, as a screen in the game's
// menu page (the page the pause menu lives in).
//
// The BL4 Mod Menu runs this file (its extensions.js) and runs it again when it changes, so it
// tears its last copy down first. Python (bl4_damage_meter/page.py) serves the board through
// GET /ext?k=carnage: polled every 250 ms while the screen shows, once a second otherwise. Clicks
// go back as {op: "ext", ext: "carnage", do: ...}. The DOM is built once per player and updated in
// place; numbers count up to their new value.

(function () {
    var ns = window.bl4ModMenu;
    if (!ns || !ns.ext) return;
    var ext = ns.ext;
    var KEY = "carnage";
    var BASE = "coui://uiresources/bl4carnage/";
    var ART = "coui://uiresources/_shared/assets/";
    var POLL_OPEN_MS = 250;
    var POLL_IDLE_MS = 1000;
    var CSS_MS = 2000;
    var TOAST_MS = 4000;
    var COUNT_MS = 650;
    var ROWS = 3;  // top sources shown per player (all of them, up to 6, when the card is expanded)

    // The game's own art for each kind of damage source
    var ITEM_ICONS = {
        pistol: "ico_ui_art_item_card_type/ico_art_item_card_weap_pistol.png",
        smg: "ico_ui_art_item_card_type/ico_art_item_card_weap_smg.png",
        assault: "ico_ui_art_item_card_type/ico_art_item_card_weap_assault.png",
        shotgun: "ico_ui_art_item_card_type/ico_art_item_card_weap_shotgun.png",
        sniper: "ico_ui_art_item_card_type/ico_art_item_card_weap_sniper.png",
        heavy: "ico_ui_art_item_card_type/ico_art_item_card_heavy_weapon_generic.png",
        turret: "ico_ui_art_item_card_type/ico_art_item_card_heavy_weapon_generic.png",
        shield: "ico_ui_art_item_card_type/ico_art_item_card_energy_shield.png",
        grenade: "ico_ui_art_item_stats/ico_ui_art_gdgt_grenade_damage.png",
        repkit: "ico_ui_art_item_card_type/ico_art_item_card_rep_kit.png",
        enhancement: "ico_ui_art_item_card_type/ico_art_item_card_enhancement.png",
        classmod: "ico_ui_art_item_card_type/ico_art_item_card_class_mod.png"
    };
    var KIND_ICONS = {
        melee: "ico_ui_art_item_stats/ico_ui_art_wpn_dmg.png",
        slam: "ico_ui_art_item_stats/ico_ui_art_wpn_scndry_dmg_radius.png",
        grenade: "ico_ui_art_item_stats/ico_ui_art_gdgt_grenade_damage.png",
        vehicle: "ico_ui_art_vehicles/ico_vehicleWeapon_machine_gun.png",
        env: "ico_ui_art_elemental_damage/ico_ui_art_elemental_all.png",
        companion: "ico_ui_art_mayhem/ico_art_vaulthunter_ultimate.png",
        skill: "ico_ui_art_mayhem/ico_art_vaulthunter_ultimate.png",
        other: "ico_ui_art_item_stats/ico_ui_art_wpn_dmg.png",
        gear: "ico_ui_art_item_stats/ico_ui_art_wpn_dmg.png"
    };
    var ICON_KILLS = "ico_ui_art_item_stats/ico_ui_art_wpn_scndry_crit_hit_dmg.png";
    var ICON_SKULL = "coui://uiresources/components/menu_dropdown/assets/skull.png";
    var ICON_CROWN = "ui_art_misc_assets/ui_icon_blue_crown.png";
    var ICON_BIG = "ico_ui_art_item_stats/ico_ui_art_wpn_dmg.png";
    var ICON_DPS = "ico_ui_art_item_stats/ico_ui_art_wpn_prmry_fire_rate.png";

    if (ext.teardowns[KEY]) {
        try {
            ext.teardowns[KEY]();
        } catch (err) {}
    }
    var cm = ns.carnage || (ns.carnage = {});
    cm.counter = Math.max(cm.counter || 0, cm.gen || 0) + 1;
    var gen = cm.counter;
    cm.gen = gen;

    var timers = [];
    function alive() {
        return cm.gen === gen;
    }
    function later(fn, ms) {
        var id = setTimeout(function () {
            var at = timers.indexOf(id);
            if (at >= 0) timers.splice(at, 1);
            if (alive()) fn();
        }, ms);
        timers.push(id);
        return id;
    }
    function log(text) {
        if (ns.log) ns.log("[carnage] " + text);
    }

    var view = cm.view || (cm.view = { open: false, own: false, scope: "fight", expanded: {} });
    var state = null;
    var ui = { cards: {} };
    var docListeners = [];

    // region Helpers

    function el(tag, cls, text) {
        var e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined && text !== null) e.textContent = text;
        return e;
    }
    function onClick(target, fn) {
        target.addEventListener("click", function (evt) {
            evt.stopPropagation();
            if (alive()) fn(evt);
        });
    }
    function art(path) {
        return path ? 'url("' + (path.indexOf("://") >= 0 ? path : ART + path) + '")' : "none";
    }
    function setText(node, text) {
        if (node.textContent !== text) node.textContent = text;
    }
    function setClass(node, cls, on) {
        if (node.classList.contains(cls) !== !!on) node.classList.toggle(cls, !!on);
    }
    function setBg(node, path) {
        var want = art(path);
        if (node.dataset.bg !== want) {
            node.dataset.bg = want;
            node.style.backgroundImage = want;
        }
    }
    function setWidth(node, fraction) {
        var want = Math.max(0, Math.min(100, fraction * 100)).toFixed(1) + "%";
        if (node.style.width !== want) node.style.width = want;
    }

    function commas(n) {
        var s = String(Math.round(n));
        var out = "";
        while (s.length > 3) {
            out = "," + s.slice(-3) + out;
            s = s.slice(0, -3);
        }
        return s + out;
    }
    function short(n) {
        var abs = Math.abs(n);
        if (abs >= 1e12) return (n / 1e12).toFixed(2) + "T";
        if (abs >= 1e9) return (n / 1e9).toFixed(2) + "B";
        if (abs >= 1e6) return (n / 1e6).toFixed(2) + "M";
        if (abs >= 1e4) return (n / 1e3).toFixed(1) + "K";
        return commas(n);
    }
    function big(n) {
        return n >= 1e10 ? short(n) : commas(n);
    }
    function pct(part, whole) {
        if (!whole) return "0%";
        var p = (part / whole) * 100;
        return (p >= 10 || p === 0 ? Math.round(p) : p.toFixed(1)) + "%";
    }
    function clock(seconds) {
        seconds = Math.max(0, Math.round(seconds || 0));
        var m = Math.floor(seconds / 60);
        var s = seconds % 60;
        if (m >= 60) return Math.floor(m / 60) + ":" + ("0" + (m % 60)).slice(-2) + ":" + ("0" + s).slice(-2);
        return m + ":" + ("0" + s).slice(-2);
    }

    // Numbers that count up to their new value (one shared animation frame loop while any moves)
    var counting = [];
    var countFrame = null;
    function countTo(node, value, fmt) {
        var from = node._value === undefined ? 0 : node._value;
        node._fmt = fmt;
        if (from === value) {
            setText(node, fmt(value));
            return;
        }
        node._from = from;
        node._to = value;
        node._value = value;
        node._start = Date.now();
        if (counting.indexOf(node) < 0) counting.push(node);
        if (countFrame === null) countFrame = requestAnimationFrame(countStep);
    }
    function countStep() {
        countFrame = null;
        if (!alive()) return;
        var now = Date.now();
        counting = counting.filter(function (node) {
            var t = Math.min(1, (now - node._start) / COUNT_MS);
            var eased = 1 - Math.pow(1 - t, 3);
            setText(node, node._fmt(node._from + (node._to - node._from) * eased));
            return t < 1;
        });
        if (counting.length) countFrame = requestAnimationFrame(countStep);
    }

    function button(text, fn, cls) {
        var t = ns.templates && ns.templates.button;
        var b = t ? t.cloneNode(true) : el("div", "list_btn");
        b.classList.add("bl4cm_btn");
        // One class per add(): a string with spaces isn't split (the Boss Report tab lost its width)
        (cls || "").split(" ").forEach(function (c) {
            if (c) b.classList.add(c);
        });
        var label = b.querySelector(".button_txt");
        if (!label) {
            label = el("p", "button_txt");
            b.appendChild(label);
        }
        label.textContent = text;
        b.addEventListener("mouseenter", function () {
            b.classList.add("hover_effect");
        });
        b.addEventListener("mouseleave", function () {
            b.classList.remove("hover_effect");
        });
        if (fn) onClick(b, fn);
        return b;
    }
    function send(payload) {
        ext.send(KEY, payload);
        pollSoon();
    }

    // endregion
    // region Screen

    function build() {
        var root = el("div", "bl4cm_root bl4cm_hidden");
        root.appendChild(el("div", "fill_content menu_bg_nostandin bl4cm_bg"));
        root.appendChild(el("div", "bl4cm_shade"));
        root.appendChild(el("div", "bl4cm_lines"));
        var frame = el("div", "bl4cm_frame");

        var header = el("div", "bl4cm_header");
        ui.bossPic = el("div", "bl4cm_boss_pic bl4cm_hidden");
        header.appendChild(ui.bossPic);
        var titles = el("div", "bl4cm_titles");
        ui.title = el("p", "bl4cm_title", "CARNAGE REPORT");
        titles.appendChild(ui.title);
        ui.subtitle = el("div", "bl4cm_subtitle");
        ui.live = el("p", "bl4cm_live", "LIVE");
        ui.subtitleText = el("p", "bl4cm_subtitle_text");
        ui.subtitle.appendChild(ui.live);
        ui.subtitle.appendChild(ui.subtitleText);
        titles.appendChild(ui.subtitle);
        header.appendChild(titles);
        var team = el("div", "bl4cm_team");
        ui.teamTotal = el("p", "bl4cm_team_total", "0");
        team.appendChild(ui.teamTotal);
        ui.teamLabel = el("p", "bl4cm_team_label", "TEAM DAMAGE");
        team.appendChild(ui.teamLabel);
        header.appendChild(team);
        frame.appendChild(header);

        var bar = el("div", "bl4cm_bar");
        ui.tabs = el("div", "bl4cm_tabs");
        ui.tabBoss = button("Boss Report", function () {
            setScope("boss");
        }, "bl4cm_tab bl4cm_tab_boss bl4cm_hidden");
        ui.tabs.appendChild(ui.tabBoss);
        ui.tabFight = button("This Fight", function () {
            setScope("fight");
        }, "bl4cm_tab");
        ui.tabSession = button("Session", function () {
            setScope("session");
        }, "bl4cm_tab");
        ui.tabs.appendChild(ui.tabFight);
        ui.tabs.appendChild(ui.tabSession);
        bar.appendChild(ui.tabs);
        ui.notice = el("p", "bl4cm_notice");
        bar.appendChild(ui.notice);
        frame.appendChild(bar);

        ui.players = el("div", "bl4cm_players");
        frame.appendChild(ui.players);
        ui.empty = el("div", "bl4cm_empty bl4cm_hidden");
        ui.emptyIcon = el("div", "bl4cm_empty_icon");
        ui.emptyIcon.style.backgroundImage = art(ICON_SKULL);
        ui.empty.appendChild(ui.emptyIcon);
        ui.emptyTitle = el("p", "bl4cm_empty_title", "NO CARNAGE YET");
        ui.empty.appendChild(ui.emptyTitle);
        ui.emptyText = el("p", "bl4cm_empty_text");
        ui.empty.appendChild(ui.emptyText);
        frame.appendChild(ui.empty);

        var footer = el("div", "bl4cm_footer");
        footer.appendChild(button("Close", close, "bl4cm_close"));
        ui.reset = button("Reset", function () {
            send({ do: "reset" });
        }, "bl4cm_reset");
        footer.appendChild(ui.reset);
        ui.hint = el("p", "bl4cm_hint");
        footer.appendChild(ui.hint);
        frame.appendChild(footer);

        ui.toast = el("p", "bl4cm_toast bl4cm_hidden");
        frame.appendChild(ui.toast);
        root.appendChild(frame);
        // Clicks on empty space mustn't reach the pause menu underneath
        onClick(root, function () {});
        document.body.appendChild(root);
        ui.root = root;
    }

    function setScope(scope) {
        if (view.scope === scope) return;
        view.scope = scope;
        send({ do: "view", scope: scope });
        render();
    }

    function open(own, scope) {
        if (!ui.root) return;
        view.open = true;
        view.own = !!own;
        if (scope) view.scope = scope;
        var pause = document.querySelector('[data-input-id="menu_pause_list"]');
        var overPause = !!pause && pause.getBoundingClientRect().height > 0;
        ui.root.classList.toggle("bl4cm_below_tabs", !overPause);
        ui.root.classList.remove("bl4cm_hidden");
        render();
        pollSoon();
    }

    function hide() {
        view.open = false;
        if (ui.root) ui.root.classList.add("bl4cm_hidden");
    }

    function close() {
        var own = view.own;
        hide();
        if (!own) return;
        if (ns.closeGameMenu) ns.closeGameMenu();
        else ext.http("POST", "/action", JSON.stringify({ op: "close_menus" }), null);
    }

    // endregion
    // region Cards

    function sourceRow() {
        var row = el("div", "bl4cm_src");
        row.rank = el("p", "bl4cm_src_rank");
        row.appendChild(row.rank);
        row.icon = el("div", "bl4cm_src_icon");
        row.iconArt = el("div", "bl4cm_src_icon_art");
        row.icon.appendChild(row.iconArt);
        row.appendChild(row.icon);
        var mid = el("div", "bl4cm_src_mid");
        var line = el("div", "bl4cm_src_line");
        row.name = el("p", "bl4cm_src_name");
        line.appendChild(row.name);
        row.elements = el("div", "bl4cm_src_elements");
        line.appendChild(row.elements);
        mid.appendChild(line);
        row.sub = el("p", "bl4cm_src_sub");
        mid.appendChild(row.sub);
        row.track = el("div", "bl4cm_src_track");
        row.fill = el("div", "bl4cm_src_fill");
        row.track.appendChild(row.fill);
        mid.appendChild(row.track);
        row.appendChild(mid);
        var right = el("div", "bl4cm_src_right");
        row.dmg = el("p", "bl4cm_src_dmg");
        right.appendChild(row.dmg);
        row.share = el("p", "bl4cm_src_share");
        right.appendChild(row.share);
        row.appendChild(right);
        return row;
    }

    function stat(parent, icon, label) {
        var box = el("div", "bl4cm_stat");
        var i = el("div", "bl4cm_stat_icon");
        i.style.backgroundImage = art(icon);
        box.appendChild(i);
        var col = el("div", "bl4cm_stat_col");
        box.value = el("p", "bl4cm_stat_value", "0");
        col.appendChild(box.value);
        col.appendChild(el("p", "bl4cm_stat_label", label));
        box.appendChild(col);
        parent.appendChild(box);
        return box;
    }

    function card(id) {
        var c = el("div", "bl4cm_card");
        c.appendChild(el("div", "bl4cm_card_glow"));
        c.appendChild(el("div", "bl4cm_card_stripe"));
        var head = el("div", "bl4cm_card_head");
        c.portrait = el("div", "bl4cm_portrait");
        head.appendChild(c.portrait);
        var who = el("div", "bl4cm_who");
        var nameLine = el("div", "bl4cm_name_line");
        c.name = el("p", "bl4cm_name");
        nameLine.appendChild(c.name);
        c.you = el("p", "bl4cm_you", "YOU");
        nameLine.appendChild(c.you);
        who.appendChild(nameLine);
        c.cls = el("p", "bl4cm_class");
        who.appendChild(c.cls);
        head.appendChild(who);
        var rank = el("div", "bl4cm_rank");
        c.crown = el("div", "bl4cm_crown");
        c.crown.style.backgroundImage = art(ICON_CROWN);
        rank.appendChild(c.crown);
        c.rank = el("p", "bl4cm_rank_text");
        rank.appendChild(c.rank);
        head.appendChild(rank);
        c.appendChild(head);

        c.total = el("p", "bl4cm_total", "0");
        c.appendChild(c.total);
        var share = el("div", "bl4cm_share");
        c.shareTrack = el("div", "bl4cm_share_track");
        c.shareFill = el("div", "bl4cm_share_fill");
        c.shareTrack.appendChild(c.shareFill);
        share.appendChild(c.shareTrack);
        c.shareText = el("p", "bl4cm_share_text");
        share.appendChild(c.shareText);
        c.appendChild(share);

        var stats = el("div", "bl4cm_stats");
        c.dps = stat(stats, ICON_DPS, "DPS");
        c.kills = stat(stats, ICON_SKULL, "KILLS");
        c.crit = stat(stats, ICON_KILLS, "CRIT DMG");
        c.big = stat(stats, ICON_BIG, "BIGGEST HIT");
        c.appendChild(stats);

        var srcHead = el("div", "bl4cm_src_head");
        c.srcTitle = srcHead.appendChild(el("p", "bl4cm_src_title", "TOP DAMAGE"));
        c.more = el("p", "bl4cm_more");
        srcHead.appendChild(c.more);
        c.appendChild(srcHead);
        c.rows = [];
        c.list = el("div", "bl4cm_src_list");
        for (var i = 0; i < 6; i++) {
            var row = sourceRow();
            c.rows.push(row);
            c.list.appendChild(row);
        }
        c.appendChild(c.list);
        c.none = el("p", "bl4cm_src_none", "No damage yet.");
        c.appendChild(c.none);
        var mix = el("div", "bl4cm_mix");
        mix.appendChild(el("p", "bl4cm_src_title", "DAMAGE MIX"));
        c.mixBar = el("div", "bl4cm_mix_bar");
        mix.appendChild(c.mixBar);
        c.mixLegend = el("div", "bl4cm_mix_legend");
        mix.appendChild(c.mixLegend);
        c.appendChild(mix);
        onClick(c, function () {
            view.expanded[id] = !view.expanded[id];
            render();
        });
        return c;
    }

    function sourceIcon(src, player) {
        if (src.kind === "item") return ITEM_ICONS[src.icon] || KIND_ICONS.gear;
        if (src.kind === "dot") return "ico_ui_art_elemental_damage/ico_ui_art_elemental_" + (src.element === "normal" ? "kinetic" : src.element) + ".png";
        if (src.kind === "skill" || src.kind === "companion") return player.trait || KIND_ICONS.skill;
        return KIND_ICONS[src.kind] || KIND_ICONS.other;
    }

    function fillRow(row, src, rank, player, tally) {
        setText(row.rank, String(rank));
        setBg(row.iconArt, sourceIcon(src, player));
        row.className = "bl4cm_src bl4cm_kind_" + src.kind + (src.rarity ? " bl4cm_rarity_" + src.rarity : "") +
            (src.kind === "dot" ? " bl4cm_elem_" + src.element : "") + (rank === 1 ? " bl4cm_src_first" : "");
        row.iconArt.className = "bl4cm_src_icon_art" + (src.kind === "item" && ["pistol", "smg", "assault", "shotgun", "sniper", "heavy", "turret"].indexOf(src.icon) >= 0 ? " bl4cm_wide" : "");
        setText(row.name, src.name);
        var sub = src.sub || "";
        if (src.kind === "item" && src.rarity) sub = src.rarity.charAt(0).toUpperCase() + src.rarity.slice(1) + " " + sub;
        setText(row.sub, sub + (src.hits ? "  |  " + commas(src.hits) + " hit" + (src.hits === 1 ? "" : "s") : ""));
        var elems = (src.elements || []).join(",");
        if (row.elements.dataset.e !== elems) {
            row.elements.dataset.e = elems;
            row.elements.innerHTML = "";
            (src.elements || []).forEach(function (e) {
                var dot = el("div", "bl4cm_elem_icon");
                dot.style.backgroundImage = art("ico_ui_art_elemental_damage/ico_ui_art_elemental_" + e + ".png");
                row.elements.appendChild(dot);
            });
        }
        setText(row.dmg, short(src.dmg));
        setText(row.share, pct(src.dmg, tally.srcTotal || tally.total));
        setWidth(row.fill, tally.top.length ? src.dmg / tally.top[0].dmg : 0);
    }

    function fillCard(c, player, tally, rank, teamTotal, isMvp, expanded, words) {
        setClass(c, "bl4cm_mvp", isMvp);
        setClass(c, "bl4cm_away", !player.here);
        setClass(c, "bl4cm_local", player.local);
        setBg(c.portrait, player.portrait || ICON_SKULL);
        setClass(c.portrait, "bl4cm_portrait_none", !player.portrait);
        setText(c.name, player.name);
        setClass(c.you, "bl4cm_hidden", !player.local);
        setText(c.cls, [player.hero, player.class].filter(Boolean).join("  |  ").toUpperCase() + (player.here ? "" : "  (LEFT)"));
        setText(c.rank, "#" + rank);
        countTo(c.total, tally.total, big);
        setWidth(c.shareFill, teamTotal ? tally.total / teamTotal : 0);
        setText(c.shareText, pct(tally.total, teamTotal) + " " + words.share);
        setText(c.srcTitle, words.sources);
        countTo(c.dps.value, tally.dps, short);
        countTo(c.kills.value, tally.kills, commas);
        setText(c.crit.value, pct(tally.crit, tally.srcTotal || tally.total));
        countTo(c.big.value, tally.big, short);
        var shown = expanded ? tally.top.length : Math.min(ROWS, tally.top.length);
        for (var i = 0; i < c.rows.length; i++) {
            var row = c.rows[i];
            var on = i < shown;
            setClass(row, "bl4cm_hidden", !on);
            if (on) fillRow(row, tally.top[i], i + 1, player, tally);
        }
        setClass(c.none, "bl4cm_hidden", tally.top.length > 0);
        var extra = tally.top.length - ROWS;
        setText(c.more, extra > 0 ? (expanded ? "SHOW LESS" : "+" + extra + " MORE") : "");
        fillMix(c, tally);
    }

    var MIX_NAMES = { guns: "GUNS", elemental: "ELEMENTAL", skills: "SKILLS", melee: "MELEE & SLAM", gear: "GEAR", other: "OTHER" };
    function fillMix(c, tally) {
        var mix = tally.mix || [];
        var sig = JSON.stringify(mix);
        if (c.mixSig === sig) return;
        c.mixSig = sig;
        var whole = mix.reduce(function (sum, m) {
            return sum + m[1];
        }, 0);
        // Same segment nodes in place when the categories stay the same (widths animate)
        var cats = mix.map(function (m) {
            return m[0];
        }).join(",");
        if (c.mixCats !== cats) {
            c.mixCats = cats;
            c.mixBar.innerHTML = "";
            c.mixLegend.innerHTML = "";
            c.mixSegs = {};
            c.mixTexts = {};
            mix.forEach(function (m) {
                c.mixSegs[m[0]] = c.mixBar.appendChild(el("div", "bl4cm_mix_seg bl4cm_mix_" + m[0]));
                var item = el("div", "bl4cm_mix_item");
                item.appendChild(el("div", "bl4cm_mix_dot bl4cm_mix_" + m[0]));
                c.mixTexts[m[0]] = item.appendChild(el("p", "bl4cm_mix_text"));
                c.mixLegend.appendChild(item);
            });
        }
        mix.forEach(function (m) {
            setWidth(c.mixSegs[m[0]], whole ? m[1] / whole : 0);
            setText(c.mixTexts[m[0]], MIX_NAMES[m[0]] + " " + pct(m[1], whole));
        });
    }

    // endregion
    // region Render

    function render() {
        if (!ui.root || !view.open || !state) return;
        try {
            renderAll();
        } catch (err) {
            log("render failed: " + err + " " + (err && err.stack));
        }
    }

    function renderAll() {
        var board = state.board;
        var report = state.report;
        var scope = view.scope === "session" ? "session" : view.scope === "boss" && report ? "boss" : "fight";
        var boss = scope === "boss";
        setClass(ui.tabBoss, "bl4cm_hidden", !report);
        setClass(ui.tabBoss, "bl4cm_active", boss);
        setClass(ui.tabFight, "bl4cm_active", scope === "fight");
        setClass(ui.tabSession, "bl4cm_active", scope === "session");
        setClass(ui.reset, "bl4cm_hidden", state.role === "client");
        setText(ui.hint, state.key ? state.key + " opens and closes this screen.  Click a player to see all their sources." : "Click a player to see all their sources.");

        var notice = "";
        if (state.role === "client" && state.host === "missing") notice = "The host doesn't run Carnage Meter: ask them to install it to see the board.";
        else if (state.role === "client" && state.host === "unknown") notice = "Waiting for the host. They need BL4 Direct and Carnage Meter for the board.";
        else if (state.role === "client" && state.stale) notice = "Waiting for the host's board...";
        else if (board && board.error) notice = "Can't count damage on this game version (" + board.error + ").";
        else if (board && !board.listening) notice = "Not listening yet: load into the world.";
        else if (state.role === "client") notice = "Live from the host's game.";
        setText(ui.notice, notice);

        var players = boss ? report.players : (board && board.players) || [];
        var info = boss ? null : board ? board[scope] : null;
        var live = scope === "fight" && board && board.fight.live;
        setClass(ui.live, "bl4cm_hidden", !live);
        var sub;
        if (boss) sub = report.boss.toUpperCase() + "  " + clock(report.seconds);
        else if (scope === "fight") {
            sub = board && board.fight.ago !== null && board.fight.ago !== undefined ? (live ? "THIS FIGHT  " : "LAST FIGHT  ") + clock(info.seconds) : "THIS FIGHT";
            if (live && board.fight.boss) sub += "  |  VS " + board.fight.boss.toUpperCase();
        } else sub = "SESSION  " + clock(info ? info.seconds : 0);
        setText(ui.subtitleText, sub + "  |  " + players.length + " VAULT HUNTER" + (players.length === 1 ? "" : "S"));
        setText(ui.title, boss ? "BOSS DEFEATED" : "CARNAGE REPORT");
        setText(ui.teamLabel, boss ? "DAMAGE TO " + report.boss.toUpperCase() : "TEAM DAMAGE");
        setClass(ui.root, "bl4cm_boss_mode", boss);
        setClass(ui.bossPic, "bl4cm_hidden", !(boss && report.picture));
        if (boss && report.picture) setBg(ui.bossPic, report.picture);
        var words = boss ? { share: "OF BOSS DAMAGE", sources: "TOP DAMAGE THIS FIGHT" } : { share: "OF TEAM DAMAGE", sources: "TOP DAMAGE" };

        var ranked = players.map(function (p) {
            if (!boss) return { p: p, t: p[scope] };
            // The boss's damage is the headline; the rest is the whole fight's
            var r = p.report;
            var t = {};
            for (var k in r) if (Object.prototype.hasOwnProperty.call(r, k)) t[k] = r[k];
            t.total = r.boss;
            t.dps = r.boss_dps;
            t.srcTotal = r.total;
            return { p: p, t: t };
        }).sort(function (a, b) {
            return b.t.total - a.t.total || (a.p.local ? -1 : 1);
        });
        var team = ranked.reduce(function (sum, r) {
            return sum + r.t.total;
        }, 0);
        countTo(ui.teamTotal, team, big);
        var any = team > 0;
        setClass(ui.empty, "bl4cm_hidden", any);
        setClass(ui.players, "bl4cm_hidden", !any);
        if (!any) {
            setText(ui.emptyText, scope === "fight" ? "Hit something hostile: every player's damage in your game shows up here, live." :
                "Nothing yet this session. Every player's damage in your game shows up here.");
            return;
        }
        setClass(ui.players, "bl4cm_count_" + ranked.length, true);
        ["1", "2", "3", "4"].forEach(function (n) {
            if (Number(n) !== ranked.length) ui.players.classList.remove("bl4cm_count_" + n);
        });
        var keep = {};
        ranked.forEach(function (r, i) {
            var id = String(r.p.id);
            keep[id] = true;
            var c = ui.cards[id];
            if (!c) c = ui.cards[id] = card(id);
            fillCard(c, r.p, r.t, i + 1, team, i === 0 && (ranked.length > 1 || boss) && r.t.total > 0, !!view.expanded[id], words);
            // In rank order (Gameface has no CSS order): only move the ones out of place
            if (ui.players.children[i] !== c) ui.players.insertBefore(c, ui.players.children[i] || null);
        });
        Object.keys(ui.cards).forEach(function (id) {
            if (!keep[id]) {
                var gone = ui.cards[id];
                if (gone.parentNode) gone.parentNode.removeChild(gone);
                delete ui.cards[id];
            }
        });
    }

    // endregion
    // region Toast

    var toastTimer = null;
    function toast(text) {
        if (!ui.toast || !text) return;
        ui.toast.textContent = text;
        ui.toast.classList.remove("bl4cm_hidden");
        if (toastTimer !== null) clearTimeout(toastTimer);
        toastTimer = setTimeout(function () {
            toastTimer = null;
            if (ui.toast) ui.toast.classList.add("bl4cm_hidden");
        }, TOAST_MS);
    }

    // endregion
    // region Link

    var pollTimer = null;
    function schedule(ms) {
        if (pollTimer !== null) clearTimeout(pollTimer);
        pollTimer = setTimeout(function () {
            pollTimer = null;
            if (alive()) poll();
        }, ms);
    }
    function pollSoon() {
        schedule(20);
    }

    function poll(done) {
        ext.get(KEY, view.open, function (snap) {
            if (!alive()) return;
            if (snap && snap.state) onState(snap.state);
            schedule(view.open ? POLL_OPEN_MS : POLL_IDLE_MS);
            if (done) done();
        });
    }

    // The page just woke up (a game menu opened): the Mod Menu keeps it hidden until this answers,
    // so the open key's screen shows without the pause menu first (menu.js, ns.wake)
    function onWake(done) {
        if (pollTimer !== null) {
            clearTimeout(pollTimer);
            pollTimer = null;
        }
        poll(done);
    }
    (ns.wake || (ns.wake = { hooks: {} })).hooks[KEY] = onWake;

    function onState(next) {
        var first = state === null;
        state = next;
        if (first && cm.lastOpenId === undefined) {
            // Fresh page: only act on requests made from now on (or a fresh one, just below)
            cm.lastClose = next.close;
            cm.lastMenusClosed = next.menus_closed;
            cm.lastToast = next.toast.id;
            if (next.view && next.view.scope) view.scope = next.view.scope;
            cm.lastPyScope = next.view ? next.view.scope : null;
        }
        var pyScope = next.view ? next.view.scope : null;
        if (pyScope !== cm.lastPyScope) {
            cm.lastPyScope = pyScope;
            // A boss died (Boss Report), or a newer fight began (back to This Fight)
            if (pyScope === "boss" || pyScope === "fight") view.scope = pyScope;
        }
        if (next.close !== cm.lastClose) {
            cm.lastClose = next.close;
            if (view.open) close();
        }
        if (next.menus_closed !== cm.lastMenusClosed) {
            cm.lastMenusClosed = next.menus_closed;
            if (view.open) hide();
        }
        if (next.open.id !== cm.lastOpenId) {
            cm.lastOpenId = next.open.id;
            if (next.open.fresh) {
                open(next.open.own, next.open.scope);
                send({ do: "opened", id: next.open.id });
            }
        }
        if (next.toast.id !== cm.lastToast) {
            cm.lastToast = next.toast.id;
            if (view.open) toast(next.toast.text);
        }
        render();
    }

    // endregion
    // region Stylesheet and pause menu entry

    function checkCss() {
        ns.get(BASE + "meter.css", function (css) {
            if (!alive()) return;
            if (css && css !== cm.cssText) {
                cm.cssText = css;
                var style = document.getElementById("bl4cm_css");
                if (!style) {
                    style = el("style");
                    style.id = "bl4cm_css";
                    document.head.appendChild(style);
                }
                style.textContent = css;
            }
            // A release can't change it: read once
            if (ns.dev !== false) later(checkCss, CSS_MS);
        });
    }

    // A "Carnage Report" entry in the pause menu, under the Mod Menu's / Kairosdex's entries
    function injectPauseEntry() {
        var list = document.querySelector('[data-input-id="menu_pause_list"]');
        if (!list || list.querySelector(".bl4cm_pause_entry")) return;
        var options = list.querySelector('div[name="Options Button"]');
        var optionsBtn = options && options.firstElementChild;
        if (!options || !optionsBtn) return;
        var after = list.querySelector(".bl4dex_pause_entry") || list.querySelector(".bl4mm_pause_entry") || options;
        var entry = el("div", options.className + " bl4cm_pause_entry");
        var slot = el("div", optionsBtn.className);
        var b = button("Carnage Report", function () {
            open(false);
        });
        var theirs = optionsBtn.querySelector(".button_txt");
        var label = b.querySelector(".button_txt");
        if (theirs && label) label.className = theirs.className;
        var native = options.querySelector(".list_btn");
        if (native) {
            var cs = getComputedStyle(native);
            if (cs.width && cs.width !== "auto") b.style.width = cs.width;
            if (cs.height && cs.height !== "auto") b.style.height = cs.height;
            b.style.position = "relative";
            b.style.flex = "0 0 auto";
        }
        b.classList.remove("bl4cm_btn");
        slot.appendChild(b);
        entry.appendChild(slot);
        after.parentNode.insertBefore(entry, after.nextSibling);
    }

    function watchPauseMenu() {
        try {
            injectPauseEntry();
        } catch (err) {
            log("pause entry: " + err);
        }
        later(watchPauseMenu, 500);
    }

    // endregion

    ext.teardowns[KEY] = function () {
        cm.gen = 0;
        if (ns.wake && ns.wake.hooks[KEY] === onWake) delete ns.wake.hooks[KEY];
        timers.forEach(clearTimeout);
        if (pollTimer !== null) clearTimeout(pollTimer);
        if (toastTimer !== null) clearTimeout(toastTimer);
        if (countFrame !== null) cancelAnimationFrame(countFrame);
        var nodes = document.querySelectorAll(".bl4cm_root, .bl4cm_pause_entry");
        for (var i = 0; i < nodes.length; i++) nodes[i].parentNode.removeChild(nodes[i]);
        docListeners.forEach(function (l) {
            document.removeEventListener(l[0], l[1]);
        });
    };

    var wasOpen = view.open;
    view.open = false;
    checkCss();
    try {
        build();
    } catch (err) {
        log("build failed: " + err + " " + (err && err.stack));
        return;
    }
    watchPauseMenu();
    log("ready");
    poll();
    if (wasOpen) open(view.own);
})();
