"use strict";

// BL4 Mod Menu loader. Runs in the game's menu page (ingame.html?view=view_status_menus), where
// the pause menu lives, and on the title screen's main menu (main_loader.js sets the context
// first). This file is the stable part: it loads menu.js and menu.css (loose files next to it),
// and every couple of seconds checks them and re-runs or re-applies them when they change, so the
// menu can be edited while the game runs. menu.js does everything else.

(function () {
    var context = window.bl4ModMenuContext || (location.href.indexOf("view=view_status_menus") >= 0 ? "pause" : "");
    if (!context) return;
    var BASE = "coui://uiresources/bl4modmenu/";
    var CHECK_MS = 2000;

    var ns = window.bl4ModMenu;
    if (ns && ns.loaderRunning) return;  // loaded twice (e.g. by the shim and a dev watcher)
    ns = window.bl4ModMenu = ns || {};
    ns.loaderRunning = true;
    ns.BASE = BASE;
    ns.context = context;  // "pause" (in game) or "main" (title screen)

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
    ns.get = get;

    function applyCss(css) {
        var style = document.getElementById("bl4mm_css");
        if (!style) {
            style = document.createElement("style");
            style.id = "bl4mm_css";
            document.head.appendChild(style);
        }
        style.textContent = css;
    }

    function check() {
        if (ns.jsText && ns.cssText && ns.dev === false) {
            // A release (menu.js read session.json): our files can't change, don't re-read them
            setTimeout(check, CHECK_MS);
            return;
        }
        get(BASE + "menu.css", function (css) {
            if (css && css !== ns.cssText) {
                ns.cssText = css;
                applyCss(css);
            }
            get(BASE + "menu.js", function (js) {
                if (js && js !== ns.jsText) {
                    ns.jsText = js;
                    try {
                        new Function(js)();
                    } catch (err) {
                        // A broken edit: keep whatever copy was running
                        if (ns.log) ns.log("menu.js failed: " + err);
                    }
                }
                setTimeout(check, CHECK_MS);
            });
        });
    }

    check();
})();
