"use strict";

// BL4 UI Link, first step. The game's debug.js (the last script of every ingame.html view) is
// shipped in our pak with one added line that loads this loose file. This only loads hub.js (also
// loose, written by whichever of our mods runs the link) and runs it: a release once, a dev build
// again whenever it changes. With none of our mods running there's no hub.js: nothing happens.
// Kept tiny and stable: every copy of every mod publishes the same file.

(function () {
    if (window.bl4linkBoot) return;
    var boot = (window.bl4linkBoot = { text: null });
    var HUB = "coui://uiresources/bl4link/hub.js";
    var MISSING_MS = 5000;
    var DEV_MS = 2000;

    function load() {
        var xhr = new XMLHttpRequest();
        xhr.onreadystatechange = function () {
            if (xhr.readyState !== 4) return;
            // Loose coui:// files answer with status 0 or 200
            var js = (xhr.status === 200 || xhr.status === 0) && xhr.responseText ? xhr.responseText : null;
            if (js && js !== boot.text) {
                boot.text = js;
                try {
                    new Function(js)();
                } catch (err) {}
            }
            if (!boot.text) setTimeout(load, MISSING_MS);
            else if (window.bl4link && window.bl4link.dev) setTimeout(load, DEV_MS);
            else boot.idle = true;  // hub.js calls window.bl4linkBoot.wake() once it knows it's dev
        };
        try {
            xhr.open("GET", HUB, true);
            xhr.send();
        } catch (err) {
            setTimeout(load, MISSING_MS);
        }
    }

    boot.wake = function () {
        if (!boot.idle) return;
        boot.idle = false;
        setTimeout(load, DEV_MS);
    };

    load();
})();
