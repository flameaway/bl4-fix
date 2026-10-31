// Carnage Meter boss fight pop-up loader, in the HUD page. The BL4 UI Link runs it there (key
// "carnagepopup", view HUD; no page of the game's is replaced). Until 0.2.0 our pak 4249 loaded it
// from the game's loot feed page; in the first session after the update that pak may still be
// mounted and load it too, so only the first copy runs (window.Bl4Cm_Loader). This stays small and
// fixed. Once a second it reads popup.json (written by the mod: {stamp, events}); a new stamp
// (re)loads popup_main.js and popup.css, and every read hands the events to the main script, which
// shows the new ones. Same arrangement as the Kairosdex's celebrate.js.
(function () {
    "use strict";

    var link = (typeof bl4linkNs !== "undefined" && bl4linkNs) || null;
    var KEY = "carnagepopup";

    // The main script only: a reload of it keeps the styles applyCss just put in
    function disposeMain() {
        try {
            if (window.Bl4Cm_Dispose) window.Bl4Cm_Dispose();
        } catch (err) {
            console.error("bl4cm dispose: " + err);
        }
        window.Bl4Cm_Dispose = null;
        window.Bl4Cm_OnState = null;
    }

    function dispose() {
        disposeMain();
        var style = document.getElementById("bl4cm_popup_css");
        if (style) style.parentNode.removeChild(style);
    }

    // The pop-up was turned off (the link drops the script): stop whichever copy runs, so turning
    // it on again starts a fresh one
    if (link) {
        link.ext.teardowns[KEY] = function () {
            var running = window.Bl4Cm_Loader;
            if (running && typeof running.stop === "function") running.stop();
            else dispose();  // a loader from before 0.2.0 (a plain true): only its panel can go
            if (running && running !== true) window.Bl4Cm_Loader = null;
        };
    }

    if (window.Bl4Cm_Loader) return;
    var me = { stopped: false };
    window.Bl4Cm_Loader = me;

    var BASE = "coui://uiresources/bl4carnage/";
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
        var style = document.getElementById("bl4cm_popup_css");
        if (!style) {
            style = document.createElement("style");
            style.id = "bl4cm_popup_css";
            document.head.appendChild(style);
        }
        style.textContent = text || "";
    }

    function applyJs(text) {
        disposeMain();
        try {
            if (text) new Function(text)();
        } catch (err) {
            console.error("bl4cm: " + err);
        }
    }

    function deliver(state) {
        try {
            if (window.Bl4Cm_OnState) window.Bl4Cm_OnState(state);
        } catch (err) {
            console.error("bl4cm state: " + err);
        }
    }

    function check() {
        if (me.stopped) return;
        get("popup.json", function (text) {
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
            get("popup.css", function (css) {
                if (me.stopped) return;
                applyCss(css);
                get("popup_main.js", function (js) {
                    if (me.stopped) return;
                    applyJs(js);
                    deliver(state);
                });
            });
        });
    }

    check();
})();
