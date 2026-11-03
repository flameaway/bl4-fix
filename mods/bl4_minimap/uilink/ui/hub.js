"use strict";

// BL4 UI Link, in every game page (ingame.html?view=...), loaded by boot.js. It finds the link's
// server (link.json, written by shared/uilink/__init__.py in whichever of our mods runs it), asks
// which scripts run in this view (GET /ext?view=), and runs each as
// `new Function("bl4linkNs", js)(ns)`. A script that leaves the listing (its mod was turned off)
// is torn down through ns.ext.teardowns[key].
//
// ns (window.bl4link) has the shape of the BL4 Mod Menu's page namespace, so a script written for
// the Mod Menu runs here unchanged once it takes `bl4linkNs`:
//   ns.ext.get(key, open, done)   GET /ext?k=key -> done(snapshot or null); open: its screen shows
//   ns.ext.send(key, payload)     POST /action {op: "ext", ext: key, ...payload}
//   ns.ext.http(method, path, body, done), ns.ext.teardowns[key]
//   ns.get(url, done), ns.log(text), ns.dev, ns.view
//   ns.wake.hooks[key](done)      the menus page woke up (it sleeps while no menu shows)
//   ns.closeGameMenu()            closes the pause menu a hotkey opened, without showing it
//   ns.templates.button           the game's list button, cleaned (menus page)
//
// Hot reload: boot.js runs a changed copy of this file in dev builds; each copy tears its last
// one down first (ever higher generation numbers).

