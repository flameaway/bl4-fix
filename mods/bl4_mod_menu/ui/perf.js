"use strict";

// The Performance page of the MODS screen (with Updates, Discover, Repositories): a benchmark of
// what every mod costs. Was the separate BL4 Mod Profiler's page until 2026-10-29.
//
// The Mod Menu runs this file in its menu page as one of its own extensions (extensions.js) and again
// whenever it changes, so it tears its last copy down first, then registers ns.modPages[MOD], where
// MOD is menu.js's key for this page: render() draws it with the Mod Menu's own rows and buttons.
// Python (bl4_mod_menu/perf/page.py) serves the state through GET /ext?k=bl4perf while the page
// shows and takes clicks as {op: "ext", ext: "bl4perf", do}.

(function () {
    var ns = window.bl4ModMenu;
    if (!ns || !ns.ext) return;
    var ext = ns.ext;
    var KEY = "bl4perf";
    var MOD = "\u0002perf"; // menu.js's PERF
    var BASE = "coui://uiresources/bl4modmenu/";
    var POLL_SHOWN_MS = 500;
    var WATCH_MS = 500;
    var CSS_MS = 2000;

    if (ext.teardowns[KEY]) {
        try {
            ext.teardowns[KEY]();
        } catch (err) {}
    }
    // Survives reloads of this file; each copy takes a new, ever higher number
    var pf = ns.bl4perfPage || (ns.bl4perfPage = {});
    pf.counter = Math.max(pf.counter || 0, pf.gen || 0) + 1;
    var gen = pf.counter;
    pf.gen = gen;

    var timers = [];
    function alive() {
        return pf.gen === gen;
    }
    function later(fn, ms) {
        var id = setTimeout(function () {
            var at = timers.indexOf(id);
            if (at >= 0) timers.splice(at, 1);
            if (alive()) fn();
        }, ms);
        timers.push(id);
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
            if (showing()) schedule(POLL_SHOWN_MS);
        });
    }

    function onState(next) {
        var text = JSON.stringify(next);
        var first = !pf.state;
        pf.state = next;
        if (first) pf.lastToast = next.toast.id;
        if (next.toast.id !== pf.lastToast) {
            pf.lastToast = next.toast.id;
            if (ns.modToast && next.toast.text) ns.modToast(next.toast.text, next.toast.error);
        }
        if (text === pf.stateText) return;
        pf.stateText = text;
        if (ns.modPageRefresh) ns.modPageRefresh(MOD);
    }

    function send(payload) {
        ext.send(KEY, payload);
        if (!polling) schedule(20);
    }

    function watch() {
        if (showing() && pollTimer === null && !polling) poll();
        later(watch, WATCH_MS);
    }

    // endregion
    // region Page

    function fixed(n, digits) {
        return (Math.round(n * Math.pow(10, digits)) / Math.pow(10, digits)).toFixed(digits);
    }

    function value(api, text, cls) {
        return api.el("p", "bl4pf_value" + (cls ? " " + cls : ""), text);
    }

    function button(api, text, fn, wide) {
        return api.button(text, fn, wide ? "bl4mm_run" : "bl4pf_btn");
    }

    var VERDICT = {
        free: ["Free", "bl4pf_free"],
        light: ["Light", "bl4pf_light"],
        heavy: ["Heavy", "bl4pf_heavy"],
        hitches: ["Hitches", "bl4pf_bad"]
    };

    function modDesc(m) {
        var lines = [];
        lines.push(fixed(m.ms_frame, 3) + " ms of Python per frame on average, its own worst call " + fixed(m.own_worst_ms, 2) + " ms" +
            (m.own_over_5ms ? ", " + m.own_over_5ms + " of its calls over 5 ms (a visible hitch each)" : "") + ".");  // prettier-ignore
        if (m.outside_slow) {
            lines.push(m.outside_slow + " slow call(s) (up to " + fixed(m.worst_ms, 2) + " ms) weren't its fault: a Python GC pause, or a " +
                "stall that hit several mods in the same frame.");  // prettier-ignore
        }
        if (m.hooks.length) {
            lines.push("");
            lines.push("Hooks (ms per frame, worst ms, calls per second):");
            m.hooks.slice(0, 8).forEach(function (h) {
                lines.push("  " + h.label + ":  " + fixed(h.ms_frame, 3) + ",  " + fixed(h.worst_ms, 2) + ",  " + Math.round(h.calls_s));
            });
            if (m.hooks.length > 8) lines.push("  and " + (m.hooks.length - 8) + " more (see the report)");
        } else {
            lines.push("No hooks seen: it does its work some other way (or only on events that didn't happen).");
        }
        if (m.threads.length) {
            lines.push("");
            lines.push("Background threads (ms of CPU per second):");
            m.threads.forEach(function (t) {
                lines.push("  " + t.name + ":  " + fixed(t.cpu_ms_s, 1));
            });
        }
        return lines.join("\n");
    }

    function render(api) {
        if (pollTimer === null && !polling) schedule(20);
        var s = pf.state;
        api.title("Benchmark");
        if (!s) {
            api.row("Loading...", "", []);
            return;
        }
        var busy = s.phase !== "idle";
        var runDesc = "Close the menus and play normally for " + Math.round(s.length) + " s: every enabled mod is timed meanwhile, " +
            "other modders' mods too. A fight or the open world shows the most. Notices on screen say when it starts and ends.";  // prettier-ignore
        if (!busy) {
            api.row("Run benchmark", runDesc, [
                button(api, "Run", function () {
                    send({ do: "run" });
                    // Measure play, not this screen: close the menus (Python waits for that)
                    if (ns.modCloseScreen && ns.context !== "main") ns.modCloseScreen(true);
                }, true)
            ]);
        } else {
            var status = s.phase === "running" ? "Measuring, " + Math.ceil(s.left) + " s left" :
                s.phase === "settling" ? "Starting..." : "Starts when the menus close";  // prettier-ignore
            api.row("Benchmark", runDesc, [
                value(api, status, "bl4pf_warn"),
                button(api, "Stop", function () {
                    send({ do: "stop" });
                })
            ]);
        }
        api.row("Length", "How long a benchmark measures. Longer runs average out more, and catch rarer hitches.", [
            value(api, Math.round(s.length) + " s"),
            button(api, "Change", function () {
                send({ do: "length" });
            })
        ]);
        if (s.message) api.row("Last message", s.message, [value(api, s.message, "bl4pf_small")]);

        var r = s.result;
        if (!r) return;
        api.title("Last run: " + r.at + (r.world ? ", " + r.world : ""));
        api.row("Frame time", Math.round(r.seconds) + " s, " + r.frames + " frames. Median " + fixed(r.median_ms, 2) + " ms, 99% of frames under " +
            fixed(r.p99_ms, 2) + " ms, worst " + fixed(r.worst_ms, 1) + " ms. Hitches: frames over " + Math.round(r.hitch_ms) +
            " ms (twice the median, at least 33 ms).", [  // prettier-ignore
            value(api, Math.round(r.fps) + " fps, median " + fixed(r.median_ms, 1) + " ms, " + r.hitches + " hitches")
        ]);
        var slow = r.slowest.map(function (f) {
            return "  " + fixed(f.ms, 1) + " ms frame: mods " + fixed(f.mods_ms, 2) + " ms" + (f.top ? ", most " + f.top + " " + fixed(f.top_ms, 2) : "");
        });
        api.row("All mods", "Python time of every mod's hooks together, per frame, and its share of a median frame. The profiler's own " +
            "timing added about " + fixed(r.overhead_ms_frame, 3) + " ms per frame during the run (not counted)." +
            (r.dispatch_us ? "\n\nNot counted either: the SDK's own cost of calling each hook, about " + fixed(r.dispatch_us, 1) + " us a call, " +
                Math.round(r.calls_frame) + " calls a frame = ~" + fixed(r.dispatch_ms_frame, 2) + " ms per frame." : "") +
            "\n\nSlowest frames:\n" +
            slow.join("\n") + (r.other_threads_ms_s > 0.05 ? "\n\nPython threads not tied to a mod: " + fixed(r.other_threads_ms_s, 1) + " ms CPU per second." : ""), [  // prettier-ignore
            value(api, fixed(r.mods_ms_frame, 2) + " ms per frame (" + fixed(r.share * 100, 1) + "%)", r.mods_ms_frame >= 1 ? "bl4pf_heavy" : "")
        ]);
        if (r.gc.length) {
            var worst = 0, count = 0;
            r.gc.forEach(function (g) {
                worst = Math.max(worst, g.worst_ms);
                count += g.count;
            });
            api.row("Python garbage collection", r.gc.map(function (g) {
                return "Generation " + g.gen + ": " + g.count + "x, average " + fixed(g.avg_ms, 2) + " ms, worst " + fixed(g.worst_ms, 2) + " ms";
            }).join("\n") + "\n\nShared by every mod. Performance Fixes keeps long full collections away.", [  // prettier-ignore
                value(api, count + "x, worst " + fixed(worst, 1) + " ms", worst > 5 ? "bl4pf_bad" : "")
            ]);
        }

        api.title("Per mod (most expensive first)");
        r.mods.forEach(function (m) {
            var v = VERDICT[m.verdict] || VERDICT.free;
            var text = fixed(m.ms_frame, 3) + " ms/frame, worst " + fixed(m.own_worst_ms, 1) + " ms";
            if (m.threads_ms_s >= 1) text += ", threads " + fixed(m.threads_ms_s, 0) + " ms/s";
            api.row(m.name, modDesc(m), [value(api, text, "bl4pf_small"), value(api, v[0], "bl4pf_tag " + v[1])]);
        });
        api.row("Report file", "The whole report as text (every hook, slow calls, slowest frames), to keep or send to a mod's author:\n" + s.report +
            "\n\n" + r.mods_off + " switched-off mod(s) weren't measured. " + r.notes.join(" "), [  // prettier-ignore
            button(api, "Open", function () {
                send({ do: "open" });
            })
        ]);
    }

    // endregion
    // region Stylesheet

    function checkCss() {
        ns.get(BASE + "perf.css", function (css) {
            if (!alive()) return;
            if (css && css !== pf.cssText) {
                pf.cssText = css;
                var style = document.getElementById("bl4pf_css");
                if (!style) {
                    style = document.createElement("style");
                    style.id = "bl4pf_css";
                    document.head.appendChild(style);
                }
                style.textContent = css;
            }
            later(checkCss, showing() ? CSS_MS : CSS_MS * 5);
        });
    }

    // endregion

    ext.teardowns[KEY] = function () {
        pf.gen = 0;
        timers.forEach(clearTimeout);
        if (pollTimer !== null) clearTimeout(pollTimer);
        if (ns.modPages && ns.modPages[MOD] && ns.modPages[MOD].gen === gen) delete ns.modPages[MOD];
    };

    ns.modPages = ns.modPages || {};
    ns.modPages[MOD] = { gen: gen, render: render };
    pf.stateText = null;
    checkCss();
    watch();
    if (ns.modPageRefresh) ns.modPageRefresh(MOD);
})();
