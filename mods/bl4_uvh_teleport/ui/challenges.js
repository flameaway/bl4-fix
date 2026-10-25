"use strict";

// BL4 UVH Boss Teleport on the challenges menu: hover an Ultimate Vault Hunter boss challenge
// ("Now Who's Gonna Watch This Core") and its info card gets
//
//     [T] Teleport to Core Observer
//         The Fadefields, the kill counts in UVH 2
//
// The BL4 Mod Menu runs this file in the game's menu page (its extensions.js) and runs it again
// when it changes, so it tears its last copy down first. Python (bl4_uvh_teleport/page.py) does
// the work: its state, polled through GET /ext?k=uvhteleport, lists the challenges that have a boss
// to go to; we tell it which one is hovered, and it reads the key from Windows (no key events reach
// this page), makes the trip and closes the menu. If the trip can't start, the card says why.
//
// The prompt copies the card's own "claim rewards" prompt (its classes, the game's key glyphs).

(function () {
    var ns = window.bl4ModMenu;
    if (!ns || !ns.ext) return;
    var ext = ns.ext;
    var KEY = "uvhteleport";
    var BASE = "coui://uiresources/bl4uvhtp/";
    var CSS_URL = BASE + "challenges.css";
    var POLL_SHOWN_MS = 120;
    var POLL_HIDDEN_MS = 1000;
    var HOVER_REPEAT_MS = 400;
    var CSS_MS = 2000;
    var NOTE_MS = 4000;

    if (ext.teardowns[KEY]) {
        try {
            ext.teardowns[KEY]();
        } catch (err) {}
    }
    // Survives reloads of this file; each copy takes a new, ever higher number
    var tp = ns.uvhtp || (ns.uvhtp = {});
    tp.counter = Math.max(tp.counter || 0, tp.gen || 0) + 1;
    var gen = tp.counter;
    tp.gen = gen;

    var timers = [];
    function alive() {
        return tp.gen === gen;
    }
    function later(fn, ms) {
        var id = setTimeout(function () {
            // Fired: drop its id, or the list grows by one per poll for as long as the page lives
            var at = timers.indexOf(id);
            if (at >= 0) timers.splice(at, 1);
            if (alive()) fn();
        }, ms);
        timers.push(id);
        return id;
    }
    function log(text) {
        if (ns.log) ns.log("[uvhteleport] " + text);
    }

    var state = null;
    var sent = { title: undefined, at: 0 };
    // The refusal shown on the card: {id, title, text, until}; seenNote = the note id at load
    var note = null;
    var seenNote = null;

    // region Helpers

    function element(tag, className, text) {
        var el = document.createElement(tag);
        el.className = className;
        if (text !== undefined) el.textContent = text;
        return el;
    }

    function plain(text) {
        return String(text || "").toLowerCase().replace(/[^a-z0-9]/g, "");
    }

    function keyImage(key) {
        var names = { Zero: "0", One: "1", Two: "2", Three: "3", Four: "4", Five: "5", Six: "6", Seven: "7", Eight: "8", Nine: "9" };
        return "coui://uiresources/Glyphs/assets/PC/Ico_Hint_PC_Key_" + (names[key] || key) + ".png";
    }

    function menu() {
        return document.querySelector("widget-menu-challenges");
    }

    function shown(el) {
        if (!el || el.Active === false) return false;
        var r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    }

    // endregion
    // region What's hovered

    // The hovered challenge's title, if its info card is up
    function hoveredTitle(m) {
        if (!m || !m.bInfoCardDisplayed || !(m.FocusedChallengeIdx >= 0)) return null;
        var data = m.DataModel;
        var title = data && data.infocard && data.infocard.title;
        if (!title) {
            var cells = data && data.challengegrid && data.challengegrid.challenges;
            var cell = cells && cells[m.FocusedChallengeIdx];
            title = cell && cell.bdiscovered ? cell.title : null;
        }
        return title ? String(title) : null;
    }

    function bossFor(title) {
        var bosses = (state && state.bosses) || [];
        var want = plain(title);
        if (!want) return null;
        for (var i = 0; i < bosses.length; i++) if (plain(bosses[i].title) === want) return bosses[i];
        return null;
    }

    // Repeated while this page runs, hovered or not: Python also takes it as "a menu is still up"
    // (the page stops running scripts once the game's menus close)
    function sendHover(title) {
        var now = Date.now();
        if (title === sent.title && now - sent.at < HOVER_REPEAT_MS) return;
        sent.title = title;
        sent.at = now;
        ext.send(KEY, { do: "hover", title: title });
    }

    // endregion
    // region The prompt

    function removePrompts() {
        var old = document.querySelectorAll(".bl4uvh_prompt");
        for (var i = 0; i < old.length; i++) old[i].parentNode.removeChild(old[i]);
    }

    function render(m, boss) {
        var tooltip = m && m.querySelector(".menu_challenges_info_card_wrapper");
        var prompt = tooltip && tooltip.querySelector(".bl4uvh_prompt");
        if (!boss || !tooltip) {
            if (prompt) prompt.style.display = "none";
            return;
        }
        if (!prompt) {
            prompt = element("div", "bl4uvh_prompt");
            var row = element("div", "claim_rewards_prompt bl4uvh_row");
            var btn = element("div", "claim_rewards_action_btn");
            btn.appendChild(element("div", "bl4uvh_key"));
            row.appendChild(btn);
            row.appendChild(element("p", "small_text_2 tooltip_button_glyph_text bl4uvh_text"));
            prompt.appendChild(row);
            prompt.appendChild(element("p", "small_text_2 bl4uvh_sub"));
            // Under the card, above the game's own "claim rewards" prompt
            var info = tooltip.querySelector(".menu_challenges_info_card");
            if (info && info.nextSibling) tooltip.insertBefore(prompt, info.nextSibling);
            else tooltip.appendChild(prompt);
        }
        prompt.style.display = "";

        var now = Date.now();
        var refused = note && note.until > now && plain(note.title) === plain(boss.title) ? note.text : null;
        var busy = state && state.busy;
        var key = prompt.querySelector(".bl4uvh_key");
        key.style.display = state && state.key && !busy ? "" : "none";
        if (state && state.key) key.style.backgroundImage = "url(" + keyImage(state.key) + ")";
        prompt.querySelector(".bl4uvh_text").textContent = busy ? "On the way to a boss..." : "Teleport to " + boss.boss;
        var sub = [boss.place];
        if (boss.uvh !== null && boss.uvh !== undefined) sub.push("the kill counts in UVH " + boss.uvh);
        prompt.querySelector(".bl4uvh_sub").textContent = refused || sub.join(", ");
        prompt.classList.toggle("bl4uvh_refused", !!refused);
        prompt.classList.toggle("bl4uvh_busy", !!busy);
    }

    // endregion

    function onState(snap) {
        state = snap && snap.state ? snap.state : null;
        if (!state || !state.note) return;
        if (seenNote === null) seenNote = state.note.id;
        if (state.note.id !== seenNote) {
            seenNote = state.note.id;
            note = { title: state.note.title, text: state.note.text, until: Date.now() + NOTE_MS };
            var m = menu();
            if (m && typeof m.PlaySoundAtNode === "function") {
                try {
                    m.PlaySoundAtNode(m, "Error");
                } catch (err) {}
            }
        }
    }

    function poll() {
        var m = menu();
        var up = shown(m);
        var title = up ? hoveredTitle(m) : null;
        // Only challenges we can go to count as hovered (the key means nothing on the others)
        var boss = title ? bossFor(title) : null;
        if (title !== tp.lastTitle) {
            tp.lastTitle = title;
            if (title) log("hovered '" + title + "'" + (boss ? ": " + boss.boss : ""));
        }
        sendHover(boss ? title : null);
        ext.get(KEY, !!boss, function (snap) {
            if (!alive()) return;
            onState(snap);
            // The state can list the bosses only now (first poll)
            if (!boss && title) boss = bossFor(title);
            try {
                if (m) render(m, up ? boss : null);
            } catch (err) {
                log("draw failed: " + err);
            }
            later(poll, up ? POLL_SHOWN_MS : POLL_HIDDEN_MS);
        });
    }

    function loadCss() {
        ns.get(CSS_URL, function (css) {
            if (!alive()) return;
            if (css && css !== tp.css) {
                tp.css = css;
                var style = document.getElementById("bl4uvh_css");
                if (!style) {
                    style = document.createElement("style");
                    style.id = "bl4uvh_css";
                    document.head.appendChild(style);
                }
                style.textContent = css;
            }
            later(loadCss, CSS_MS);
        });
    }

    ext.teardowns[KEY] = function () {
        tp.gen = 0;
        timers.forEach(clearTimeout);
        removePrompts();
    };

    loadCss();
    poll();
    log("ready");
})();
