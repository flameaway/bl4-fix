"use strict";

// BL4 Tidy Pause Menu: moves the pause menu's News panel to the right side, under the party list,
// or hides it. The game puts it at the bottom left (left 12rem, bottom 9rem), where it covers the
// last entries of a pause list that mods made longer (Quit Game ended up under it).
//
// The BL4 Mod Menu runs this file in the game's menu page (its extensions.js) and runs it again
// when it changes, so it tears its last copy down first. Python (bl4_tidy_pause/page.py) says
// where the panel goes: state {news: "right" | "hidden" | "default"}. No answer (the mod is off or
// the Mod Menu's server is gone) puts the panel back where the game had it.
//
// We only set inline styles on the panel's own box (.news_mini) and clear exactly those to restore.

(function () {
    var ns = window.bl4ModMenu;
    if (!ns || !ns.ext) return;
    var ext = ns.ext;
    var KEY = "tidypause";
    var POLL_MS = 1000;
    // A gap kept between the party list and the moved panel, in px; closer than that and the
    // panel hides (a long friends list would run into it)
    var GAP_PX = 13;

    if (ext.teardowns[KEY]) {
        try {
            ext.teardowns[KEY]();
        } catch (err) {}
    }
    // Survives reloads of this file; each copy takes a new, ever higher number
    var tp = ns.tidyPause || (ns.tidyPause = {});
    tp.counter = Math.max(tp.counter || 0, tp.gen || 0) + 1;
    var gen = tp.counter;
    tp.gen = gen;

    var timers = [];
    var mode = "default";

    function alive() {
        return tp.gen === gen;
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
        if (ns.log) ns.log("[tidypause] " + text);
    }

    function panel() {
        return document.querySelector("#def_menu_news_pause .news_mini");
    }

    function set(node, prop, value) {
        if (node.style[prop] !== value) node.style[prop] = value;
    }

    function restore(node) {
        if (!node) return;
        set(node, "left", "");
        set(node, "right", "");
        set(node, "display", "");
        set(node, "visibility", "");
    }

    // The panel's place on the right: mirror the game's own left offset, so it lines up with the
    // party list's right edge (both 12rem from the side)
    function sideOffset(node) {
        if (!tp.side) {
            var left = node.style.left ? "" : getComputedStyle(node).left;
            tp.side = left && left !== "auto" ? left : "12rem";
        }
        return tp.side;
    }

    function crowded(node) {
        var party = document.querySelector("#def_lobby_info_pause .lobby_info_main");
        if (!party) return false;
        var p = party.getBoundingClientRect();
        var n = node.getBoundingClientRect();
        if (p.height === 0 || n.height === 0) return false;
        var overlapX = p.left < n.right && n.left < p.right;
        return overlapX && p.bottom + GAP_PX > n.top;
    }

    function apply() {
        var node = panel();
        if (!node) return;
        if (mode === "hidden") {
            set(node, "display", "none");
            return;
        }
        if (mode !== "right") {
            restore(node);
            return;
        }
        set(node, "display", "");
        var side = sideOffset(node);
        set(node, "left", "auto");
        set(node, "right", side);
        set(node, "visibility", crowded(node) ? "hidden" : "");
    }

    function poll(done) {
        ext.get(KEY, false, function (snap) {
            if (!alive()) return;
            var next = snap && snap.state && snap.state.news;
            mode = next === "right" || next === "hidden" ? next : "default";
            try {
                apply();
            } catch (err) {
                log("apply: " + err);
            }
            if (done) done();
        });
    }

    var pollTimer = null;
    function loop() {
        pollTimer = null;
        poll(function () {
            if (alive()) pollTimer = later(loop, POLL_MS);
        });
    }

    // The page just woke up (a menu opened): the Mod Menu keeps it hidden until this answers, so
    // the panel never shows at its old place first
    function onWake(done) {
        if (pollTimer !== null) {
            clearTimeout(pollTimer);
            pollTimer = null;
        }
        poll(function () {
            done();
            if (alive() && pollTimer === null) pollTimer = later(loop, POLL_MS);
        });
    }
    var wake = ns.wake || (ns.wake = { hooks: {} });
    wake.hooks[KEY] = onWake;

    ext.teardowns[KEY] = function () {
        tp.gen = 0;
        timers.forEach(clearTimeout);
        if (wake.hooks[KEY] === onWake) delete wake.hooks[KEY];
        restore(panel());
    };

    loop();
    log("ready");
})();
