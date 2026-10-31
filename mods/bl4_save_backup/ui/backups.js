"use strict";

// BL4 Save Backups' page in the BL4 Mod Menu screen (MODS > BL4 Save Backups).
//
// Run by the Mod Menu's extensions.js in the game's menu page (and again when this file changes),
// so it retires its last copy first. Like BL4 Direct Connect's page (direct.js), it registers
// ns.modPages[MOD]: the Mod Menu calls render(api) to draw the page with its own rows and buttons,
// above the mod's options. Four views, switched here: the overview, one character's history, the
// bank's history, and every backup.
//
// Python (bl4_save_backup/page.py) serves a small state through GET /ext?k=bl4savebackup (polled
// while the page shows); the big view is a loose file (view.json) fetched when state.rev moves.
// Clicks go back as {op: "ext", ext: "bl4savebackup", do: ...}. A restore asks for a second click.

(function () {
    var ns = window.bl4ModMenu;
    if (!ns || !ns.ext) return;
    var ext = ns.ext;
    var KEY = "bl4savebackup";
    var MOD = "BL4 Save Backups";
    var BASE = "coui://uiresources/bl4savebackup/";
    var POLL_MS = 500;
    var WATCH_MS = 500;
    var CSS_MS = 2000;
    var CONFIRM_MS = 4000;

    if (ext.teardowns[KEY]) {
        try {
            ext.teardowns[KEY]();
        } catch (err) {}
    }
    // Survives reloads of this file; each copy takes a new, ever higher number
    var sb = ns.bl4sb || (ns.bl4sb = {});
    sb.counter = Math.max(sb.counter || 0, sb.gen || 0) + 1;
    var gen = sb.counter;
    sb.gen = gen;
    sb.page = sb.page || { view: "home" };

    var timers = [];
    function alive() {
        return sb.gen === gen;
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
        if (ns.log) ns.log("[bl4savebackup] " + text);
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

    function poll() {
        if (!showing()) return;
        polling = true;
        ext.get(KEY, true, function (snap) {
            polling = false;
            if (!alive()) return;
            if (snap && snap.state) onState(snap.state);
            if (showing()) schedule(POLL_MS);
        });
    }

    function onState(next) {
        var first = !sb.state;
        sb.state = next;
        if (first) sb.lastToast = next.toast.id;
        if (next.toast.id !== sb.lastToast) {
            sb.lastToast = next.toast.id;
            if (ns.modToast && next.toast.text) ns.modToast(next.toast.text, next.toast.error);
        }
        if (next.rev !== sb.viewRev) loadView(next);
        var text = JSON.stringify(next) + "|" + sb.viewRev;
        if (text === sb.stateText) return;
        sb.stateText = text;
        refresh();
    }

    function loadView(s) {
        if (sb.loading === s.rev) return;
        sb.loading = s.rev;
        ns.get(s.view, function (text) {
            if (!alive()) return;
            sb.loading = null;
            if (!text) return;
            try {
                sb.data = JSON.parse(text);
                sb.viewRev = s.rev;
            } catch (err) {
                return;  // half a file: the next poll tries again
            }
            refresh();
        });
    }

    function refresh() {
        if (ns.modPageRefresh) ns.modPageRefresh(MOD);
    }

    function send(payload) {
        ext.send(KEY, payload);
        if (!polling) schedule(30);
    }

    function watch() {
        if (showing() && pollTimer === null && !polling) poll();
        later(watch, WATCH_MS);
    }

    // endregion
    // region Formatting

    function plural(n, one, many) {
        return n + " " + (n === 1 ? one : many || one + "s");
    }

    function commas(n) {
        var s = String(Math.round(Math.abs(n)));
        var out = "";
        while (s.length > 3) {
            out = "," + s.slice(-3) + out;
            s = s.slice(0, -3);
        }
        return (n < 0 ? "-" : "") + s + out;
    }

    function short(n) {
        var a = Math.abs(n);
        if (a >= 1e9) return (n / 1e9).toFixed(1) + "B";
        if (a >= 1e6) return (n / 1e6).toFixed(1) + "M";
        if (a >= 1e4) return Math.round(n / 1e3) + "K";
        return commas(n);
    }

    function hours(s) {
        var h = Math.floor(s / 3600);
        var m = Math.floor((s % 3600) / 60);
        return h ? h + " h " + m + " m" : m + " m";
    }

    function signed(n, unit) {
        return (n > 0 ? "+" : "") + n + (unit || "");
    }

    var DIFFICULTY = { Easy: "Easy", Normal: "Normal", Hard: "Hard" };

    // "Level 52, 66 h 33 m, $33.2M"
    function brief(i) {
        if (i.ok === false) return "Damaged";
        var parts = [];
        if (i.level !== undefined) parts.push("Level " + i.level + (i.uvh ? " UVH " + i.uvh : ""));
        if (i.playtime !== undefined) parts.push(hours(i.playtime));
        if (i.cash !== undefined) parts.push("$" + short(i.cash));
        return parts.join(", ");
    }

    function details(i) {
        if (i.ok === false) return "This copy didn't read whole (" + (i.error || "damaged") + "), so it can't be restored.";
        if (i.ok !== true) return "The mod couldn't open this save to read it (" + (i.error || "no key") + "), but the copy is kept.";
        var lines = [];
        var level = "Level " + i.level;
        if (i.spec) level += ", Specialization " + i.spec;
        if (i.difficulty) level += ", " + (DIFFICULTY[i.difficulty] || i.difficulty);
        if (i.uvh) level += ", Ultimate Vault Hunter " + i.uvh;
        lines.push(level + ".");
        lines.push("Played " + hours(i.playtime || 0) + (i.story_done ? ", story done." : "."));
        lines.push("Cash $" + commas(i.cash || 0) + ", Eridium " + commas(i.eridium || 0) + ".");
        lines.push(plural(i.backpack || 0, "item") + " (" + (i.equipped || 0) + " equipped), Lost Loot " + (i.lost_loot || 0) + ".");
        return lines.join(" ");
    }

    function changes(d) {
        if (!d) return "";
        var out = [];
        if (d.level) out.push("level " + signed(d.level));
        if (d.uvh) out.push("UVH " + signed(d.uvh));
        if (d.items_added) out.push(plural(d.items_added, "item") + " gained");
        if (d.items_removed) out.push(plural(d.items_removed, "item") + " gone");
        if (d.cash) out.push("cash " + (d.cash > 0 ? "+$" : "-$") + commas(Math.abs(d.cash)));
        if (d.playtime > 60) out.push(hours(d.playtime) + " played");
        return out.length ? "Since the version before: " + out.join(", ") + "." : "";
    }

    function versusNow(v, thing) {
        if (v.current) return "This is your save as it is now.";
        if (v.gone_now === undefined) return "";
        var out = [];
        if (v.gone_now) out.push(plural(v.gone_now, "item") + " here that aren't in " + thing + " now");
        if (v.new_now) out.push(plural(v.new_now, "item") + " in " + thing + " now that this version doesn't have (they'd be gone)");
        return out.length ? "Compared with now: " + out.join("; ") + "." : "Same items as now.";
    }

    // endregion
    // region Parts

    function value(api, text, cls) {
        return api.el("p", "bl4sb_value" + (cls ? " " + cls : ""), text);
    }

    function button(api, text, fn, enabled, wide) {
        var b = api.button(text, fn, wide ? "bl4mm_run" : "bl4sb_btn");
        if (enabled === false) b.classList.add("bl4mm_disabled");
        return b;
    }

    // A restore button: the first click arms it ("Sure?"), the second within CONFIRM_MS restores
    function restoreButton(api, id, text, payload, enabled) {
        var armed = sb.armed && sb.armed.id === id && Date.now() - sb.armed.at < CONFIRM_MS;
        return button(api, armed ? "Sure?" : text, function () {
            if (sb.state && sb.state.busy) return;
            if (sb.armed && sb.armed.id === id && Date.now() - sb.armed.at < CONFIRM_MS) {
                sb.armed = null;
                send(payload);
            } else {
                sb.armed = { id: id, at: Date.now() };
                later(refresh, CONFIRM_MS + 50);
            }
            refresh();
        }, enabled);
    }

    function go(view, arg) {
        sb.page = { view: view, arg: arg };
        sb.armed = null;
        refresh();
    }

    function back(api, title) {
        api.title(title);
        api.row("Back", "Back to the list of characters and backups.", [button(api, "Back", function () {
            go("home");
        }, true, true)]);
    }

    // endregion
    // region Views

    function render(api) {
        if (pollTimer === null && !polling) schedule(20);
        var s = sb.state;
        var d = sb.data;
        if (!s || !d) {
            api.title("Save backups");
            api.row("Loading...", "", []);
            return;
        }
        var page = sb.page;
        if (page.view === "char") {
            var ch = null;
            d.characters.forEach(function (c) {
                if (c.guid === page.arg) ch = c;
            });
            if (ch) return renderCharacter(api, s, d, ch);
        } else if (page.view === "bank") {
            return renderBank(api, s, d);
        } else if (page.view === "backups") {
            return renderBackups(api, s, d);
        }
        renderHome(api, s, d);
    }

    function renderStatus(api, s) {
        if (s.busy) api.row("Working", s.busy, [value(api, s.busy, "bl4sb_warn")]);
        if (s.result) {
            api.title("Last restore");
            s.result.results.forEach(function (r) {
                api.row(r.what || r.target || "Restore", r.ok
                    ? "Restored when the game closed. Your saves from just before are in the backup marked \"Before a restore\" (All backups), if you want them back."
                    : "Not restored: " + (r.error || "unknown error") + ". Nothing was changed for this one.",  // prettier-ignore
                    [value(api, r.ok ? "Restored" : "Failed: " + (r.error || "error"), r.ok ? "bl4sb_on" : "bl4sb_off")]);
            });
            api.row("Got it", "Hides this until the next restore.", [button(api, "OK", function () {
                send({ do: "dismiss" });
            }, true, true)]);
        }
        if (s.pending && s.pending.length) {
            api.title("Restores when you quit the game");
            s.pending.forEach(function (p) {
                api.row(p.what, "The game keeps your saves in memory and saves them again as you play and when you quit, so the " +
                    "restore happens right after it closes. Your saves as they are then are backed up first (\"Before a restore\").", [  // prettier-ignore
                    value(api, p.target, "bl4sb_small"),
                    button(api, "Remove", function () {
                        send({ do: "cancel", target: p.target });
                    })
                ]);
            });
            api.row("Quit the game now", "Closes the game normally (it saves first), then the restore happens. Start the game again afterwards.", [
                button(api, s.quitting ? "Quitting..." : "Quit game", function () {
                    send({ do: "quit" });
                }, !s.quitting),
                button(api, "Cancel all", function () {
                    send({ do: "cancel" });
                })
            ]);
        }
    }

    function renderHome(api, s, d) {
        renderStatus(api, s);
        api.title("Backups");
        var last = d.last;
        api.row("Last backup", last
            ? "Made " + last.created_text + " (" + last.label.toLowerCase() + "): " + last.files.length + " save files, " + last.size + ". A backup is made each time you start the game, when a save changed since the one before."
            : "No backup yet. One is made each time you start the game.",  // prettier-ignore
            [value(api, last ? last.created_text : "None yet"),
             button(api, "Back up now", function () {
                 send({ do: "backup" });
             }, !s.busy)]);  // prettier-ignore
        var fit = d.fit_1gb ? " At " + d.per_backup + " a backup, 1 GB holds about " + commas(d.fit_1gb) + " of yours." : "";
        api.row("Backups kept", "Keeps " + (s.keep === "All that fit in 1 GB" ? "as many as fit in 1 GB" : "the newest " + s.keep) +
            " (Backups to keep, below), never more than 1 GB in all. Backups marked Keep are never removed." + fit, [  // prettier-ignore
            value(api, plural(d.count, "backup") + ", " + d.size),
            button(api, "Show all", function () {
                go("backups");
            })
        ]);

        api.title("Characters");
        if (!d.characters.length) api.row("No characters yet", "", []);
        d.characters.forEach(function (c) {
            var n = c.versions.length;
            api.row(c.name + (c.exists ? "" : " (deleted)"),
                (c.exists ? "Slot " + c.slot + " now. " : "Not in your saves any more; its backups are kept. ") + details(c.now) +
                " " + plural(n, "version") + " in the backups. History shows each one and restores it.",  // prettier-ignore
                [value(api, brief(c.now) + ", " + plural(n, "version"), "bl4sb_small"),
                 button(api, "History", function () {
                     go("char", c.guid);
                 })]);  // prettier-ignore
        });

        api.title("Bank");
        var bank = d.bank;
        api.row("Bank", "The bank is shared by every character (in profile.sav). History shows each version of it; a restore puts " +
            "back only the bank, or the whole profile.", [  // prettier-ignore
            value(api, (bank.now !== null && bank.now !== undefined ? plural(bank.now, "item") + ", " : "") + plural(bank.versions.length, "version"), "bl4sb_small"),
            button(api, "History", function () {
                go("bank");
            })
        ]);
        api.row("Backup folder", "Opens the folder in Explorer: one folder per backup with plain copies of your save files. " + d.folder, [
            button(api, "Open folder", function () {
                send({ do: "folder" });
            }, true, true)
        ]);
    }

    function renderCharacter(api, s, d, c) {
        renderStatus(api, s);
        back(api, c.name + " history");
        if (c.exists) api.row("Now", "Your save as it is now (slot " + c.slot + "). " + details(c.now), [value(api, brief(c.now), "bl4sb_small")]);
        c.versions.forEach(function (v) {
            var desc = [details(v.info), changes(v.vs_older), versusNow(v, "the backpack"),
                v.current ? "" : "Restore puts this version back when you quit the game (your save as it is then is backed up first). Shared progress and the bank stay as they are."]  // prettier-ignore
                .filter(function (x) { return x; }).join(" ");
            api.row((v.current ? "Now: " : "") + "Saved " + v.saved_text, desc, [
                value(api, brief(v.info), "bl4sb_small" + (v.ok === false ? " bl4sb_off" : "")),
                restoreButton(api, "c" + v.id, "Restore", { do: "restore", kind: "character", backup: v.backup, file: v.file },
                    !v.current && v.ok !== false && !s.busy)  // prettier-ignore
            ]);
        });
    }

    function renderBank(api, s, d) {
        renderStatus(api, s);
        back(api, "Bank history");
        var bank = d.bank;
        if (bank.now !== null && bank.now !== undefined) api.row("Now", "Your bank as it is now.", [value(api, plural(bank.now, "item"), "bl4sb_small")]);
        if (!bank.versions.length) api.row("No versions yet", "", []);
        bank.versions.forEach(function (v) {
            var since = v.added || v.removed ? "Since the version before: " + v.added + " in, " + v.removed + " out. " : "";
            var desc = since + versusNow(v, "the bank") +
                " Bank puts back only this bank (the rest of your profile stays). Profile puts back the whole profile.sav of that day:" +
                " bank, shared progress, vault cards, settings. Either happens when you quit the game.";  // prettier-ignore
            api.row((v.current ? "Now: " : "") + "Saved " + v.saved_text, desc, [
                value(api, plural(v.items, "item"), "bl4sb_small"),
                restoreButton(api, "b" + v.id, "Bank", { do: "restore", kind: "bank", backup: v.backup }, !v.current && !s.busy),
                restoreButton(api, "p" + v.id, "Profile", { do: "restore", kind: "profile", backup: v.backup }, !s.busy)
            ]);
        });
    }

    function renderBackups(api, s, d) {
        renderStatus(api, s);
        back(api, "All backups");
        d.sessions.forEach(function (b) {
            var who = b.files.map(function (f) {
                return f.who + (f.level ? " (level " + f.level + ")" : "") + (f.ok === false ? " (damaged)" : "");
            }).join(", ");
            var desc = b.label + ", " + b.created_text + ", " + b.size + ": " + who + ". Keep stops it from ever being removed. " +
                "Restore all puts back every save in it when you quit the game (characters into the slot they're in now).";  // prettier-ignore
            api.row(b.created_text + (b.keep ? " (kept)" : ""), desc, [
                value(api, b.label + ", " + b.files.length + " files", "bl4sb_small" + (b.bad ? " bl4sb_warn" : "")),
                button(api, b.keep ? "Unkeep" : "Keep", function () {
                    send({ do: "keep", backup: b.name, keep: !b.keep });
                }),
                restoreButton(api, "a" + b.name, "Restore all", { do: "restore", kind: "all", backup: b.name }, !s.busy)
            ]);
        });
    }

    // The options the page already has as rows (they stay in the console's mod menu)
    var HIDDEN = ["Back up now", "Open the backup folder"];
    function keep(node) {
        return HIDDEN.indexOf(node.id) < 0 && HIDDEN.indexOf(node.name) < 0;
    }

    // endregion
    // region Stylesheet

    function checkCss() {
        ns.get(BASE + "backups.css", function (css) {
            if (!alive()) return;
            if (css && css !== sb.cssText) {
                sb.cssText = css;
                var style = document.getElementById("bl4sb_css");
                if (!style) {
                    style = document.createElement("style");
                    style.id = "bl4sb_css";
                    document.head.appendChild(style);
                }
                style.textContent = css;
            }
            if (ns.dev !== false) later(checkCss, showing() ? CSS_MS : CSS_MS * 5);
        });
    }

    // endregion

    ext.teardowns[KEY] = function () {
        sb.gen = 0;
        timers.forEach(clearTimeout);
        if (pollTimer !== null) clearTimeout(pollTimer);
        if (ns.modPages && ns.modPages[MOD] && ns.modPages[MOD].gen === gen) delete ns.modPages[MOD];
    };

    ns.modPages = ns.modPages || {};
    ns.modPages[MOD] = { gen: gen, render: render, keep: keep, moreTitle: "Settings" };
    sb.stateText = null;
    checkCss();
    watch();
    refresh();
    log("ready");
})();
