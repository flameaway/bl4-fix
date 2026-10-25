"use strict";

// BL4 Reroll on the item card: hover a class mod or enhancement in your backpack and
//
//     [T] Reroll All (5 Eridium)     every modifier rolls again (after asking)
//     [G] Pick One                   marks a modifier (G again: the next); T then replaces it
//
// The BL4 Mod Menu runs this file in the game's menu page (its extensions.js) and runs it again
// when it changes, so it tears its last copy down first. Python (bl4_reroll/card.py) does the
// work: we tell it which card is showing (the card has no item id, so we send what it shows: name,
// level, rarity, modifier texts) and it finds the backpack item, reads the keys from Windows (no
// key events reach this page) and rerolls. Its state, polled through GET /ext?k=reroll, says
// which item the card is, which modifier is picked, and the last roll: for each modifier its old
// and new name and other names it could have rolled. We play that as a slot machine: each
// rerolled line spins through names and stops, one after another, on what it got. Then we say
// "landed" and Python changes the item (in place, the way the game's Firmware Transfer does), and
// a notice in the corner says what happened.
//
// To know which item a card is, Python also sends every rerollable item's card details, and we
// match them here, drawing the prompts and highlights from the card's own update (like the
// Detailed Item Cards mod) inside the card's layout. The game's rows are never moved; they get a
// class for the highlight, their text is hidden while a reel covers them, and only the slot
// machine is drawn in our own layer on top of the card.

