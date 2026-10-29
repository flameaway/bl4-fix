// Kairosdex celebrations loader, in the HUD page. The BL4 UI Link runs it there (key
// "kdxcelebrate", view HUD; no page of the game's is replaced). Until 0.7.0 our pak 4248 loaded it
// from the game's banner widget page; in the first session after the update that pak may still be
// mounted and load it too, so only the first copy runs (window.Bl4Kdx_Loader). This stays small
// and fixed. Once a second it reads celebrate.json (written by the mod: {stamp, events}); a new
// stamp (re)loads celebrate_main.js and celebrate.css, and every read hands the events to the main
// script, which shows the new ones.
(function () {
    "use strict";

    var link = (typeof bl4linkNs !== "undefined" && bl4linkNs) || null;
    var KEY = "kdxcelebrate";

    function dispose() {
        try {
            if (window.Bl4Kdx_Dispose) window.Bl4Kdx_Dispose();
        } catch (err) {
            console.error("bl4kdx dispose: " + err);
        }
        window.Bl4Kdx_Dispose = null;
        window.Bl4Kdx_OnState = null;
        var style = document.getElementById("bl4kdx_celebrate_css");
        if (style) style.parentNode.removeChild(style);
    }

    // The mod was turned off (the link drops the script): stop whichever copy runs, so turning
    // it on again starts a fresh one
    if (link) {
        link.ext.teardowns[KEY] = function () {
            var running = window.Bl4Kdx_Loader;
            if (running && typeof running.stop === "function") running.stop();
            else dispose();  // a loader from before 0.7.0 (a plain true): only its banners can go
            if (running && running !== true) window.Bl4Kdx_Loader = null;
        };
    }

    if (window.Bl4Kdx_Loader) return;
    var me = { stopped: false };
    window.Bl4Kdx_Loader = me;

    var BASE = "coui://uiresources/bl4weapondex/";
    var CHECK_MS = 1000;
    var loaded = { stamp: null, text: null };
    var timer = null;

    me.stop = function () {
        me.stopped = true;
        clearTimeout(timer);
        dispose();
    };

    function get(name, done) {
        var xhr = new XMLHttpRequest();
        xhr.onreadystatechange = function () {
            if (xhr.readyState !== 4) return;
            // Loose coui:// files report status 0 or 200
            done((xhr.status === 200 || xhr.status === 0) && xhr.responseText ? xhr.responseText : null);
        };
        try {
            xhr.open("GET", BASE + name, true);
            xhr.send();
        } catch (err) {
            done(null);
        }
    }

    function applyCss(text) {
        var style = document.getElementById("bl4kdx_celebrate_css");
        if (!style) {
            style = document.createElement("style");
            style.id = "bl4kdx_celebrate_css";
            document.head.appendChild(style);
        }
        style.textContent = text || "";
    }

    function applyJs(text) {
        dispose();
        try {
            if (text) new Function(text)();
        } catch (err) {
            console.error("bl4kdx: " + err);
        }
    }

    function deliver(state) {
        try {
            if (window.Bl4Kdx_OnState) window.Bl4Kdx_OnState(state);
        } catch (err) {
            console.error("bl4kdx state: " + err);
        }
    }

    function check() {
        if (me.stopped) return;
        get("celebrate.json", function (text) {
            if (me.stopped) return;
            timer = setTimeout(check, CHECK_MS);
            if (!text || text === loaded.text) return;
            loaded.text = text;
            var state;
            try {
                state = JSON.parse(text);
            } catch (err) {
                return;  // caught mid-write: next time
            }
            if (state.stamp === loaded.stamp) {
                deliver(state);
                return;
            }
            loaded.stamp = state.stamp;
            get("celebrate.css", function (css) {
                if (me.stopped) return;
                applyCss(css);
                get("celebrate_main.js", function (js) {
                    if (me.stopped) return;
                    applyJs(js);
                    deliver(state);
                });
            });
        });
    }

    check();
})();
