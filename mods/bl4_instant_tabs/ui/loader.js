"use strict";

// BL4 Instant Menu Tabs loader. Runs in the game's menu page (ingame.html?view=view_status_menus):
// the BL4 UI Link runs it there (shared/uilink, key "instanttabs"), and our old copies of the
// inventory and map pages (pak 4246, until 0.1.x) load it too until the next game start. This file
// is the stable part: every couple of seconds it reads config.json and fade.js (loose files next to
// it, written by the mod) and re-runs fade.js when it changes, so the fade skip can be edited while
// the game runs.

(function () {
    if (location.href.indexOf("view=view_status_menus") < 0) return;
    var KEY = "instanttabs";
    var BASE = "coui://uiresources/bl4instanttabs/";
    var CHECK_MS = 2000;
    // The link's page namespace (or the Mod Menu's, while it carries our scripts); none from the pak
    var link = (typeof bl4linkNs !== "undefined" && bl4linkNs) || window.bl4ModMenu || null;
    var teardowns = link && link.ext && link.ext.teardowns;

    // A copy the link ran before (a hot reload of this file): stop it first
    if (teardowns && teardowns[KEY]) {
        try {
            teardowns[KEY]();
        } catch (err) {}
    }

    var ns = window.bl4InstantTabs;
    if (ns && ns.loaderRunning) {
        // Both old pages load us, and the link too this session: one copy runs. Turning the mod
        // off reaches the old pages' copy through config.json (fade.js reads it at each call)
        return;
    }
    ns = window.bl4InstantTabs = ns || {};
    ns.loaderRunning = true;
    ns.config = ns.config || { skipFade: true };
    var stopped = false;

    function get(url, done) {
        var xhr = new XMLHttpRequest();
        xhr.onreadystatechange = function () {
            if (xhr.readyState !== 4 || stopped) return;
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

    function check() {
        ns.timer = null;
        get(BASE + "config.json", function (text) {
            if (text) {
                try {
                    ns.config = JSON.parse(text);
                } catch (err) {
                    // Caught mid-write: keep the last one
                }
            }
            get(BASE + "fade.js", function (js) {
                if (js && js !== ns.jsText) {
                    ns.jsText = js;
                    try {
                        new Function(js)();
                    } catch (err) {
                        // A broken edit: keep whatever copy was running
                    }
                }
                if (!stopped) ns.timer = setTimeout(check, CHECK_MS);
            });
        });
    }

    check();

    // The mod was turned off (or this file changed): undo everything, so the link can run us again
    if (teardowns) {
        teardowns[KEY] = function () {
            stopped = true;
            clearTimeout(ns.timer);
            ns.timer = null;
            // The game's own PlayAnimation back (fade.js only ever wraps the copy kept in ns.origPlay)
            if (ns.origPlay && typeof Animator !== "undefined") Animator.prototype.PlayAnimation = ns.origPlay;
            ns.jsText = null;
            ns.loaderRunning = false;
        };
    }
})();