(function () {
    var ns = window.bl4ModMenu;
    if (!ns || !ns.ext) return;
    var ext = ns.ext;
    var KEY = "reroll";
    var BASE = "coui://uiresources/bl4reroll/";
    var CSS_URL = BASE + "card.css";
    var FRAME_MS = 16;
    // Which card shows is checked every frame while a class mod / enhancement card is up or the
    // reels spin, and this often otherwise (a card the game fills is drawn at once by onCard)
    var WATCH_IDLE_MS = 50;
    // Python's state is fetched this often while a class mod / enhancement card is up or the reels
    // spin (key presses show quickly), otherwise slowly; a new such card fetches at once
    var POLL_FAST_MS = 60;
    var POLL_IDLE_MS = 500;
    var HOVER_REPEAT_MS = 400;
    var CSS_MS = 2000;
    // Slot machine timing: the first reel spins this long, each next one stops this much later
    var SPIN_MS = 1100;
    var STAGGER_MS = 420;
    var ONE_SPIN_MS = 1500;
    // Names each reel passes before landing (more for the later reels: they spin longer)
    var SPIN_STEPS = 16;
    var STAGGER_STEPS = 6;
    var TICK_SOUND_MS = 55;
    // After the last reel stops: tell Python to make the item (card.py's LANDED_S matches)
    var LANDED_MS = 800;
    var BANNER_MS = 3200;
    var ERROR_MS = 3200;
    var TOAST_MS = 4500;
    // The game's UI sound cues (ui_script_backpack functions, played through ScriptEvent_Spatial)
    var SOUND_PAY = "BuyItem";
    var SOUND_TICK = "BackpackSlotFocused";
    var SOUND_STOP = "ClickClassMod";
    var SOUND_DONE = "MarkItemFavorite";
    var SOUND_ERROR = "Error";
    var SILENT_REASONS = { "not rerollable": 1, "not in your backpack": 1 };

    if (ext.teardowns[KEY]) {
        try {
            ext.teardowns[KEY]();
        } catch (err) {}
    }
    // Survives reloads of this file; each copy takes a new, ever higher number
    var rr = ns.reroll || (ns.reroll = {});
    rr.counter = Math.max(rr.counter || 0, rr.gen || 0) + 1;
    var gen = rr.counter;
    rr.gen = gen;

    var timers = [];
    function alive() {
        return rr.gen === gen;
    }
    function forget(id) {
        var at = timers.indexOf(id);
        if (at >= 0) timers.splice(at, 1);
    }
    function later(fn, ms) {
        var id = setTimeout(function () {
            // Fired: drop its id, or the list grows by one per poll for as long as the page lives
            forget(id);
            if (alive()) fn();
        }, ms);
        timers.push(id);
        return id;
    }
    function cancel(id) {
        clearTimeout(id);
        forget(id);
    }
    function log(text) {
        if (ns.log) ns.log("[reroll] " + text);
    }

    var state = null;
    var shown = { card: null, fp: null, prevFp: null, sentFp: undefined, sentAt: 0 };
    var anim = null;

    // region Helpers

    function element(tag, className, text) {
        var el = document.createElement(tag);
        el.className = className;
        if (text !== undefined) el.textContent = text;
        return el;
    }

    function playSound(id) {
        if (typeof WidgetMgr === "undefined" || !WidgetMgr.WidgetMap) return;
        WidgetMgr.WidgetMap.forEach(function (widget) {
            if (typeof widget.TriggerWidgetEvent === "function") widget.TriggerWidgetEvent("ScriptEvent_Spatial", id, 0, 0);
        });
    }

    function keyImage(key) {
        var names = { Zero: "0", One: "1", Two: "2", Three: "3", Four: "4", Five: "5", Six: "6", Seven: "7", Eight: "8", Nine: "9" };
        return "coui://uiresources/Glyphs/assets/PC/Ico_Hint_PC_Key_" + (names[key] || key) + ".png";
    }

    function rect(el) {
        return el ? el.getBoundingClientRect() : null;
    }

    // Coherent arrays: iterate by index
    function list(arr) {
        var out = [];
        if (!arr) return out;
        for (var i = 0; i < arr.length; i++) out.push(arr[i]);
        return out;
    }

    // endregion
    // region Which card is showing

    function visibleCards() {
        var out = [];
        var cards = document.querySelectorAll("widget-item-card");
        for (var i = 0; i < cards.length; i++) {
            var card = cards[i];
            var data = card.DataModel;
            var tooltip = card.querySelector(".item_card_tooltip");
            // The card element measures 0 high and "visible" flickers; the tooltip box is what
            // has a size while the card is up
            if (data && data.name && tooltip && tooltip.getBoundingClientRect().height > 0) out.push(card);
        }
        return out;
    }

    // The hovered item's card (not the equipped one it's compared with)
    function currentCard() {
        var cards = visibleCards();
        for (var i = 0; i < cards.length; i++) if (cards[i].DataModel.mousepositioned && !cards[i].DataModel.comparemode) return cards[i];
        for (var j = 0; j < cards.length; j++) if (!cards[j].DataModel.comparemode) return cards[j];
        return cards[0] || null;
    }

    function cardInfo(card) {
        var d = card && card.DataModel;
        if (!d || d.abbreviated || (d.itemtype !== "classmod" && d.itemtype !== "enhancement")) return null;
        return {
            name: String(d.name || "").trim(),
            level: String(d.level || ""),
            rarity: String(d.rarityident || ""),
            type: d.itemtype,
            banner: String(d.bannertype || ""),
            secondary: list(d.secondary_stat_entries).map(function (e) { return String(e.value || ""); }),
            // Class mod skills: "<graph>,<node>" and "+N", to tell apart mods with the same stats
            skills: list(d.primary_stat_entries).map(function (e) { return [String(e.loc_text || ""), String(e.value || "")]; }),
        };
    }

    function rows(card) {
        return card.querySelectorAll(".item_card_secondary_stats_enhance");
    }

    function rowText(row) {
        return row && row.querySelector(".item_card_secondary_stat_enhance_text");
    }

    // endregion
    // region Our layer on the card

    function layer(card) {
        var host = card.querySelector(".item_card");
        if (!host) return null;
        var box = host.querySelector(".bl4rr_layer");
        if (!box) {
            box = element("div", "bl4rr_layer");
            host.appendChild(box);
        }
        return box;
    }

    // The item cards mod may scale the card: sizes inside it are screen sizes divided by this
    function cardScale(card) {
        var host = card.querySelector(".item_card");
        var scale = host && host.offsetWidth > 0 ? rect(host).width / host.offsetWidth : 1;
        return scale > 0 ? scale : 1;
    }

    // Places things in our layer at screen rectangles
    function placer(card) {
        var box = layer(card);
        if (!box) return null;
        var origin = rect(box);
        var scale = cardScale(card);
        return {
            box: box,
            place: function (el, r, pad) {
                pad = pad || 0;
                el.style.left = (r.left - origin.left) / scale - pad + "px";
                el.style.top = (r.top - origin.top) / scale - pad + "px";
                el.style.width = r.width / scale + 2 * pad + "px";
                el.style.height = r.height / scale + 2 * pad + "px";
            },
            scale: scale,
            origin: origin,
        };
    }

    function clearLayer(card) {
        var box = card && card.querySelector(".bl4rr_layer");
        if (box) box.parentNode.removeChild(box);
        if (card) {
            var all = rows(card);
            for (var i = 0; i < all.length; i++) {
                var p = rowText(all[i]);
                if (p && p.bl4rrHidden) {
                    p.style.visibility = "";
                    p.bl4rrHidden = false;
                }
            }
        }
    }

    function hideRow(card, index, hide) {
        var p = rowText(rows(card)[index]);
        if (!p) return;
        if (hide && !p.bl4rrHidden) {
            p.style.visibility = "hidden";
            p.bl4rrHidden = true;
        } else if (!hide && p.bl4rrHidden) {
            p.style.visibility = "";
            p.bl4rrHidden = false;
        }
    }

    // endregion
    // region Recognising the item
    //
    // Python lists every rerollable item in the backpack (state.items) with what its card shows:
    // kind, level, rarity, equipped, its modifiers' names, class mod skills. We match the card
    // against that list ourselves (the same rules as card.py's match()), so the prompts go on in
    // the same moment the game fills the card, the way the Detailed Item Cards mod draws.

    var TAGS = /\[\/?\w+\]|<[^>]*>/g;
    var NUMBERS = /[-+]?\d[\d,.]*\s*%?/g;
    var TRAILER = /\b(?:is|are) (?:increased|decreased|reduced) by\b/g;

    function plain(text) {
        var t = String(text || "").replace(TAGS, " ").replace(NUMBERS, " ").toLowerCase().replace(TRAILER, " ");
        return t.replace(/[^a-z' ]/g, " ").replace(/\s+/g, " ").trim();
    }

    function firstNumber(text) {
        var m = /\d+/.exec(String(text || ""));
        return m ? parseInt(m[0], 10) : null;
    }

    // Which card row shows each of the item's lines, or null if they don't all appear
    function rowsFor(names, rowTexts) {
        var taken = {};
        var lineTaken = {};
        var order = names.map(function (_, i) { return i; });
        order.sort(function (a, b) { return names[b].length - names[a].length || a - b; });
        [true, false].forEach(function (exact) {
            order.forEach(function (i) {
                if (lineTaken[i] !== undefined || !names[i]) return;
                for (var r = 0; r < rowTexts.length; r++) {
                    if (taken[r] !== undefined) continue;
                    if (exact ? rowTexts[r] === names[i] : rowTexts[r].indexOf(names[i]) >= 0) {
                        taken[r] = i;
                        lineTaken[i] = r;
                        return;
                    }
                }
            });
        });
        var out = [];
        for (var i = 0; i < names.length; i++) {
            if (lineTaken[i] === undefined) return null;
            out.push(lineTaken[i]);
        }
        return out;
    }

    function sameSkills(a, b) {
        var ka = Object.keys(a), kb = Object.keys(b);
        if (ka.length !== kb.length) return false;
        for (var i = 0; i < ka.length; i++) if (a[ka[i]] !== b[ka[i]]) return false;
        return true;
    }

    // {item, rows} for the card, {ambiguous: true}, or null (not one of ours)
    function matchCard(info) {
        if (!info || !state || !state.items) return null;
        var rowTexts = info.secondary.map(plain);
        var level = firstNumber(info.level);
        var rarity = info.rarity.toLowerCase();
        var equipped = info.banner === "equipped";
        var skills = {}, hasSkills = false;
        info.skills.forEach(function (entry) {
            if (!entry[0]) return;
            var node = entry[0].toLowerCase();
            skills[node] = (skills[node] || 0) + (firstNumber(entry[1]) || 0);
            hasSkills = true;
        });
        var found = [];
        state.items.forEach(function (item) {
            if (item.kind !== info.type || item.level !== level || item.equipped !== equipped) return;
            if (item.rarity && rarity && item.rarity !== rarity) return;
            if (hasSkills && Object.keys(item.skills).length && !sameSkills(item.skills, skills)) return;
            var rows = rowsFor(item.plain, rowTexts);
            if (rows) found.push({ item: item, rows: rows });
        });
        if (!found.length) return null;
        for (var i = 1; i < found.length; i++) if (found[i].item.serial !== found[0].item.serial) return { ambiguous: true };
        return found[0];
    }

    // endregion
    // region Prompts and highlights
    //
    // Everything here goes inside the card's own layout (prompts after the game's, a class on the
    // game's modifier rows, a box after the modifiers), never on top of it, so nothing has to be
    // positioned and nothing can show up in the wrong place while the card moves.

    // "[T] Reroll All (5 Eridium)" next to the game's own prompts. One entry per kind, added after
    // the game's list and only ever updated, never moved. Runs on every poll, so it only writes
    // what changed, and measures the game's glyph only when the prompt (re)appears.
    function setStyle(el, name, value) {
        if (el.style[name] !== value) el.style[name] = value;
    }
    function promptEntry(card, className, key, text, dim) {
        var wrapper = card.querySelector(".item_card_glyph_wrapper");
        if (!wrapper) return;
        var prompt = wrapper.querySelector("." + className);
        if (!text) {
            if (prompt && prompt.bl4rrShown !== false) {
                prompt.style.display = "none";
                prompt.bl4rrShown = false;
            }
            return;
        }
        if (!prompt) {
            prompt = element("div", "tooltip_button_glyph_container bl4rr_prompt " + className);
            var inner = element("div", "tooltip_button_glyph_container");
            inner.appendChild(element("div", "bl4rr_glyph_key"));
            inner.appendChild(element("div", "tooltip_button_glyph_text"));
            prompt.appendChild(inner);
            wrapper.appendChild(prompt);
        }
        var icon = prompt.querySelector(".bl4rr_glyph_key");
        // clearCard() hides prompts behind our back: trust the element, not just our mark
        var appearing = prompt.bl4rrShown !== true || prompt.style.display === "none";
        if (appearing) {
            prompt.style.display = "";
            prompt.bl4rrShown = true;
        }
        if (prompt.bl4rrDim !== !!dim) {
            prompt.bl4rrDim = !!dim;
            prompt.classList.toggle("bl4rr_dim", !!dim);
        }
        setStyle(icon, "display", key ? "" : "none");
        if (appearing || !prompt.bl4rrSized) {
            var native = wrapper.querySelector("gbx-glyph");
            var size = native ? native.getBoundingClientRect() : null;
            if (size && size.height > 0) {
                var s = cardScale(card);
                setStyle(icon, "width", size.width / s + "px");
                setStyle(icon, "height", size.height / s + "px");
                prompt.bl4rrSized = true;
            }
        }
        if (key && prompt.bl4rrKey !== key) {
            prompt.bl4rrKey = key;
            icon.style.backgroundImage = "url(" + keyImage(key) + ")";
        }
        var label = prompt.querySelector(".tooltip_button_glyph_text");
        if (label.textContent !== text) label.textContent = text;
    }

    function keyName(key) {
        var digits = ["Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"];
        return digits.indexOf(key) >= 0 ? String(digits.indexOf(key)) : String(key || "");
    }

    // The lines G steps through (not the legendary's own), top to bottom
    function pickOrder(m) {
        var free = [];
        for (var i = 0; i < m.item.locked.length; i++) if (!m.item.locked[i]) free.push(i);
        free.sort(function (a, b) { return m.rows[a] - m.rows[b]; });
        return free;
    }

    // The picked line and "reroll all?" for this card's item, from Python's state
    function choice(m) {
        var handle = m && m.item ? m.item.handle : null;
        var pick = state && state.pick && state.pick.handle === handle ? state.pick.line : null;
        return { pick: pick === undefined ? null : pick, confirm: !!(state && handle !== null && state.confirm === handle) };
    }

    function renderPrompts(card, info, m) {
        var keys = (state && state.keys) || {};
        var costs = (state && state.costs) || {};
        var reroll = null, pick = null, dim = false, keyed = false;
        if (info && state && state.blocked) {
            reroll = "Reroll: " + state.blocked;
            dim = true;
        } else if (m && m.ambiguous) {
            reroll = "Reroll: use the mod menu for this one";
            dim = true;
        } else if (m) {
            var c = choice(m);
            var free = pickOrder(m);
            keyed = true;
            if (c.confirm) {
                reroll = "Yes, Reroll All (" + costs.all + " Eridium)";
                pick = "Pick One Instead";
            } else if (c.pick === null) {
                reroll = "Reroll All (" + costs.all + " Eridium)";
                pick = free.length > 1 || (free.length === 1 && m.item.names.length > 1) ? "Pick One" : null;
            } else {
                reroll = "Reroll " + m.item.names[c.pick] + " (" + costs.one + " Eridium)";
                pick = free[free.length - 1] === c.pick ? "Back to All" : "Next";
            }
            dim = state.eridium !== null && state.eridium < (c.pick === null ? costs.all : costs.one);
        }
        promptEntry(card, "bl4rr_prompt_reroll", keyed ? keys.reroll : null, reroll, dim);
        promptEntry(card, "bl4rr_prompt_pick", keyed && pick ? keys.pick : null, pick, false);
    }

    // The picked modifier (purple) or, while asking "reroll all?", every one that would change
    // (gold): a class on the game's own rows
    function renderRows(card, m) {
        var all = rows(card);
        var c = m && m.item && !(anim && anim.card === card) ? choice(m) : { pick: null, confirm: false };
        var picked = c.pick !== null ? m.rows[c.pick] : -1;
        var asked = {};
        if (c.confirm) pickOrder(m).forEach(function (i) { asked[m.rows[i]] = true; });
        for (var r = 0; r < all.length; r++) {
            all[r].classList.toggle("bl4rr_row_pick", r === picked);
            all[r].classList.toggle("bl4rr_row_ask", !!asked[r]);
        }
    }

    // "Reroll all modifiers?" under the modifiers while it waits for the second press
    function renderAsk(card, m) {
        var host = card.querySelector(".item_card_enhancement");
        var box = card.querySelector(".bl4rr_ask");
        var c = m && m.item ? choice(m) : { confirm: false };
        if (!c.confirm || !host) {
            if (box) box.style.display = "none";
            return;
        }
        if (!box || box.parentNode !== host) {
            if (box) box.parentNode.removeChild(box);
            box = element("div", "bl4rr_ask");
            box.appendChild(element("p", "bl4rr_ask_title", "Reroll all modifiers?"));
            box.appendChild(element("p", "bl4rr_ask_text"));
            host.appendChild(box);
        }
        var keys = (state && state.keys) || {};
        var costs = (state && state.costs) || {};
        box.style.display = "";
        box.querySelector(".bl4rr_ask_text").textContent =
            "Press " + keyName(keys.reroll) + " again to spend " + costs.all + " Eridium, or " + keyName(keys.pick) + " to pick one instead.";
    }

    function render(card) {
        if (!card) return;
        var info = cardInfo(card);
        var m = matchCard(info);
        try {
            renderPrompts(card, info, m);
            renderRows(card, m);
            renderAsk(card, m);
        } catch (err) {
            log("draw failed: " + err);
        }
    }

    // A card that closed or moved on to another item keeps our additions otherwise
    function clearCard(card) {
        if (!card) return;
        var ours = card.querySelectorAll(".bl4rr_prompt, .bl4rr_ask");
        for (var i = 0; i < ours.length; i++) ours[i].style.display = "none";
        var all = rows(card);
        for (var r = 0; r < all.length; r++) {
            all[r].classList.remove("bl4rr_row_pick");
            all[r].classList.remove("bl4rr_row_ask");
        }
    }

    // endregion
    // region Slot machine

    function easeOut(t) {
        // Fast, then a long slow-down, with a small bounce past the line at the very end
        var s = 1.2;
        t -= 1;
        return t * t * ((s + 1) * t + s) + 1;
    }

    function textStyle(from, to) {
        if (!from) return;
        var cs = getComputedStyle(from);
        to.style.fontSize = cs.fontSize;
        to.style.fontFamily = cs.fontFamily;
        to.style.letterSpacing = cs.letterSpacing;
        to.style.textTransform = cs.textTransform;
    }

    function startRoll(card, roll) {
        stopAnim(true);
        var at = placer(card);
        if (!at) return;
        var now = Date.now();
        var reels = [];
        var order = 0;
        for (var i = 0; i < roll.lines.length; i++) {
            var line = roll.lines[i];
            if (!line.spins) continue;
            var duration = roll.kind === "one" ? ONE_SPIN_MS : SPIN_MS + order * STAGGER_MS;
            var steps = SPIN_STEPS + order * STAGGER_STEPS;
            var names = [line.old];
            var pool = line.reel.length ? line.reel : [line.old];
            for (var k = 0; names.length < steps; k++) names.push(pool[k % pool.length]);
            names.push(line["new"]);
            var win = element("div", "bl4rr_reel");
            var strip = element("div", "bl4rr_strip");
            for (var n = 0; n < names.length; n++) strip.appendChild(element("p", "bl4rr_reel_name", names[n]));
            win.appendChild(strip);
            at.box.appendChild(win);
            textStyle(rowText(rows(card)[line.row]), win);
            reels.push({ line: line, win: win, strip: strip, steps: names.length - 1, start: now, duration: duration, lastStep: -1, stopped: false });
            order++;
        }
        var banner = element("div", "bl4rr_banner");
        // Drawn with CSS: the game's font has no bullet glyphs
        function bulbs() {
            var row = element("div", "bl4rr_bulbs");
            for (var b = 0; b < 3; b++) row.appendChild(element("div", "bl4rr_bulb"));
            return row;
        }
        var bulbsL = bulbs();
        var title = element("p", "bl4rr_banner_title", roll.kind === "one" ? "REROLLING" : "REROLL");
        var bulbsR = bulbs();
        banner.appendChild(bulbsL);
        banner.appendChild(title);
        banner.appendChild(bulbsR);
        var sub = element("p", "bl4rr_banner_sub", "-" + roll.cost + " Eridium");
        at.box.appendChild(banner);
        at.box.appendChild(sub);
        anim = { card: card, roll: roll, reels: reels, banner: banner, title: title, sub: sub, started: now, finished: 0, lastTick: 0, revealed: false };
        playSound(SOUND_PAY);
        step();
    }

    function showError(card, text) {
        stopAnim(true);
        var at = placer(card);
        if (!at) return;
        var banner = element("div", "bl4rr_banner bl4rr_banner_error");
        banner.appendChild(element("p", "bl4rr_banner_title", text));
        at.box.appendChild(banner);
        anim = { card: card, error: true, banner: banner, started: Date.now(), reels: [] };
        playSound(SOUND_ERROR);
        step();
    }

    function stopAnim(clear) {
        if (!anim) return;
        if (clear) {
            var parts = [anim.banner, anim.sub];
            for (var i = 0; i < anim.reels.length; i++) parts.push(anim.reels[i].win);
            for (var j = 0; j < parts.length; j++) if (parts[j] && parts[j].parentNode) parts[j].parentNode.removeChild(parts[j]);
            for (var k = 0; k < anim.reels.length; k++) hideRow(anim.card, anim.reels[k].line.row, false);
        }
        anim = null;
    }

    function placeBanner(card, at) {
        var section = card.querySelector(".item_card_secondary_section") || card.querySelector(".item_card_enhancement");
        var r = rect(section);
        if (!r || r.height <= 0) return;
        var w = r.width / at.scale;
        anim.banner.style.left = (r.left - at.origin.left) / at.scale + "px";
        anim.banner.style.width = w + "px";
        anim.banner.style.top = (r.top - at.origin.top) / at.scale + "px";
        if (anim.sub) {
            anim.sub.style.left = (r.left - at.origin.left) / at.scale + "px";
            anim.sub.style.width = w + "px";
            anim.sub.style.top = (r.bottom - at.origin.top) / at.scale + "px";
        }
    }

    function finishText(roll) {
        var spun = roll.lines.filter(function (l) { return l.spins; });
        var changed = spun.filter(function (l) { return l.changed; });
        if (roll.kind === "one") return "REPLACED!";
        if (!changed.length) return "SAME AGAIN!";
        if (changed.length === spun.length && spun.length > 1) return "JACKPOT!";
        return "NEW MODIFIERS!";
    }

    // Each frame: move the reels, stop them in turn, then show the result
    function step() {
        if (!alive() || !anim) return;
        var card = anim.card;
        var tooltip = card.querySelector(".item_card_tooltip");
        if (!tooltip || tooltip.getBoundingClientRect().height <= 0) {
            // The card went away: nothing to show it on
            clearLayer(card);
            anim = null;
            return;
        }
        var at = placer(card);
        if (!at) return;
        var now = Date.now();
        placeBanner(card, at);

        if (anim.error) {
            if (now - anim.started > ERROR_MS) {
                stopAnim(true);
                return;
            }
            later(step, FRAME_MS);
            return;
        }

        // Bulbs chase while the reels spin
        var lit = Math.floor((now - anim.started) / 110) % 2 === 0;
        anim.banner.classList.toggle("bl4rr_lit", lit);

        var all = rows(card);
        var spinning = 0;
        var ticked = false;
        for (var i = 0; i < anim.reels.length; i++) {
            var reel = anim.reels[i];
            var row = all[reel.line.row];
            var p = rowText(row);
            if (p) {
                at.place(reel.win, rect(p), 0);
                hideRow(card, reel.line.row, true);
            }
            var t = Math.min(1, (now - reel.start) / reel.duration);
            var pos = reel.steps * easeOut(t);
            var h = reel.win.offsetHeight || 20;
            if (reel.h !== h) {
                // Every name exactly one window high, so whole steps line up with the window
                reel.h = h;
                for (var c = 0; c < reel.strip.children.length; c++) {
                    reel.strip.children[c].style.height = h + "px";
                    reel.strip.children[c].style.lineHeight = h + "px";
                }
            }
            reel.strip.style.transform = "translateY(" + -pos * h + "px)";
            var whole = Math.floor(pos + 0.5);
            if (whole !== reel.lastStep && t < 1) {
                reel.lastStep = whole;
                ticked = true;
            }
            if (t >= 1 && !reel.stopped) {
                reel.stopped = true;
                reel.win.classList.add(reel.line.changed ? "bl4rr_won" : "bl4rr_same");
                playSound(SOUND_STOP);
            }
            if (!reel.stopped) spinning++;
        }
        if (ticked && now - anim.lastTick > TICK_SOUND_MS) {
            anim.lastTick = now;
            playSound(SOUND_TICK);
        }

        if (spinning === 0 && !anim.finished) {
            anim.finished = now;
            anim.title.textContent = finishText(anim.roll);
            anim.banner.classList.add("bl4rr_done");
            playSound(SOUND_DONE);
        }
        if (anim.finished) {
            anim.banner.classList.add("bl4rr_lit");
            // Python makes the item once the reels have landed; that replaces the card's item
            if (!anim.landedSent && now - anim.finished > LANDED_MS) {
                anim.landedSent = true;
                ext.send(KEY, { do: "landed", id: anim.roll.id });
            }
            if (now - anim.finished > BANNER_MS) {
                stopAnim(true);
                return;
            }
        }
        later(step, FRAME_MS);
    }

    // endregion
    // region Talking to Python

    function sendHover(fp, info) {
        var now = Date.now();
        if (fp === shown.sentFp && (fp === null || now - shown.sentAt < HOVER_REPEAT_MS)) return;
        shown.sentFp = fp;
        shown.sentAt = now;
        ext.send(KEY, { do: "hover", fp: fp, card: info });
    }

    function onState(snap) {
        state = snap && snap.state ? snap.state : null;
        if (!state) return;
        var roll = state.roll || { id: 0 };
        // A fresh page: don't replay a roll from before it loaded
        if (rr.lastRoll === undefined) rr.lastRoll = roll.id;
        if (rr.lastApplied === undefined) rr.lastApplied = roll.applied ? roll.id : -1;
        if (roll.applied && rr.lastApplied !== roll.id) {
            rr.lastApplied = roll.id;
            applied(roll);
        }
        if (roll.id !== rr.lastRoll) {
            rr.lastRoll = roll.id;
            var card = shown.card;
            if (card && (roll.fp === shown.fp || roll.fp === shown.prevFp)) {
                if (roll.error) showError(card, roll.error);
                else if (roll.lines) startRoll(card, roll);
            }
        }
    }

    // The item was changed (or not): say so on the card if it's still up, and in a notice (the
    // card may have closed or moved on by then)
    function applied(roll) {
        var result = roll.applied;
        if (anim && anim.roll && anim.roll.id === roll.id && anim.sub) {
            anim.sub.textContent = result.ok ? "-" + roll.cost + " Eridium, " + result.left + " left" : result.error;
        }
        if (result.ok) {
            toast("Reroll", result.message, result.equipped ? "Equip it from your backpack to use the new modifiers" : "", false);
        } else {
            toast("Reroll failed", result.error, "", true);
            playSound(SOUND_ERROR);
        }
    }

    function toast(title, text, note, error) {
        var box = document.getElementById("bl4rr_toast");
        if (!box) {
            box = element("div", "bl4rr_toast bl4rr_toast_hidden");
            box.id = "bl4rr_toast";
            box.appendChild(element("p", "bl4rr_toast_title"));
            box.appendChild(element("p", "bl4rr_toast_text"));
            box.appendChild(element("p", "bl4rr_toast_note"));
            document.body.appendChild(box);
        }
        box.querySelector(".bl4rr_toast_title").textContent = title;
        box.querySelector(".bl4rr_toast_text").textContent = text;
        box.querySelector(".bl4rr_toast_note").textContent = note;
        box.classList.toggle("bl4rr_toast_error", !!error);
        box.classList.remove("bl4rr_toast_hidden");
        clearTimeout(rr.toastTimer);
        rr.toastTimer = setTimeout(function () { box.classList.add("bl4rr_toast_hidden"); }, TOAST_MS);
    }

    // A class mod / enhancement card is up, or the reels spin: watch every frame, poll fast
    function busy() {
        return !!anim || !!(shown.card && shown.fp);
    }

    // Which card shows, checked every frame while busy() (a card that closes or changes item is
    // cleaned up at once) and every few frames otherwise (onCard draws a card the game fills at
    // once; this catches one that wasn't laid out yet then)
    function watch() {
        if (!alive()) return;
        watchOnce();
        later(watch, busy() ? FRAME_MS : WATCH_IDLE_MS);
    }

    function watchOnce() {
        try {
            var card = currentCard();
            var info = cardInfo(card);
            var fp = info ? JSON.stringify(info) : null;
            if (card !== shown.card) {
                if (shown.card) {
                    clearCard(shown.card);
                    clearLayer(shown.card);
                }
                if (anim && anim.card !== card) stopAnim(false);
                shown.card = card;
                shown.prevFp = null;
            }
            if (fp !== shown.fp) {
                shown.prevFp = shown.fp;
                shown.fp = fp;
                // The card shows something else now (the swap changed its item)
                if (anim && anim.landedSent) stopAnim(true);
                if (card) {
                    card.bl4rrFp = fp;
                    if (info) layer(card);  // laid out ahead of a roll
                    render(card);
                }
                sendHover(fp, info);
                if (info) pollSoon();
            }
        } catch (err) {
            log("watch failed: " + err);
        }
    }

    // The game filled a card: draw on it in the same moment
    rr.onCard = function (card) {
        if (!alive()) return;
        if (card !== currentCard()) {
            // Not showing yet (no size): look again over the next frames rather than waiting out
            // the idle watch
            later(watchOnce, FRAME_MS);
            later(watchOnce, 2 * FRAME_MS);
            return;
        }
        var info = cardInfo(card);
        var fp = info ? JSON.stringify(info) : null;
        card.bl4rrFp = fp;
        render(card);
        // The game's modifier rows may only be built after this event: once more next frame
        later(function () {
            render(card);
        }, 0);
        if (fp !== shown.fp) {
            shown.card = card;
            shown.prevFp = shown.fp;
            shown.fp = fp;
            sendHover(fp, info);
            if (info) pollSoon();
        }
    };

    var hookTries = 0;
    function hookCards() {
        if (!alive()) return;
        if (typeof Widget_ItemCard === "undefined") {
            if (hookTries++ < 600) later(hookCards, 200);
            return;
        }
        var proto = Widget_ItemCard.prototype;
        if (!proto.bl4rrHooked) {
            proto.bl4rrHooked = true;
            var original = proto.Event_DataUpdated;
            proto.Event_DataUpdated = function () {
                original.apply(this, arguments);
                var page = window.bl4ModMenu && window.bl4ModMenu.reroll;
                if (page && page.onCard) {
                    try {
                        page.onCard(this);
                    } catch (err) {}
                }
            };
        }
    }

    // One poll at a time: the timer waiting for the next one, or null while one is out
    var pollTimer = null;
    function poll() {
        pollTimer = null;
        var card = shown.card;
        sendHover(shown.fp, cardInfo(card));
        ext.get(KEY, false, function (snap) {
            if (!alive()) return;
            onState(snap);
            if (shown.card) render(shown.card);
            pollTimer = later(poll, busy() ? POLL_FAST_MS : POLL_IDLE_MS);
        });
    }

    // A class mod / enhancement card just showed: fetch now instead of after the idle wait
    function pollSoon() {
        if (pollTimer === null) return;  // one is out already
        cancel(pollTimer);
        pollTimer = later(poll, 0);
    }

    // endregion
    // region Styles

    function loadCss() {
        ns.get(CSS_URL, function (css) {
            if (!alive()) return;
            if (css && css !== rr.css) {
                rr.css = css;
                var style = document.getElementById("bl4rr_css");
                if (!style) {
                    style = document.createElement("style");
                    style.id = "bl4rr_css";
                    document.head.appendChild(style);
                }
                style.textContent = css;
            }
            later(loadCss, CSS_MS);
        });
    }

    // endregion

    ext.teardowns[KEY] = function () {
        rr.gen = 0;
        rr.onCard = null;
        timers.forEach(clearTimeout);
        stopAnim(true);
        var box = document.getElementById("bl4rr_toast");
        if (box) box.parentNode.removeChild(box);
        var cards = document.querySelectorAll("widget-item-card");
        for (var i = 0; i < cards.length; i++) {
            clearCard(cards[i]);
            clearLayer(cards[i]);
            var ours = cards[i].querySelectorAll(".bl4rr_prompt, .bl4rr_ask");
            for (var j = 0; j < ours.length; j++) ours[j].parentNode.removeChild(ours[j]);
        }
    };

    loadCss();
    hookCards();
    watch();
    poll();
    log("ready");
})();
