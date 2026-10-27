// Carnage Meter boss fight pop-up loader. The HUD's loot feed widget (loot_feed.html, in our pak)
// loads this file, and the game only reads paks at startup, so this stays small and fixed. Once a
// second it reads popup.json (written by the mod: {stamp, events}); a new stamp (re)loads
// popup_main.js and popup.css, and every read hands the events to the main script, which shows
// the new ones. Same arrangement as the Kairosdex's celebrate.js.
(function () {
    "use strict";

    if (window.Bl4Cm_Loader) return;
    window.Bl4Cm_Loader = true;

    var BASE = "coui://uiresources/bl4carnage/";
    var CHECK_MS = 1000;
    var loaded = { stamp: null, text: null };

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
        try {
            if (window.Bl4Cm_Dispose) window.Bl4Cm_Dispose();
        } catch (err) {
            console.error("bl4cm dispose: " + err);
        }
        window.Bl4Cm_Dispose = null;
        window.Bl4Cm_OnState = null;
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
        get("popup.json", function (text) {
            setTimeout(check, CHECK_MS);
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
                applyCss(css);
                get("popup_main.js", function (js) {
                    applyJs(js);
                    deliver(state);
                });
            });
        });
    }

    check();
})();
