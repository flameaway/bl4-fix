// Carnage Meter boss fight pop-up (HUD page). popup.js runs this and hands it popup.json's events;
// a boss event the page hasn't shown yet, and that is fresh, slides a results panel in at the top
// of the screen: the boss, each player's damage to it with a bar, their top 3 sources, the MVP. It
// holds for a few seconds and fades. Nothing to click (the HUD takes no input); the full report is
// the Boss Report tab of the Carnage Report (F9).
//
// Reloaded by popup.js when the files change: Bl4Cm_Dispose removes this copy's panel and timers.
(function () {
    "use strict";

    var ART = "coui://uiresources/_shared/assets/";
    var CROWN = ART + "ui_art_misc_assets/ui_icon_blue_crown.png";
    var SKULL = "coui://uiresources/components/menu_dropdown/assets/skull.png";
    var FRESH_S = 30;  // events older than this never show (the HUD page reloads with each map)
    var HOLD_MS = 4500;
    var FADE_MS = 700;
    var ROWS = 4;

    // Shown ids outlive a reload of this script (window), so an update never replays one
    var seen = window.Bl4Cm_Seen || (window.Bl4Cm_Seen = {});
    var timers = [];
    var current = null;

    function later(fn, ms) {
        timers.push(setTimeout(fn, ms));
    }

    function el(tag, cls, text) {
        var e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined && text !== null) e.textContent = text;
        return e;
    }

    function commas(n) {
        var s = String(Math.round(n));
        var out = "";
        while (s.length > 3) {
            out = "," + s.slice(-3) + out;
            s = s.slice(0, -3);
        }
        return s + out;
    }

    function clock(seconds) {
        seconds = Math.max(0, Math.round(seconds || 0));
        return Math.floor(seconds / 60) + ":" + ("0" + (seconds % 60)).slice(-2);
    }

    function pct(part, whole) {
        if (!whole) return "0%";
        var p = (part / whole) * 100;
        return (p >= 10 || p === 0 ? Math.round(p) : p.toFixed(1)) + "%";
    }

    function remove(node) {
        if (node && node.parentNode) node.parentNode.removeChild(node);
    }

    function show(ev) {
        remove(current);
        var players = (ev.players || []).slice(0, ROWS);
        var root = el("div", "bl4cmp_root");
        var panel = el("div", "bl4cmp_panel");
        panel.appendChild(el("div", "bl4cmp_stripe"));

        var head = el("div", "bl4cmp_head");
        var pic = el("div", "bl4cmp_pic" + (ev.picture ? "" : " bl4cmp_pic_none"));
        pic.style.backgroundImage = 'url("' + (ev.picture || SKULL) + '")';
        head.appendChild(pic);
        var titles = el("div", "bl4cmp_titles");
        titles.appendChild(el("p", "bl4cmp_kicker", "BOSS DEFEATED"));
        titles.appendChild(el("p", "bl4cmp_boss", (ev.boss || "Boss").toUpperCase()));
        titles.appendChild(el("p", "bl4cmp_meta", clock(ev.seconds) + "  |  " + commas(ev.team || 0) + " DAMAGE"));
        head.appendChild(titles);
        panel.appendChild(head);

        var fills = [];
        players.forEach(function (p, i) {
            var row = el("div", "bl4cmp_row" + (i === 0 && players.length > 1 ? " bl4cmp_mvp" : "") + (p.local ? " bl4cmp_you" : ""));
            var rank = el("div", "bl4cmp_rank");
            if (i === 0 && players.length > 1) {
                var crown = el("div", "bl4cmp_crown");
                crown.style.backgroundImage = 'url("' + CROWN + '")';
                rank.appendChild(crown);
            } else {
                rank.appendChild(el("p", "bl4cmp_rank_text", "#" + (i + 1)));
            }
            row.appendChild(rank);
            var portrait = el("div", "bl4cmp_portrait");
            portrait.style.backgroundImage = 'url("' + (p.portrait ? (p.portrait.indexOf("://") >= 0 ? p.portrait : ART + p.portrait) : SKULL) + '")';
            row.appendChild(portrait);
            var mid = el("div", "bl4cmp_mid");
            var line = el("div", "bl4cmp_line");
            line.appendChild(el("p", "bl4cmp_name", p.name));
            if (p.local) line.appendChild(el("p", "bl4cmp_tag", "YOU"));
            if (i === 0 && players.length > 1) line.appendChild(el("p", "bl4cmp_tag bl4cmp_tag_mvp", "MVP"));
            mid.appendChild(line);
            var track = el("div", "bl4cmp_track");
            var fill = el("div", "bl4cmp_fill");
            track.appendChild(fill);
            fills.push([fill, ev.team ? p.dmg / ev.team : 0]);
            mid.appendChild(track);
            // Top damage sources: "1 JELLY 45%", coloured by rarity / element / kind
            var tops = p.tops || [];
            if (tops.length) {
                var list = el("div", "bl4cmp_tops");
                tops.forEach(function (t, j) {
                    var chip = el("div", "bl4cmp_src");
                    chip.appendChild(el("p", "bl4cmp_src_rank", String(j + 1)));
                    chip.appendChild(el("p", "bl4cmp_src_name bl4cmp_top_" + (t.look || "plain"), (t.name || "").toUpperCase()));
                    chip.appendChild(el("p", "bl4cmp_src_share", t.share + "%"));
                    list.appendChild(chip);
                });
                mid.appendChild(list);
            }
            row.appendChild(mid);
            var right = el("div", "bl4cmp_right");
            right.appendChild(el("p", "bl4cmp_dmg", commas(p.dmg)));
            right.appendChild(el("p", "bl4cmp_pct", pct(p.dmg, ev.team)));
            row.appendChild(right);
            panel.appendChild(row);
        });
        if (ev.key) panel.appendChild(el("p", "bl4cmp_hint", ev.key + "  FULL CARNAGE REPORT"));
        root.appendChild(panel);
        document.body.appendChild(root);
        current = root;

        // Gameface lays new DOM out a frame later: start the transitions after that
        requestAnimationFrame(function () {
            requestAnimationFrame(function () {
                if (current !== root) return;
                root.classList.add("bl4cmp_in");
                fills.forEach(function (f) {
                    f[0].style.width = Math.max(0, Math.min(100, f[1] * 100)).toFixed(1) + "%";
                });
            });
        });
        later(function () {
            if (current !== root) return;
            root.classList.remove("bl4cmp_in");
            root.classList.add("bl4cmp_out");
            later(function () {
                remove(root);
                if (current === root) current = null;
            }, FADE_MS);
        }, HOLD_MS);
    }

    window.Bl4Cm_OnState = function (state) {
        var now = Date.now() / 1000;
        var events = (state && state.events) || [];
        for (var i = 0; i < events.length; i++) {
            var ev = events[i];
            if (!ev || ev.kind !== "boss" || seen[ev.id]) continue;
            seen[ev.id] = true;
            if (now - (ev.t || 0) <= FRESH_S) show(ev);
        }
    };

    window.Bl4Cm_Dispose = function () {
        timers.forEach(clearTimeout);
        timers = [];
        remove(current);
        current = null;
        var left = document.querySelectorAll(".bl4cmp_root");
        for (var i = 0; i < left.length; i++) remove(left[i]);
    };
})();
