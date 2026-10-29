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

    // The "!" on the MODS entries: anything to look at
    function badgeCount() {
        return updatesWaiting() + freshMods().length;
    }

    // Where MODS opens while it has a "!": updates first, else the new mods
    function badgePage() {
        return updatesWaiting() ? UPDATES : freshMods().length ? DISCOVER : null;
    }

    function pseudoPage(key) {
        if (key === UPDATES) return updatesPage();
        if (key === DISCOVER) return { name: "Discover", pseudo: DISCOVER, desc: "", version: "", status: "", coop: "" };
        if (key === REPOS) return { name: "Repositories", pseudo: REPOS, desc: "", version: "", status: "", coop: "" };
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
        frame.appendChild(header);

        var body = el("div", "bl4mm_body");
        var modsCol = el("div", "bl4mm_mods");
        ui.mods = modsCol;
        body.appendChild(modsCol);

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
        document.body.appendChild(root);
        ui.root = root;
        // The game's scrollbar (the inventory and popups use it); without it, the wheel still scrolls
        if (typeof window.OAKScroll === "function") {
            try {
                ui.scroller = window.OAKScroll(ui.options);
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
                } catch (err) {}
            });
        });
    }

    // An entry for one of our own pages (Updates, Discover, Repositories) at the top of the list
    function pseudoButton(key, word, kind) {
        var b = listButton(pseudoPage(key).name, function () {
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
            var word = count ? count + " new" : { checking: "Checking", downloading: "Downloading",
                ready: "Restart", error: "Error", current: "Up to date" }[u.status] || "";  // prettier-ignore
            var ub = pseudoButton(UPDATES, word, count || u.status === "ready" ? "warn" : u.status === "error" ? "off" : "on");
            setBadge(q(ub, ".button_txt") || ub, count);
        }
        // Discover's count: mods dropped into the folder waiting to load first, else what's new
        var fresh = freshMods().length;
        var db = pseudoButton(DISCOVER, rs.found ? rs.found + " to load" : fresh ? fresh + " new" : rs.discover ? String(rs.discover) : "",
            rs.found || fresh ? "warn" : "on");  // prettier-ignore
        setBadge(q(db, ".button_txt") || db, fresh);
        pseudoButton(REPOS, rs.sources ? String(rs.sources) : "", "on");
        mods.forEach(function (mod) {
            var b = listButton(mod.name, function () {
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
        if (!view.nested.length && mod.can_toggle) {
            addEntry(mod, { kind: "enable", id: "", name: "Enabled", title: "Enabled",
                            desc: "Turns " + mod.name + " on or off right now.", value: mod.enabled,
                            off: "Off", on: "On" }, []);  // prettier-ignore
        }
        addNodes(mod, page.nodes, page.prefix, 0, custom && custom.keep);
        if (!ui.options.children.length) {
            ui.options.appendChild(el("p", "bl4mm_empty small_text_2", "This mod has no settings."));
        }
        ui.options.scrollTop = scroll;
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
                u.status === "error" ? "No update information." : "Everything is up to date."));  // prettier-ignore
        }
    }

    // Updates from the added repositories, under BL4 Fix's. Returns how many it listed.
    function renderRepoUpdates() {
        var r = repoData();
        var list = (r && r.updates) || [];
        if (!list.length) return 0;
        var jobs = r.jobs || {};
        addTitle("From your repositories");
        var open = [];
        list.forEach(function (item) {
            var job = jobs[item.id];
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

    // "   45%", "   RELOADED"...: a job's state after a version or name
    function jobWord(job) {
        if (!job) return "";
        if (job.state === "downloading") {
            var p = job.progress || [0, 0];
            return "   " + (p[1] ? Math.floor((100 * p[0]) / p[1]) + "%" : "...");
        }
        return "   " + ({ loading: "LOADING", loaded: "INSTALLED", reloaded: "RELOADED", staged: "AT RESTART",
                         failed: "FAILED", removed: "REMOVED" }[job.state] || "");  // prettier-ignore
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

    // The button (or state) for a Discover entry: Install, a progress figure, or how it ended
    function installPart(item, jobs) {
        var job = jobs[item.id];
        var state = job ? job.state : "";
        if (!state || state === "failed") {
            return listButton(state === "failed" ? "Try again" : "Install", function () {
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

        var actions = el("div", "bl4mm_update_actions");
        actions.appendChild(installPart(item, r.jobs || {}));
        if (item.homepage) {
            actions.appendChild(listButton("Web page", function () {
                sendRepo("repo_link", item.homepage);
            }, "bl4mm_update_btn"));
        }
        ui.options.appendChild(actions);

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
        var box = boxState();
        var pending = r.pending;
        var editing = box.editing;
        var editName = editing ? sourceName(r, editing) : "";
        if (!pending) {
            addTitle(editing ? "Edit " + editName : "Add a repository");
            addTextBox(box, editing
                ? "Change the link of " + editName + ". Enter saves, Esc stops typing, Ctrl+V pastes."
                : "Click the box and paste (Ctrl+V) or type the modder's GitHub link, e.g.\n" +
                  "https://github.com/flameaway/bl4-fix\nor just flameaway/bl4-fix. An https link to an index works " +
                  "too. Enter adds, Esc stops typing. (Or type modrepo add <link> in the console.)");  // prettier-ignore
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
            addInfoRow(pending.name, pending.count + " mod" + (pending.count === 1 ? "" : "s"),
                (pending.description ? pending.description + "\n\n" : "") + pending.key +
                (pending.mods && pending.mods.length ? "\n\nMods: " + pending.mods.join(", ") : ""), null);  // prettier-ignore
            if (pending.description) ui.options.appendChild(el("p", "bl4mm_new_blurb", pending.description));
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
            var word = s.hidden ? "REMOVED" : s.official ? "BUILT IN" : s.status === "error" ? "ERROR" : s.status === "checking" ? "CHECKING" : (s.message || "").toUpperCase();
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
                (web ? "\n\n" + web : ""), buttons);  // prettier-ignore
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

    function isGithubKey(key) {
        return /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(key || "");
    }

    // Where a repository's button goes: its GitHub page, else its own home page, else the index
    function repoWebLink(s) {
        if (isGithubKey(s.key)) return "https://github.com/" + s.key;
        if (s.homepage && /^https:\/\//i.test(s.homepage)) return s.homepage;
        return /^https:\/\//i.test(s.url || "") ? s.url : "";
    }

    // The text box: Python reads the keys (none reach this page) and sends the text in its state
    function addTextBox(box, desc) {
        var wrap = make("regular", "menu_options_regular_entry");
        wrap.classList.add("bl4mm_entry_wrap");
        var entry = q(wrap, ".menu_options_entry") || wrap;
        var label = q(wrap, ".menu_options_entry_text");
        if (label) label.textContent = "Link";
        var comp = q(wrap, ".menu_options_entry_component") || wrap;
        comp.innerHTML = "";
        var field = el("div", "bl4mm_textbox" + (box.typing ? " bl4mm_typing" : ""));
        var text = el("p", "bl4mm_textbox_text" + (box.text || box.typing ? "" : " bl4mm_textbox_hint"),
            box.text || (box.typing ? "" : "https://github.com/flameaway/bl4-fix"));  // prettier-ignore
        field.appendChild(text);
        if (box.typing) field.appendChild(el("div", "bl4mm_caret"));
        comp.appendChild(field);
        onClick(entry, function () {
            send({ op: box.typing ? "box_stop" : "box_type" });
        });
        entry.addEventListener("mouseenter", function () {
            entry.classList.add("hover_effect");
            view.details = { node: { name: "Link", title: "Repository link", desc: desc } };
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

    function addEntry(mod, node, path) {
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
                comp.appendChild(listButton("Open", function () {
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
            ui.detailsBody.textContent = (item.description || item.blurb || "") + (view.item ? "" : "\n\nClick for pictures and details.");
            var first = (item.images || [])[0];
            if (first && ui.detailsPic) ui.detailsPic.appendChild(picture(first, "bl4mm_details_shot"));
            return;
        }
        if (d && d.node) {
            ui.detailsTitle.textContent = d.node.title || d.node.name;
            var meta = [];
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
                " an https link to a repository index, and their mods show on Discover and update from Updates." +
                "\n\nModders: see docs/mod-repos.md in the BL4 Fix repository for how to publish one.";  // prettier-ignore
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
        ui.detailsMeta.textContent = [shown.author ? "by " + shown.author : "", shown.version, shown.status, shown.coop]
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
        if (ui.scroller && ui.scroller.destroy) {
            try {
                ui.scroller.destroy();
            } catch (err) {}
        }
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
