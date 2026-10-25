"use strict";

// BL4 Instant Menu Tabs loader. Runs in the game's menu page (ingame.html?view=view_status_menus),
// added by our copies of the inventory and map pages. This file is the stable part: every couple
// of seconds it reads config.json and fade.js (loose files next to it, written by the mod) and
// re-runs fade.js when it changes, so the fade skip can be edited while the game runs.

(function () {
    if (location.href.indexOf("view=view_status_menus") < 0) return;
    var BASE = "coui://uiresources/bl4instanttabs/";
    var CHECK_MS = 2000;

    var ns = window.bl4InstantTabs;
    if (ns && ns.loaderRunning) return;  // both pages load us
    ns = window.bl4InstantTabs = ns || {};
    ns.loaderRunning = true;
    ns.config = ns.config || { skipFade: true };

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

    function check() {
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
                setTimeout(check, CHECK_MS);
            });
        });
    }

    check();
})();
