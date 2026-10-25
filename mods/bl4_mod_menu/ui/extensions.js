"use strict";

// BL4 Mod Menu: loads the page scripts other mods register (extensions.py). menu.js runs this file
// and re-runs it when it changes. Every couple of seconds it asks Python which scripts are
// registered (GET /ext) and runs any that are new or whose file changed, so an extension's script
// can be edited while the game runs too.
//
// For extension scripts it offers ns.ext:
//   ns.ext.get(key, open, done)   GET /ext?k=key -> done(snapshot or null); open: its screen shows
//   ns.ext.send(key, payload)     POST /action {op: "ext", ext: key, ...payload}
//   ns.ext.http(method, path, body, done)
// An extension script may be re-run at any time: it should tear its last copy down first (keep its
// teardown in ns.ext.teardowns[key]) and stop its timers when a newer copy starts.

(function () {
    var ns = window.bl4ModMenu;
    if (!ns) return;
    if (ns.extTeardown) {
        try {
            ns.extTeardown();
        } catch (err) {}
    }
    // Strictly increasing, so a retired copy can never think it's current again
    ns.extCounter = Math.max(ns.extCounter || 0, ns.extGen || 0) + 1;
    var gen = ns.extCounter;
    ns.extGen = gen;

    var CHECK_MS = 2000;
    var timers = [];

    function alive() {
        return ns.extGen === gen;
    }

    function later(fn, ms) {
        var id = setTimeout(function () {
            var at = timers.indexOf(id);
            if (at >= 0) timers.splice(at, 1);
            if (alive()) fn();
        }, ms);
        timers.push(id);
    }

    function log(text) {
        if (ns.log) ns.log("[extensions] " + text);
    }

    var ext = ns.ext || (ns.ext = { port: 0, token: "", texts: {}, teardowns: {} });
    // Find the server again: an older copy may have linked to another game's (two games on one PC)
    ext.port = 0;

    function loadSession(done) {
        // menu.js's ns.findSession picks this game's server when several games share session.json
        ns.findSession(function (session) {
            ext.port = session ? session.port : 0;
            ext.token = session ? session.token : "";
            if (session) ns.dev = session.dev;
            ext.states = {};  // a new server: its revisions mean nothing to what we kept
            done();
        });
    }

    ext.http = function (method, path, body, done) {
        if (!ext.port) {
            loadSession(function () {
                if (ext.port) ext.http(method, path, body, done);
                else if (done) done(-1, "");
            });
            return;
        }
        var xhr = new XMLHttpRequest();
        xhr.onreadystatechange = function () {
            if (xhr.readyState !== 4) return;
            // The server restarted (hot reload, game restart): find it again next time
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

    // key -> {rev, text}: the last state the server sent, sent again only once it changed
    ext.states = ext.states || {};

    ext.get = function (key, open, done) {
        var kept = ext.states[key];
        var path = "/ext?k=" + encodeURIComponent(key) + "&open=" + (open ? 1 : 0) + "&v=" + (kept ? kept.rev : "");
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

    function run(key, url) {
        if (ext.texts[key] && ns.dev === false) return;  // a release: the script can't change
        ns.get(url, function (js) {
            if (!alive() || !js || js === ext.texts[key]) return;
            ext.texts[key] = js;
            try {
                new Function(js)();
                log("ran " + key);
            } catch (err) {
                // A broken edit: keep whatever copy was running
                log(key + " failed: " + err);
            }
        });
    }

    function check() {
        ext.http("GET", "/ext", null, function (status, text) {
            if (!alive()) return;
            if (status === 200) {
                try {
                    var listing = JSON.parse(text) || {};
                    for (var key in listing) if (Object.prototype.hasOwnProperty.call(listing, key)) run(key, listing[key]);
                } catch (err) {
                    log("bad listing: " + err);
                }
            }
            later(check, CHECK_MS);
        });
    }

    ns.extTeardown = function () {
        ns.extGen = 0;
        timers.forEach(clearTimeout);
    };

    check();
})();
