// BL4 Easy Respec loader. The BL4 UI Link runs this in the menu page (shared/uilink, key "respec"),
// and the old Skills page pak (until 0.1.x) loads it too until the next game start. It loads the
// real script, respec_main.js, and our styles, respec.css, and loads them again whenever they change
// on disk, so updating the mod's loose files applies without restarting the game.
(function () {
    "use strict";

    const KEY = "respec";
    // The link's page namespace (or the Mod Menu's, while it carries our scripts); none from the pak
    const link = (typeof bl4linkNs !== "undefined" && bl4linkNs) || window.bl4ModMenu || null;
    const teardowns = link && link.ext && link.ext.teardowns;

    // A copy the link ran before (a hot reload of this file): stop it first
    if (teardowns && teardowns[KEY]) {
        try {
            teardowns[KEY]();
        } catch (err) {
            console.error("bl4respec teardown: " + err);
        }
    }
    function disposeMain() {
        try {
            if (window.Bl4Respec_Dispose) window.Bl4Respec_Dispose();
        } catch (err) {
            console.error("bl4respec dispose: " + err);
        }
        window.Bl4Respec_Dispose = null;
    }
    if (window.Bl4Respec_Loader && typeof window.Bl4Respec_Dispose === "function") {
        // The old pak's copy runs (this session only) and its main script is up: leave it.
        // Turning the mod off stops it through the loose files (an empty respec_main.js, stamp "off")
        return;
    }
    // Otherwise take over: no loader yet, or one whose main script was disposed and never ran
    // again (seen 2026-10-29 after hot reloads with the old pak mounted: right click did nothing)
    window.Bl4Respec_Loader = true;

    const BASE = "coui://uiresources/bl4respec/";
    const CHECK_MS = 1000;
    const loaded = { js: null, css: null, stamp: null };
    let stopped = false;

    function get(name, done, failed) {
        const xhr = new XMLHttpRequest();
        xhr.onreadystatechange = () => {
            if (xhr.readyState !== 4 || stopped) return;
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
        // The old pak's page links respec.css itself; take that over so a change replaces the old rules
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
        disposeMain();
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
    const timer = setInterval(check, CHECK_MS);

    // The mod was turned off (or this file changed): undo everything, so the link can run us again
    function stop() {
        stopped = true;
        clearInterval(timer);
        disposeMain();
        const style = document.getElementById("bl4respec_css");
        if (style) style.remove();
        window.Bl4Respec_Loader = false;
    }
    if (teardowns) teardowns[KEY] = stop;
})();
