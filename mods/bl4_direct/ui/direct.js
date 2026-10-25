"use strict";

// BL4 Direct Connect's page in the BL4 Mod Menu screen (F6, or BL4 Direct Connect in MODS).
//
// The BL4 Mod Menu runs this file in the game's menu page (its extensions.js) and runs it again
// when it changes, so it tears its last copy down first. It registers ns.modPages[MOD]: when the
// Mod Menu shows this mod, it calls our render() to draw the page with its own parts (the game's
// Options rows, category dividers and list buttons), above the mod's remaining options. So the page
// looks and behaves like every other page of the Mod Menu: hover a row for its details on the right.
//
// Python (bl4_direct/page.py) serves the state through GET /ext?k=bl4direct (polled while the page
// shows) and takes clicks back as {op: "ext", ext: "bl4direct", do: ...}. Typing happens in Python
// (no key events reach this page): a click on a field starts it, and the text comes back in the state.

(function () {
    var ns = window.bl4ModMenu;
    if (!ns || !ns.ext) return;
    var ext = ns.ext;
    var KEY = "bl4direct";
    var MOD = "BL4 Direct Connect";
    var BASE = "coui://uiresources/bl4directconnect/";
    var POLL_SHOWN_MS = 250;
    var POLL_TYPING_MS = 50;
    var WATCH_MS = 500;
    var CSS_MS = 2000;

    if (ext.teardowns[KEY]) {
        try {
            ext.teardowns[KEY]();
        } catch (err) {}
    }
    // Survives reloads of this file; each copy takes a new, ever higher number
    var dc = ns.bl4dc || (ns.bl4dc = {});
    dc.counter = Math.max(dc.counter || 0, dc.gen || 0) + 1;
    var gen = dc.counter;
    dc.gen = gen;

    var timers = [];
    function alive() {
        return dc.gen === gen;
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
        if (ns.log) ns.log("[bl4direct] " + text);
    }

    // region Link

    var pollTimer = null;
    var polling = false;

    function showing() {
        var v = ns.view;
        return !!(v && v.open && v.mod === MOD && !(v.nested && v.nested.length));
    }

    function schedule(ms) {
        if (pollTimer !== null) clearTimeout(pollTimer);
        pollTimer = setTimeout(function () {
            pollTimer = null;
            if (alive()) poll();
        }, ms);
    }

    function pollSoon() {
        if (!polling) schedule(20);
    }

    // Only while the page shows: Python reads "it polled with open=1 just now" as "it's showing"
    function poll() {
        if (!showing()) return;
        polling = true;
        ext.get(KEY, true, function (snap) {
            polling = false;
            if (!alive()) return;
            if (snap && snap.state) onState(snap.state);
            if (showing()) schedule(dc.state && dc.state.typing ? POLL_TYPING_MS : POLL_SHOWN_MS);
        });
    }

    function onState(next) {
        var text = JSON.stringify(next);
        var first = !dc.state;
        dc.state = next;
        if (first) dc.lastToast = next.toast.id;
        if (next.toast.id !== dc.lastToast) {
            dc.lastToast = next.toast.id;
            if (ns.modToast && next.toast.text) ns.modToast(next.toast.text, next.toast.error);
        }
        if (text === dc.stateText) return;
        dc.stateText = text;
        if (ns.modPageRefresh) ns.modPageRefresh(MOD);
    }

    function send(payload) {
        ext.send(KEY, payload);
        pollSoon();
    }

    // Host / Stop / Leave / Join change the map: close the menus first (Python waits for that)
    function travel(payload) {
        send(payload);
        if (ns.modCloseScreen) ns.modCloseScreen(ns.context !== "main");
    }

    // The page shows: poll at once (the Mod Menu just opened it, or switched to it)
    function watch() {
        if (showing() && pollTimer === null && !polling) poll();
        later(watch, WATCH_MS);
    }

    // endregion
    // region Page

    function friendsText(n) {
        return n === 1 ? "1 friend" : n + " friends";
    }

    function value(api, text, cls) {
        return api.el("p", "bl4dc_value" + (cls ? " " + cls : ""), text);
    }

    // wide: the row's only part (spans it, like the Mod Menu's Run buttons)
    function button(api, text, fn, enabled, wide) {
        var b = api.button(text, fn, wide ? "bl4mm_run" : "bl4dc_btn");
        if (enabled === false) b.classList.add("bl4mm_disabled");
        return b;
    }

    // A text field: click to type in it (Python reads the keys), or Paste
    function field(api, name, hint) {
        var s = dc.state;
        var text = s.fields[name] || "";
        var typing = s.typing === name;
        var f = api.el("div", "bl4dc_field" + (typing ? " bl4dc_typing" : ""));
        if (text) {
            f.appendChild(api.el("p", "bl4dc_field_text", text));
        } else if (!typing) {
            f.appendChild(api.el("p", "bl4dc_field_text bl4dc_field_hint", hint));
        }
        if (typing) f.appendChild(api.el("div", "bl4dc_caret"));
        api.hoverable(f);
        api.onClick(f, function () {
            send({ do: "type", field: typing ? null : name });
        });
        return f;
    }

    function render(api) {
        if (pollTimer === null && !polling) schedule(20);
        var s = dc.state;
        if (!s) {
            api.title("Direct connect");
            api.row("Loading...", "", []);
            return;
        }
        var hosting = s.mode === "hosting";
        var connected = s.mode === "connected";

        api.title("Your game");
        var status, statusDesc, statusCls = "";
        if (hosting) {
            status = "Hosting, " + friendsText(s.connected) + " in";
            statusDesc = "You're hosting on UDP port " + s.port + ". Friends in your list join from their own BL4 Direct page.";
            statusCls = "bl4dc_on";
        } else if (connected) {
            status = "In a friend's game";
            statusDesc = "You're connected to a friend's game directly, without SHiFT.";
            statusCls = "bl4dc_on";
        } else if (s.intent) {
            status = s.in_game ? "Restarting hosting..." : "Hosts when you load in";
            statusDesc = "Hosting picks up again by itself after a map change. Don't resume cancels it.";
            statusCls = "bl4dc_warn";
        } else {
            status = "Not in a direct game";
            statusDesc = "Host your game for friends, or join a friend who is hosting.";
        }
        api.row("Status", statusDesc, [value(api, status, statusCls)]);

        if (hosting) {
            api.row("Stop hosting", "Reloads your map as a normal game. Friends who are in get dropped.", [
                button(api, "Stop", function () {
                    travel({ do: "stop" });
                }, true, true)
            ]);
        } else if (connected) {
            api.row("Leave game", "Back to the main menu, and normal online play.", [
                button(api, "Leave", function () {
                    travel({ do: "leave" });
                }, true, true)
            ]);
        } else if (s.intent) {
            api.row("Don't resume", "Cancels hosting: your next map loads as a normal game.", [
                button(api, "Cancel", function () {
                    send({ do: "dont_resume" });
                }, true, true)
            ]);
        } else {
            var hostDesc = !s.in_game
                ? "Load into your game first (Continue), then host."
                : "Reloads your current map as a co-op host on UDP port " + s.port + ". Only friends in your list (and your local network) can get in." +
                  (s.firewall ? "" : " Windows asks once to let your friends through the firewall.");  // prettier-ignore
            api.row("Host a game", hostDesc, [
                button(api, "Host", function () {
                    travel({ do: "host" });
                }, s.in_game, true)
            ]);
        }

        // Your address
        var ipName = hosting ? "Friends join you at" : "Your public IP";
        var ipDesc = "Send this to your friends: they add it on their own BL4 Direct page (Add a friend). You add theirs the same way.";
        if (s.ip.state === "ok") {
            var shown = hosting ? s.ip.ip + ":" + s.port : s.ip.ip;
            api.row(ipName, ipDesc, [
                value(api, shown, "bl4dc_addr"),
                button(api, "Copy", function () {
                    send({ do: "copy", what: hosting ? "address" : "ip" });
                })
            ]);
        } else if (s.ip.state === "failed") {
            api.row(ipName, "Couldn't look up your public IP (offline?). " + ipDesc, [
                value(api, "Couldn't look it up", "bl4dc_off"),
                button(api, "Retry", function () {
                    send({ do: "retry_ip" });
                })
            ]);
        } else {
            api.row(ipName, ipDesc, [value(api, "Looking it up...")]);
        }

        if (hosting || !s.firewall) {
            var fwParts = [value(api, s.firewall ? "Up to date" : s.asking ? "Waiting for Windows" : "Needs updating",
                s.firewall ? "bl4dc_on" : "bl4dc_warn")];  // prettier-ignore
            if (!s.firewall) {
                fwParts.push(button(api, "Update", function () {
                    send({ do: "firewall" });
                }, !s.asking));
            }
            api.row("Firewall", "Only the friends in your list (and your local network) can reach a game you host. " +
                "After adding or removing friends, Windows asks once to update the rule.", fwParts);  // prettier-ignore
        }

        api.row("Versions", "You and your friends should see the same versions here, or joining may fail.", [
            value(api, s.compat, "bl4dc_small")
        ]);

        api.title("Friends");
        if (!s.friends.length) {
            api.row("No friends yet", "Add a friend below with their public IP: they see it on their own BL4 Direct page.", [
                value(api, "Add one below")
            ]);
        }
        s.friends.forEach(function (f) {
            var addr = f.address + (f.port !== 7777 ? ":" + f.port : "");
            api.row(f.name, "Join " + f.name + "'s game at " + f.address + ":" + f.port + ". They must be hosting and have your IP in their list. " +
                "Remove takes them off your list (and out of your firewall rule when you host).", [  // prettier-ignore
                value(api, addr, "bl4dc_addr bl4dc_small"),
                button(api, "Join", function () {
                    travel({ do: "join", name: f.name });
                }, !connected),
                button(api, "Remove", function () {
                    send({ do: "remove", name: f.name });
                })
            ]);
        });

        api.title("Add a friend");
        api.row("Name", "Click the box and type a name for them. Enter or Tab goes to the IP box.", [field(api, "name", "Their name")]);
        api.row("IP address", "Their public IP, e.g. 203.0.113.5 (they see it on their own BL4 Direct page). Click the box and type, " +
            "or copy it and press Paste (Ctrl+V works while typing). Enter adds them.", [  // prettier-ignore
            field(api, "ip", "e.g. 203.0.113.5"),
            button(api, "Paste", function () {
                send({ do: "paste", field: "ip" });
            })
        ]);
        api.row("Add friend", "Adds them to your list. If you're hosting, Windows asks to let them through your firewall.", [
            button(api, "Add", function () {
                send({ do: "add" });
            }, !!(s.fields.name.trim() && s.fields.ip.trim()), true)
        ]);

    }

    // The mod's own options: the ones this page replaces are hidden
    function keep(node) {
        var hide = (dc.state && dc.state.hide) || [];
        return hide.indexOf(node.id) < 0 && hide.indexOf(node.name) < 0;
    }

    // endregion
    // region Stylesheet

    function checkCss() {
        ns.get(BASE + "direct.css", function (css) {
            if (!alive()) return;
            if (css && css !== dc.cssText) {
                dc.cssText = css;
                var style = document.getElementById("bl4dc_css");
                if (!style) {
                    style = document.createElement("style");
                    style.id = "bl4dc_css";
                    document.head.appendChild(style);
                }
                style.textContent = css;
            }
            // Only re-read while the page shows (a dev edit); otherwise rarely
            later(checkCss, showing() ? CSS_MS : CSS_MS * 5);
        });
    }

    // endregion

    ext.teardowns[KEY] = function () {
        dc.gen = 0;
        timers.forEach(clearTimeout);
        if (pollTimer !== null) clearTimeout(pollTimer);
        if (ns.modPages && ns.modPages[MOD] && ns.modPages[MOD].gen === gen) delete ns.modPages[MOD];
    };

    ns.modPages = ns.modPages || {};
    ns.modPages[MOD] = { gen: gen, render: render, keep: keep, moreTitle: "More" };
    dc.stateText = null;
    checkCss();
    watch();
    if (ns.modPageRefresh) ns.modPageRefresh(MOD);
    log("ready");
})();
