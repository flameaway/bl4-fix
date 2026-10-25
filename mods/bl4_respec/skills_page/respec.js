// BL4 Easy Respec loader. The Skills page (menu_skills.html, in our pak) loads this file, and the
// game only reads paks at startup, so this stays small and fixed. It loads the real script,
// respec_main.js, and our styles, respec.css, and loads them again whenever they change on disk,
// so updating the mod's loose files applies without restarting the game.
(function () {
    "use strict";

    if (window.Bl4Respec_Loader) return;
    window.Bl4Respec_Loader = true;

    const BASE = "coui://uiresources/bl4respec/";
    const CHECK_MS = 1000;
    const loaded = { js: null, css: null, stamp: null };

    function get(name, done, failed) {
        const xhr = new XMLHttpRequest();
        xhr.onreadystatechange = () => {
            if (xhr.readyState !== 4) return;
            if (xhr.status === 200 || (xhr.status === 0 && xhr.responseText)) {
                done(xhr.responseText);
            } else if (failed) {
                failed();
            }
        };
        try {
            xhr.open("GET", BASE + name, true);
            xhr.send();
        } catch (err) {
            console.error("bl4respec loader: " + err);
        }
    }

    function applyCss(text) {
        if (text === loaded.css) return;
        loaded.css = text;
        // The page links respec.css itself; take that over so a change replaces the old rules
        document.querySelectorAll('link[href*="bl4respec/respec.css"]').forEach((link) => link.remove());
        let style = document.getElementById("bl4respec_css");
        if (!style) {
            style = document.createElement("style");
            style.id = "bl4respec_css";
            document.head.appendChild(style);
        }
        style.textContent = text;
    }

    function applyJs(text) {
        if (text === loaded.js) return;
        loaded.js = text;
        try {
            if (window.Bl4Respec_Dispose) window.Bl4Respec_Dispose();
        } catch (err) {
            console.error("bl4respec dispose: " + err);
        }
        window.Bl4Respec_Dispose = null;
        try {
            new Function(text)();
        } catch (err) {
            console.error("bl4respec: " + err);
        }
    }

    function readFiles() {
        get("respec.css", applyCss);
        get("respec_main.js", applyJs);
    }

    function check() {
        // The mod rewrites stamp.txt after changing our files (an update, on/off): read them only
        // when it moved. No stamp (an older copy of the mod): read them every time.
        get("stamp.txt", (stamp) => {
            if (stamp === loaded.stamp) return;
            loaded.stamp = stamp;
            readFiles();
        }, () => {
            loaded.stamp = null;
            readFiles();
        });
    }

    check();
    setInterval(check, CHECK_MS);
})();
