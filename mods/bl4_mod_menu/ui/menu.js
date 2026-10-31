"use strict";

// BL4 Mod Menu: a MODS entry in the pause menu that opens a settings screen for every SDK mod.
// On the title screen (ns.context "main", see main_loader.js) the entry goes under Options
// there. Both show a "!" while mod updates are waiting; the screen's Updates page lists them
// with Update all (updates.py does the work).
//
// The screen is built from the game's own markup, fetched at runtime from the player's install:
// the Options menu's entry templates (menu_options/menu_options.html) and the toggle, slider,
// stepper, rebind and list button components. Their stylesheets are already in this page, so it
// looks and animates like the game's Options menu. The templates are cleaned before use: data
// bindings and localisation ids are stripped and the custom elements become plain divs, so the
// game's component scripts never try to drive them; this file sets their state classes itself.
//
// Python (web.py) serves the mods and options as JSON on 127.0.0.1 (port and token in
// session.json) and takes actions back: {op: set/reset/press/enable/capture, mod, path, value}.
// It applies them on the game thread, so every change takes effect at once and is saved.
//
// This file is loaded by loader.js and may be re-run at any time (hot reload): it tears the
// previous copy down first. State that must survive (templates, the server link) lives on ns.

(function () {
    var ns = window.bl4ModMenu;
    if (!ns) return;
    if (ns.teardown) {
        try {
            ns.teardown();
        } catch (err) {}
    }
    // Each copy of this file gets a new number; only the newest one (ns.gen) keeps running. The
    // counter only goes up, so a retired copy can never match again.
    ns.counter = Math.max(ns.counter || 0, ns.gen || 0) + 1;
    var gen = ns.counter;
    ns.gen = gen;

    var BASE = ns.BASE;
    var POLL_OPEN_MS = 200;
    var POLL_IDLE_MS = 1000;
    // While typing into the Repositories box, so the letters show up quickly
    var POLL_TYPING_MS = 60;
    var TOAST_MS = 4000;
    var TEMPLATES = {
        options: "coui://uiresources/menu_options/menu_options.html",
        toggle: "coui://uiresources/components/toggle/toggle.html",
        slider: "coui://uiresources/components/slider/slider.html",
        stepper: "coui://uiresources/components/stepper/stepper.html",
        rebind: "coui://uiresources/components/rebind_entry/rebind_entry.html",
        button: "coui://uiresources/components/list_button/list_button.html"
    };
    var MAX_STEPPER_PIPS = 12;
    // view.mod for the Updates, Discover and Repositories pages (not mod names: no mod can be called these)
    var UPDATES = "\u0002updates";
    var DISCOVER = "\u0002discover";
    var REPOS = "\u0002repos";
    var REPORT = "\u0002report";
    // Performance: drawn by ui/perf.js (an extension of ours) through ns.modPages[PERF]
    var PERF = "\u0002perf";
    // Search: every mod's settings that match what's typed in its box
    var SEARCH = "\u0002search";
    // The most results drawn at once (each is a full Options row, redrawn on every poll while typing)
    var MAX_SEARCH_HITS = 60;
    // Our own settings: listed with our pages, right under Report a problem, not among the mods
    var SELF_NAME = "BL4 Mod Menu";
    // The Discover / Repositories data (GET /repos) is re-read this often while one of those pages shows
    var REPOS_POLL_MS = 1000;
    // A gap this long between polls means this page was asleep (we poll every 200 ms while open)
    var WAKE_GAP_MS = 1500;

    var timers = [];
    var docListeners = [];

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

    function listenDoc(type, fn) {
        document.addEventListener(type, fn);
        docListeners.push([type, fn]);
    }

    function unlistenDoc(type, fn) {
        document.removeEventListener(type, fn);
        docListeners = docListeners.filter(function (l) {
            return l[0] !== type || l[1] !== fn;
        });
    }

    ns.log = function (text) {
        if (window.bl4dReport) window.bl4dReport("[mod menu] " + text);
    };

    // region Server link

    var link = ns.link || (ns.link = { port: 0, token: "", text: null, state: null, lastOpenId: null, lastMsgId: null });
    // Find the server again: an older copy may have linked to another game's (two games on one PC)
    link.port = 0;

    // done({port, token, dev}) for this game's server, or done(null). session.json lists every
    // running game's server; with more than one, the one that answers /hello is ours (web.py checks
    // which process the connection comes from). Shared with extensions.js.
    ns.findSession = function (done) {
        ns.get(BASE + "session.json", function (text) {
            var session = null;
            try {
                session = JSON.parse(text);
            } catch (err) {}
            if (!session || !session.port) return done(null);
            var servers = session.servers || [];
            if (servers.length < 2) return done(servers[0] || session);
            var i = servers.length;  // newest last: try it first
            (function next() {
                if (--i < 0) return done(null);
                var s = servers[i];
                var xhr = new XMLHttpRequest();
                xhr.onreadystatechange = function () {
                    if (xhr.readyState !== 4) return;
                    if (xhr.status === 200) done(s);
                    else next();
                };
                try {
                    xhr.open("GET", "http://127.0.0.1:" + s.port + "/hello?t=" + s.token, true);
                    xhr.send(null);
                } catch (err) {
                    next();
                }
            })();
        });
    };

    function loadSession(done) {
        ns.findSession(function (session) {
            link.port = session ? session.port : 0;
            link.token = session ? session.token : "";
            if (session) ns.dev = session.dev;  // false: a release, our files can't change (no hot reload)
            done();
        });
    }

    function http(method, path, body, done) {
        var xhr = new XMLHttpRequest();
        xhr.onreadystatechange = function () {
            if (xhr.readyState === 4 && done) done(xhr.status, xhr.responseText);
        };
        try {
            var sep = path.indexOf("?") < 0 ? "?" : "&";
            xhr.open(method, "http://127.0.0.1:" + link.port + path + sep + "t=" + link.token, true);
            xhr.send(body || null);
        } catch (err) {
            if (done) done(-1, "");
        }
    }

    function send(action) {
        if (!link.port) return;
        http("POST", "/action", JSON.stringify(action), function () {
            pollSoon();
        });
    }

    var pollTimer = null;
    function pollSoon() {
        if (pollTimer !== null) clearTimeout(pollTimer);
        pollTimer = setTimeout(function () {
            pollTimer = null;
            if (alive()) poll();
        }, 30);
    }

    // done (optional): called once this poll has answered (or failed)
    function poll(done) {
        if (pollTimer !== null) return;
        if (!link.port) {
            loadSession(function () {
                if (!alive()) return;
                if (link.port) poll(done);
                else {
                    schedulePoll(POLL_IDLE_MS);
                    if (done) done();
                }
            });
            return;
        }
        var where = ns.context === "main" ? "&ctx=main" : "";
        http("GET", "/state?open=" + (view.open ? 1 : 0) + where, null, function (status, text) {
            if (!alive()) return;
            if (status !== 200) {
                ns.log("poll failed " + status + ", re-reading session");
                // The mod restarted its server (hot reload, game restart): find it again
                link.port = 0;
                schedulePoll(POLL_IDLE_MS);
                if (done) done();
                return;
            }
            link.polledAt = Date.now();
            if (text !== link.text) {
                link.text = text;
                try {
                    onState(JSON.parse(text));
                } catch (err) {
                    ns.log("bad state: " + err);
                }
            }
            schedulePoll(!view.open ? POLL_IDLE_MS : boxState().typing ? POLL_TYPING_MS : POLL_OPEN_MS);
            if (done) done();
        });
    }

    function schedulePoll(ms) {
        if (pollTimer !== null) clearTimeout(pollTimer);
        pollTimer = setTimeout(function () {
            pollTimer = null;
            if (alive()) poll();
        }, ms);
    }

    // endregion
    // region Waking up, and hiding the game's menus

    // This page sleeps while none of the game's menus shows, timers included: on waking, the next
    // poll could still be most of POLL_IDLE_MS away. So F7/F8 showed the pause menu for up to a
    // second before our screen, and a screen left open when the menus closed showed again on the
    // next Esc. A frame callback notices the wake (a long gap between frames), hides everything on
    // this page and polls at once (Python answers on its next frame); once every poll asked for has
    // answered, the page shows again with whatever is current: our screen, or the game's menu.
    // Extensions that open screens add a poll of their own: ns.wake.hooks[key] = function (done).
    var WAKE_FRAME_GAP_MS = 300;
    var WAKE_COVER_MS = 400;  // never hide the page longer than this for a wake
    var CLOSE_COVER_MS = 1000;  // after closing the menu we opened: if it's still up by then, show it
    var wake = ns.wake || (ns.wake = { hooks: {} });
    // reason -> Date.now() deadline. While any is set, the page's top-level layers are hidden.
    var covers = {};

    function applyCover() {
        var on = false;
        for (var k in covers) if (Object.prototype.hasOwnProperty.call(covers, k)) on = true;
        var kids = document.body ? document.body.children : [];
        for (var i = 0; i < kids.length; i++) {
            if (kids[i].tagName === "STYLE" || kids[i].tagName === "SCRIPT") continue;
            if (kids[i].classList.contains("bl4mm_covered") !== on) kids[i].classList.toggle("bl4mm_covered", on);
        }
    }

    function cover(reason, ms) {
        covers[reason] = Date.now() + ms;
        applyCover();
    }

    function onWake() {
        // The menus closed while we slept: a cover from before (closing our own menu) is done
        covers = {};
        cover("wake", WAKE_COVER_MS);
        var pending = 1;
        var waking = {};
        function done() {
            if (waking.done || !alive()) return;
            if (--pending > 0) return;
            waking.done = true;
            // Show the page from the next frame, once what the answers changed has been laid out
            if (Object.prototype.hasOwnProperty.call(covers, "wake")) covers.wake = Date.now();
        }
        if (pollTimer !== null) {
            clearTimeout(pollTimer);
            pollTimer = null;
        }
        poll(done);
        for (var key in wake.hooks) {
            if (!Object.prototype.hasOwnProperty.call(wake.hooks, key)) continue;
            pending++;
            try {
                wake.hooks[key](done);
            } catch (err) {
                ns.log("wake hook " + key + ": " + err);
                done();
            }
        }
    }

    var lastFrame = Date.now();
    function onFrame() {
        if (!alive()) return;
        var now = Date.now();
        var gap = now - lastFrame;
        lastFrame = now;
        if (gap > WAKE_FRAME_GAP_MS) onWake();
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

    // Closes the game's menu we opened for a screen (F7/F8), without showing it on the way out:
    // the page stays hidden until it sleeps. The pause menu's own Back to Game entry closes it at
    // once; Esc (posted by Python, held for a few frames) is the fallback.
    function closeGameMenu() {
        cover("closing", CLOSE_COVER_MS);
        var list = document.querySelector('[data-input-id="menu_pause_list"]');
        var resume = list && list.firstElementChild;
        if (resume && list.getBoundingClientRect().height > 0) {
            try {
                resume.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
                // Closed, this page sleeps and the timer only fires at the next wake, long after:
                // then it's too late to matter. Still running soon after: the click didn't take.
                var at = Date.now();
                later(function () {
                    if (Date.now() - at < 600) send({ op: "close_menus" });
                }, 300);
                return;
            } catch (err) {
                ns.log("resume click: " + err);
            }
        }
        send({ op: "close_menus" });
    }
    ns.closeGameMenu = closeGameMenu;

    // endregion
    // region Templates

    function cleanMarkup(html) {
        return html
            .replace(/<script[\s\S]*?<\/script>/gi, "")
            .replace(/<link[^>]*>/gi, "")
            .replace(/<(\/?)(?:gbx|oak|widget)-[a-z-]+/gi, "<$1div")
            .replace(/\s(?:data-bind[\w-]*|data-l10n-id|gbx-data-l10n-id|data-input[\w-]*|gue-comment|tooltip-data-key)="[^"]*"/gi, "");
    }

    function parse(html) {
        var holder = document.createElement("div");
        holder.innerHTML = cleanMarkup(html);
        return holder;
    }

    function loadTemplates(done) {
        if (ns.templates) {
            done();
            return;
        }
        var names = Object.keys(TEMPLATES);
        var got = {};
        var left = names.length;
        names.forEach(function (name) {
            ns.get(TEMPLATES[name], function (html) {
                got[name] = html;
                if (--left === 0) {
                    var t = {};
                    if (got.options) {
                        var opts = parse(got.options);
                        t.title = opts.querySelector(".menu_options_title_entry");
                        t.regular = opts.querySelector(".menu_options_regular_entry");
                    }
                    ["toggle", "slider", "stepper", "rebind", "button"].forEach(function (n) {
                        if (got[n]) t[n] = parse(got[n]).firstElementChild;
                    });
                    ns.templates = t;
                    done();
                }
            });
        });
    }

    function make(name, fallbackClass) {
        var t = ns.templates && ns.templates[name];
        if (t) return t.cloneNode(true);
        return el("div", fallbackClass || "bl4mm_fallback_" + name);
    }

    // endregion
    // region DOM helpers

    function el(tag, cls, text) {
        var e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined) e.textContent = text;
        return e;
    }

    function q(root, sel) {
        return root ? root.querySelector(sel) : null;
    }

    function hoverable(target, effectOn) {
        var host = effectOn || target;
        target.addEventListener("mouseenter", function () {
            host.classList.add("hover_effect");
        });
        target.addEventListener("mouseleave", function () {
            host.classList.remove("hover_effect");
        });
    }

    function onClick(target, fn) {
        target.addEventListener("click", function (evt) {
            evt.stopPropagation();
            fn(evt);
        });
    }

    // A list button from the game's component, with our text
    function listButton(text, fn, extraClass) {
        var b = make("button", "list_btn");
        b.classList.add("bl4mm_btn");
        if (extraClass) b.classList.add(extraClass);
        var label = q(b, ".button_txt") || b;
        label.textContent = text;
        hoverable(b);
        if (fn) onClick(b, fn);
        return b;
    }

    // endregion
    // region State

    // item: the Discover entry shown full size (its id), shot: which of its pictures
    var view = { open: false, mod: null, nested: [], details: null, dragging: false, installNew: {}, newTags: {},
                 item: null, shot: 0 };  // prettier-ignore
    var ui = {};

    function currentMods() {
        return (link.state && link.state.mods) || [];
    }

    function updatesState() {
        return (link.state && link.state.updates) || null;
    }

    function updatesPage() {
        var u = updatesState() || {};
        return { name: "Updates", pseudo: UPDATES, desc: u.message || "", version: u.release || "", status: "", coop: "" };
    }

    // The small repositories summary in /state (counts, the toast); the pages' data is repoData()
    function reposState() {
        return (link.state && link.state.repos) || {};
    }

    // Updates waiting, BL4 Fix's and the repositories': the "!" on Updates
    function updatesWaiting() {
        var u = updatesState() || {};
        return (u.updates !== undefined ? u.updates : u.count || 0) + (reposState().updates || 0);
    }

    // Mods you never had that weren't shown to you yet: the "!" on Discover (they're listed there,
    // not on Updates)
    function freshMods() {
        var u = updatesState() || {};
        return u.fresh || [];
    }

    // "Check for updates automatically?" isn't answered yet (a new install): Updates asks, with a "!"
    function updatesQuestion() {
        return !!(updatesState() || {}).ask;
    }

    // The "!" on the MODS entries: anything to look at
    function badgeCount() {
        return updatesWaiting() + freshMods().length + (updatesQuestion() ? 1 : 0);
    }

    // Where MODS opens while it has a "!": updates (or the question) first, else the new mods
    function badgePage() {
        return updatesWaiting() || updatesQuestion() ? UPDATES : freshMods().length ? DISCOVER : null;
    }

    function pseudoPage(key) {
        if (key === UPDATES) return updatesPage();
        if (key === DISCOVER) return { name: "Discover", pseudo: DISCOVER, desc: "", version: "", status: "", coop: "" };
        if (key === REPOS) return { name: "Repositories", pseudo: REPOS, desc: "", version: "", status: "", coop: "" };
        if (key === REPORT) return { name: "Report a problem", pseudo: REPORT, desc: "", version: "", status: "", coop: "" };
        if (key === PERF) return { name: "Performance", pseudo: PERF, desc: "", version: "", status: "", coop: "" };
        if (key === SEARCH) return { name: "Search", pseudo: SEARCH, desc: "", version: "", status: "", coop: "" };
        return null;
    }

    function selectedMod() {
        var pseudo = pseudoPage(view.mod);
        if (pseudo) return pseudo;
        var mods = currentMods();
        // No list yet (the screen opened before a full snapshot came): keep the mod asked for
        if (!mods.length) return null;
        for (var i = 0; i < mods.length; i++) if (mods[i].name === view.mod) return mods[i];
        for (var j = 0; j < mods.length; j++) {
            if (mods[j].options && mods[j].options.length && mods[j].name !== "Python SDK") {
                view.mod = mods[j].name;
                return mods[j];
            }
        }
        view.mod = mods.length ? mods[0].name : null;
        return mods[0] || null;
    }

    function onState(state) {
        // A new server (game restart, hot reload) counts from zero again: forget the old ids
        var first = link.state === null || link.stateToken !== link.token;
        link.stateToken = link.token;
        // While our screen is closed Python sends only the header (no mods list, to save work):
        // keep the last list, so opening draws at once; the open=1 poll right after refreshes it
        if (!state.mods && link.state && link.state.mods) state.mods = link.state.mods;
        link.state = state;
        if (first) {
            link.lastOpenId = null;
            link.lastMsgId = state.message.id;
            link.lastCloseId = state.close;
            link.lastEscId = state.esc;
            link.lastRepoMsgId = state.repos && state.repos.message ? state.repos.message.id : null;
            repoLink.rev = null;  // a new server: its revisions mean nothing to what we kept
            for (var asked in askedPictures) delete askedPictures[asked];  // nor does what we asked the old one for
        }
        var rm = state.repos && state.repos.message;
        if (rm && rm.id !== link.lastRepoMsgId) {
            link.lastRepoMsgId = rm.id;
            if (rm.text) toast(rm.text, rm.error);
        }
        // Something on the repositories' side moved: re-read their pages' data now
        if (state.repos && state.repos.rev !== link.lastRepoRev) {
            link.lastRepoRev = state.repos.rev;
            if (repoPageShows()) pollReposSoon();
        }
        if (state.close !== link.lastCloseId) {
            // The open key, pressed while we're showing: same as Back from the top page
            link.lastCloseId = state.close;
            if (view.open) {
                view.nested = [];
                view.backTo = null;
                back();
            }
        }

        if (state.esc !== link.lastEscId) {
            // Esc on the title screen (Python swallows it there, or the game would ask "quit?"): Back
            link.lastEscId = state.esc;
            if (view.open) back();
        }
        // Typing goes with the Repositories page
        if (state.box && state.box.typing && !(view.open && view.mod === REPOS) && !link.boxStopSent) {
            link.boxStopSent = true;
            send({ op: "box_stop" });
        } else if (!(state.box && state.box.typing)) {
            link.boxStopSent = false;
        }
        // and the search's with the Search page
        if (state.search && state.search.typing && !(view.open && view.mod === SEARCH) && !link.searchStopSent) {
            link.searchStopSent = true;
            send({ op: "search_stop" });
        } else if (!(state.search && state.search.typing)) {
            link.searchStopSent = false;
        }

        if (state.message.id !== link.lastMsgId) {
            link.lastMsgId = state.message.id;
            toast(state.message.text, state.message.error);
        }
        // The game closed its menus since we opened (Esc, Back, ...): we went with them. Python
        // counts every close, so this holds even when this page slept through it.
        if (view.open && state.menus_closed !== view.closeMark) {
            closeScreen();
        }
        // Open requests come from F7. This page may only wake up (its menu opening) just after one,
        // so act on any we haven't seen while it's fresh; each is handled once either way.
        if (state.open.id !== link.lastOpenId) {
            link.lastOpenId = state.open.id;
            if (state.open.fresh) openScreen(state.open.mod, state.open.own);
        }
        if (view.open && !view.dragging) render();
    }

    // endregion
    // region Screen

    function build() {
        var root = el("div", "bl4mm_root bl4mm_hidden");
        root.id = "bl4mm_root";
        root.appendChild(el("div", "fill_content menu_bg_nostandin bl4mm_bg"));
        root.appendChild(el("div", "bl4mm_shade"));

        var frame = el("div", "bl4mm_frame");
        var header = el("div", "bl4mm_header");
        header.appendChild(el("p", "bl4mm_title", "MODS"));
        ui.crumb = el("p", "bl4mm_crumb");
        header.appendChild(ui.crumb);
        // The search bar, right of the crumb: on every page
        header.appendChild(el("div", "bl4mm_header_gap"));
        ui.searchBar = buildSearchBar();
        header.appendChild(ui.searchBar.root);
        frame.appendChild(header);

        var body = el("div", "bl4mm_body");
        // The mods column scrolls inside a wrapper too, with its own game scrollbar
        var modsWrap = el("div", "bl4mm_mods_wrap");
        ui.mods = el("div", "bl4mm_mods");
        modsWrap.appendChild(ui.mods);
        body.appendChild(modsWrap);

        // The column scrolls inside a wrapper: the game's scrollbar goes next to it, on its right edge
        var optionsWrap = el("div", "bl4mm_options_wrap");
        ui.options = el("div", "bl4mm_options menu_options_entry_list");
        optionsWrap.appendChild(ui.options);
        body.appendChild(optionsWrap);

        var details = el("div", "menu_options_details_container bl4mm_details");
        details.appendChild(el("div", "menu_options_details_gradient_bg menu_options_details_gradient_bg_bottom"));
        var inner = el("div", "menu_options_details_container_inner");
        var d = el("div", "menu_options_details");
        ui.detailsTitle = el("div", "menu_options_details_title med_text_1");
        ui.detailsMeta = el("p", "bl4mm_details_meta small_text_2");
        ui.detailsBody = el("p", "menu_options_details_description small_text_2");
        d.appendChild(ui.detailsTitle);
        d.appendChild(ui.detailsMeta);
        d.appendChild(ui.detailsBody);
        // A Discover entry's first picture, under its description (empty otherwise)
        ui.detailsPic = el("div", "bl4mm_details_pic");
        d.appendChild(ui.detailsPic);
        inner.appendChild(d);
        details.appendChild(inner);
        body.appendChild(details);
        frame.appendChild(body);

        var footer = el("div", "bl4mm_footer");
        ui.back = listButton("Back", back, "bl4mm_back");
        footer.appendChild(ui.back);
        ui.hint = el("p", "bl4mm_hint", "Changes take effect right away and are saved.");
        footer.appendChild(ui.hint);
        frame.appendChild(footer);

        ui.toast = el("p", "bl4mm_toast bl4mm_hidden");
        frame.appendChild(ui.toast);

        root.appendChild(frame);
        // Clicks on empty space shouldn't fall through to the pause menu underneath
        root.addEventListener("click", function (evt) {
            evt.stopPropagation();
        });
        // A click anywhere but the box being typed in stops typing (capture: entries stop propagation)
        root.addEventListener("click", function (evt) {
            stopTypingOutside(evt.target);
        }, true);  // prettier-ignore
        document.body.appendChild(root);
        ui.root = root;
        // The game's scrollbar (the inventory and popups use it); without it, the wheel still scrolls
        if (typeof window.OAKScroll === "function") {
            try {
                ui.scroller = window.OAKScroll(ui.options);
                ui.modsScroller = window.OAKScroll(ui.mods);
            } catch (err) {
                ns.log("scrollbar failed: " + err);
            }
        }
    }

    // own: we opened the game's menu just for this (F7), so Back closes that too
    function openScreen(modName, own) {
        view.own = !!own;
        // Closes of the game's menus from here on close us too
        view.closeMark = link.state ? link.state.menus_closed : undefined;
        if (modName) {
            if (modName !== view.mod) view.nested = [];
            view.mod = modName;
        }
        view.open = true;
        view.details = null;
        view.backTo = null;
        // The status menus (inventory etc., where F7 opens us) draw their tab bar in a layer above
        // this page, so start below it there; the pause menu has none
        var pause = document.querySelector('[data-input-id="menu_pause_list"]');
        var overPause = !!pause && pause.getBoundingClientRect().height > 0;
        ui.root.classList.toggle("bl4mm_below_tabs", ns.context !== "main" && !overPause);
        ui.root.classList.remove("bl4mm_hidden");
        render();
        pollSoon();
    }

    function closeScreen() {
        view.open = false;
        view.own = false;
        // A slider drag whose mouseup we never got must not set anything later
        if (view.endDrag) view.endDrag(null);
        view.dragging = false;
        if (ui.root) ui.root.classList.add("bl4mm_hidden");
        if (link.state && link.state.capture) send({ op: "close_capture" });
    }

    function back() {
        if (view.item) {
            view.item = null;
            view.details = null;
            render();
            return;
        }
        // A setting opened from the Search page: Back goes back to the results
        if (view.backTo && view.nested.length <= view.backTo.depth) {
            view.mod = view.backTo.mod;
            view.nested = [];
            view.backTo = null;
            view.details = null;
            ui.options.scrollTop = 0;
            render();
            return;
        }
        if (view.nested.length) {
            view.nested.pop();
            view.details = null;
            render();
            return;
        }
        var own = view.own;
        closeScreen();
        if (own) closeGameMenu();
    }

    function render() {
        if (!ui.root) return;
        try {
            var mod = selectedMod();
            renderSearchBar();
            renderMods();
            renderOptions(mod);
            renderDetails(mod);
        } catch (err) {
            ns.log("render failed: " + err + " " + (err && err.stack));
        }
        updateScrollbar();
    }

    // The scrollbar measures the column again once the new rows are laid out (a frame later here)
    function updateScrollbar() {
        if (!ui.scroller || ui.scrollbarQueued) return;
        ui.scrollbarQueued = true;
        requestAnimationFrame(function () {
            requestAnimationFrame(function () {
                ui.scrollbarQueued = false;
                if (!alive() || !ui.options) return;
                try {
                    ui.options.dispatchEvent(new CustomEvent("oakscrollupdate"));
                    if (ui.modsScroller) ui.mods.dispatchEvent(new CustomEvent("oakscrollupdate"));
                } catch (err) {}
            });
        });
    }

    // An entry for one of our own pages (Updates, Discover, Repositories) at the top of the list
    function pseudoButton(key, word, kind) {
        var b = listButton(pseudoPage(key).name, function () {
            view.backTo = null;
            if (view.mod !== key) {
                view.mod = key;
                view.nested = [];
                view.details = null;
                view.item = null;
                ui.options.scrollTop = 0;
                pollReposSoon();
            }
            render();
        }, "bl4mm_mod_btn");
        if (view.mod === key) b.classList.add("bl4mm_selected");
        if (word) b.appendChild(el("div", "bl4mm_status bl4mm_status_" + kind, word));
        b.addEventListener("mouseenter", function () {
            view.details = { mod: pseudoPage(key) };
            renderDetails(selectedMod());
        });
        ui.mods.appendChild(b);
        return b;
    }

    function renderMods() {
        var mods = currentMods();
        ui.mods.innerHTML = "";
        var u = updatesState();
        var rs = reposState();
        if (u && u.status !== "off") {
            var count = updatesWaiting();
            var asking = updatesQuestion();
            var word = count ? count + " new" : asking ? "Set up" : { checking: "Checking", downloading: "Downloading",
                ready: "Restart", error: "Error", current: "Up to date" }[u.status] || (u.auto ? "" : "Manual");  // prettier-ignore
            var ub = pseudoButton(UPDATES, word, count || asking || u.status === "ready" ? "warn" : u.status === "error" ? "off" : "on");
            setBadge(q(ub, ".button_txt") || ub, count || asking);
        }
        // Discover's count: mods dropped into the folder waiting to load first, else what's new
        var fresh = freshMods().length;
        var db = pseudoButton(DISCOVER, rs.found ? rs.found + " to load" : fresh ? fresh + " new" : rs.discover ? String(rs.discover) : "",
            rs.found || fresh ? "warn" : "on");  // prettier-ignore
        setBadge(q(db, ".button_txt") || db, fresh);
        pseudoButton(REPOS, rs.sources ? String(rs.sources) : "", "on");
        pseudoButton(PERF, "", "on");
        var rp = reportState();
        pseudoButton(REPORT, rp.errors ? errorsText(rp.errors) : "", "warn");
        // Our own settings right under Report a problem, then every other mod
        var own = mods.filter(function (mod) {
            return mod.name === SELF_NAME;
        });
        var others = mods.filter(function (mod) {
            return mod.name !== SELF_NAME;
        });
        own.concat(others).forEach(function (mod) {
            var b = listButton(mod.name, function () {
                view.backTo = null;
                if (view.mod !== mod.name) {
                    view.mod = mod.name;
                    view.nested = [];
                    view.details = null;
                    ui.options.scrollTop = 0;
                }
                render();
            }, "bl4mm_mod_btn");
            if (mod.name === view.mod) b.classList.add("bl4mm_selected");
            if (mod.icon) {
                // The icon.png the mod carries (hotload.publish_icons), left of its name
                var inner = q(b, ".button_inner") || b;
                inner.insertBefore(picture(mod.icon, "bl4mm_mod_icon"), inner.firstChild);
                b.classList.add("bl4mm_has_icon");
            }
            // Errors it logged this session (errors.py), next to its status
            if (mod.errors) b.appendChild(el("div", "bl4mm_status bl4mm_status_warn bl4mm_errors", errorsText(mod.errors)));
            var pip = el("div", "bl4mm_status bl4mm_status_" + mod.status_kind, mod.status);
            b.appendChild(pip);
            b.addEventListener("mouseenter", function () {
                view.details = { mod: mod };
                renderDetails(selectedMod());
            });
            ui.mods.appendChild(b);
        });
    }

    // The option list on the current page: the mod's own, or a nested page's
    function pageNodes(mod) {
        var nodes = mod.options || [];
        var names = [];
        for (var i = 0; i < view.nested.length; i++) {
            var found = findNested(nodes, view.nested[i]);
            if (!found) {
                view.nested = view.nested.slice(0, i);
                break;
            }
            nodes = found.node.children;
            names.push(found.node.name);
        }
        return { nodes: nodes, names: names, prefix: view.nested.length ? nestedPrefix(mod) : [] };
    }

    // Finds a nested page by its path (ids joined), searching through groups
    function findNested(nodes, key) {
        var path = key.split("\u0001");
        var list = nodes;
        var node = null;
        for (var i = 0; i < path.length; i++) {
            node = null;
            for (var j = 0; j < list.length; j++) if (list[j].id === path[i]) node = list[j];
            if (!node) return null;
            list = node.children || [];
        }
        return node && node.kind === "nested" ? { node: node } : null;
    }

    function nestedPrefix() {
        var out = [];
        view.nested.forEach(function (key) {
            out = out.concat(key.split("\u0001"));
        });
        return out;
    }

    function renderOptions(mod) {
        var scroll = ui.options.scrollTop;
        ui.options.innerHTML = "";
        if (!mod) return;
        if (mod.pseudo) {
            if (mod.pseudo === DISCOVER) renderDiscover();
            else if (mod.pseudo === REPOS) renderRepos();
            else if (mod.pseudo === REPORT) renderReport();
            else if (mod.pseudo === PERF) renderPerf(mod);
            else if (mod.pseudo === SEARCH) renderSearch();
            else renderUpdates();
            ui.options.scrollTop = scroll;
            return;
        }
        var page = pageNodes(mod);
        ui.crumb.textContent = [mod.name].concat(page.names).join("  /  ");
        ui.back.querySelector(".button_txt").textContent = view.nested.length ? "Back" : "Close";

        // A page the mod draws itself (ns.modPages, see "Mod pages"), above its remaining options
        var custom = view.nested.length ? null : modPage(mod.name);
        if (custom) {
            try {
                custom.render(modPageApi(mod));
            } catch (err) {
                ns.log("mod page " + mod.name + " failed: " + err + " " + (err && err.stack));
            }
            if (custom.moreTitle) addTitle(custom.moreTitle);
        }
        if (!view.nested.length && mod.can_toggle) addEntry(mod, enableNode(mod), []);
        addNodes(mod, page.nodes, page.prefix, 0, custom && custom.keep);
        if (!ui.options.children.length) {
            ui.options.appendChild(el("p", "bl4mm_empty small_text_2", "This mod has no settings."));
        }
        ui.options.scrollTop = scroll;
    }

    // The "Enabled" row heading a mod's page (also a search result)
    function enableNode(mod) {
        return { kind: "enable", id: "", name: "Enabled", title: "Enabled", desc: "Turns " + mod.name + " on or off right now.",
                 value: mod.enabled, off: "Off", on: "On" };  // prettier-ignore
    }

    // keep (optional): which options to draw (a mod page hides the ones it replaces)
    function addNodes(mod, nodes, prefix, depth, keep) {
        nodes.forEach(function (node) {
            var path = prefix.concat([node.id]);
            if (node.kind === "group") {
                // mods_base wraps a mod's own options in an "Options" group; that title says nothing
                if (!(depth === 0 && view.nested.length === 0 && node.name === "Options")) addTitle(node.name);
                addNodes(mod, node.children || [], path, depth + 1, keep);
            } else if (!keep || keep(node)) {
                addEntry(mod, node, path);
            }
        });
    }

    // The Performance page: ui/perf.js draws it (ns.modPages[PERF], the same API as a mod's page)
    function renderPerf(mod) {
        ui.crumb.textContent = "Performance";
        ui.back.querySelector(".button_txt").textContent = "Close";
        var custom = modPage(PERF);
        if (!custom) {
            ui.options.appendChild(el("p", "bl4mm_empty small_text_2", "Loading..."));
            return;
        }
        try {
            custom.render(modPageApi(mod));
        } catch (err) {
            ns.log("performance page failed: " + err + " " + (err && err.stack));
        }
    }

    // region Mod pages

    // Another mod's page script (an extension, see extensions.js) can draw its own content on its
    // page in this screen:
    //   ns.modPages[modName] = { render: function (api) {...}, keep: function (node) -> bool,
    //                            moreTitle: "More" }
    // render draws rows with api (below) above the mod's own options, each time this screen
    // renders the page; keep (optional) hides options the page replaces; moreTitle (optional) heads
    // the options under it. The script calls ns.modPageRefresh(modName) when its content changed,
    // and can tell whether its page shows from ns.view (open, mod, nested).
    function modPage(name) {
        var p = ns.modPages && ns.modPages[name];
        return p && typeof p.render === "function" ? p : null;
    }

    function modPageApi(mod) {
        return {
            el: el,
            onClick: onClick,
            hoverable: hoverable,
            title: addTitle,
            // An Options-style row: name on the left, parts (elements) on the right; desc shows in
            // the details pane on hover
            row: function (name, desc, parts) {
                return addRow(mod, name, desc, parts);
            },
            // The game's list button
            button: function (text, fn, cls) {
                return listButton(text, fn, cls);
            },
            // A picture (a div showing it, scaled to fit): a coui:// url (a loose file your mod
            // publishes, or Texture2D'...'), or an https url the Mod Menu downloads once and caches
            image: function (src, cls) {
                return picture(src, cls);
            },
            toast: toast
        };
    }

    function addRow(mod, name, desc, parts) {
        var wrap = make("regular", "menu_options_regular_entry");
        wrap.classList.add("bl4mm_entry_wrap");
        var entry = q(wrap, ".menu_options_entry") || wrap;
        var text = q(wrap, ".menu_options_entry_text");
        if (text) text.textContent = name;
        var comp = q(wrap, ".menu_options_entry_component") || wrap;
        comp.innerHTML = "";
        (parts || []).forEach(function (part) {
            if (part) comp.appendChild(part);
        });
        entry.addEventListener("mouseenter", function () {
            entry.classList.add("hover_effect");
            view.details = { mod: mod, node: { name: name, title: name, desc: desc || "" } };
            renderDetails(mod);
        });
        entry.addEventListener("mouseleave", function () {
            entry.classList.remove("hover_effect");
        });
        ui.options.appendChild(wrap);
        return wrap;
    }

    ns.modPageRefresh = function (name) {
        if (alive() && ui.root && view.open && !view.dragging && view.mod === name) render();
    };
    ns.modToast = function (text, error) {
        if (alive()) toast(text, error);
    };
    // Closes this screen; closeMenu: also the game's menu under it (before a map change)
    ns.modCloseScreen = function (closeMenu) {
        if (!alive()) return;
        closeScreen();
        if (closeMenu) closeGameMenu();
    };

    // endregion

    // region Search

    var SEARCH_HINT = "Search every mod's settings";
    var SEARCH_HELP = "Type any part of a setting's name or description, or a mod's name: every matching setting of" +
        " every mod shows on the Search page, and works right there. Upper or lower case doesn't matter.\n\nClick" +
        " the box to type. Enter or Esc stops typing, Backspace deletes a letter, Delete clears it.";  // prettier-ignore

    function searchState() {
        return (link.state && link.state.search) || { text: "", typing: false };
    }

    // Typing stops on a click outside its box (the box's own click toggles it itself)
    function stopTypingOutside(target) {
        var searching = searchState().typing;
        if (!searching && !boxState().typing) return;
        for (var n = target; n && n !== ui.root; n = n.parentNode) {
            if (n.classList && n.classList.contains("bl4mm_textbox_entry")) return;
        }
        send({ op: searching ? "search_stop" : "box_stop" });
    }

    // The search bar in the header, on every page: a click opens the Search page and starts typing
    // (a click while typing stops it). Drawn once; renderSearchBar only updates it.
    function buildSearchBar() {
        var bar = { root: el("div", "bl4mm_searchbar bl4mm_textbox_entry"), shown: null };
        bar.field = el("div", "bl4mm_textbox bl4mm_search_field");
        bar.text = el("p", "bl4mm_textbox_text");
        bar.caret = el("div", "bl4mm_caret bl4mm_hidden");
        bar.field.appendChild(bar.text);
        bar.field.appendChild(bar.caret);
        bar.count = el("p", "bl4mm_search_count");
        bar.root.appendChild(bar.count);
        bar.root.appendChild(bar.field);
        onClick(bar.root, function () {
            var typing = searchState().typing;
            view.backTo = null;
            if (view.mod !== SEARCH) {
                view.mod = SEARCH;
                view.nested = [];
                view.details = null;
                view.item = null;
                ui.options.scrollTop = 0;
                typing = false;
            }
            send({ op: typing ? "search_stop" : "search_type" });
            render();
        });
        bar.root.addEventListener("mouseenter", function () {
            bar.field.classList.add("bl4mm_search_hover");
            view.details = { node: { name: "Search", title: "Search", desc: SEARCH_HELP } };
            renderDetails(selectedMod());
        });
        bar.root.addEventListener("mouseleave", function () {
            bar.field.classList.remove("bl4mm_search_hover");
        });
        return bar;
    }

    function renderSearchBar() {
        var bar = ui.searchBar;
        if (!bar) return;
        var s = searchState();
        var words = searchWords(s.text);
        var n = words.length ? countHits(searchResults(words)) : -1;
        var key = (s.typing ? "1" : "0") + n + "|" + (s.text || "");
        if (bar.shown === key) return;
        bar.shown = key;
        bar.field.classList.toggle("bl4mm_typing", !!s.typing);
        bar.text.classList.toggle("bl4mm_textbox_hint", !s.text && !s.typing);
        bar.text.textContent = s.text || (s.typing ? "" : SEARCH_HINT);
        bar.caret.classList.toggle("bl4mm_hidden", !s.typing);
        bar.count.textContent = n < 0 ? "" : n === 1 ? "1 setting" : n + " settings";
    }

    // Lowercase letters and digits only, like Kairosdex's search: "less fog", "Less-Fog" and
    // "LESS FOG" all match "Less Fog"
    function norm(text) {
        return String(text || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
    }

    function searchWords(raw) {
        return String(raw || "").split(/\s+/).map(norm).filter(function (w) {
            return !!w;
        });
    }

    // Every word somewhere in the text (any order)
    function matchesAll(haystack, words) {
        for (var i = 0; i < words.length; i++) if (haystack.indexOf(words[i]) < 0) return false;
        return true;
    }

    // Every mod's matching settings: [{mod, hits: [{node, path, label, nested, start}]}]. A setting matches
    // when each word is in its name, title, description or choices, or in its mod's name or the
    // groups / pages it sits in; a mod's Enabled row when they're in the mod's name or description.
    // (kept for the state and words it was found for: the list's pip and the page share one search)
    var lastSearch = { state: null, key: null, out: [] };
    function searchResults(words) {
        var out = [];
        if (!words.length) return out;
        var key = words.join(" ");
        if (lastSearch.state === link.state && lastSearch.key === key) return lastSearch.out;
        lastSearch = { state: link.state, key: key, out: out };
        currentMods().forEach(function (mod) {
            var hits = [];
            var modText = norm(mod.name);
            if (mod.can_toggle && matchesAll(modText + "|" + norm(mod.desc), words)) {
                hits.push({ node: enableNode(mod), path: [], label: "Enabled", nested: [], start: 0 });
            }
            searchNodes(mod.options || [], { path: [], names: [], nested: [], start: 0, text: modText, depth: 0 }, words, hits);
            if (hits.length) out.push({ mod: mod, hits: hits });
        });
        return out;
    }

    // at: where these nodes sit (ids from the mod's top, group / page names, the nested pages to
    // open to show them and where the innermost one starts in the path, the text they match on)
    function searchNodes(nodes, at, words, hits) {
        nodes.forEach(function (node) {
            var path = at.path.concat([node.id]);
            // mods_base wraps a mod's own options in an "Options" group: no name to show or match
            var named = !(node.kind === "group" && at.depth === 0 && at.nested.length === 0 && node.name === "Options");
            var names = named ? at.names.concat([node.name]) : at.names;
            var own = [norm(node.name), norm(node.title), norm(node.desc)].concat((node.choices || []).map(norm)).join("|");
            if (node.kind !== "group" && matchesAll(at.text + "|" + own, words)) {
                hits.push({ node: node, path: path, label: names.join("  /  "), nested: at.nested, start: at.start });
            }
            if (node.kind === "group" || node.kind === "nested") {
                var page = node.kind === "nested";
                searchNodes(node.children || [], {
                    path: path,
                    names: names,
                    nested: page ? at.nested.concat([path.slice(at.start).join("\u0001")]) : at.nested,
                    start: page ? path.length : at.start,
                    text: at.text + (named ? "|" + norm(node.name) : ""),
                    depth: at.depth + 1
                }, words, hits);  // prettier-ignore
            }
        });
    }

    function countHits(results) {
        var n = 0;
        results.forEach(function (r) {
            n += r.hits.length;
        });
        return n;
    }

    // The node with its group / page path as its row name (the details pane keeps its own title)
    function relabel(node, label) {
        if (label === node.name) return node;
        var copy = {};
        for (var k in node) if (Object.prototype.hasOwnProperty.call(node, k)) copy[k] = node[k];
        copy.name = label;
        copy.title = node.title || node.name;
        return copy;
    }

    function renderSearch() {
        var s = searchState();
        ui.crumb.textContent = "Search";
        ui.back.querySelector(".button_txt").textContent = "Close";
        if (s.text) {
            var actions = el("div", "bl4mm_update_actions");
            actions.appendChild(listButton("Clear", function () {
                send({ op: "search_clear" });
            }, "bl4mm_update_btn"));
            ui.options.appendChild(actions);
        }
        var words = searchWords(s.text);
        if (!words.length) {
            ui.options.appendChild(el("p", "bl4mm_update_msg", "Finds settings in every mod at once: type a word in the" +
                " box at the top, e.g. \"key\" for every hotkey, \"fog\", or a mod's name for all its settings."));  // prettier-ignore
            return;
        }
        var results = searchResults(words);
        var total = countHits(results);
        if (!total) {
            ui.options.appendChild(el("p", "bl4mm_empty small_text_2", "No setting matches \"" + s.text.trim() + "\"."));
            return;
        }
        var shown = 0;
        for (var i = 0; i < results.length && shown < MAX_SEARCH_HITS; i++) {
            var r = results[i];
            addTitle(r.mod.name);
            for (var j = 0; j < r.hits.length && shown < MAX_SEARCH_HITS; j++, shown++) {
                var hit = r.hits[j];
                addEntry(r.mod, relabel(hit.node, hit.label), hit.path, hit.node.kind === "nested" ? openFromSearch(r.mod, hit) : null);
            }
        }
        if (total > shown) {
            ui.options.appendChild(el("p", "bl4mm_update_msg", "Showing " + shown + " of " + total + " settings: type more to narrow it down."));
        }
    }

    // Open on a nested page in the results: that page of its mod; Back comes back here
    function openFromSearch(mod, hit) {
        return function () {
            var keys = hit.nested.concat([hit.path.slice(hit.start).join("\u0001")]);
            view.mod = mod.name;
            view.nested = keys;
            view.backTo = { mod: SEARCH, depth: keys.length };
            view.details = null;
            ui.options.scrollTop = 0;
            render();
        };
    }

    // endregion

    // region Report a problem

    function reportState() {
        return (link.state && link.state.report) || {};
    }

    function errorsText(n) {
        return n + (n === 1 ? " error" : " errors");
    }

    function modNameOf(id) {
        var mods = currentMods();
        for (var i = 0; i < mods.length; i++) if (mods[i].id === id) return mods[i].name;
        return id || "Unknown mod";
    }

    // Errors this session (newest first) and the bug report: make it, show it, open GitHub's issue page
    function renderReport() {
        var r = reportState();
        ui.crumb.textContent = "Report a problem";
        ui.back.querySelector(".button_txt").textContent = "Close";
        var busy = r.status === "building";
        addTitle("Bug report");
        ui.options.appendChild(el("p", "bl4mm_update_msg", r.message ||
            "Makes a zip with what a bug report needs: your mods and their versions, this session's and the" +
            " last session's log, the errors below and the last crash's summary. Nothing is sent anywhere:" +
            " you attach the zip to a new issue on GitHub yourself."));  // prettier-ignore
        var actions = el("div", "bl4mm_update_actions");
        actions.appendChild(listButton(busy ? "Making..." : "Make a bug report", function () {
            if (!busy) send({ op: "report_make" });
        }, "bl4mm_update_btn"));
        if (r.zip) {
            actions.appendChild(listButton("Show the zip", function () {
                send({ op: "report_show" });
            }, "bl4mm_update_btn"));
        }
        actions.appendChild(listButton("Open issue page", function () {
            send({ op: "report_issue" });
        }, "bl4mm_update_btn"));
        ui.options.appendChild(actions);

        addTitle(r.errors ? "Errors this session (" + r.errors + ")" : "Errors this session");
        var recent = r.recent || [];
        if (!recent.length) {
            ui.options.appendChild(el("p", "bl4mm_empty small_text_2", "No mod has logged an error since the game started."));
        }
        recent.forEach(function (item) {
            addInfoRow(modNameOf(item.mod), item.count > 1 ? item.count + " times" : "", item.msg || "", null);
        });
        if (r.errors > recent.length) {
            ui.options.appendChild(el("p", "bl4mm_empty small_text_2", "The bug report has all of them, with their tracebacks."));
        }
    }

    // endregion

    // region Updates page

    function updatesLine(u) {
        if (u.status === "downloading" && u.progress && u.progress[1]) {
            return "Downloading... " + Math.floor((100 * u.progress[0]) / u.progress[1]) + "%";
        }
        return u.message || "";
    }

    // A row in the game's Options entry style: a name, a value on the right, maybe a button
    function addInfoRow(name, value, desc, button) {
        var wrap = make("regular", "menu_options_regular_entry");
        wrap.classList.add("bl4mm_entry_wrap");
        var entry = q(wrap, ".menu_options_entry") || wrap;
        var text = q(wrap, ".menu_options_entry_text");
        if (text) text.textContent = name;
        var comp = q(wrap, ".menu_options_entry_component") || wrap;
        comp.innerHTML = "";
        if (value) comp.appendChild(el("p", "bl4mm_update_ver", value));
        [].concat(button || []).forEach(function (b) {
            comp.appendChild(b);
        });
        entry.addEventListener("mouseenter", function () {
            entry.classList.add("hover_effect");
            view.details = { node: { name: name, title: name, desc: desc || "" } };
            renderDetails(selectedMod());
        });
        entry.addEventListener("mouseleave", function () {
            entry.classList.remove("hover_effect");
        });
        ui.options.appendChild(wrap);
    }

    function renderUpdates() {
        var u = updatesState() || {};
        ui.crumb.textContent = "Updates";
        ui.back.querySelector(".button_txt").textContent = "Close";
        var busy = u.status === "checking" || u.status === "downloading";
        var items = u.items || [];

        if (u.ask) {
            // Asked once, after the Mod Menu is installed: until then nothing checks on its own
            addTitle("Check for updates automatically?");
            ui.options.appendChild(el("p", "bl4mm_update_msg",
                "Once per launch, the Mod Menu can ask GitHub (in the background) whether BL4 Fix has newer versions " +
                "of your mods, and show them here with a \"!\" on MODS. Nothing downloads or installs until you press " +
                "Update all. That check sends GitHub the usual web request (your IP address), nothing else."));  // prettier-ignore
            ui.options.appendChild(el("p", "bl4mm_update_msg",
                "No: nothing is checked until you press Check again. You can change this any time on Repositories: " +
                "BL4 Fix's Check at launch switch."));  // prettier-ignore
            var answer = el("div", "bl4mm_update_actions");
            answer.appendChild(listButton("Yes, check at launch", function () {
                send({ op: "updates_auto", value: true });
            }, "bl4mm_update_btn"));
            answer.appendChild(listButton("No, I'll check myself", function () {
                send({ op: "updates_auto", value: false });
            }, "bl4mm_update_btn"));
            ui.options.appendChild(answer);
        }

        addTitle(u.release ? "Mod updates (" + u.release + ")" : "Mod updates");
        ui.options.appendChild(el("p", "bl4mm_update_msg", updatesLine(u)));
        items.forEach(function (item) {
            var ready = item.state === "staged";
            addInfoRow(item.name, item.installed + "  ->  " + item.latest + (ready ? "   READY" : ""),
                (item.notes ? item.notes + "\n\n" : "") + (ready ? "Downloaded: installs when the game closes." :
                "Installed: " + item.installed + ". Latest: " + item.latest + "."), null);  // prettier-ignore
        });

        // "updates" counts waiting updates only ("count", the badge, also counts new mods)
        var waiting = u.updates !== undefined ? u.updates : u.count;
        var actions = el("div", "bl4mm_update_actions");
        if (!busy && waiting) {
            actions.appendChild(listButton("Update all", function () {
                send({ op: "updates_apply", value: [] });
            }, "bl4mm_update_btn"));
        }
        if (!busy && u.status === "ready") {
            // Reload the staged mods that can, without a restart (the rest wait for it)
            actions.appendChild(listButton("Load now", function () {
                send({ op: "updates_load_now" });
            }, "bl4mm_update_btn"));
        }
        if (!busy && u.status === "ready" && u.can_restart) {
            actions.appendChild(listButton("Restart now", function () {
                send({ op: "updates_restart" });
            }, "bl4mm_update_btn"));
        }
        if (!busy) {
            actions.appendChild(listButton("Check again", function () {
                send({ op: "updates_check" });
            }, "bl4mm_update_btn"));
        }
        if (actions.children.length) ui.options.appendChild(actions);

        var repoUpdates = renderRepoUpdates();
        if (!items.length && !repoUpdates && !busy) {
            ui.options.appendChild(el("p", "bl4mm_empty small_text_2",
                u.status === "error" ? "No update information." : u.status === "idle" ? "Not checked yet this launch." :
                "Everything is up to date."));  // prettier-ignore
        }
        renderRollbacks();
    }

    // Roll back (undo.py): mods whose last update kept the copy it replaced, when that copy's version
    // differs from the installed one. Roll back stages it like an update and reloads it when it can
    function renderRollbacks() {
        var r = repoData();
        var undo = (r && r.undo) || {};
        var list = undo.rollbacks || [];
        if (!list.length) return;
        var jobs = undo.jobs || {};
        addTitle("Roll back");
        ui.options.appendChild(el("p", "bl4mm_update_msg",
            "Each update keeps the copy it replaced. Roll back puts that one back: right away when the mod can " +
            "reload, else when the game closes."));  // prettier-ignore
        list.forEach(function (item) {
            var job = jobs[item.id];
            var state = job ? job.state : "";
            var b = null;
            if (!state || state === "failed") {
                b = listButton(state === "failed" ? "Try again" : "Roll back", function () {
                    sendRepo("repo_rollback", [item.id]);
                }, "bl4mm_update_btn");
            }
            var word = { working: "   ...", staged: "   AT RESTART", reloaded: "   ROLLED BACK", failed: "   FAILED" }[state] || "";
            addInfoRow(item.name, item.installed + "  ->  back to " + item.previous + word,
                "Installed: " + item.installed + ". The copy you had before: " + item.previous + "." +
                (job && job.message ? "\n\n" + job.message : "\n\nUpdates won't offer " + item.installed +
                " again until the game starts again."), b);  // prettier-ignore
        });
    }

    // Safe mode (the setup app set other modders' mods aside, undo.py): a line and Restore, on
    // Discover and Repositories
    function renderSafeMode(r) {
        var safe = r && r.undo && r.undo.safe;
        if (!safe || !safe.count) return;
        var names = (safe.names || []).map(function (n) {
            return n.name || n.file;
        });
        addTitle("Safe mode");
        addInfoRow("Safe mode: " + safe.count + " mod" + (safe.count === 1 ? "" : "s") + " from other modders " +
            (safe.count === 1 ? "is" : "are") + " set aside", "",
            "BL4 Fix Setup's safe mode moved these out of your mods folder (into sdk_mods\\.bl4_safe_mode), so " +
            "they don't load: " + names.join(", ") + ".\n\nRestore moves them back. They then show under \"In your " +
            "mods folder\" on Discover, where Load starts one now; otherwise they load at the next start.",
            r.undo.restoring ? el("p", "bl4mm_card_state", "RESTORING") : listButton("Restore", function () {
                sendRepo("repo_safe_restore");
            }, "bl4mm_update_btn"));  // prettier-ignore
    }

    // Updates from the added repositories, under BL4 Fix's. Returns how many it listed.
    function renderRepoUpdates() {
        var r = repoData();
        var list = (r && r.updates) || [];
        if (!list.length) return 0;
        var jobs = r.jobs || {};
        addTitle("From your repositories");
        renderUpdateReview(list, jobs);
        var open = [];
        list.forEach(function (item) {
            var job = jobs[item.id];
            // An install or reload that finished earlier this session: listed again means a newer
            // version came out since, so it can be updated (it read "INSTALLED" with no button)
            if (job && (job.state === "loaded" || job.state === "reloaded")) job = null;
            var state = job ? job.state : "";
            var b = null;
            if (!state || state === "failed") {
                open.push(item.id);
                b = listButton("Update", function () {
                    send({ op: "repo_update", value: [item.id] });
                }, "bl4mm_update_btn");
            }
            addInfoRow(item.name, item.installed + "  ->  " + item.version + jobWord(job),
                (item.notes ? item.notes + "\n\n" : "") + "From " + item.repo_name + "." +
                (job && job.message ? "\n\n" + job.message : "\n\nUpdate reloads it right away when it can; otherwise it installs when the game closes."), b);  // prettier-ignore
        });
        if (open.length > 1) {
            var actions = el("div", "bl4mm_update_actions");
            actions.appendChild(listButton("Update all " + open.length, function () {
                send({ op: "repo_update", value: open });
            }, "bl4mm_update_btn"));
            ui.options.appendChild(actions);
        }
        return list.length;
    }

    // The confirm step for updates downloaded from repositories (one or Update all): each one's
    // version and size change, and what its code uses that the installed copy didn't (repos._review)
    function renderUpdateReview(list, jobs) {
        var waiting = list.filter(function (item) {
            var job = jobs[item.id];
            return job && job.state === "review" && job.kind === "update";
        });
        if (!waiting.length) return;
        addTitle(waiting.length === 1 ? "Update " + waiting[0].name + "?" : "Update these " + waiting.length + " mods?");
        var news = [];
        waiting.forEach(function (item) {
            var job = jobs[item.id];
            var fresh = job.new_uses || [];
            if (fresh.length) news.push(item.name + ": " + fresh.join(", "));
            addInfoRow(item.name, (job.old_version || item.installed) + "  ->  " + (job.version || item.version) + (fresh.length ? "   NEW: " +
                fresh.join(", ").toUpperCase() : ""), "Size: " + (fmtSize(job.old_size) || "?") + "  ->  " + fmtSize(job.size) +
                "\n\nIts code uses: " + usesText(job.uses) + "." + (job.old_read ? "" : "\n\nThe installed copy couldn't be read, so all of it counts as new.") +
                whereText(job) + "\n\nFrom " + item.repo_name + ".", null);  // prettier-ignore
        });
        ui.options.appendChild(el("p", "bl4mm_update_msg" + (news.length ? " bl4mm_risk" : ""), news.length
            ? "New in " + (waiting.length === 1 ? "this update" : "these updates") + ": " + news.join("; ") + ". " + (jobs[waiting[0].id].hint || "")
            : "Nothing new on the list of what the code uses. " + (jobs[waiting[0].id].hint || "")));  // prettier-ignore
        var ids = waiting.map(function (item) {
            return item.id;
        });
        var actions = el("div", "bl4mm_update_actions");
        actions.appendChild(listButton(waiting.length === 1 ? "Update" : "Update all " + waiting.length, function () {
            sendRepo("repo_update_confirm", ids);
        }, "bl4mm_update_btn"));
        actions.appendChild(listButton("Cancel", function () {
            sendRepo("repo_review_cancel", ids);
        }, "bl4mm_update_btn"));
        ui.options.appendChild(actions);
    }

    // What a scanned download's code uses, for a sentence
    function usesText(uses) {
        return uses && uses.length ? uses.join(", ") : "nothing on the list (network, programs, native code, registry, dynamic code)";
    }

    // Where the scan saw each thing, one line each (the details pane)
    function whereText(job) {
        var where = job.where || {};
        var lines = (job.uses || []).filter(function (u) {
            return !!where[u];
        }).map(function (u) {
            return u + ": " + where[u];
        });
        return lines.length ? "\n\nSeen in:\n" + lines.join("\n") : "";
    }

    // "   45%", "   RELOADED"...: a job's state after a version or name
    function jobWord(job) {
        if (!job) return "";
        if (job.state === "downloading") {
            var p = job.progress || [0, 0];
            return "   " + (p[1] ? Math.floor((100 * p[0]) / p[1]) + "%" : "...");
        }
        return "   " + ({ loading: "LOADING", loaded: "INSTALLED", reloaded: "RELOADED", staged: "AT RESTART",
                         failed: "FAILED", removed: "REMOVED", review: "WAITING FOR OK" }[job.state] || "");  // prettier-ignore
    }

    // endregion
    // region Pictures

    // Pictures are divs with a background image (what the game's own pages do). coui:// urls show
    // as they are; https ones are downloaded by Python (repos.want_images) into a loose cache folder
    // next to this page, and show once it's there (the repositories' data says where).
    var askedPictures = ns.askedPictures || (ns.askedPictures = {});
    var pictureQueue = [];

    function pictureUrl(src) {
        if (!src) return "";
        if (/^(coui:|Texture2D'|TextureRenderTarget2D')/.test(src)) return src;
        if (!/^https?:\/\//i.test(src)) return "";
        var cached = (repoData() && repoData().images) || {};
        if (cached[src] !== undefined) return cached[src];  // "" = couldn't be fetched
        if (!askedPictures[src]) {
            askedPictures[src] = true;
            pictureQueue.push(src);
            if (pictureQueue.length === 1) {
                later(function () {
                    var urls = pictureQueue.splice(0, pictureQueue.length);
                    if (urls.length) send({ op: "repo_images", value: urls });
                }, 50);
            }
        }
        return null;  // on its way
    }

    // A div showing src (scaled to fit); empty (the placeholder style) until it's there. letter:
    // shown while there's no picture (a mod's initial)
    function picture(src, cls, letter) {
        var d = el("div", "bl4mm_pic" + (cls ? " " + cls : ""));
        var url = pictureUrl(src);
        if (url) {
            d.style.backgroundImage = "url(" + url + ")";
        } else {
            d.classList.add("bl4mm_pic_empty");
            if (letter) d.appendChild(el("p", "bl4mm_pic_letter", letter.charAt(0).toUpperCase()));
        }
        return d;
    }
    ns.picture = picture;

    // endregion
    // region Repositories' data

    // GET /repos (repos.py): the Discover and Repositories pages' data and Updates' repository part.
    // Read once a second while one of those pages shows, at once after an action or when /state says
    // it moved; an unchanged state comes back as {"same": rev}.
    var repoLink = ns.repoLink || (ns.repoLink = { rev: null, data: null });
    var repoTimer = null;

    function repoData() {
        return repoLink.data;
    }

    function repoPageShows() {
        return view.open && (view.mod === DISCOVER || view.mod === REPOS || view.mod === UPDATES);
    }

    function pollRepos() {
        if (repoTimer !== null) clearTimeout(repoTimer);
        repoTimer = null;
        if (!alive() || !link.port || !repoPageShows()) return;
        http("GET", "/repos?v=" + (repoLink.rev === null ? "" : repoLink.rev), null, function (status, text) {
            if (!alive()) return;
            if (status === 200) {
                try {
                    var data = JSON.parse(text);
                    if (data.same === undefined) {
                        repoLink.data = data;
                        repoLink.rev = data.rev;
                        if (repoPageShows() && !view.dragging) render();
                    }
                } catch (err) {
                    ns.log("bad repos state: " + err);
                }
            }
            if (repoTimer === null && repoPageShows()) repoTimer = setTimeout(pollRepos, REPOS_POLL_MS);
        });
    }

    function pollReposSoon() {
        if (repoTimer !== null) clearTimeout(repoTimer);
        repoTimer = setTimeout(pollRepos, 30);
    }

    function sendRepo(op, value) {
        send({ op: op, value: value });
        pollReposSoon();
    }

    function fmtSize(bytes) {
        if (!bytes) return "";
        if (bytes < 1024 * 1024) return Math.max(1, Math.round(bytes / 1024)) + " KB";
        return (bytes / 1024 / 1024).toFixed(1) + " MB";
    }

    // endregion
    // region Discover page

    function findItem(r, id) {
        var list = (r && r.discover) || [];
        for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i];
        return null;
    }

    // Shows a Discover entry's own page (its pictures; the confirm step of an install)
    function openItem(item) {
        view.item = item.id;
        view.shot = 0;
        view.details = { item: item };
        ui.options.scrollTop = 0;
        render();
    }

    // The button (or state) for a Discover entry: Install, a progress figure, or how it ended.
    // Another modder's mod is downloaded and read first, then its page asks (renderInstallReview)
    function installPart(item, jobs) {
        var job = jobs[item.id];
        var state = job ? job.state : "";
        if (state === "review") {
            return listButton("Review", function () {
                openItem(item);
            }, "bl4mm_update_btn");
        }
        if (!state || state === "failed") {
            return listButton(state === "failed" ? "Try again" : "Install", function () {
                if (!isOfficialRepo(repoData() || {}, item.repo) && view.item !== item.id) openItem(item);
                sendRepo("repo_install", [item.id]);
            }, "bl4mm_update_btn");
        }
        return el("p", "bl4mm_card_state" + (state === "loaded" ? " bl4mm_status_on" : ""), jobWord(job).trim());
    }

    function renderDiscover() {
        var r = repoData();
        ui.crumb.textContent = "Discover";
        ui.back.querySelector(".button_txt").textContent = view.item ? "Back" : "Close";
        if (!r) {
            ui.options.appendChild(el("p", "bl4mm_update_msg", "Reading your repositories..."));
            return;
        }
        if (view.item) {
            var item = findItem(r, view.item);
            if (item) return renderItem(r, item);
            view.item = null;
        }
        renderSafeMode(r);
        var actions = el("div", "bl4mm_update_actions");
        actions.appendChild(listButton("Open mods folder", function () {
            sendRepo("mods_folder");
        }, "bl4mm_update_btn"));
        actions.appendChild(checkButton(r, "Refresh repositories"));
        ui.options.appendChild(actions);

        addRepoFilter(r);

        // Mods dropped into sdk_mods the game hasn't loaded (or replaced since it did): with "All" only
        var found = discoverFilter(r) ? [] : r.found || [];
        if (found.length) {
            addTitle("In your mods folder");
            ui.options.appendChild(el("p", "bl4mm_update_msg", "Not running yet. Load starts a mod now, no restart needed."));
            found.forEach(function (f) {
                var changed = f.kind === "changed";
                var b = f.problem ? null : listButton(changed ? "Reload" : "Load", function () {
                    send({ op: changed ? "mods_reload" : "mods_load", value: [f.id] });
                }, "bl4mm_update_btn");
                addInfoRow(f.name || f.id, (f.version || "") + (changed ? "   CHANGED" : f.problem ? "   CAN'T LOAD" : ""),
                    (f.problem ? f.problem + "\n\n" : "") + (f.description ? f.description + "\n\n" : "") + f.file +
                    (changed ? "\n\nThis file was replaced while the game runs. Reload swaps the running copy for it." :
                        "\n\nLoad imports it like the game does at start and turns it on."), b);  // prettier-ignore
            });
        }

        var all = r.discover || [];
        var jobs = r.jobs || {};
        var shown = discoverFilter(r);
        var list = all.filter(function (item) {
            return !shown || item.repo === shown.key;
        });
        ui.options.appendChild(el("p", "bl4mm_update_msg", r.refreshing ? "Checking repositories..." :
            list.length ? "Mods from your repositories that you don't have yet. Add more sources on Repositories." :
            shown ? "Nothing new from " + shown.name + ": you have every mod it offers." :
            "Nothing new: you have every mod your repositories offer. Add more on Repositories."));  // prettier-ignore
        // Repositories whose check-at-launch switch is off weren't read yet: their mods aren't listed.
        // BL4 Fix's release read but refused (unsigned, offline) has no status either: Updates says why
        var refused = (updatesState() || {}).status === "error";
        var unread = (r.sources || []).filter(function (s) {
            return !s.hidden && !s.status && (!shown || s.key === shown.key) && !(s.official && refused);
        });
        if (refused && (r.sources || []).some(function (s) { return s.official && !s.hidden && !s.status && (!shown || s.key === shown.key); })) {
            ui.options.appendChild(el("p", "bl4mm_update_msg", "BL4 Fix's release couldn't be read this launch: Updates says why."));
        }
        if (unread.length && !r.refreshing) {
            ui.options.appendChild(el("p", "bl4mm_update_msg", (unread.length === 1 ? unread[0].name + " wasn't" :
                unread.length + " repositories weren't") + " read this launch (check at launch is off): Refresh repositories lists " +
                (unread.length === 1 ? "its" : "their") + " mods."));  // prettier-ignore
        }
        // New mods the badge counted are on screen now: tell Python (no badge for them again). They
        // keep their NEW tag for the rest of this page's life
        var fresh = freshMods();
        var unseen = list.filter(function (item) {
            return fresh.indexOf(item.id) >= 0 && !view.newTags[item.id];
        }).map(function (item) {
            return item.id;
        });
        if (unseen.length) {
            unseen.forEach(function (id) {
                view.newTags[id] = true;
            });
            send({ op: "updates_seen", value: unseen });
        }
        var group = null;
        list.forEach(function (item) {
            if (item.repo_name !== group) {
                group = item.repo_name;
                addTitle(group);
            }
            ui.options.appendChild(card(item, jobs));
        });
    }

    // The repositories Discover can show: {key, name} of each one that isn't removed
    function discoverSources(r) {
        var out = [];
        var names = {};
        (r.sources || []).forEach(function (s) {
            if (s.hidden) return;
            var name = s.name || s.key;
            // Two repositories with the same name: tell them apart by their link
            out.push({ key: s.key, name: names[name] ? name + " (" + s.key + ")" : name });
            names[name] = true;
        });
        return out;
    }

    // The repository Discover shows ({key, name}), or null for all of them
    function discoverFilter(r) {
        if (!view.discoverRepo) return null;
        var list = discoverSources(r);
        for (var i = 0; i < list.length; i++) if (list[i].key === view.discoverRepo) return list[i];
        view.discoverRepo = null;  // removed since: back to all
        return null;
    }

    // "Repository  < All >": the game's stepper, cycling through All and each repository
    function addRepoFilter(r) {
        var sources = discoverSources(r);
        var choices = ["All"].concat(sources.map(function (s) {
            return s.name;
        }));
        var current = discoverFilter(r);
        var counts = {};
        (r.discover || []).forEach(function (item) {
            counts[item.repo] = (counts[item.repo] || 0) + 1;
        });
        var node = { choices: choices, value: current ? current.name : "All", wrap: true };
        var wrap = make("regular", "menu_options_regular_entry");
        wrap.classList.add("bl4mm_entry_wrap");
        var entry = q(wrap, ".menu_options_entry") || wrap;
        var label = q(wrap, ".menu_options_entry_text");
        if (label) label.textContent = "Repository";
        var comp = q(wrap, ".menu_options_entry_component") || wrap;
        comp.innerHTML = "";
        comp.classList.add("menu_options_entry_stepper");
        comp.appendChild(stepper(node, function (op, value) {
            var i = choices.indexOf(value);
            view.discoverRepo = i > 0 ? sources[i - 1].key : null;
            view.details = null;
            ui.options.scrollTop = 0;
            render();
        }));
        var desc = "Which repository's mods to show. Click the arrows (or the name) to switch.\n\n" +
            sources.map(function (s) {
                return s.name + ": " + (counts[s.key] || 0) + " new";
            }).join("\n");  // prettier-ignore
        entry.addEventListener("mouseenter", function () {
            entry.classList.add("hover_effect");
            view.details = { node: { name: "Repository", title: "Repository", desc: desc } };
            renderDetails(selectedMod());
        });
        entry.addEventListener("mouseleave", function () {
            entry.classList.remove("hover_effect");
        });
        ui.options.appendChild(wrap);
    }

    // A Discover entry: icon, name, who and what, a line of description, Install on the right.
    // Click it for the full page (pictures, notes).
    function card(item, jobs) {
        var c = el("div", "bl4mm_card");
        c.appendChild(picture(item.icon, "bl4mm_card_icon", item.name));
        var text = el("div", "bl4mm_card_text");
        text.appendChild(el("p", "bl4mm_card_name", item.name));
        var meta = [view.newTags[item.id] ? "NEW" : "", item.author ? "by " + item.author : "", item.version, fmtSize(item.size)].filter(function (s) {
            return !!s;
        });
        text.appendChild(el("p", "bl4mm_card_meta", meta.join("   |   ")));
        if (item.blurb || item.description) text.appendChild(el("p", "bl4mm_card_blurb", item.blurb || item.description));
        c.appendChild(text);
        var part = installPart(item, jobs);
        part.classList.add("bl4mm_card_action");
        c.appendChild(part);
        c.addEventListener("mouseenter", function () {
            c.classList.add("hover_effect");
            view.details = { item: item };
            renderDetails(selectedMod());
        });
        c.addEventListener("mouseleave", function () {
            c.classList.remove("hover_effect");
        });
        onClick(c, function () {
            view.item = item.id;
            view.shot = 0;
            view.details = { item: item };
            ui.options.scrollTop = 0;
            render();
        });
        return c;
    }

    // One Discover entry, full size: its pictures (click for the next), description and notes
    function renderItem(r, item) {
        ui.crumb.textContent = "Discover  /  " + item.name;
        var head = el("div", "bl4mm_item_head");
        head.appendChild(picture(item.icon, "bl4mm_item_icon", item.name));
        var text = el("div", "bl4mm_card_text");
        text.appendChild(el("p", "bl4mm_item_name", item.name));
        var meta = [item.author ? "by " + item.author : "", item.version, fmtSize(item.size), "from " + item.repo_name];
        text.appendChild(el("p", "bl4mm_card_meta", meta.filter(function (s) { return !!s; }).join("   |   ")));  // prettier-ignore
        head.appendChild(text);
        ui.options.appendChild(head);

        var job = (r.jobs || {})[item.id];
        var reviewing = job && job.state === "review" && job.kind === "install";
        var actions = el("div", "bl4mm_update_actions");
        if (!reviewing) actions.appendChild(installPart(item, r.jobs || {}));
        if (item.homepage) {
            actions.appendChild(listButton("Web page", function () {
                sendRepo("repo_link", item.homepage);
            }, "bl4mm_update_btn"));
        }
        ui.options.appendChild(actions);
        if (reviewing) renderInstallReview(item, job);
        if (!isOfficialRepo(r, item.repo) && !reviewing) {
            ui.options.appendChild(el("p", "bl4mm_update_msg bl4mm_risk", "From " + item.repo_name + " (" + item.repo + "), not " +
                "BL4 Fix: nobody at BL4 Fix checked it. Like any mod it runs with the game's rights on your PC, so install it " +
                "only if you trust its author."));  // prettier-ignore
        }

        var shots = item.images || [];
        if (shots.length) {
            var at = Math.min(view.shot, shots.length - 1);
            var shot = picture(shots[at], "bl4mm_shot");
            if (shots.length > 1) {
                shot.appendChild(el("p", "bl4mm_shot_count", at + 1 + " / " + shots.length));
                onClick(shot, function () {
                    view.shot = (at + 1) % shots.length;
                    render();
                });
                // The next one starts downloading now, so the click shows it at once
                pictureUrl(shots[(at + 1) % shots.length]);
            }
            ui.options.appendChild(shot);
        }
        var desc = item.description || item.blurb;
        if (desc) ui.options.appendChild(el("p", "bl4mm_item_desc", desc));
        if (item.notes) {
            addTitle("What's new");
            ui.options.appendChild(el("p", "bl4mm_item_desc", item.notes));
        }
    }

    // The confirm step of an install from another modder's repository: downloaded and checked, its
    // code read (repos._review); Install uses those same bytes, Cancel drops them
    function renderInstallReview(item, job) {
        addTitle("Install " + item.name + "?");
        var uses = job.uses || [];
        addInfoRow("It uses", uses.length ? uses.join(", ").toUpperCase() : "NOTHING LISTED",
            "What " + item.name + "'s code uses, read from the download before anything runs: " + usesText(uses) + "." +
            whereText(job) + "\n\n" + (job.hint || ""), null);  // prettier-ignore
        addInfoRow("Version", (job.version || item.version || "?") + "   |   " + fmtSize(job.size),
            "The file you install: downloaded, its checksum matched the repository's, nothing of it has run.", null);  // prettier-ignore
        ui.options.appendChild(el("p", "bl4mm_update_msg bl4mm_risk", "Install " + item.name + "? " + (uses.length ? "It uses: " +
            uses.join(", ") + ". " : "") + (job.hint || "") + " From " + item.repo_name + " (" + item.repo + "), not BL4 Fix: " +
            "nobody at BL4 Fix checked it, and like any mod it runs with the game's rights on your PC."));  // prettier-ignore
        var actions = el("div", "bl4mm_update_actions");
        actions.appendChild(listButton("Install", function () {
            sendRepo("repo_install_confirm", [item.id]);
        }, "bl4mm_update_btn"));
        actions.appendChild(listButton("Cancel", function () {
            sendRepo("repo_review_cancel", [item.id]);
        }, "bl4mm_update_btn"));
        ui.options.appendChild(actions);
    }

    // endregion
    // region Repositories page

    function renderRepos() {
        var r = repoData();
        ui.crumb.textContent = "Repositories";
        ui.back.querySelector(".button_txt").textContent = "Close";
        if (!r) {
            ui.options.appendChild(el("p", "bl4mm_update_msg", "Reading your repositories..."));
            return;
        }
        ui.options.appendChild(el("p", "bl4mm_update_msg",
            "Add other modders' repositories to get their mods on Discover, with updates. Mods run code on " +
            "your PC like any mod: only add repositories you trust."));  // prettier-ignore
        renderSafeMode(r);
        var box = boxState();
        var pending = r.pending;
        var editing = box.editing;
        var editName = editing ? sourceName(r, editing) : "";
        if (!pending) {
            addTitle(editing ? "Edit " + editName : "Add a repository");
            addTextBox(box, editing
                ? "Change the link of " + editName + ". Enter saves, Esc stops typing, Ctrl+V pastes."
                : "Click the box and paste (Ctrl+V) or type the modder's GitHub link, e.g.\n" +
                  "https://github.com/flameaway/bl4-fix\nor just flameaway/bl4-fix. A raw.githubusercontent.com link to " +
                  "an index works too; only GitHub is allowed. Enter adds, Esc stops typing. (Or type modrepo add <link> " +
                  "in the console.)");  // prettier-ignore
            var actions = el("div", "bl4mm_update_actions");
            actions.appendChild(listButton(editing ? "Save" : "Add", function () {
                send({ op: "box_submit" });
            }, "bl4mm_update_btn"));
            actions.appendChild(listButton("Paste", function () {
                send({ op: "box_paste" });
            }, "bl4mm_update_btn"));
            if (editing || box.text) {
                actions.appendChild(listButton(editing ? "Cancel" : "Clear", function () {
                    send({ op: "box_cancel" });
                }, "bl4mm_update_btn"));
            }
            ui.options.appendChild(actions);
        } else if (pending.status === "checking") {
            addTitle(pending.replace ? "Checking the new link" : "Adding");
            addInfoRow(pending.key, "CHECKING", "Reading the repository...", null);
        } else {
            addTitle(pending.replace ? "Use this link for " + (pending.replace_name || pending.replace) + "?" : "Add this repository?");
            // The link next to the name: the name is the repository's own word for itself
            addInfoRow(pending.name === pending.key ? pending.name : pending.name + "  (" + pending.key + ")",
                pending.count + " mod" + (pending.count === 1 ? "" : "s"),
                (pending.description ? pending.description + "\n\n" : "") + pending.key +
                (pending.mods && pending.mods.length ? "\n\nMods: " + pending.mods.join(", ") : ""), null);  // prettier-ignore
            if (pending.description) ui.options.appendChild(el("p", "bl4mm_new_blurb", pending.description));
            if (pending.no_checksum && pending.no_checksum.length) {
                ui.options.appendChild(el("p", "bl4mm_update_msg", "Left out (no SHA-256 checksum listed): " + pending.no_checksum.join(", ") + "."));
            }
            if (pending.id_note) ui.options.appendChild(el("p", "bl4mm_update_msg", pending.id_note));
            // Every time, also for an edited link: what adding a repository means
            ui.options.appendChild(el("p", "bl4mm_update_msg bl4mm_risk", "Before you add it: " + (r.risk || "")));
            addToggleRow("Check for updates at launch", "Reads this repository once per launch (in the background), so " +
                "Discover lists its new mods and Updates its new versions. Nothing downloads until you press Install or " +
                "Update.\n\nOff: it's read only when you press Refresh. You can change this later on its row below.",
                !!pending.auto, function (on) {
                    sendRepo("repo_add_auto", on);
                });  // prettier-ignore
            var confirm = el("div", "bl4mm_update_actions");
            confirm.appendChild(listButton(pending.replace ? "Save" : "Add", function () {
                sendRepo("repo_add_confirm");
            }, "bl4mm_update_btn"));
            confirm.appendChild(listButton("Cancel", function () {
                sendRepo("repo_add_cancel");
            }, "bl4mm_update_btn"));
            ui.options.appendChild(confirm);
        }

        addTitle("Your repositories");
        var checkRow = el("div", "bl4mm_update_actions");
        checkRow.appendChild(checkButton(r, "Refresh all"));
        ui.options.appendChild(checkRow);
        (r.sources || []).forEach(function (s) {
            var word = s.hidden ? "REMOVED" : s.official ? "BUILT IN" : s.blocked ? "BLOCKED" : s.status === "error" ? "ERROR" :
                s.status === "checking" ? "CHECKING" : s.status ? (s.message || "").toUpperCase() : "NOT CHECKED";  // prettier-ignore
            var web = repoWebLink(s);
            var buttons = [];
            if (web) {
                buttons.push(listButton(isGithubKey(s.key) ? "GitHub" : "Web page", function () {
                    sendRepo("repo_link", web);
                }, "bl4mm_row_btn"));
            }
            if (!s.official) {
                buttons.push(listButton("Edit", function () {
                    send({ op: "box_edit", value: s.key });
                }, "bl4mm_row_btn"));
            }
            if (s.hidden) {
                buttons.push(listButton("Restore", function () {
                    sendRepo("repo_show_official");
                }, "bl4mm_row_btn"));
            } else {
                buttons.push(listButton("Remove", function () {
                    sendRepo("repo_remove", s.key);
                }, "bl4mm_row_btn"));
            }
            addInfoRow(s.name || s.key, word,
                (s.description ? s.description + "\n\n" : "") +
                (s.official ? "The BL4 Fix mods (" + s.key + "), built in. Remove takes them off Discover; the ones " +
                    "you have still update on the Updates page." : s.url || s.key) +
                (s.release ? "\n\nNewest release: " + s.release : "") + (s.status === "error" ? "\n\n" + s.message : "") +
                (s.id_note ? "\n\n" + s.id_note : "") +
                (s.no_checksum && s.no_checksum.length ? "\n\nLeft out (no SHA-256 checksum listed): " + s.no_checksum.join(", ") : "") +
                (web ? "\n\n" + web : ""), buttons);  // prettier-ignore
            if (s.hidden) return;
            if (s.blocked) addTrustAgain(r, s);
            // Its own check-at-launch switch, the only one: BL4 Fix's sets the Mod Menu's hidden "Check for
            // updates at launch" option (no second copy in the Mod Menu's settings to confuse with these)
            addToggleRow("Check at launch", (s.official ? "Asks GitHub once per launch whether BL4 Fix has newer versions of " +
                "your mods." : "Reads " + (s.name || s.key) + " once per launch, so Discover lists its new mods and " +
                "Updates its new versions.") + " Nothing downloads until you press Install or Update.\n\nOff: only " +
                "Refresh reads it.", !!s.auto, function (on) {
                    if (s.official) sendRepo("updates_auto", on);
                    else sendRepo("repo_auto", { key: s.key, on: on });
                }, true);  // prettier-ignore
        });

        var installed = r.installed || [];
        if (installed.length) {
            addTitle("Installed from repositories");
            var jobs = r.jobs || {};
            installed.forEach(function (m) {
                var job = jobs[m.id];
                var gone = job && job.state === "removed";
                var b = gone ? null : listButton("Remove", function () {
                    sendRepo("repo_remove_mod", [m.id]);
                }, "bl4mm_update_btn");
                addInfoRow(m.id, (m.version || "") + jobWord(job),
                    "From " + m.repo_name + ". Remove turns it off now and deletes its file when the game closes.", b);  // prettier-ignore
            });
        }
    }

    // Under a blocked repository's row (its link leads to a different GitHub repository now): why,
    // and Trust again, which shows the warning once more before it accepts the new repository
    function addTrustAgain(r, s) {
        var asking = view.trusting === s.key;
        ui.options.appendChild(el("p", "bl4mm_update_msg bl4mm_risk", asking ? "Trust " + (s.name || s.key) + " again? " +
            "Its link now leads to a different repository than the one you added: maybe its modder renamed or moved it, " +
            "maybe someone else took the name. Its mods and updates come from the new one from now on. " + (r.risk || "") : s.message));  // prettier-ignore
        var actions = el("div", "bl4mm_update_actions");
        if (asking) {
            actions.appendChild(listButton("Trust it", function () {
                view.trusting = null;
                sendRepo("repo_trust", s.key);
            }, "bl4mm_update_btn"));
            actions.appendChild(listButton("Cancel", function () {
                view.trusting = null;
                render();
            }, "bl4mm_update_btn"));
        } else {
            actions.appendChild(listButton("Trust again", function () {
                view.trusting = s.key;
                render();
            }, "bl4mm_update_btn"));
        }
        ui.options.appendChild(actions);
    }

    // Refreshes every repository: new mods and updates (and says what it found when done). While it
    // runs it reads "Refreshing..." and does nothing, so a click always shows something.
    function checkButton(r, text) {
        var b = listButton(r.refreshing ? "Refreshing..." : text, function () {
            if (!(repoData() || {}).refreshing) sendRepo("repo_refresh");
        }, "bl4mm_update_btn");
        if (r.refreshing) b.classList.add("bl4mm_disabled");
        return b;
    }

    function boxState() {
        return (link.state && link.state.box) || { text: "", typing: false, editing: null };
    }

    function sourceName(r, key) {
        var list = r.sources || [];
        for (var i = 0; i < list.length; i++) if (list[i].key === key) return list[i].name || key;
        return key;
    }

    function isOfficialRepo(r, key) {
        var list = r.sources || [];
        for (var i = 0; i < list.length; i++) if (list[i].key === key) return !!list[i].official;
        return false;
    }

    function isGithubKey(key) {
        return /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(key || "");
    }

    // Where a repository's button goes: its GitHub page, else its own home page, else the index
    function repoWebLink(s) {
        if (isGithubKey(s.key)) return "https://github.com/" + s.key;
        if (s.homepage && /^https:\/\//i.test(s.homepage)) return s.homepage;
        return /^https:\/\//i.test(s.url || "") ? s.url : "";
    }

    // A row with the game's Off/On toggle for something that isn't a mod's option (a repository's
    // check-at-launch switch); fn(on) when it's clicked. sub: indented, a part of the row above
    function addToggleRow(name, desc, value, fn, sub) {
        var wrap = make("regular", "menu_options_regular_entry");
        wrap.classList.add("bl4mm_entry_wrap");
        if (sub) wrap.classList.add("bl4mm_sub_row");
        var entry = q(wrap, ".menu_options_entry") || wrap;
        var label = q(wrap, ".menu_options_entry_text");
        if (label) label.textContent = name;
        var comp = q(wrap, ".menu_options_entry_component") || wrap;
        comp.innerHTML = "";
        comp.appendChild(toggle({ off: "Off", on: "On", value: value }, function (op, on) {
            fn(on);
        }));
        entry.addEventListener("mouseenter", function () {
            entry.classList.add("hover_effect");
            view.details = { node: { name: name, title: name, desc: desc } };
            renderDetails(selectedMod());
        });
        entry.addEventListener("mouseleave", function () {
            entry.classList.remove("hover_effect");
        });
        ui.options.appendChild(wrap);
    }

    // The Repositories box (addTextBox's default spec)
    var REPO_BOX = { label: "Link", title: "Repository link", hint: "https://github.com/flameaway/bl4-fix", type: "box_type", stop: "box_stop" };  // prettier-ignore

    // A text box: Python reads the keys (none reach this page) and sends the text in its state.
    // spec (optional, REPO_BOX by default): label, title (details pane), hint (shown while empty),
    // and the ops that start and stop typing
    function addTextBox(box, desc, spec) {
        spec = spec || REPO_BOX;
        var wrap = make("regular", "menu_options_regular_entry");
        wrap.classList.add("bl4mm_entry_wrap");
        var entry = q(wrap, ".menu_options_entry") || wrap;
        entry.classList.add("bl4mm_textbox_entry");
        var label = q(wrap, ".menu_options_entry_text");
        if (label) label.textContent = spec.label;
        var comp = q(wrap, ".menu_options_entry_component") || wrap;
        comp.innerHTML = "";
        var field = el("div", "bl4mm_textbox" + (box.typing ? " bl4mm_typing" : ""));
        var text = el("p", "bl4mm_textbox_text" + (box.text || box.typing ? "" : " bl4mm_textbox_hint"),
            box.text || (box.typing ? "" : spec.hint));  // prettier-ignore
        field.appendChild(text);
        if (box.typing) field.appendChild(el("div", "bl4mm_caret"));
        comp.appendChild(field);
        onClick(entry, function () {
            send({ op: box.typing ? spec.stop : spec.type });
        });
        entry.addEventListener("mouseenter", function () {
            entry.classList.add("hover_effect");
            view.details = { node: { name: spec.label, title: spec.title, desc: desc } };
            renderDetails(selectedMod());
        });
        entry.addEventListener("mouseleave", function () {
            entry.classList.remove("hover_effect");
        });
        ui.options.appendChild(wrap);
    }

    // endregion

    function addTitle(text) {
        var t = make("title", "menu_options_title_entry");
        var title = q(t, ".cat_divider_title") || t;
        title.textContent = text;
        t.classList.add("bl4mm_title_entry");
        ui.options.appendChild(t);
    }

    // open (optional): what a nested page's Open does instead of going down a page (search results)
    function addEntry(mod, node, path, open) {
        var wrap = make("regular", "menu_options_regular_entry");
        wrap.classList.add("bl4mm_entry_wrap");
        var entry = q(wrap, ".menu_options_entry") || wrap;
        var text = q(wrap, ".menu_options_entry_text");
        if (text) text.textContent = node.name;
        var comp = q(wrap, ".menu_options_entry_component") || wrap;
        entry.addEventListener("mouseenter", function () {
            entry.classList.add("hover_effect");
            view.details = { mod: mod, node: node, path: path };
            renderDetails(mod);
        });
        entry.addEventListener("mouseleave", function () {
            entry.classList.remove("hover_effect");
        });

        var act = function (op, value) {
            // A rebind waits for a key, which the search's typing would swallow
            if (op === "capture" && searchState().typing) send({ op: "search_stop" });
            if (node.kind === "enable") send({ op: "enable", mod: mod.name, value: value });
            else send({ op: op, mod: mod.name, path: path, value: value });
        };
        switch (node.kind) {
            case "bool":
            case "enable":
                comp.appendChild(toggle(node, act));
                break;
            case "slider":
                comp.classList.add("menu_options_entry_slider");
                comp.appendChild(slider(node, act));
                break;
            case "choice":
                comp.classList.add("menu_options_entry_stepper");
                comp.appendChild(stepper(node, act));
                break;
            case "keybind":
                comp.classList.add("menu_options_entry_rebind");
                comp.appendChild(rebind(mod, node, path, act));
                break;
            case "button":
                comp.classList.add("menu_options_entry_button");
                comp.appendChild(listButton("Run", function () {
                    act("press");
                }, "bl4mm_run"));
                break;
            case "nested":
                comp.classList.add("menu_options_entry_button");
                comp.appendChild(listButton("Open", open || function () {
                    view.nested.push(path.slice(nestedPrefix().length).join("\u0001"));
                    view.details = null;
                    ui.options.scrollTop = 0;
                    render();
                }, "bl4mm_run"));
                break;
            default:
                comp.appendChild(el("p", "bl4mm_value small_text_1", node.text || ""));
        }
        if (node.changed && node.kind !== "keybind") {
            var reset = el("div", "bl4mm_reset");
            reset.appendChild(el("div", "rebind_entry_btn_icon binding_reset"));
            hoverable(reset);
            onClick(reset, function () {
                act("reset");
            });
            entry.appendChild(reset);
        }
        ui.options.appendChild(wrap);
    }

    // endregion
    // region Controls

    function toggle(node, act) {
        var t = make("toggle", "toggle");
        var left = q(t, ".toggle_button_left");
        var right = q(t, ".toggle_button_right");
        if (!left || !right) return t;
        var texts = t.querySelectorAll(".toggle_button_text");
        if (texts[0]) texts[0].textContent = node.off;
        if (texts[1]) texts[1].textContent = node.on;
        left.classList.toggle("active", !node.value);
        right.classList.toggle("active", !!node.value);
        onClick(left, function () {
            if (node.value) act("set", false);
        });
        onClick(right, function () {
            if (!node.value) act("set", true);
        });
        return t;
    }

    function fmt(node, value) {
        if (node.int) return String(Math.round(value));
        return String(Math.round(value * 100) / 100);
    }

    function snap(node, value) {
        var lo = node.min, hi = node.max;
        if (node.step > 0) value = lo + Math.round((value - lo) / node.step) * node.step;
        value = Math.min(hi, Math.max(lo, value));
        return node.int ? Math.round(value) : Math.round(value * 1e6) / 1e6;
    }

    function slider(node, act) {
        var s = make("slider", "slider");
        var fill = q(s, ".slider_fill");
        var lines = q(s, ".slider_lines_filled");
        var handle = q(s, ".slider_handle");
        var values = s.querySelectorAll(".slider_value");
        var indicator = q(s, ".slider_default_indicator");
        if (indicator) indicator.classList.add("invisible");
        for (var i = 1; i < values.length; i++) values[i].style.display = "none";
        var shown = node.value;

        function show(value) {
            shown = value;
            var pct = node.max > node.min ? ((value - node.min) / (node.max - node.min)) * 100 : 0;
            if (fill) fill.style.width = pct + "%";
            if (lines) lines.style.width = pct + "%";
            if (handle) handle.style.left = pct + "%";
            if (values[0]) values[0].textContent = fmt(node, value);
        }
        show(node.value);

        var buttons = s.querySelectorAll(".slider_button");
        if (buttons[0]) onClick(buttons[0], function () {
            act("set", snap(node, shown - node.step));
        });
        if (buttons[1]) onClick(buttons[1], function () {
            act("set", snap(node, shown + node.step));
        });

        var bar = q(s, ".slider_fill_container") || q(s, ".slider_bar");
        var hit = q(s, ".slider_bar") || bar;
        if (!bar || !hit) return s;
        function at(evt) {
            var rect = bar.getBoundingClientRect();
            var frac = rect.width > 0 ? (evt.clientX - rect.left) / rect.width : 0;
            return snap(node, node.min + Math.min(1, Math.max(0, frac)) * (node.max - node.min));
        }
        hit.addEventListener("mousedown", function (evt) {
            evt.stopPropagation();
            view.dragging = true;
            if (handle) handle.classList.add("dragged");
            show(at(evt));
            if (view.endDrag) view.endDrag(null);
            var move = function (e) {
                show(at(e));
            };
            // e is null when the drag is called off (screen closed, a new drag): nothing is set
            var up = function (e) {
                unlistenDoc("mousemove", move);
                unlistenDoc("mouseup", up);
                if (view.endDrag === up) view.endDrag = null;
                view.dragging = false;
                if (handle) handle.classList.remove("dragged");
                if (!e || !view.open) return;
                var value = at(e);
                show(value);
                if (value !== node.value) act("set", value);
            };
            view.endDrag = up;
            listenDoc("mousemove", move);
            listenDoc("mouseup", up);
        });
        return s;
    }

    function stepper(node, act) {
        var s = make("stepper", "stepper");
        var choices = node.choices || [];
        var index = choices.indexOf(node.value);
        var value = q(s, ".stepper_value");
        if (value) value.textContent = node.value;
        var pips = q(s, ".stepper_indicator_container");
        if (pips) {
            var pipTemplate = q(pips, ".stepper_inner_cntr");
            pips.innerHTML = "";
            if (pipTemplate && choices.length > 1 && choices.length <= MAX_STEPPER_PIPS) {
                choices.forEach(function (_, i) {
                    var pip = pipTemplate.cloneNode(true);
                    var ind = q(pip, ".stepper_indicator");
                    if (ind) ind.classList.toggle("active", i === index);
                    pips.appendChild(pip);
                });
            }
        }
        function step(delta) {
            if (!choices.length) return;
            var target = index + delta;
            if (!node.wrap && (target < 0 || target >= choices.length)) return;
            act("set", choices[((target % choices.length) + choices.length) % choices.length]);
        }
        var left = q(s, ".stepper_button_left");
        var right = q(s, ".stepper_button_right");
        if (left) {
            onClick(left, function () {
                step(-1);
            });
            if (!node.wrap && index <= 0) left.classList.add("bl4mm_disabled");
        }
        if (right) {
            onClick(right, function () {
                step(1);
            });
            if (!node.wrap && index >= choices.length - 1) right.classList.add("bl4mm_disabled");
        }
        var center = q(s, ".stepper_center");
        if (center) onClick(center, function () {
            step(1);
        });
        return s;
    }

    // The game's own key art, e.g. Glyphs/assets/PC/Ico_Hint_PC_Key_J.png
    function keyImage(key) {
        var names = { Zero: "0", One: "1", Two: "2", Three: "3", Four: "4", Five: "5", Six: "6", Seven: "7", Eight: "8", Nine: "9" };
        return "coui://uiresources/Glyphs/assets/PC/Ico_Hint_PC_Key_" + (names[key] || key) + ".png";
    }

    function rebind(mod, node, path, act) {
        var r = make("rebind", "rebind_entry");
        var primary = q(r, ".rebind_entry_glyph_primary");
        var secondary = q(r, ".rebind_entry_glyph_secondary");
        if (secondary) secondary.style.display = "none";
        var capture = link.state && link.state.capture;
        var capturing = !!capture && capture.mod === mod.name && capture.path.join("\u0001") === path.join("\u0001");
        if (primary) {
            primary.innerHTML = "";
            primary.classList.add("bl4mm_key");
            if (capturing) {
                primary.classList.add("hover_effect");
                primary.appendChild(el("p", "bl4mm_key_text small_text_1", "Press a key"));
            } else if (node.value && /^([A-Z]|F\d+|Zero|One|Two|Three|Four|Five|Six|Seven|Eight|Nine)$/.test(node.value)) {
                var img = el("img", "bl4mm_key_img");
                img.src = keyImage(node.value);
                primary.appendChild(img);
            } else {
                primary.appendChild(el("p", "bl4mm_key_text small_text_1", node.text));
            }
            hoverable(primary);
            if (node.rebindable) onClick(primary, function () {
                if (capturing) send({ op: "close_capture" });
                else act("capture");
            });
        }
        var clear = q(r, ".binding_clear_btn");
        var reset = q(r, ".binding_reset_btn");
        if (clear) {
            hoverable(clear);
            if (!node.rebindable || !node.value) clear.classList.add("bl4mm_disabled");
            else onClick(clear, function () {
                act("set", null);
            });
        }
        if (reset) {
            hoverable(reset);
            if (!node.rebindable || !node.changed) reset.classList.add("bl4mm_disabled");
            else onClick(reset, function () {
                act("reset");
            });
        }
        return r;
    }

    // endregion
    // region Details and messages

    function renderDetails(mod) {
        var d = view.details;
        if (ui.detailsPic) ui.detailsPic.innerHTML = "";
        if (d && d.item) {
            var item = d.item;
            ui.detailsTitle.textContent = item.name;
            ui.detailsMeta.textContent = [item.author ? "by " + item.author : "", item.version, "from " + item.repo_name]
                .filter(function (s) { return !!s; })
                .join("   |   ");  // prettier-ignore
            // Read from its download on Install (the confirm step), shown here too
            var scanned = ((repoData() || {}).jobs || {})[item.id];
            ui.detailsBody.textContent = (item.description || item.blurb || "") + (view.item ? "" : "\n\nClick for pictures and details.") +
                (scanned && scanned.uses ? "\n\nIts code uses: " + usesText(scanned.uses) + ". " + (scanned.hint || "") : "");  // prettier-ignore
            var first = (item.images || [])[0];
            if (first && ui.detailsPic) ui.detailsPic.appendChild(picture(first, "bl4mm_details_shot"));
            return;
        }
        if (d && d.node) {
            ui.detailsTitle.textContent = d.node.title || d.node.name;
            var meta = [];
            // A search result: whose setting it is
            if (view.mod === SEARCH && d.mod && d.mod.name) meta.push(d.mod.name);
            if (d.node.default !== undefined) meta.push("Default: " + d.node.default);
            if (d.node.kind === "keybind" && d.node.rebindable) meta.push("Click the key, then press the new one. Esc cancels.");
            ui.detailsMeta.textContent = meta.join("   |   ");
            ui.detailsBody.textContent = d.node.desc || "";
            return;
        }
        var shown = (d && d.mod) || mod;
        if (!shown) return;
        if (shown.pseudo === DISCOVER) {
            ui.detailsTitle.textContent = "Discover";
            ui.detailsMeta.textContent = "";
            ui.detailsBody.textContent =
                "Mods from your repositories you don't have yet, with their pictures. Install puts a mod in your" +
                " mods folder and starts it right away, no restart.\n\nOpen mods folder: drop any .sdkmod file in," +
                " and it shows here to load, also without a restart.";  // prettier-ignore
            return;
        }
        if (shown.pseudo === REPOS) {
            ui.detailsTitle.textContent = "Repositories";
            ui.detailsMeta.textContent = "";
            ui.detailsBody.textContent =
                "Where mods come from. BL4 Fix is built in; add any modder's GitHub repository (owner/name) or" +
                " a link to an index file on GitHub, and their mods show on Discover and update from Updates. Only" +
                " GitHub is allowed: repositories, downloads and pictures all come from there." +
                "\n\nModders: see docs/mod-repos.md in the BL4 Fix repository for how to publish one.";  // prettier-ignore
            return;
        }
        if (shown.pseudo === SEARCH) {
            ui.detailsTitle.textContent = "Search";
            ui.detailsMeta.textContent = "";
            ui.detailsBody.textContent =
                "Finds a setting in every mod at once. Type part of its name or description, or a mod's name: every" +
                " match shows with its mod, and you can change it right there.";  // prettier-ignore
            return;
        }
        if (shown.pseudo === PERF) {
            ui.detailsTitle.textContent = "Performance";
            ui.detailsMeta.textContent = "";
            ui.detailsBody.textContent =
                "How much each mod costs your frame rate. Run a benchmark, close the menus and play for a few seconds:" +
                " every enabled mod is timed, other modders' mods too, and the results show here, most expensive" +
                " first, with a text report to send to a mod's author.";  // prettier-ignore
            return;
        }
        if (shown.pseudo === REPORT) {
            ui.detailsTitle.textContent = "Report a problem";
            ui.detailsMeta.textContent = "";
            ui.detailsBody.textContent =
                "Something broke? Make a bug report here and attach the zip to a new issue on GitHub (Open issue" +
                " page). After a crash, start the game again first: the report then has the crashed session's log." +
                "\n\nThe number is how many different errors the mods logged since the game started.";  // prettier-ignore
            return;
        }
        if (shown.pseudo) {
            var u = updatesState() || {};
            ui.detailsTitle.textContent = "Updates";
            ui.detailsMeta.textContent = u.release ? "Latest release: " + u.release : "";
            ui.detailsBody.textContent = (updatesLine(u) || "") + (u.page ? "\n\nReleases: " + u.page : "") +
                "\n\nUpdates download from GitHub and install when the game closes (Restart now does both).";  // prettier-ignore
            return;
        }
        ui.detailsTitle.textContent = shown.name;
        ui.detailsMeta.textContent = [shown.author ? "by " + shown.author : "", shown.version, shown.status, shown.coop,
                                      shown.errors ? errorsText(shown.errors) + " this session (Report a problem)" : ""]
            .filter(function (s) { return !!s; })
            .join("   |   ");
        ui.detailsBody.textContent = shown.desc || "";
    }

    var toastTimer = null;
    function toast(text, error) {
        if (!ui.toast || !text) return;
        ui.toast.textContent = text;
        ui.toast.classList.toggle("bl4mm_toast_error", !!error);
        ui.toast.classList.remove("bl4mm_hidden");
        if (toastTimer !== null) clearTimeout(toastTimer);
        toastTimer = setTimeout(function () {
            toastTimer = null;
            if (ui.toast) ui.toast.classList.add("bl4mm_hidden");
        }, TOAST_MS);
    }

    // endregion
    // region Pause menu entry

    // The game's button is a <gbx-list-button>, which its stylesheet sizes by tag (45rem); our copy is a
    // plain div that rule doesn't match, so it would stretch across the screen. Copy the size over.
    function matchSize(entry, options) {
        var native = options.querySelector(".list_btn");
        if (!native) return;
        var cs = getComputedStyle(native);
        var ours = entry.querySelector(".list_btn");
        if (!ours || !cs.width || cs.width === "auto" || ours.style.width === cs.width) return;
        ours.style.width = cs.width;
        if (cs.height && cs.height !== "auto") ours.style.height = cs.height;
        ours.style.position = "relative";
        ours.style.flex = "0 0 auto";
        ns.log("pause entry sized " + cs.width + " x " + cs.height);
    }

    // The "!" next to a MODS label while updates are waiting (hidden otherwise). Drawn as a bar and
    // a dot, not the font's "!": the glyph sits off-centre in its line box whatever the alignment.
    // show: whether to show it (default: anything to look at)
    function setBadge(label, show) {
        if (!label) return;
        show = show === undefined ? !!badgeCount() : !!show;
        var badge = label.querySelector(".bl4mm_badge");
        if (!badge && !show) return;
        if (badge && !badge.querySelector(".bl4mm_badge_bar")) {
            badge.parentNode.removeChild(badge);  // a text "!" from an older menu.js
            badge = null;
        }
        if (!badge) {
            badge = el("span", "bl4mm_badge");
            badge.appendChild(el("span", "bl4mm_badge_bar"));
            badge.appendChild(el("span", "bl4mm_badge_dot"));
            label.appendChild(badge);
        }
        badge.classList.toggle("bl4mm_hidden", !show);
    }

    function injectPauseEntry() {
        var list = document.querySelector('[data-input-id="menu_pause_list"]');
        if (!list) return;
        var options = list.querySelector('div[name="Options Button"]');
        var optionsBtn = options && options.firstElementChild;
        if (!options || !optionsBtn) return;
        var existing = list.querySelector(".bl4mm_pause_entry");
        if (existing) {
            matchSize(existing, options);
            setBadge(q(existing, ".button_txt"));
            return;
        }
        var entry = el("div", options.className + " bl4mm_pause_entry");
        // Same structure as theirs: a full-size slot (their <gbx-component>) holding the button
        var slot = el("div", optionsBtn.className);
        var b = make("button", "list_btn");
        var label = q(b, ".button_txt");
        var theirs = q(optionsBtn, ".button_txt");
        if (label) {
            label.textContent = "Mods";
            if (theirs) label.className = theirs.className;
        }
        hoverable(b);
        onClick(b, function () {
            openScreen(badgePage());
        });
        slot.appendChild(b);
        entry.appendChild(slot);
        options.parentNode.insertBefore(entry, options.nextSibling);
        matchSize(entry, options);
        setBadge(label);
    }

    function watchPauseMenu() {
        try {
            injectPauseEntry();
        } catch (err) {
            ns.log("pause entry: " + err);
        }
        later(watchPauseMenu, 500);
    }

    // endregion
    // region Title screen entry

    function shown(node) {
        return !!node && node.getBoundingClientRect().height > 0;
    }

    // The entry MODS goes under: Options (hidden in some states), else Game Settings above it
    function mainAnchor(list) {
        var options = list.querySelector('div[name="Options Button"]');
        if (shown(options) && q(options, ".mm_button_txt")) return options;
        var settings = list.querySelector('div[name="Game Settings Button"]');
        if (shown(settings) && q(settings, ".mm_button_txt")) return settings;
        return null;  // not drawn yet
    }

    // MODS right under Options, copied from the game's own rendered Options button, so it looks
    // the same (cleaned like the templates: no bindings, no custom elements). Hover works like
    // theirs: GbxButton puts "hover_effect" on the inner gbx-main-menu-button
    // (.menu_button_wrapper), which lights the background, the orange bars and the arrows, and
    // the button plays ElementFocused on mouseenter.
    function injectMainEntry() {
        var list = document.querySelector('[data-input-id="menu_main_list"]');
        if (!list) return;
        var anchor = mainAnchor(list);
        var existing = list.querySelector(".bl4mm_main_entry");
        if (existing) {
            // Options can show up after we placed MODS under Game Settings
            if (anchor && anchor.nextElementSibling !== existing) list.insertBefore(existing, anchor.nextSibling);
            setBadge(q(existing, ".bl4mm_main_label") || existing);
            return;
        }
        if (!anchor) return;
        var entry = parse(anchor.outerHTML).firstElementChild;
        if (!entry) return;
        entry.removeAttribute("name");
        entry.classList.add("bl4mm_main_entry");
        // Their state at the moment we copied it (hovered, disabled, a "new" pip)
        var stale = entry.querySelectorAll(".hover_effect, .disabled, .pip.visible");
        for (var i = 0; i < stale.length; i++) {
            ["hover_effect", "disabled", "visible"].forEach(function (c) { stale[i].classList.remove(c); });
        }
        var theirs = q(anchor, ".mm_button_txt");
        var label = q(entry, ".mm_button_txt");
        if (label) {
            var text = (theirs && theirs.textContent || "").trim();
            label.textContent = text && text === text.toUpperCase() ? "MODS" : "Mods";
            label.classList.add("bl4mm_main_label");
        }
        var wrapper = q(entry, ".menu_button_wrapper") || entry.firstElementChild || entry;
        hoverable(entry, wrapper);
        entry.addEventListener("mouseenter", function () {
            var widget = document.querySelector("widget-menu-main");
            try {
                if (widget && typeof widget.PlaySound === "function") widget.PlaySound("ElementFocused");
            } catch (err) {}
        });
        onClick(entry, function () {
            openScreen(badgePage());
        });
        list.insertBefore(entry, anchor.nextSibling);
        setBadge(label || entry);
        ns.log("main menu entry added under " + anchor.getAttribute("name"));
    }

    function watchMainMenu() {
        try {
            injectMainEntry();
        } catch (err) {
            ns.log("main entry: " + err);
        }
        later(watchMainMenu, 500);
    }

    // endregion

    ns.teardown = function () {
        ns.gen = 0;
        [ui.scroller, ui.modsScroller].forEach(function (scroller) {
            if (scroller && scroller.destroy) {
                try {
                    scroller.destroy();
                } catch (err) {}
            }
        });
        timers.forEach(clearTimeout);
        if (pollTimer !== null) clearTimeout(pollTimer);
        if (repoTimer !== null) clearTimeout(repoTimer);
        docListeners.forEach(function (l) {
            document.removeEventListener(l[0], l[1]);
        });
        // Every copy's screen, in case an older copy was never torn down
        var roots = document.querySelectorAll(".bl4mm_root");
        for (var r = 0; r < roots.length; r++) roots[r].parentNode.removeChild(roots[r]);
        var entries = document.querySelectorAll(".bl4mm_pause_entry, .bl4mm_main_entry");
        for (var i = 0; i < entries.length; i++) entries[i].parentNode.removeChild(entries[i]);
        var covered = document.querySelectorAll(".bl4mm_covered");
        for (var c = 0; c < covered.length; c++) covered[c].classList.remove("bl4mm_covered");
    };

    // A reload keeps the screen open on the same page
    // (only if the old copy was polling right up to now: if the page just woke up, the screen was
    // left over from a closed menu)
    var wasOpen = ns.view && ns.view.open && link.polledAt && Date.now() - link.polledAt < WAKE_GAP_MS;
    if (ns.view) {
        view.mod = ns.view.mod;
        view.nested = ns.view.nested || [];
        view.installNew = ns.view.installNew || {};
        view.newTags = ns.view.newTags || {};
        view.item = ns.view.item || null;
        view.shot = ns.view.shot || 0;
    }
    ns.view = view;

    // Screens other mods add to this page (extensions.py): extensions.js loads their scripts. It
    // runs on its own; this just (re)runs it when its file changes.
    function checkExtensions() {
        if (ns.extJsText && ns.dev === false) {
            later(checkExtensions, 2000);
            return;
        }
        ns.get(BASE + "extensions.js", function (js) {
            if (!alive()) return;
            if (js && js !== ns.extJsText) {
                ns.extJsText = js;
                try {
                    new Function(js)();
                } catch (err) {
                    ns.log("extensions.js failed: " + err);
                }
            }
            later(checkExtensions, 2000);
        });
    }
    checkExtensions();

    loadTemplates(function () {
        if (!alive()) return;
        try {
            build();
        } catch (err) {
            ns.log("build failed: " + err + " " + (err && err.stack));
            return;
        }
        ns.log("ready, templates: " + Object.keys(ns.templates).filter(function (k) {
            return !!ns.templates[k];
        }).join(","));
        if (ns.context === "main") watchMainMenu();
        else watchPauseMenu();
        if (wasOpen) openScreen();
        poll();
        if (typeof requestAnimationFrame === "function") {
            lastFrame = Date.now();
            requestAnimationFrame(onFrame);
        }
    });
})();