(function () {
    var BASE = "coui://uiresources/bl4link/";
    var MENUS = "view_status_menus";
    var match = /[?&]view=([^&#]+)/.exec(location.href);
    var VIEW = match ? decodeURIComponent(match[1]) : "";
    var MENUS_PAGE = VIEW === MENUS;
    // The menus page only runs while a menu shows; other pages run all the time, so ask less often
    var CHECK_MS = MENUS_PAGE ? 2000 : 4000;
    var NO_SERVER_MS = MENUS_PAGE ? 2000 : 6000;
    var WAKE_FRAME_GAP_MS = 300;
    var WAKE_COVER_MS = 400;  // never hide the page longer than this for a wake
    var CLOSE_COVER_MS = 1000;  // after closing the menu a hotkey opened: if it's still up by then, show it

    var ns = window.bl4link || (window.bl4link = {});
    if (ns.teardown) {
        try {
            ns.teardown();
        } catch (err) {}
    }
    // Strictly increasing, so a retired copy can never think it's current again
    ns.counter = Math.max(ns.counter || 0, ns.gen || 0) + 1;
    var gen = ns.counter;
    ns.gen = gen;
    ns.view = VIEW;
    var timers = [];

    function alive() {
        return ns.gen === gen;
    }

    function later(fn, ms) {
        var id = setTimeout(function () {
            var at = timers.indexOf(id);
            if (at >= 0) timers.splice(at, 1);
            if (alive()) fn();
        }, ms);
        timers.push(id);
    }

    // Loose coui:// files answer with status 0 or 200
    ns.get = function (url, done) {
        var xhr = new XMLHttpRequest();
        xhr.onreadystatechange = function () {
            if (xhr.readyState !== 4) return;
            done((xhr.status === 200 || xhr.status === 0) && xhr.responseText ? xhr.responseText : null);
        };
        try {
            xhr.open("GET", url, true);
            xhr.send();
        } catch (err) {
            done(null);
        }
    };

    // region Link

    var ext = ns.ext || (ns.ext = { teardowns: {} });
    ext.teardowns = ext.teardowns || {};
    ext.port = 0;
    ext.token = "";
    ext.states = {};  // key -> {rev, text}: the last state the server sent

    ns.log = function (text) {
        if (window.bl4dReport) window.bl4dReport("[link] " + text);
        if (ns.dev && ext.port) ext.http("POST", "/action", JSON.stringify({ op: "log", text: VIEW + ": " + text }), null);
    };

    ext.http = function (method, path, body, done) {
        if (!ext.port) {
            if (done) done(-1, "");
            return;
        }
        var xhr = new XMLHttpRequest();
        xhr.onreadystatechange = function () {
            if (xhr.readyState !== 4) return;
            // The server stopped or moved (hot reload, game restart): look it up again
            if (xhr.status !== 200) ext.port = 0;
            if (done) done(xhr.status, xhr.responseText);
        };
        try {
            var sep = path.indexOf("?") < 0 ? "?" : "&";
            xhr.open(method, "http://127.0.0.1:" + ext.port + path + sep + "t=" + ext.token, true);
            xhr.send(body || null);
        } catch (err) {
            ext.port = 0;
            if (done) done(-1, "");
        }
    };

    ext.get = function (key, open, done) {
        var kept = ext.states[key];
        var path = "/ext?k=" + encodeURIComponent(key) + "&open=" + (open ? 1 : 0) + "&view=" + encodeURIComponent(VIEW) +
            "&v=" + (kept ? kept.rev : "");
        ext.http("GET", path, null, function (status, text) {
            var snap = null;
            if (status === 200) {
                try {
                    snap = JSON.parse(text);
                    if (snap && snap.same !== undefined) {
                        // Unchanged: a fresh copy of what we kept (scripts may change what they get)
                        snap = kept && kept.rev === snap.same ? { menus: snap.menus, state: JSON.parse(kept.text) } : null;
                        if (!snap) delete ext.states[key];
                    } else if (snap && snap.rev !== undefined) {
                        ext.states[key] = { rev: snap.rev, text: text.slice(text.indexOf('"state":') + 8, -1) };
                    }
                } catch (err) {
                    snap = null;
                }
            }
            done(snap);
        });
    };

    ext.send = function (key, payload) {
        var action = { op: "ext", ext: key };
        for (var k in payload) if (Object.prototype.hasOwnProperty.call(payload, k)) action[k] = payload[k];
        ext.http("POST", "/action", JSON.stringify(action), null);
    };

    // done(true) once ext.port/token name this game's server. link.json lists every running game's
    // server (newest last); with more than one, ours is the one that answers /hello with 200.
    function findServer(done) {
        ns.get(BASE + "link.json", function (text) {
            var servers = [];
            try {
                servers = JSON.parse(text).servers || [];
            } catch (err) {}
            var i = servers.length;
            (function next() {
                if (--i < 0 || !alive()) return done(false);
                var s = servers[i];
                if (!s || !s.port || !s.token) return next();
                var xhr = new XMLHttpRequest();
                xhr.onreadystatechange = function () {
                    if (xhr.readyState !== 4) return;
                    if (xhr.status !== 200) return next();
                    ext.port = s.port;
                    ext.token = s.token;
                    ext.states = {};  // a new server: its revisions mean nothing to what we kept
                    var wasDev = ns.dev;
                    ns.dev = !!s.dev;
                    if (ns.dev && !wasDev && window.bl4linkBoot && window.bl4linkBoot.wake) window.bl4linkBoot.wake();
                    done(true);
                };
                try {
                    xhr.open("GET", "http://127.0.0.1:" + s.port + "/hello?t=" + s.token, true);
                    xhr.send(null);
                } catch (err) {
                    next();
                }
            })();
        });
    }

    // endregion
    // region Scripts

    var scripts = ns.scripts || (ns.scripts = {});  // key -> {url, text}

    function stopScript(key) {
        var teardown = ext.teardowns[key];
        if (teardown) {
            try {
                teardown();
            } catch (err) {}
        }
        delete ext.teardowns[key];
        delete ext.states[key];
        delete scripts[key];
    }

    function runScript(key, url) {
        var known = scripts[key];
        if (known && known.url === url && !ns.dev) return;  // a release: the script can't change
        ns.get(url, function (js) {
            if (!alive() || !js) return;
            var now = scripts[key];
            if (now && now.url === url && now.text === js) return;
            scripts[key] = { url: url, text: js };
            try {
                new Function("bl4linkNs", js)(ns);
                ns.log("ran " + key);
            } catch (err) {
                // A broken edit: keep whatever copy was running
                ns.log(key + " failed: " + err);
            }
        });
    }

    function stopAll() {
        for (var key in scripts) if (Object.prototype.hasOwnProperty.call(scripts, key)) stopScript(key);
    }

    function check() {
        if (!ext.port) {
            findServer(function (ok) {
                if (!alive()) return;
                if (!ok) {
                    stopAll();
                    later(check, NO_SERVER_MS);
                    return;
                }
                check();
            });
            return;
        }
        ext.http("GET", "/ext?view=" + encodeURIComponent(VIEW), null, function (status, text) {
            if (!alive()) return;
            var listing = null;
            if (status === 200) {
                try {
                    listing = JSON.parse(text) || {};
                } catch (err) {}
            }
            if (listing) {
                for (var key in scripts) {
                    if (Object.prototype.hasOwnProperty.call(scripts, key) && !Object.prototype.hasOwnProperty.call(listing, key)) {
                        stopScript(key);
                        ns.log("stopped " + key);
                    }
                }
                for (var k in listing) if (Object.prototype.hasOwnProperty.call(listing, k)) runScript(k, listing[k]);
            }
            later(check, CHECK_MS);
        });
    }

    // endregion
    // region The menus page: waking up, hiding the game's menus (the Mod Menu's menu.js, cut down)

    // The menus page sleeps while none of the game's menus shows, timers included. On waking, a
    // script's next poll could be most of a second away, so a hotkey that opens a screen showed the
    // pause menu first. A frame callback notices the wake (a long gap between frames), hides the
    // page's top-level layers and runs the scripts' wake hooks (an immediate poll); once they've
    // answered, the page shows again.
    var wake = ns.wake || (ns.wake = { hooks: {} });
    var covers = {};  // reason -> Date.now() deadline

    function ensureStyle() {
        if (document.getElementById("bl4link_css") || !document.head) return;
        var style = document.createElement("style");
        style.id = "bl4link_css";
        style.textContent = ".bl4link_covered { opacity: 0 !important; }";
        document.head.appendChild(style);
    }

    function applyCover() {
        var on = false;
        for (var k in covers) if (Object.prototype.hasOwnProperty.call(covers, k)) on = true;
        if (on) ensureStyle();
        var kids = document.body ? document.body.children : [];
        for (var i = 0; i < kids.length; i++) {
            if (kids[i].tagName === "STYLE" || kids[i].tagName === "SCRIPT") continue;
            if (kids[i].classList.contains("bl4link_covered") !== on) kids[i].classList.toggle("bl4link_covered", on);
        }
    }

    function cover(reason, ms) {
        covers[reason] = Date.now() + ms;
        applyCover();
    }

    function onWake() {
        covers = {};
        var keys = Object.keys(wake.hooks);
        if (!keys.length || !ext.port) {
            applyCover();
            return;
        }
        cover("wake", WAKE_COVER_MS);
        var pending = keys.length;
        var finished = false;
        function done() {
            if (finished || !alive()) return;
            if (--pending > 0) return;
            finished = true;
            // Show the page from the next frame, once what the answers changed has been laid out
            if (Object.prototype.hasOwnProperty.call(covers, "wake")) covers.wake = Date.now();
        }
        keys.forEach(function (key) {
            try {
                wake.hooks[key](done);
            } catch (err) {
                ns.log("wake hook " + key + ": " + err);
                done();
            }
        });
    }

    var lastFrame = Date.now();
    function onFrame() {
        if (!alive()) return;
        var now = Date.now();
        var gap = now - lastFrame;
        lastFrame = now;
        if (gap > WAKE_FRAME_GAP_MS) {
            onWake();
            if (!ext.port) later(check, 0);
        }
        var changed = false;
        for (var k in covers) {
            if (Object.prototype.hasOwnProperty.call(covers, k) && now > covers[k]) {
                delete covers[k];
                changed = true;
            }
        }
        if (changed) applyCover();
        requestAnimationFrame(onFrame);
    }

    // Closes the game's menu a hotkey opened, without showing it on the way out: the pause menu's
    // own Back to Game entry closes it at once; Python (close_menus: the pause menu's UI state) is
    // the fallback
    ns.closeGameMenu = function () {
        cover("closing", CLOSE_COVER_MS);
        var list = document.querySelector('[data-input-id="menu_pause_list"]');
        var resume = list && list.firstElementChild;
        if (resume && list.getBoundingClientRect().height > 0) {
            try {
                resume.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
                // Closed, this page sleeps and the timer only fires at the next wake, long after.
                // Still running soon after: the click didn't take
                var at = Date.now();
                later(function () {
                    if (Date.now() - at < 600) ext.http("POST", "/action", JSON.stringify({ op: "close_menus" }), null);
                }, 300);
                return;
            } catch (err) {
                ns.log("resume click: " + err);
            }
        }
        ext.http("POST", "/action", JSON.stringify({ op: "close_menus" }), null);
    };

    // The game's list button, cleaned like the Mod Menu's templates: no scripts, bindings or custom
    // elements, so the game's component scripts never try to drive our copies
    function loadButton() {
        if (ns.templates) return;
        ns.get("coui://uiresources/components/list_button/list_button.html", function (html) {
            if (!html || !alive()) return;
            var holder = document.createElement("div");
            holder.innerHTML = html
                .replace(/<script[\s\S]*?<\/script>/gi, "")
                .replace(/<link[^>]*>/gi, "")
                .replace(/<(\/?)(?:gbx|oak|widget)-[a-z-]+/gi, "<$1div")
                .replace(/\s(?:data-bind[\w-]*|data-l10n-id|gbx-data-l10n-id|data-input[\w-]*|gue-comment|tooltip-data-key)="[^"]*"/gi, "");
            ns.templates = { button: holder.firstElementChild };
        });
    }

    // endregion

    ns.teardown = function () {
        ns.gen = 0;
        timers.forEach(clearTimeout);
        covers = {};
        if (MENUS_PAGE) applyCover();
    };

    check();
    if (MENUS_PAGE) {
        loadButton();
        if (typeof requestAnimationFrame === "function") requestAnimationFrame(onFrame);
    }
})();
