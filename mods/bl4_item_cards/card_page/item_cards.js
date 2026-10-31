"use strict";

// BL4 Direct detailed item cards: the skill list on class mod cards, and where legendaries drop.
//
// The game gives the card each skill's name, "+N" points, icon, description and skill tree colour.
// The mod's Python side adds skills_live.json next to this file with, per skill, the max rank,
// your points, and the skill's numbers formatted for every rank (plus your UVH level). Each skill
// is drawn as:
//
//     [icon] Name                    0/5  +2
//     description
//     Splash Damage: +21%            (or "+14% -> +35%" when you already have points in it)
//
// Legendary cards get a "Drops from" block from drops.json (made from the game's loot data by
// tools/gen_item_cards.py): each boss with its drop chance at your UVH level and its Big Encore
// chance and location, or "World drop".
//
// Cards can be scaled (the mod's "Card size" option), and one taller than the screen scrolls with
// the mouse wheel.
//
// This file and item_cards.css are loose files (OakGame/Content/uiresources/bl4direct/). Since
// 0.2.0 the BL4 UI Link (../uilink, key "itemcards") runs this file in every game page (it's no
// longer loaded by a replaced item_card.html in a pak), and runs it again when it changes in dev
// builds. Running it again replaces the drawing code and redraws open cards; the hook into the
// game's card class is only installed once. The link tears it down when the mod is turned off
// (teardown below). What the old page added to the card's markup, this adds itself (decorate):
// the box our skill list goes in, each secondary stat's name, rendered markup in the banner and
// red text. In dev builds it also reloads the styles and drop data when they change.

