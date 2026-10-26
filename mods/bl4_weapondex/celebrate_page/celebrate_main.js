// Kairosdex celebrations (loaded by celebrate.js inside the game's HUD banner widget,
// celebratory_notifications.html). A boss beaten for the first time, or a legendary / pearl
// obtained for the first time, gets the game's own big banner: a copy of the widget's "special
// notification" markup (the level up / mission complete banner), filled with our text and picture,
// played with the widget's own animation (celebratory_notification_special, from its .data file)
// and the same hold-then-reverse timing as Widget_CelebratoryNotifications.PlayCelebratoryAnimation.
//
// Events come from the mod through celebrate.json (window.Bl4Kdx_OnState): {id, t, kind
// ("boss" | "legendary" | "pearl"), verb, name, sub, label, amount, picture, icon, hold_ms, sound}.
// Only events newer than MAX_AGE_S show (the HUD page reloads on every map), each once, one at a
// time. The game's own banners come first: ours waits while one shows, and steps aside if one
// starts.
(function () {
    "use strict";

    var ns = (window.bl4kdxCeleb = window.bl4kdxCeleb || { counter: 0, seen: {} });
    var gen = ++ns.counter;  // never reset: a retired copy sees it moved and stops
    var ANIM = "celebratory_notification_special";
    var ANIM_MS = 1400;  // the animation's length (its last keyframe is at 1.3 s)
    var HOLD_MS = 6500;  // shown for this long in all, like a level up
    var MAX_AGE_S = 30;
    var RETRY_MS = 500;
    var timers = [];
    var queue = [];
    var current = null;  // {node, widget, event, shownAt}

    function live() {
        return gen === ns.counter;
    }

    function later(fn, ms) {
        var id = setTimeout(function () {
            var at = timers.indexOf(id);
            if (at >= 0) timers.splice(at, 1);
            if (live()) fn();
        }, ms);
        timers.push(id);
    }

    function widget() {
        var w = document.querySelector("widget-notification-message");
        return w && typeof w.PlayAnimation === "function" && w.querySelector(".celeb_special_notif_cntr") ? w : null;
    }

    // region The game's own banners

    function shows(el) {
        if (!el) return false;
        for (var node = el; node && node.classList; node = node.parentElement) {
            if (node.classList.contains("invisible") || node.classList.contains("inactive")) return false;
            if (node.classList.contains("celeb_notif_wrapper")) break;
        }
        var opacity = parseFloat(window.getComputedStyle(el).opacity);
        return !(opacity < 0.05);
    }

    function nativeShowing(w) {
        try {
            var special = null;
            var roots = w.querySelectorAll(".celeb_special_notif_cntr");
            for (var i = 0; i < roots.length && !special; i++) {
                if (!roots[i].classList.contains("bl4kdx_celeb")) special = roots[i].querySelector(".celeb_sn_notif_txt");
            }
            var generic = w.querySelector(".celeb_notif .celeb_notif_txt");
            return (shows(special) && !!special.textContent) || (shows(generic) && !!generic.textContent);
        } catch (err) {
            return false;
        }
    }

    // The game starting a banner of its own: ours goes away (its animation stops all of the
    // widget's animations, ours included) and comes back after it if it had only just started.
    // The wrapper is installed once per widget and calls whichever copy of this script is current.
    function watchNative(w) {
        if (!w.bl4kdxNative) {
            var original = w.PlayCelebratoryAnimation;
            if (typeof original !== "function") return;
            w.bl4kdxNative = original;
            w.PlayCelebratoryAnimation = function () {
                try {
                    if (window.bl4kdxCeleb && window.bl4kdxCeleb.onNative) window.bl4kdxCeleb.onNative();
                } catch (err) {
                    console.error("bl4kdx: " + err);
                }
                return original.apply(this, arguments);
            };
        }
        ns.onNative = function () {
            if (!live() || !current) return;
            var shown = current;
            finish(true);
            if (Date.now() - shown.shownAt < 2500) queue.unshift(shown.event);
        };
    }

    // endregion
    // region Showing one

    function clean(root) {
        var all = [root].concat(Array.prototype.slice.call(root.querySelectorAll("*")));
        all.forEach(function (el) {
            Array.prototype.slice.call(el.attributes).forEach(function (attr) {
                if (attr.name.indexOf("data-bind") === 0 || attr.name === "style") el.removeAttribute(attr.name);
            });
            el.classList.remove("invisible");
            el.classList.remove("inactive");
        });
    }

    function setText(root, selector, text) {
        var el = root.querySelector(selector);
        if (el) el.textContent = text || "";
        return el;
    }

    function element(className, text) {
        var el = document.createElement("div");
        el.className = className;
        if (text) el.textContent = text;
        return el;
    }

    function hide(el) {
        if (el) el.classList.add("bl4kdx_gone");
    }

    function build(w, e) {
        var node = w.querySelector(".celeb_special_notif_cntr").cloneNode(true);
        clean(node);
        node.classList.add("bl4kdx_celeb");
        node.classList.add("bl4kdx_" + (e.kind || "legendary"));
        node.classList.add(e.picture ? "bl4kdx_has_picture" : "bl4kdx_no_picture");

        var art = node.querySelector(".celeb_sn_vault_icon_bkg");
        if (art) art.className = "celeb_sn_vault_icon_bkg bl4kdx_art";

        // The icon above the text: the boss's portrait or the item's inventory render, else an icon
        var icon = node.querySelector(".celeb_sn_notif_icon");
        if (icon) {
            icon.className = "celeb_sn_notif_icon bl4kdx_icon";
            var url = e.picture || e.icon;
            if (url) icon.style.backgroundImage = 'url("' + url + '")';
            var glow = document.createElement("div");
            glow.className = "bl4kdx_glow";
            icon.parentNode.insertBefore(glow, icon);
        }

        setText(node, ".celeb_notif_verb_description_txt", e.verb);
        // Inside the banner's name line, so the game's animation brings them in with it
        var name = node.querySelector(".celeb_sn_notif_txt");
        if (name) {
            name.textContent = "";
            name.appendChild(element("bl4kdx_band", ""));
            name.appendChild(element("bl4kdx_name", e.name));
            if (e.sub) name.appendChild(element("bl4kdx_sub", e.sub));
        }

        // The reward row: "KAIROSDEX  125 / 318"
        setText(node, ".celeb_notif_reward_txt", e.label);
        setText(node, ".celeb_notif_reward_txt_amount", e.amount);
        hide(node.querySelector(".celeb_notif_reward_icon"));
        hide(node.querySelector(".celeb_sn_notif_btn_prompt"));
        if (!e.label && !e.amount) hide(node.querySelector(".celeb_notif_reward_wrapper"));
        return node;
    }

    function show(w, e) {
        var node = build(w, e);
        w.querySelector(".celeb_notif_wrapper").appendChild(node);
        var hold = Math.max(ANIM_MS * 2, e.hold_ms || HOLD_MS);
        current = { node: node, widget: w, event: e, shownAt: Date.now() };
        w.PlayAnimation(ANIM, { root: node });
        if (e.sound) {
            try {
                w.PlaySound(e.sound);
            } catch (err) {
                console.error("bl4kdx sound: " + err);
            }
        }
        var mine = current;
        later(function () {
            if (current !== mine) return;
            w.PlayAnimation(ANIM, { root: node, direction: "reverse" });
        }, hold - ANIM_MS);
        // Not on the animation's onEnd: the widget may have stopped it (a banner of its own)
        later(function () {
            if (current === mine) finish(false);
        }, hold + 100);
    }

    function finish(abrupt) {
        var shown = current;
        current = null;
        if (!shown) return;
        try {
            if (abrupt) shown.widget.StopAnimation(ANIM, { root: shown.node });
        } catch (err) {
            // the widget's own stop may have got there first
        }
        if (shown.node.parentNode) shown.node.parentNode.removeChild(shown.node);
        later(pump, 400);
    }

    function pump() {
        if (current || !queue.length) return;
        var w = widget();
        if (!w) {
            later(pump, RETRY_MS);
            return;
        }
        watchNative(w);
        if (nativeShowing(w)) {
            later(pump, RETRY_MS);
            return;
        }
        var e = queue.shift();
        try {
            show(w, e);
        } catch (err) {
            console.error("bl4kdx show: " + err);
            current = null;
            later(pump, RETRY_MS);
        }
    }

    // endregion

    window.Bl4Kdx_OnState = function (state) {
        if (!live() || !state || !state.events) return;
        var now = Date.now() / 1000;
        if (Object.keys(ns.seen).length > 200) ns.seen = {};  // the file only ever holds the latest few
        for (var i = 0; i < state.events.length; i++) {
            var e = state.events[i];
            if (!e || ns.seen[e.id]) continue;
            ns.seen[e.id] = true;
            if (now - (e.t || 0) <= MAX_AGE_S) queue.push(e);
        }
        pump();
    };

    window.Bl4Kdx_Dispose = function () {
        timers.forEach(clearTimeout);
        timers = [];
        if (current) finish(true);
        queue = [];
        ns.onNative = null;
        var left = document.querySelectorAll(".bl4kdx_celeb");
        for (var i = 0; i < left.length; i++) left[i].parentNode.removeChild(left[i]);
        ns.counter++;  // retire this copy for good
    };
})();