(function () {
    var link = (typeof bl4linkNs !== "undefined" && bl4linkNs) || null;
    var LINK_KEY = "itemcards";
    var BASE = "coui://uiresources/bl4direct/";
    var CSS_URL = BASE + "item_cards.css";
    var LIVE_URL = BASE + "skills_live.json";
    var DROPS_URL = BASE + "drops.json";
    // The Weapondex's boss pictures (bl4_weapondex/bake.py): <id>.png, listed {id: version} in its
    // index.json. Without the Weapondex (or its pictures) the skull stays; looked for again this often
    var BOSS_PICTURES = "coui://uiresources/bl4weapondex/bosses/";
    var BOSS_PICTURES_RETRY_MS = 60000;
    var HOTKEY_URL = BASE + "hotkey.json";
    // Card size and "fit to screen" from the mod's options
    var SETTINGS_URL = BASE + "settings.json";
    var SETTINGS_POLL_MS = 1000;
    var LAYOUT_MS = 50;
    // The part of the screen a card may use, as fractions of its height from the top and bottom
    // edges: menus draw their tab bars over the top ~13% and their button prompts over the bottom
    // ~5.5% (inventory, measured at 16:9). One wheel notch is SCROLL_STEP of the screen.
    var MENU_MARGINS = { top: 0.135, bottom: 0.06 };
    var HUD_MARGINS = { top: 0.02, bottom: 0.02 };
    var SCROLL_STEP = 0.12;
    // The card's decorative lines hang this far below its box (rem, measured ~37px at 1318px)
    var DECOR_BOTTOM_REM = 3;
    // Messages to Python ride on UI sound events (see encore_waypoint.py): ScriptEvent_Spatial
    // calls this ui_script function with position {x, y}; the inventory page's script has the
    // first, the HUD's the second. x = -(MARK + op * 0x10000 + arg), y = the key press number.
    var CHANNELS = ["ClickPistol", "FailToUseGadget"];
    var MARK = 1 << 23;
    var OP_PIN_MACHINE = 1;
    var OP_TRAVEL = 2;
    var HOTKEY_POLL_MS = 250;
    var POPUP_MS = 2600;
    // The map's own "waypoint placed" sound; the inventory page has the map widget loaded
    var WAYPOINT_SOUND = "AddWaypoint";
    var PLAYER_KEY = "$player";
    var LIVE_MAX_AGE_MS = 1000;
    var RELOAD_CHECK_MS = 2000;
    var MAX_TRACKED_CARDS = 8;
    // Waiting for the game's card class: this often for a minute, then this often (a page may
    // load the card widget later, or never)
    var INSTALL_FAST_MS = 100;
    var INSTALL_FAST_TRIES = 600;
    var INSTALL_SLOW_MS = 2000;
    // Banner / red text with markup tags ("[secondary]...[/secondary]") we render ourselves
    var MARKUP_TAG = /\[\/?\w+[^\]]*\]/;

    // Survives reloads of this file
    var ns = window.bl4dItemCards;
    if (!ns) {
        ns = window.bl4dItemCards = { live: null, loadedAt: 0, loading: false, waiting: [], cards: [], generation: 0 };
    }
    var generation = ++ns.generation;
    ns.off = false;

    function current() {
        return ns.generation === generation && !ns.off;
    }

    function get(url, done) {
        var xhr = new XMLHttpRequest();
        xhr.onreadystatechange = function () {
            if (xhr.readyState !== 4) return;
            // Loose coui:// files report status 0 or 200
            done((xhr.status === 200 || xhr.status === 0) && xhr.responseText ? xhr.responseText : null);
        };
        try {
            xhr.open("GET", url, true);
            xhr.send();
        } catch (err) {
            done(null);
        }
    }

    function loadLive(done) {
        if (ns.live && Date.now() - ns.loadedAt < LIVE_MAX_AGE_MS) {
            done();
            return;
        }
        ns.waiting.push(done);
        if (ns.loading) return;
        ns.loading = true;
        get(LIVE_URL, function (text) {
            if (text) {
                try {
                    ns.live = JSON.parse(text);
                } catch (err) {
                    // keep the last good copy; the mod replaces the file atomically, so this is rare
                }
            }
            ns.loadedAt = Date.now();
            ns.loading = false;
            var callbacks = ns.waiting;
            ns.waiting = [];
            for (var i = 0; i < callbacks.length; i++) callbacks[i]();
        });
    }

    // region Drawing

    function markup(text) {
        if (typeof MarkupMgr !== "undefined") {
            return MarkupMgr.ResolveMarkupText(text, { attributes: [{ name: "max_num_glyph", value: 1 }] });
        }
        return String(text).replace(/\[\/?[a-z_]+\]/g, "");
    }

    function element(tag, className, text) {
        var el = document.createElement(tag);
        el.className = className;
        if (text !== undefined) el.textContent = text;
        return el;
    }

    // A div, not a p, wherever markup goes in: it comes back as a <p>, and a p can't contain a p
    function markupElement(className, text) {
        var el = element("div", className);
        el.innerHTML = markup(text);
        return el;
    }

    function points(entry) {
        var match = /-?\d+/.exec(String(entry.value));
        return match ? parseInt(match[0], 10) : 0;
    }

    function statLines(info, bonus) {
        var spent = info.spent || 0;
        var lines = element("div", "bl4d_cm_skill_stats");
        for (var i = 0; i < info.lines.length; i++) {
            var line = info.lines[i];
            var text = line.text;
            for (var placeholder in line.values) {
                var values = line.values[placeholder];
                var last = values.length - 1;
                var now = values[Math.min(spent, last)];
                var withMod = values[Math.min(spent + bonus, last)];
                var shown = spent > 0 && now !== withMod ? now + " \u2192 " + withMod : withMod;
                text = text.split(placeholder).join(shown);
            }
            lines.appendChild(markupElement("bl4d_cm_stat_line", text));
        }
        return lines;
    }

    function skillRow(entry, info) {
        var row = element("div", "bl4d_cm_skill " + (entry.comparison || ""));
        var head = element("div", "bl4d_cm_skill_head");
        var icon = element("div", "bl4d_cm_skill_icon");
        if (entry.image) icon.style.backgroundImage = "url(" + entry.image + ")";
        head.appendChild(icon);
        head.appendChild(element("p", "bl4d_cm_skill_name", entry.ident));
        if (info) {
            head.appendChild(element("p", "bl4d_cm_skill_rank", info.spent === null ? "max " + info.max : info.spent + "/" + info.max));
        }
        head.appendChild(element("p", "bl4d_cm_skill_points", entry.value));
        row.appendChild(head);
        if (entry.description) row.appendChild(markupElement("bl4d_cm_skill_desc", entry.description));
        if (info && info.lines.length) row.appendChild(statLines(info, points(entry)));
        return row;
    }

    // region What the old page added to the card's markup
    //
    // Added once and never moved (moving our nodes next to the game's data-bound ones made it
    // insert its red text again), only refilled; marked data-bl4d so teardown finds them. The old
    // pak's page (until its removal takes effect at the next start) already has a host, bound
    // labels and markup bindings: those are left alone, nothing is added twice.

    function ours(el) {
        el.setAttribute("data-bl4d", "1");
        return el;
    }

    // True when it made a new box. data-bind-if takes the card's sections out of the page while
    // they don't apply (seen in game: a hidden card has no class mod row), and ours with them
    function ensureHost(card) {
        if (card.querySelector(".bl4d_cm_skills")) return false;  // ours, or the old page's
        var row = card.querySelector(".item_card_class_mod_stat");
        if (!row || !row.parentNode) return false;
        var host = ours(element("div", "bl4d_cm_skills"));
        host.setAttribute("name", "BL4D class mod skills");
        row.parentNode.insertBefore(host, row.nextSibling);
        return true;
    }

    // The stat's name (loc_text, e.g. "Splash Radius") in front of each compact secondary stat
    function statLabels(card, data) {
        var wrapper = card.querySelector(".item_card_tertiary_stat_wrapper");
        if (!wrapper) return;
        var entries = data && data.tertiary_stat_entries;
        var rows = wrapper.querySelectorAll(".item_card_tertiary_stat_group");
        for (var i = 0; i < rows.length; i++) {
            var value = rows[i].querySelector(".item_card_secondary_stat_value");
            if (!value) continue;
            var label = rows[i].querySelector(".bl4d_stat_label");
            if (label && !label.hasAttribute("data-bl4d")) continue;  // the old page's bound one
            var entry = entries && i < entries.length ? entries[i] : null;
            var text = entry && entry.loc_text ? String(entry.loc_text) : "";
            if (!text) {
                if (label) label.parentNode.removeChild(label);
                continue;
            }
            if (!label) {
                label = ours(element("p", "bl4d_stat_label"));
                value.parentNode.insertBefore(label, value);
            }
            if (label.textContent !== text) label.textContent = text;
        }
    }

    // The game's element bound to `$data.<field>` (bindings point at the instance's model)
    function boundTo(card, field) {
        var cache = card.bl4dBound || (card.bl4dBound = {});
        var el = cache[field];
        if (el && card.contains(el)) return el;
        el = null;
        var suffix = "." + field + "}}";
        var all = card.querySelectorAll("[data-bind-value]");
        for (var i = 0; i < all.length; i++) {
            var value = all[i].getAttribute("data-bind-value") || "";
            if (value.length >= suffix.length && value.slice(-suffix.length) === suffix) {
                el = all[i];
                break;
            }
        }
        cache[field] = el;
        return el;
    }

    // Banner ("Currently Equipped" ...) and red flavour text can carry markup tags, which the
    // game's plain value binding shows as text: while they do, our rendered copy shows instead
    function markupField(card, data, field) {
        var source = boundTo(card, field);
        if (!source || !source.parentNode) return;
        var copy = source.bl4dCopy;
        if (copy && copy.parentNode !== source.parentNode) copy = source.bl4dCopy = null;
        var text = data && data[field] ? String(data[field]) : "";
        if (!text || !MARKUP_TAG.test(text)) {
            if (copy && copy.style.display !== "none") copy.style.display = "none";
            if (source.classList.contains("bl4d_markup_hidden")) source.classList.remove("bl4d_markup_hidden");
            return;
        }
        if (!copy) {
            copy = source.bl4dCopy = ours(document.createElement("div"));
            source.parentNode.insertBefore(copy, source.nextSibling);
        }
        // Look like the game's element (its bound classes change with the banner type)
        var classes = source.className.replace(/\s*\bbl4d_markup_hidden\b/g, "") + " bl4d_markup";
        if (copy.className !== classes) copy.className = classes;
        if (copy.bl4dText !== text) {
            copy.bl4dText = text;
            copy.innerHTML = markup(text);
        }
        if (copy.style.display) copy.style.display = "";
        if (!source.classList.contains("bl4d_markup_hidden")) source.classList.add("bl4d_markup_hidden");
    }

    function decorate(card) {
        var data = card.DataModel;
        try {
            // A new box after the card's update (the layout loop, the next frame): fill it now
            if (ensureHost(card) && ns.live) render(card);
        } catch (err) {}
        try {
            statLabels(card, data);
        } catch (err) {}
        try {
            markupField(card, data, "bannertext");
            markupField(card, data, "redtext");
        } catch (err) {}
    }

    // Takes back everything this script added to a card
    function undecorate(card) {
        var added = card.querySelectorAll("[data-bl4d], .bl4d_drops, .bl4d_glyph, .bl4d_glyph_travel, .bl4d_fw");
        for (var i = 0; i < added.length; i++) {
            if (added[i].parentNode) added[i].parentNode.removeChild(added[i]);
        }
        var oldHost = card.querySelector(".bl4d_cm_skills");  // the old page's: empty it
        if (oldHost) oldHost.innerHTML = "";
        var hidden = card.querySelectorAll(".bl4d_markup_hidden");
        for (var j = 0; j < hidden.length; j++) hidden[j].classList.remove("bl4d_markup_hidden");
        card.classList.remove("bl4d_active");
        card.bl4dBound = null;
        var target = card.querySelector(".item_card");
        if (target && target.bl4dTransform) setTransform(target, "");
    }

    // endregion

    function render(card) {
        var host = card.querySelector(".bl4d_cm_skills");
        if (!host) return;
        var data = card.DataModel;
        var entries = data && data.primary_stat_entries;
        if (!data || data.itemtype !== "classmod" || data.abbreviated || !entries || !entries.length) {
            host.innerHTML = "";
            card.classList.remove("bl4d_active");
            return;
        }
        host.innerHTML = "";
        for (var i = 0; i < entries.length; i++) {
            host.appendChild(skillRow(entries[i], ns.live && ns.live[entries[i].loc_text]));
        }
        // Only now hide the game's compact icon row (see item_cards.css)
        card.classList.add("bl4d_active");
    }

    // endregion
    // region Drop sources

    function loadDrops(done) {
        if (ns.drops) {
            loadBossPictures(done);
            return;
        }
        get(DROPS_URL, function (text) {
            try {
                ns.drops = text ? JSON.parse(text) : null;
                ns.dropsText = text;
            } catch (err) {
                ns.drops = null;
            }
            loadBossPictures(done);
        });
    }

    function loadBossPictures(done) {
        if (ns.bossPictures || (ns.bossPicturesAt && Date.now() - ns.bossPicturesAt < BOSS_PICTURES_RETRY_MS)) {
            done();
            return;
        }
        ns.bossPicturesAt = Date.now();
        get(BOSS_PICTURES + "index.json", function (text) {
            var map = null;
            try {
                map = text ? JSON.parse(text) : null;
            } catch (err) {}
            if (Array.isArray(map)) {
                // A list of ids from older bakes
                var ids = map;
                map = {};
                for (var i = 0; i < ids.length; i++) map[ids[i]] = 1;
            }
            ns.bossPictures = map && typeof map === "object" ? map : null;
            done();
        });
    }

    // The Weapondex's id for a boss name: its words joined by "-" ("Ol' Thrash" -> "ol-thrash")
    function bossSlug(name) {
        var words = String(name || "").toLowerCase().replace(/'/g, "").match(/[a-z0-9]+/g);
        return words ? words.join("-") : "boss";
    }

    // The boss's picture as a tiny icon before its name, else the skull
    function bossIcon(name) {
        var id = bossSlug(name);
        var version = ns.bossPictures && ns.bossPictures[id];
        if (!version) return element("div", "bl4d_drop_icon bl4d_drop_icon_boss");
        var icon = element("div", "bl4d_drop_icon bl4d_drop_boss_picture");
        icon.style.backgroundImage = "url(" + BOSS_PICTURES + id + ".png?v=" + version + ")";
        return icon;
    }

    // The card shows the full name, with rolled prefixes in front ("Jobbed Down-Bad Ripple",
    // "Electric Avatar"), so try dropping leading words. A name can belong to a weapon and a class mod.
    function dropEntries(data) {
        var name = String(data.name || "").trim().toLowerCase();
        if (!name || !ns.drops) return null;
        var classMod = data.itemtype === "classmod";
        var words = name.split(/\s+/);
        for (var i = 0; i < words.length; i++) {
            var list = ns.drops[words.slice(i).join(" ")];
            if (list) {
                var matching = list.filter(function (e) { return (e.item.indexOf("classmod") === 0) === classMod; });
                if (matching.length) return matching;
            }
        }
        return null;
    }

    function percent(chance) {
        var value = chance * 100;
        var text = value < 1 ? value.toFixed(2) : value.toFixed(1);
        return text.replace(/\.?0+$/, "") + "%";
    }

    // [skull] Boss name
    //         Per kill     9.5%   UVH 1
    //  [icon] Big Encore   30%    Location
    //         Waypoint set on this machine
    // The label column has a fixed width so the chances line up under each other.
    function chanceRow(className, iconClass, label, rate, note) {
        var row = element("div", "bl4d_drop_row bl4d_drop_chance " + className);
        var slot = element("div", "bl4d_drop_slot");
        if (iconClass) slot.appendChild(element("div", "bl4d_drop_icon " + iconClass));
        row.appendChild(slot);
        row.appendChild(element("p", "bl4d_drop_label", label));
        row.appendChild(element("p", "bl4d_drop_rate", rate));
        if (note) row.appendChild(element("p", "bl4d_drop_note", note));
        return row;
    }

    function sourceRows(source, uvh) {
        var rows = [];
        var head = element("div", "bl4d_drop_row bl4d_drop_head");
        head.appendChild(bossIcon(source.boss));
        head.appendChild(element("p", "bl4d_drop_boss", source.boss));
        rows.push(head);
        if (source.uvh0 !== undefined) {
            var rate, note;
            if (uvh === null || uvh === undefined) {
                rate = percent(source.uvh0);
                note = "UVH 5: " + percent(source.uvh5);
            } else {
                // The game's chance grows linearly with the UVH level
                rate = percent(source.uvh0 + (source.uvh5 - source.uvh0) * uvh / 5);
                note = uvh > 0 ? "UVH " + uvh : "";
            }
            rows.push(chanceRow("bl4d_drop_kill", "bl4d_drop_icon_kill", "Per kill", rate, note));
        }
        if (source.encore !== undefined) {
            rows.push(chanceRow("bl4d_drop_encore", "bl4d_drop_icon_encore", "Big Encore", percent(source.encore), source.encore_map || ""));
            if (source.encore_id !== undefined && pinnedHere(source.encore_id)) {
                var done = element("div", "bl4d_drop_row bl4d_drop_waypoint");
                done.appendChild(element("p", "bl4d_drop_waypoint_text", waypointText()));
                rows.push(done);
            }
        }
        return rows;
    }

    // region Big Encore waypoint

    function pinnedHere(machine) {
        var pinned = ns.hotkey && ns.hotkey.pinned;
        return !!pinned && pinned.id === machine;
    }

    function waypointText() {
        var pinned = ns.hotkey && ns.hotkey.pinned;
        return "Waypoint set on this machine" + (pinned && pinned.elsewhere ? " (shows on its map)" : "");
    }

    // The game's own key art, e.g. Glyphs/assets/PC/Ico_Hint_PC_Key_J.png
    function keyImage(key) {
        var names = { Zero: "0", One: "1", Two: "2", Three: "3", Four: "4", Five: "5", Six: "6", Seven: "7", Eight: "8", Nine: "9" };
        return "coui://uiresources/Glyphs/assets/PC/Ico_Hint_PC_Key_" + (names[key] || key) + ".png";
    }

    // "[J] Encore Waypoint  [K] Travel to Encore" next to the game's Select / Inspect / Mark
    // prompts. One entry each per card, added after the game's list and only ever updated, never
    // moved (see renderDrops).
    function promptEntry(wrapper, className, key, text) {
        var prompt = wrapper.querySelector("." + className);
        if (!key || !text) {
            if (prompt && prompt.style.display !== "none") prompt.style.display = "none";
            return;
        }
        if (!prompt) {
            prompt = element("div", "tooltip_button_glyph_container " + className);
            var inner = element("div", "tooltip_button_glyph_container");
            inner.appendChild(element("div", "bl4d_glyph_key"));
            inner.appendChild(element("div", "tooltip_button_glyph_text"));
            prompt.appendChild(inner);
            wrapper.appendChild(prompt);
        }
        if (prompt.style.display) prompt.style.display = "";
        var icon = prompt.querySelector(".bl4d_glyph_key");
        // Match the size of the game's own key icons on this card
        var native = wrapper.querySelector("gbx-glyph");
        var size = native ? native.getBoundingClientRect() : null;
        if (size && size.height > 0 && icon.bl4dSize !== size.width + "x" + size.height) {
            icon.bl4dSize = size.width + "x" + size.height;
            icon.style.width = size.width + "px";
            icon.style.height = size.height + "px";
        }
        // Only write what changed: this also runs from the layout loop
        if (icon.bl4dKey !== key) {
            icon.bl4dKey = key;
            icon.style.backgroundImage = "url(" + keyImage(key) + ")";
        }
        var label = prompt.querySelector(".tooltip_button_glyph_text");
        if (label.textContent !== text) label.textContent = text;
    }

    function renderPrompt(card, machine) {
        var wrapper = card.querySelector(".item_card_glyph_wrapper");
        if (!wrapper) return;
        var status = ns.hotkey || {};
        var has = machine !== null;
        promptEntry(wrapper, "bl4d_glyph", status.key, has && (pinnedHere(machine) ? "Encore Waypoint Set" : "Encore Waypoint"));
        promptEntry(wrapper, "bl4d_glyph_travel", status.travel_key, has && "Travel to Encore");
    }

    function send(op, arg, value) {
        var x = -(MARK + op * 0x10000 + arg);
        WidgetMgr.WidgetMap.forEach(function (widget) {
            if (typeof widget.TriggerWidgetEvent !== "function") return;
            for (var i = 0; i < CHANNELS.length; i++) widget.TriggerWidgetEvent("ScriptEvent_Spatial", CHANNELS[i], x, value);
        });
    }

    function firstMachine(data) {
        var entries = (data && !data.abbreviated && dropEntries(data)) || [];
        for (var j = 0; j < entries.length; j++) {
            for (var k = 0; k < entries[j].sources.length; k++) {
                if (entries[j].sources[k].encore_id !== undefined) return entries[j].sources[k].encore_id;
            }
        }
        return null;
    }

    // The machine to send the waypoint to: the first Big Encore source on the card being shown
    function shownMachine() {
        for (var i = ns.cards.length - 1; i >= 0; i--) {
            var card = ns.cards[i];
            var data = card.DataModel;
            // The card element itself measures 0 high and the model's "visible" flag flickers;
            // the tooltip box inside it is what has a size while the card is on screen
            var tooltip = card.querySelector(".item_card_tooltip");
            if (!data || !data.name || !tooltip || tooltip.getBoundingClientRect().height <= 0) continue;
            return firstMachine(data);
        }
        return null;
    }

    function playSound(id) {
        WidgetMgr.WidgetMap.forEach(function (widget) {
            if (typeof widget.TriggerWidgetEvent === "function") widget.TriggerWidgetEvent("ScriptEvent_Spatial", id, 0, 0);
        });
    }

    var TRIP_TEXT = {
        travelling: ["Fast travelling", "To the nearest station, then on to the machine"],
        arrived: ["Arrived", null],
        blocked: ["Couldn't get closer", "No free spot next to the machine: follow the waypoint"],
        failed: ["Trip stopped", "The map didn't load in time: follow the waypoint"],
        host_only: ["Needs the host", "In co-op the host needs BL4 Direct to move you; the waypoint still works"],
    };

    function showTrip(trip) {
        var text = TRIP_TEXT[trip.state];
        if (!text) return;
        var where = "Moxxi's Big Encore \u00b7 " + trip.boss + (trip.place ? " (" + trip.place + ")" : "");
        popup(text[0], where, text[1] || "");
        if (trip.state === "arrived" || trip.state === "travelling") playSound(WAYPOINT_SOUND);
    }

    function popup(title, text, note) {
        var box = document.getElementById("bl4d_popup");
        if (!box) {
            box = element("div", "bl4d_popup bl4d_popup_hidden");
            box.id = "bl4d_popup";
            var head = element("div", "bl4d_popup_head");
            head.appendChild(element("div", "bl4d_drop_icon bl4d_drop_icon_encore"));
            head.appendChild(element("p", "bl4d_popup_title"));
            box.appendChild(head);
            box.appendChild(element("p", "bl4d_popup_text"));
            box.appendChild(element("p", "bl4d_popup_note"));
            document.body.appendChild(box);
        }
        box.querySelector(".bl4d_popup_title").textContent = title;
        box.querySelector(".bl4d_popup_text").textContent = text;
        box.querySelector(".bl4d_popup_note").textContent = note;
        box.classList.remove("bl4d_popup_hidden");
        clearTimeout(ns.popupTimer);
        ns.popupTimer = setTimeout(function () { box.classList.add("bl4d_popup_hidden"); }, POPUP_MS);
    }

    // A box in the corner like the game's own notices, gone after a couple of seconds
    function confirmWaypoint(pinned) {
        popup(
            "Waypoint set",
            "Moxxi's Big Encore \u00b7 " + pinned.boss + (pinned.place ? " (" + pinned.place + ")" : ""),
            pinned.elsewhere ? "On another map: it shows up when you travel there" : "Follow the route on your map and compass"
        );
        playSound(WAYPOINT_SOUND);
    }

    function pollHotkey() {
        if (ns.hotkeyGeneration !== generation || !current()) return;  // a newer copy took over, or off
        get(HOTKEY_URL, function (text) {
            if (ns.hotkeyGeneration !== generation || !current()) return;
            if (text && text === ns.hotkeyText) {
                // Unchanged (most polls): everything below follows from the text alone
                setTimeout(pollHotkey, HOTKEY_POLL_MS);
                return;
            }
            var status = null;
            try {
                status = text ? JSON.parse(text) : null;
            } catch (err) {}
            if (status) ns.hotkeyText = text;
            if (status) {
                var before = ns.hotkey;
                ns.hotkey = status;
                if (before && status.presses !== before.presses) {
                    var machine = shownMachine();
                    if (machine !== null) {
                        send(OP_PIN_MACHINE, machine, status.presses);
                        ns.sentSeq = status.presses;
                    }
                }
                if (before && status.travel_presses !== before.travel_presses) {
                    var target = shownMachine();
                    if (target !== null) send(OP_TRAVEL, target, status.travel_presses);
                }
                // Confirm in the page that asked, once Python says it's done (a trip has its own
                // popups, below)
                if (status.pinned && status.pinned.seq === ns.sentSeq && ns.confirmedSeq !== ns.sentSeq) {
                    ns.confirmedSeq = ns.sentSeq;
                    confirmWaypoint(status.pinned);
                }
                // Trips: show each new state once. A trip to another map reloads the pages, so
                // this isn't tied to the page that asked; a fresh page skips what was already there.
                var trip = status.travel;
                var tripKey = trip ? trip.seq + "/" + trip.state : "";
                if (!before) {
                    ns.tripShown = tripKey;
                } else if (trip && tripKey !== ns.tripShown) {
                    ns.tripShown = tripKey;
                    showTrip(trip);
                }
                var pinnedChanged = !before || JSON.stringify(before.pinned) !== JSON.stringify(status.pinned);
                if (pinnedChanged || !before || before.key !== status.key) {
                    for (var i = 0; i < ns.cards.length; i++) renderDrops(ns.cards[i]);
                }
            }
            setTimeout(pollHotkey, HOTKEY_POLL_MS);
        });
    }

    // endregion

    function renderDrops(card) {
        var bottom = card.querySelector(".item_card_bottom_half");
        if (!bottom) return;
        // One box per card, kept at the end of the section and only ever refilled: moving nodes
        // next to the game's data-bound elements made the game insert its red text again on
        // every update
        var box = bottom.querySelector(".bl4d_drops");
        if (!box) {
            box = element("div", "bl4d_drops");
            bottom.appendChild(box);
        }
        renderPrompt(card, firstMachine(card.DataModel));
        // Clear out red text copies an earlier version of this script caused (keep the game's own)
        var reds = bottom.querySelectorAll("#item_card_red_text");
        for (var r = 1; r < reds.length; r++) reds[r].parentNode.removeChild(reds[r]);
        box.innerHTML = "";
        box.style.display = "none";
        var data = card.DataModel;
        if (!data || data.abbreviated) return;
        var entries = dropEntries(data);
        if (!entries) return;
        var player = ns.live && ns.live[PLAYER_KEY];
        var uvh = player ? player.uvh : null;

        box.style.display = "";
        box.appendChild(element("p", "bl4d_drops_title", "Drops from"));
        var world = false;
        var any = false;
        for (var i = 0; i < entries.length; i++) {
            world = world || entries[i].world;
            for (var j = 0; j < entries[i].sources.length; j++) {
                var rows = sourceRows(entries[i].sources[j], uvh);
                for (var k = 0; k < rows.length; k++) box.appendChild(rows[k]);
                any = true;
            }
        }
        if (world) box.appendChild(element("p", "bl4d_drops_line", any ? "Also a world drop" : "World drop"));
        if (!any && !world) box.appendChild(element("p", "bl4d_drops_line", "No drop source (pack, reward or vendor item)"));
    }

    // endregion
    // region Firmware
    //
    // The game's firmware row shows the name and three pips: how many equipped items share this
    // firmware, with a green pip for the one this item would add (red: the one it would cost).
    // Below it we list every tier with its numbers (from the live file's "$firmware", evaluated
    // for your character) and what equipping this item does to each:
    //
    //     Equipped 1 of 3 -> 2 with this item
    //     [1] Splash Radius is increased by +25%                       Active
    //     [2] Splash Damage is increased by +20%                       Unlocks
    //     [3] On kill, drop a Missile Strike on nearby enemies         Needs 3

    var FIRMWARE_KEY = "$firmware";
    var FIRMWARE_TIERS = 3;
    var COMPARE_BETTER = "item_card_compare_better";
    var COMPARE_WORSE = "item_card_compare_worse";

    function firmwareInfo(data) {
        var all = ns.live && ns.live[FIRMWARE_KEY];
        var fw = data.firmware;
        if (!all || !fw) return null;
        var ident = String(data.firmwareident || "").toLowerCase();
        if (ident && all[ident]) return all[ident];
        var name = String(fw.loc_text || "").trim().toLowerCase();
        for (var key in all) {
            if (all[key].name.toLowerCase() === name || (ident && ident.indexOf(key.replace(/^fw_/, "")) >= 0)) return all[key];
        }
        return null;
    }

    function firmwareCounts(data) {
        var now = clamp(parseInt(data.firmwarecount, 10) || 0, 0, FIRMWARE_TIERS);
        var comparison = data.firmware && data.firmware.comparison;
        var after = comparison === COMPARE_BETTER ? now + 1 : comparison === COMPARE_WORSE ? now - 1 : now;
        return { now: now, after: clamp(after, 0, FIRMWARE_TIERS) };
    }

    function tierRow(tier, counts) {
        var need = tier.count;
        var state, label;
        if (counts.now >= need && counts.after >= need) {
            state = "active";
            label = "Active";
        } else if (counts.after >= need) {
            state = "gain";
            label = "Unlocks";
        } else if (counts.now >= need) {
            state = "lose";
            label = "Lost";
        } else {
            state = "locked";
            label = "Needs " + need;
        }
        var row = element("div", "bl4d_fw_tier bl4d_fw_" + state);
        row.appendChild(element("p", "bl4d_fw_pieces", String(need)));
        var lines = element("div", "bl4d_fw_lines");
        for (var i = 0; i < tier.lines.length; i++) {
            var line = tier.lines[i];
            var text = line.text;
            for (var placeholder in line.values) text = text.split(placeholder).join(line.values[placeholder][0]);
            lines.appendChild(markupElement("bl4d_fw_line", text));
        }
        row.appendChild(lines);
        row.appendChild(element("p", "bl4d_fw_state", label));
        return row;
    }

    function renderFirmware(card) {
        var data = card.DataModel;
        var host = card.querySelector('[gbx-component-id="firmware"]');
        var box = card.querySelector(".bl4d_fw");
        var info = data && data.firmwarevisible && !data.abbreviated && (ns.settings || {}).firmware !== false && firmwareInfo(data);
        if (!host || !info) {
            if (box) box.parentNode.removeChild(box);
            return;
        }
        // Inside the game's firmware component, after its row: it comes and goes with the
        // firmware (data-bind-if), and we never move the game's own nodes
        if (!box || box.parentNode !== host) {
            if (box) box.parentNode.removeChild(box);
            box = element("div", "bl4d_fw");
            host.appendChild(box);
        }
        box.innerHTML = "";
        var counts = firmwareCounts(data);
        var summary = "Equipped " + counts.now + " of " + FIRMWARE_TIERS;
        if (counts.after !== counts.now) summary += " → " + counts.after + " with this item";
        box.appendChild(element("p", "bl4d_fw_summary" + (counts.after > counts.now ? " bl4d_fw_gain" : counts.after < counts.now ? " bl4d_fw_lose" : ""), summary));
        for (var i = 0; i < info.tiers.length; i++) box.appendChild(tierRow(info.tiers[i], counts));
    }

    // endregion
    // region Size and scrolling
    //
    // The game places the card by transforming .item_card_tooltip in menus (translate(anchor
    // point) then translate(x%, y%), y = -100% when the card's bottom sits at the hovered item, so
    // a tall card grows up off the top of the screen) and .item_card_wrapper on the HUD
    // (translateX(-50%) translateY(-100%)). We leave both alone and transform .item_card inside
    // them: scaled from the side the game anchored to, then moved back on screen. The wrapper's
    // box is where the game put the card, unaffected by what we do to its child.
    // A card taller than the screen starts with its top showing and scrolls with the mouse wheel
    // (wheel events do reach menu pages; on the HUD there's no wheel, so it stays at the top).

    function loadSettings() {
        if (ns.settingsGeneration !== generation || !current()) return;  // a newer copy took over, or off
        get(SETTINGS_URL, function (text) {
            if (ns.settingsGeneration !== generation || !current()) return;
            var settings = null;
            try {
                settings = text ? JSON.parse(text) : null;
            } catch (err) {}
            ns.settings = settings || {};
            // Redraw open cards when an option that changes their content flips
            if (ns.settingsText !== undefined && text !== ns.settingsText) {
                for (var i = 0; i < ns.cards.length; i++) ns.refresh(ns.cards[i]);
                refreshSlots();
            }
            ns.settingsText = text;
            setTimeout(loadSettings, SETTINGS_POLL_MS);
        });
    }

    function clamp(value, low, high) {
        return Math.max(low, Math.min(high, value));
    }

    // How far into the card the game's anchor is, from "translate(x%, y%)" or "translateX(x%)
    // translateY(y%)" on either element: 0 = left/top edge, 1 = right/bottom edge
    function anchorOf(elements) {
        for (var i = 0; i < elements.length; i++) {
            var transform = getComputedStyle(elements[i]).transform || "";
            var both = /translate\(\s*(-?[\d.]+)%\s*,\s*(-?[\d.]+)%\s*\)/.exec(transform);
            var x = both ? both[1] : (/translateX\(\s*(-?[\d.]+)%\s*\)/.exec(transform) || [])[1];
            var y = both ? both[2] : (/translateY\(\s*(-?[\d.]+)%\s*\)/.exec(transform) || [])[1];
            if (x !== undefined || y !== undefined) {
                return { x: clamp(-parseFloat(x || 0) / 100, 0, 1), y: clamp(-parseFloat(y || 0) / 100, 0, 1) };
            }
        }
        return { x: 0, y: 0 };
    }

    function setTransform(target, transform) {
        if (target.bl4dTransform === transform) return;
        target.bl4dTransform = transform;
        target.style.transformOrigin = "0 0";
        target.style.transform = transform;
    }

    // Returns how far the card can scroll (0 when it fits)
    function layout(card) {
        var tooltip = card.querySelector(".item_card_tooltip");
        var wrapper = card.querySelector(".item_card_wrapper");
        var target = card.querySelector(".item_card");
        if (!tooltip || !wrapper || !target) return 0;
        // Earlier versions transformed the wrapper, which on the HUD replaced the game's own
        if (wrapper.bl4dTransform) {
            setTransform(wrapper, "");
            wrapper.style.transformOrigin = "";
        }
        var box = wrapper.getBoundingClientRect();
        if (box.height <= 0) {
            setTransform(target, "");
            return 0;
        }
        var view = { width: window.innerWidth, height: window.innerHeight };
        var margins = /view=HUD/i.test(location.href) ? HUD_MARGINS : MENU_MARGINS;
        var edge = view.height * margins.top;
        var bottomEdge = view.height * (1 - margins.bottom);
        var room = bottomEdge - edge;
        var rem = parseFloat(getComputedStyle(document.documentElement).fontSize) || 0;
        var full = box.height + DECOR_BOTTOM_REM * rem;
        var settings = ns.settings || {};
        var scale = settings.scale > 0 ? settings.scale : 1;
        if (settings.fit && full * scale > room) scale = room / full;
        var width = box.width * scale;
        var height = box.height * scale;
        var anchor = anchorOf([tooltip, wrapper]);
        var left = clamp(box.left + (box.width - width) * anchor.x, 0, Math.max(0, view.width - width));
        var top = box.top + (box.height - height) * anchor.y;

        // A different item (or slot) starts at the top again
        var key = (card.DataModel && card.DataModel.name) + "|" + getComputedStyle(tooltip).transform;
        if (card.bl4dScrollKey !== key) {
            card.bl4dScrollKey = key;
            card.bl4dScroll = 0;
        }
        var range = Math.max(0, full * scale - room);
        if (range > 0) {
            card.bl4dScroll = clamp(card.bl4dScroll || 0, 0, range);
            top = edge - card.bl4dScroll;
        } else {
            card.bl4dScroll = 0;
            top = clamp(top, edge, bottomEdge - full * scale);
        }
        var dx = Math.round(left - box.left);
        var dy = Math.round(top - box.top);
        setTransform(target, dx || dy || scale !== 1 ? "translate(" + dx + "px, " + dy + "px) scale(" + scale + ")" : "");
        card.bl4dScrollRange = range;
        return range;
    }

    function shownCard() {
        for (var i = ns.cards.length - 1; i >= 0; i--) {
            var tooltip = ns.cards[i].querySelector(".item_card_tooltip");
            if (tooltip && tooltip.getBoundingClientRect().height > 0) return ns.cards[i];
        }
        return null;
    }

    function layoutAll() {
        if (ns.layoutGeneration !== generation || !current()) return;
        for (var i = 0; i < ns.cards.length; i++) {
            try {
                layout(ns.cards[i]);
            } catch (err) {}
        }
        // The game rebuilds its prompt row (data-bind-if / data-bind-for) without telling the
        // card, which took our J / K prompts with it until the next hover; put them back
        var shown = shownCard();
        if (shown) {
            try {
                renderPrompt(shown, firstMachine(shown.DataModel));
            } catch (err) {}
            // Same for rows data-bind-for made after the card's update (labels, markup)
            decorate(shown);
        }
        setTimeout(layoutAll, LAYOUT_MS);
    }

    function onWheel(event) {
        var card = shownCard();
        if (!card || !card.bl4dScrollRange || !event.deltaY) return;
        card.bl4dScroll = (card.bl4dScroll || 0) + (event.deltaY > 0 ? 1 : -1) * window.innerHeight * SCROLL_STEP;
        layout(card);
        event.preventDefault();
    }

    // endregion

    function track(card) {
        var i = ns.cards.indexOf(card);
        if (i >= 0) ns.cards.splice(i, 1);
        ns.cards.push(card);
        if (ns.cards.length > MAX_TRACKED_CARDS) ns.cards.shift();
    }

    // Debug probes an older copy of this file may have left running on the page
    clearInterval(ns.geoTimer);
    clearInterval(ns.probeTimer);
    if (ns.probeWheel) {
        document.removeEventListener("wheel", ns.probeWheel, true);
        ns.probeWheel = null;
    }

    ns.refresh = function (card) {
        if (!current()) return;
        track(card);
        decorate(card);
        // data-bind-for may make the card's rows after this update: look again next frame
        if (typeof requestAnimationFrame === "function") {
            requestAnimationFrame(function () {
                if (current()) decorate(card);
            });
        }
        loadLive(function () {
            loadDrops(function () {
                if (!current()) return;
                // never break the game's own card
                try {
                    render(card);
                } catch (err) {}
                try {
                    renderDrops(card);
                } catch (err) {}
                try {
                    renderFirmware(card);
                } catch (err) {}
            });
        });
    };

    // region Legendary names on the inventory's equip slots and switch grid

    // The equip screen's slots only show the item's picture. We add the legendary's own name:
    // "Churned Aching Golden God" -> "Golden God". The slot's data has the full rolled name
    // (`name`, prefixes in front) and a generic `basename` ("Ripper Shotgun"); the legendary names
    // are drops.json's keys, so prefixes come off the same way as for drop sources.
    // Weapons: in the slot's picture frame (.wheel-slot__weapon-slot), which the game turns
    // upright in all four (rotated) wheel slots, at its top: the 2:1 thumbnail (512x256) leaves a
    // ~2rem empty band there in the 15.5 x 12 rem frame (measured 2026-10-29), and the slot's pips,
    // logo, element icons and number all sit outside the frame.
    // Gear (shield, grenade, repair kit, enhancement, class mod): the picture fills the slot and the
    // pips, type icon and firmware/element icons take its top, so the name runs along the bottom,
    // clear of the mark icon in the bottom corner (item_cards.css).
    var SLOT_NAME_RARITIES = { legendary: 1, pearl: 1, pearlescent: 1 };
    var SLOT_IDENT = /^([a-z_]+?)(\d+)$/;

    // The "Full legendary names" option keeps the rolled prefixes
    function legendaryName(entry) {
        var words = String(entry.name || "").trim().split(/\s+/);
        if (!words[0]) return "";
        if ((ns.settings || {}).full_names === true) return words.join(" ");
        for (var i = 0; ns.drops && i < words.length; i++) {
            var name = words.slice(i).join(" ");
            if (ns.drops[name.toLowerCase()]) return name;
        }
        return words.join(" ");  // a name drops.json doesn't know (another language, a new item)
    }

    // Long names get a smaller size: limits = the most characters at the normal, small and tiny
    // sizes. Measured in the weapon frame: 20 fit at normal ("Jail-Broken Gatling"), 22 at small
    // ("Upgraded Finnity XXX-L"), 27 at tiny ("Superconducting Plasma Coil", the longest base
    // name). Longer (full names with prefixes, a translation): tiny on up to two lines
    function nameSize(text, limits) {
        if (text.length <= limits[0]) return "";
        if (text.length <= limits[1]) return " bl4d_slot_name_small";
        return text.length <= limits[2] ? " bl4d_slot_name_tiny" : " bl4d_slot_name_tiny bl4d_slot_name_wrap";
    }

    function slotNameSize(text, gear) {
        return nameSize(text, gear ? [13, 16, 19] : [20, 23, 27]);
    }

    // The element the name goes in: the weapon's upright picture frame, or the gear slot's box
    function slotHost(slot, type) {
        if (type === "weapon") return slot.querySelector(".wheel-slot__weapon-slot");
        var colour = slot.querySelector(".oak_equip_slot_color");
        return colour && colour.parentNode;
    }

    function setSlotName(host, text, className) {
        var label = host.querySelector(".bl4d_slot_name");
        if (!text) {
            if (label) label.parentNode.removeChild(label);
            return;
        }
        if (!label) {
            label = ours(element("p", className));
            host.appendChild(label);
        }
        if (label.textContent !== text) label.textContent = text;
        if (label.className !== className) label.className = className;
    }

    function renderSlotNames(menu) {
        var on = (ns.settings || {}).slot_names !== false;
        var slots = menu.querySelectorAll("oak-equip-slot[slot-ident]");
        for (var i = 0; i < slots.length; i++) {
            var ident = SLOT_IDENT.exec(slots[i].getAttribute("slot-ident") || "");
            var host = ident && slotHost(slots[i], ident[1]);
            if (!host) continue;
            var data = on ? menu.GetEquipementSlotDataFromTypeAndSubIndex(ident[1], parseInt(ident[2], 10)) : null;
            var text = data && !data.isempty && SLOT_NAME_RARITIES[data.rarityident] ? legendaryName(data) : "";
            var gear = ident[1] !== "weapon";
            setSlotName(host, text, "bl4d_slot_name" + (gear ? " bl4d_gear_name" : "") + slotNameSize(text, gear));
        }
    }

    // The switch grid (a slot's items, the game's backpack popup) is made of InventoryItemCells
    // (239x168 at 1408px high). The bottom left shows the filter's / sort's stat, so the name goes
    // in the top row: after the pips, before the element icons (right edge). A new item's NEW
    // badge (a 5rem diamond on the middle of the top edge) covers that row: until it's seen, the
    // name sits under the type icon instead. Other cell grids (vendors, bank, Lost Loot) show
    // prices there: only cells in the popup get a name.
    function inSwitchGrid(cell) {
        if (cell.bl4dInPopup === undefined) {
            var node = cell.parentElement;
            while (node && node.tagName && node.tagName.toLowerCase() !== "widget-popup-backpack") node = node.parentElement;
            cell.bl4dInPopup = !!(node && node.tagName);
        }
        return cell.bl4dInPopup;
    }

    // The top row is ~9rem wide (11 characters at the normal size); a new item's row is the cell's width
    function cellNameSize(text, isNew) {
        return nameSize(text, isNew ? [16, 19, 22] : [11, 14, 16]);
    }

    function renderCellName(cell) {
        var host = cell.querySelector(".iic_content");
        if (!host) return;
        var data = cell.DataModel;
        var on = (ns.settings || {}).slot_names !== false && inSwitchGrid(cell);
        var text = on && data && !data.isempty && SLOT_NAME_RARITIES[data.rarityident] ? legendaryName(data) : "";
        var isNew = data && data.isnew;
        setSlotName(host, text, "bl4d_slot_name bl4d_cell_name" + (isNew ? " bl4d_cell_name_new" : "") + cellNameSize(text, isNew));
    }

    ns.cell = function (cell) {
        if (!current()) return;
        loadDrops(function () {
            if (!current()) return;
            try {
                renderCellName(cell);
            } catch (err) {}  // never break the game's own cell
        });
    };

    ns.slots = function (menu) {
        if (!current()) return;
        loadDrops(function () {
            if (!current()) return;
            try {
                renderSlotNames(menu);
            } catch (err) {}  // never break the game's own menu
        });
    };

    function refreshSlots() {
        var menus = document.querySelectorAll("widget-menu-equip");
        for (var i = 0; i < menus.length; i++) ns.slots(menus[i]);
        var cells = document.querySelectorAll("widget-popup-backpack inventory-item-cell");
        for (var j = 0; j < cells.length; j++) ns.cell(cells[j]);
    }

    // Hook the equip menu's and the item cell's classes once, like the card's (only the inventory
    // page has them)
    var slotTries = 0;
    function installSlots() {
        if (!current()) return;
        if (typeof Widget_EquipMenu === "undefined" || typeof InventoryItemCell === "undefined") {
            setTimeout(installSlots, slotTries++ < INSTALL_FAST_TRIES ? INSTALL_FAST_MS : INSTALL_SLOW_MS);
            return;
        }
        var proto = Widget_EquipMenu.prototype;
        if (!proto.bl4dSlotsHooked) {
            proto.bl4dSlotsHooked = true;
            var original = proto.Event_DataUpdated;
            proto.Event_DataUpdated = function () {
                original.apply(this, arguments);
                var page = window.bl4dItemCards;
                if (page && !page.off && page.slots) page.slots(this);
            };
        }
        var cellProto = InventoryItemCell.prototype;
        if (!cellProto.bl4dNameHooked) {
            cellProto.bl4dNameHooked = true;
            var originalCell = cellProto.Event_DataUpdated;
            cellProto.Event_DataUpdated = function () {
                originalCell.apply(this, arguments);
                var page = window.bl4dItemCards;
                if (page && !page.off && page.cell) page.cell(this);
            };
        }
        refreshSlots();
    }

    // endregion

    // region Styles and reloading

    function applyCss(css) {
        if (!document.head) return;
        // The old page linked the stylesheet itself; take that over so a change replaces its rules
        var links = document.querySelectorAll('link[href*="bl4direct/item_cards.css"]');
        for (var i = 0; i < links.length; i++) links[i].parentNode.removeChild(links[i]);
        var style = document.getElementById("bl4d_item_cards_css");
        if (!style) {
            style = document.createElement("style");
            style.id = "bl4d_item_cards_css";
            document.head.appendChild(style);
        }
        if (style.textContent !== css) style.textContent = css;
    }

    function loadCss() {
        get(CSS_URL, function (css) {
            if (!current() || !css) return;
            ns.css = css;
            applyCss(css);
        });
    }

    // Dev builds only (settings.json "dev"): the styles and the drop data, when they change on
    // disk. The link re-runs this file itself when it changes.
    function checkForChanges() {
        if (!current()) return;  // a newer copy of this file took over, or the mod was turned off
        if (!ns.settings || ns.settings.dev !== true) {
            // A release: our files can't change while the game runs, don't re-read them
            setTimeout(checkForChanges, RELOAD_CHECK_MS);
            return;
        }
        get(CSS_URL, function (css) {
            if (!current()) return;
            if (css && css !== ns.css) {
                ns.css = css;
                applyCss(css);
            }
            // The drop data changes when the mod is updated; pick that up too
            get(DROPS_URL, function (drops) {
                if (!current()) return;
                if (drops && drops !== ns.dropsText) {
                    try {
                        ns.drops = JSON.parse(drops);
                        ns.dropsText = drops;
                        for (var i = 0; i < ns.cards.length; i++) ns.refresh(ns.cards[i]);
                    } catch (err) {}
                }
                setTimeout(checkForChanges, RELOAD_CHECK_MS);
            });
        });
    }

    // endregion

    // Everything else waits for the game's card class: a page without item cards costs nothing
    // but this wait
    function start() {
        ns.hotkeyGeneration = generation;
        pollHotkey();
        ns.settingsGeneration = generation;
        loadSettings();
        ns.layoutGeneration = generation;
        layoutAll();
        if (ns.wheel) document.removeEventListener("wheel", ns.wheel, true);
        ns.wheel = onWheel;
        document.addEventListener("wheel", onWheel, true);
        setTimeout(checkForChanges, RELOAD_CHECK_MS);
        loadCss();
    }

    // Hook the game's card class once; the hook calls whichever copy of this file is current
    var tries = 0;
    function install() {
        if (!current()) return;
        if (typeof Widget_ItemCard === "undefined") {
            setTimeout(install, tries++ < INSTALL_FAST_TRIES ? INSTALL_FAST_MS : INSTALL_SLOW_MS);
            return;
        }
        var proto = Widget_ItemCard.prototype;
        if (!proto.bl4dHooked) {
            proto.bl4dHooked = true;
            var original = proto.Event_DataUpdated;
            proto.Event_DataUpdated = function () {
                original.apply(this, arguments);
                var page = window.bl4dItemCards;
                if (page && !page.off && page.refresh) page.refresh(this);
            };
        }
        start();
        for (var i = 0; i < ns.cards.length; i++) ns.refresh(ns.cards[i]);
    }

    // The mod was turned off (the link stops this script): undo everything on the page. A hook
    // an older copy installed calls ns.refresh, so that becomes a no-op too.
    function teardown() {
        if (ns.generation !== generation) return;  // a newer copy runs: it's that one's job
        ns.off = true;
        ns.generation++;
        ns.refresh = function () {};
        ns.slots = function () {};
        ns.cell = function () {};
        if (ns.wheel) document.removeEventListener("wheel", ns.wheel, true);
        ns.wheel = null;
        clearTimeout(ns.popupTimer);
        var remove = [document.getElementById("bl4d_item_cards_css"), document.getElementById("bl4d_popup")];
        remove = remove.concat(Array.prototype.slice.call(document.querySelectorAll(".bl4d_slot_name")));
        for (var i = 0; i < remove.length; i++) {
            if (remove[i] && remove[i].parentNode) remove[i].parentNode.removeChild(remove[i]);
        }
        var cards = Array.prototype.slice.call(document.querySelectorAll("widget-item-card"));
        for (var j = 0; j < ns.cards.length; j++) if (cards.indexOf(ns.cards[j]) < 0) cards.push(ns.cards[j]);
        for (var k = 0; k < cards.length; k++) {
            try {
                undecorate(cards[k]);
            } catch (err) {}
        }
        ns.cards = [];
        ns.css = undefined;
    }

    if (link && link.ext) {
        link.ext.teardowns = link.ext.teardowns || {};
        link.ext.teardowns[LINK_KEY] = teardown;
    }
    install();
    installSlots();
})();
