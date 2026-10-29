"use strict";

// BL4 Build Presets: five build squares in the inventory, under the Reward Center button.
//
//   empty square   click: save your current build into it
//   saved square   click: load it        (hover: what's in it; the bar under the row: Save over
//                  it / Name / Clear; or right-click it to save over it, after a prompt)
//
// The BL4 UI Link (uilink/ui/hub.js, as `new Function("bl4linkNs", js)(ns)`) runs this file in the
// game's menu page, or the BL4 Mod Menu's extensions.js does until the link's pak has loaded; either
// runs it again when it changes, so it tears its last copy down first. Python (bl4_builds/page.py) does the
// work: we poll GET /ext?k=builds for what each square holds and post clicks back. When saving we
// also send each equipped item's name: the game names items natively, so we focus each equip
// slot in turn (what a hover does, with the card kept hidden) and read the name off its item card.
// Naming a build: Python reads the typing (no key events reach this page) and sends the text in
// its state; we show it with a caret. A square's strip shows its skill points per tree, in the
// skill screen's tree colors.
//
// The squares are built from the Reward Center button's own classes (menu_equip.css), so they
// look and hover like it, and they play the inventory's own UI sounds.

(function () {
    var ns = (typeof bl4linkNs !== "undefined" && bl4linkNs) || window.bl4ModMenu;
    if (!ns || !ns.ext) return;
    var ext = ns.ext;
    var KEY = "builds";
    var BASE = "coui://uiresources/bl4builds/";
    var CSS_URL = BASE + "builds.css";
    var SLOTS = 5;
    var POLL_SHOWN_MS = 250;
    // Right after the inventory opens or a click, poll fast: Python's first answer is the state
    // from when the menu was last open (or from before the click), the fresh one follows a frame later
    var POLL_FAST_MS = 50;
    var FAST_FOR_MS = 1200;
    var fastUntil = 0;
    // While the inventory isn't showing we only look for it (a DOM check), often, so the squares
    // are there the moment it opens
    var POLL_HIDDEN_MS = 150;
    var CSS_MS = 2000;
    var CONFIRM_MS = 3000;
    var TOAST_MS = 5000;
    var TOAST_ERROR_MS = 8000;
    var POLL_NAMING_MS = 80;
    // Waiting for the item card to show a focused slot's item
    var SCAN_SLOT_MS = 600;
    var SCAN_STEP_MS = 30;
    // Space between the Reward Center button and our row, in rem
    var GAP_REM = 2.2;
    // How often the row's place is measured again once the menu has opened
    var PLACE_MS = 1000;

    if (ext.teardowns[KEY]) {
        try {
            ext.teardowns[KEY]();
        } catch (err) {}
    }
    // Survives reloads of this file; each copy takes a new, ever higher number
    var bp = ns.builds || (ns.builds = {});
    bp.counter = Math.max(bp.counter || 0, bp.gen || 0) + 1;
    var gen = bp.counter;
    bp.gen = gen;

    var timers = [];
    // bp survives reloads of this file; a timer handle left in it by an older copy would block ours
    clearTimeout(bp.chipsTimer);
    bp.chipsTimer = null;
    function alive() {
        return bp.gen === gen;
    }
    function later(fn, ms) {
        var id = setTimeout(function () {
            // Fired: drop its id, or the list grows by one per poll for as long as the page lives
            var at = timers.indexOf(id);
            if (at >= 0) timers.splice(at, 1);
            if (alive()) fn();
        }, ms);
        timers.push(id);
    }
    function log(text) {
        if (ns.log) ns.log("[builds] " + text);
    }

    // The last state Python sent, kept across reloads of this file and menu openings: the squares
    // draw from it at once, and the fresh state follows a moment later
    var state = bp.lastState || null;
    var seenToast = null;  // the toast id when we started: older toasts aren't shown
    var seenPrompt = null;  // same for right-click prompts
    var prompting = 0;  // the build a save-over prompt is asking about
    var hovered = 0;
    // The square the action bar (Save / Name / Clear, under the row) works on: the last one
    // hovered. It stays while the mouse goes down to the bar, and a moment after it leaves both
    var chipsFor = 0;
    var CHIPS_GRACE_MS = 350;
    // The mouse is on the action bar: the panel stays for its square
    var onBar = false;
    var confirm = { n: 0, what: "", until: 0 };
    var busy = 0;

    // region Helpers

    function element(tag, className, text) {
        var el = document.createElement(tag);
        el.className = className;
        if (text !== undefined) el.textContent = text;
        return el;
    }

    // Setting the same text still replaces the text node (and lays the page out again)
    function setText(el, text) {
        if (el.textContent !== text) el.textContent = text;
    }

    function rem() {
        return parseFloat(getComputedStyle(document.documentElement).fontSize) || 10;
    }

    // Coherent arrays: iterate by index
    function list(arr) {
        var out = [];
        if (!arr) return out;
        for (var i = 0; i < arr.length; i++) out.push(arr[i]);
        return out;
    }

    function equipMenu() {
        return document.querySelector("widget-menu-equip");
    }

    function shown(menu) {
        if (!menu || menu.Active === false) return false;
        var r = menu.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    }

    function sound(node, id) {
        var menu = equipMenu();
        if (menu && typeof menu.PlaySoundAtNode === "function") {
            try {
                menu.PlaySoundAtNode(node, id);
            } catch (err) {}
        }
    }

    function words(text) {
        return String(text || "")
            .replace(/[_-]+/g, " ")
            .replace(/([a-z])([A-Z])/g, "$1 $2")
            .replace(/\b\w/g, function (c) { return c.toUpperCase(); })
            .trim();
    }

    // endregion
    // region What the inventory shows for each slot

    // The data model's internal names that don't read like the game's
    var MAKERS = { Borg: "Ripper" };
    var KINDS = [
        [/^Smg$/, "SMG"],
        [/^Ar$|^Assault$/, "Assault Rifle"],
        [/^Sniper$/, "Sniper Rifle"],
        [/^Heavy$/, "Heavy Weapon"],
        [/^Repairkit$|^Repair Kit$/, "Repkit"],
        [/^Classmod\b.*/, "Class Mod"],
        [/^Class Mod\b.*/, "Class Mod"]
    ];

    function kindName(text) {
        for (var i = 0; i < KINDS.length; i++) if (KINDS[i][0].test(text)) return KINDS[i][1];
        return text;
    }

    // "Legendary Jakobs Shotgun" for each equip slot (keyed by the slot's index), from the equip
    // menu's own data model
    function slotLabels() {
        var out = {};
        var menu = equipMenu();
        var data = menu && menu.DataModel;
        if (!data || !data.equipmentslots) return out;
        var slots = list(data.equipmentslots);
        for (var i = 0; i < slots.length; i++) {
            var s = slots[i];
            if (!s || s.isempty) continue;
            var rarity = words(s.rarityident);
            var maker = words(s.manufacturerident);
            maker = MAKERS[maker] || maker;
            var kind = kindName(words(s.itemsubtype || s.itemtype));
            out[String(i)] = {
                label: [rarity, maker, kind].filter(function (x) { return x; }).join(" "),
                desc: [maker, kind].filter(function (x) { return x; }).join(" "),
                rarity: String(s.rarityident || "").toLowerCase(),
                raw: { rarity: s.rarityident, maker: s.manufacturerident, type: s.itemtype, subtype: s.itemsubtype }
            };
        }
        return out;
    }

    function cleanName(text) {
        return String(text || "").replace(/\[[^\]]*\]|<[^>]*>/g, "").replace(/\s+/g, " ").trim();
    }

    // The item card that shows the equip slot we focused: not a comparison card, and of the
    // slot's item type
    // The card names types its own way: weapon_sg for a shotgun slot ("weapon", "shotgun"),
    // energy_shield for "shield", heavy_weapon_gadget for "gadget"
    var WEAPON_CODES = { shotgun: "sg", sniper: "sr", pistol: "ps", smg: "sm", assault: "ar", assaultrifle: "ar", ar: "ar" };

    function typeMatch(slot, d) {
        var have = String(slot.itemtype || "").toLowerCase();
        var card = String(d.itemtype || "").toLowerCase();
        if (!have || !card) return false;
        if (have === card) return true;
        if (have === "weapon") {
            var code = WEAPON_CODES[String(slot.itemsubtype || "").toLowerCase()];
            return code ? card === "weapon_" + code : card.indexOf("weapon_") === 0;
        }
        return card.indexOf(have) >= 0;
    }

    function cards() {
        return document.querySelectorAll("widget-item-card");
    }

    // The card that shows the slot we focused: of the slot's type, and changed since `before`
    // (other cards keep showing whatever they showed, e.g. the last backpack item hovered).
    // With `any`, an unchanged card will do too (two identical items in a row), as long as it
    // changed at some point during the scan.
    function cardFor(slot, before, start, any) {
        var all = cards();
        for (var i = 0; i < all.length; i++) {
            var d = all[i].DataModel;
            if (!d || !d.name || d.comparemode || !typeMatch(slot, d)) continue;
            var print = cardPrint(d);
            if (print !== before[i] || (any && print !== start[i])) return d;
        }
        return null;
    }

    function prints() {
        var out = [];
        var all = cards();
        for (var i = 0; i < all.length; i++) out.push(all[i].DataModel ? cardPrint(all[i].DataModel) : null);
        return out;
    }

    function cardPrint(d) {
        return [d.name, d.level, d.rarityident, d.itemtype, d.manufacturerident].join("|");
    }

    // Each equipped item's name, as its item card shows it: focus each slot (the game fills the
    // card), read the name, move on. The cards stay hidden meanwhile. done({slot: name})
    function scanNames(done) {
        var menu = equipMenu();
        var data = menu && menu.DataModel;
        if (!menu || !data || !data.equipmentslots || typeof menu.TriggerWidgetEvent !== "function") {
            done({});
            return;
        }
        var slots = list(data.equipmentslots);
        var todo = [];
        for (var i = 0; i < slots.length; i++) if (slots[i] && !slots[i].isempty) todo.push(i);
        var names = {};
        var start = prints();
        document.body.classList.add("bl4b_scanning");
        function finish() {
            try {
                menu.TriggerWidgetEvent("OnEquipmentSlotFocused", -1);
                document.dispatchEvent(new CustomEvent("Event_ItemTooltipUnfocused"));
            } catch (err) {}
            document.body.classList.remove("bl4b_scanning");
            done(names);
        }
        var retried = false;
        function next(k) {
            if (k >= todo.length && !retried && alive()) {
                // A slot whose card already showed its item when we started never changes: try
                // the missed ones again, now that the card shows another item
                retried = true;
                todo = todo.filter(function (index) { return !names[String(index)]; });
                k = 0;
            }
            if (k >= todo.length || !alive()) {
                finish();
                return;
            }
            var index = todo[k];
            var started = Date.now();
            var before = prints();
            try {
                menu.TriggerWidgetEvent("OnEquipmentSlotFocused", index);
            } catch (err) {
                finish();
                return;
            }
            (function wait() {
                var timedOut = Date.now() - started > SCAN_SLOT_MS;
                var card = cardFor(slots[index], before, start, timedOut);
                if (card) names[String(index)] = cleanName(card.name);
                if (card || timedOut) next(k + 1);
                else setTimeout(wait, SCAN_STEP_MS);
            })();
        }
        next(0);
    }

    // endregion
    // region Drawing

    function row() {
        return document.getElementById("bl4b_row");
    }

    function build(menu) {
        var old = row();
        if (old && old.bl4bMenu === menu && menu.contains(old)) return old;
        if (old && old.parentNode) old.parentNode.removeChild(old);
        var cta = menu.querySelector(".mrc_cta_btn_wrapper");
        var parent = cta ? cta.parentNode : menu;
        var box = element("div", "bl4b_row");
        box.id = "bl4b_row";
        box.bl4bMenu = menu;
        var squares = element("div", "bl4b_squares");
        for (var n = 1; n <= SLOTS; n++) squares.appendChild(square(n));
        box.appendChild(squares);
        box.appendChild(actionBar());
        box.appendChild(promptBox());
        var panel = element("div", "bl4b_panel bl4b_hidden");
        panel.id = "bl4b_panel";
        box.appendChild(panel);
        var toastBox = element("div", "bl4b_toast bl4b_hidden");
        toastBox.id = "bl4b_toast";
        toastBox.appendChild(element("p", "bl4b_toast_title"));
        toastBox.appendChild(element("div", "bl4b_toast_details"));
        box.appendChild(toastBox);
        parent.appendChild(box);
        return box;
    }

    function square(n) {
        // The Reward Center button's parts, so it looks and hovers the same
        var sq = element("div", "bl4b_square mrc_cta_btn");
        sq.setAttribute("data-n", String(n));
        sq.appendChild(element("div", "mrc_cta_btn_border"));
        var hover = element("div", "mrc_cta_btn_hover_content");
        sq.appendChild(hover);
        var cap = element("div", "mrc_cta_btn_cap");
        cap.appendChild(element("div", "mrc_cta_btn_cap_hover"));
        cap.appendChild(element("div", "mrc_cta_btn_cap_hover right"));
        sq.appendChild(cap);
        var body = element("div", "bl4b_body");
        body.appendChild(element("p", "bl4b_number mrc_cta_btn_text", String(n)));
        body.appendChild(element("p", "bl4b_caption"));
        sq.appendChild(body);
        sq.appendChild(element("div", "bl4b_trees"));
        sq.appendChild(element("div", "bl4b_current_mark"));

        sq.addEventListener("mouseenter", onPointer);
        sq.addEventListener("mouseleave", onPointer);
        sq.addEventListener("click", function () {
            if (endNaming()) return;
            if (prompting) {
                closePrompt();
                return;
            }
            var info = squareState(n);
            if (!info || busy) return;
            if (info.empty) send("save", n);
            else send("load", n);
            sound(sq, "ElementClicked");
        });
        return sq;
    }

    // Where the pointer is decides what shows (enter/leave events alone left the buttons up):
    // over a square, it's hovered and its buttons show; over those buttons, they stay; anywhere
    // else, the panel goes at once and the buttons after CHIPS_GRACE_MS.
    var mouse = { x: -1, y: -1 };

    function inside(el, x, y) {
        if (!el) return false;
        var r = el.getBoundingClientRect();
        return r.width > 0 && x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
    }

    function setHovered(n, box) {
        if (hovered === n) return;
        hovered = n;
        if (n) sound(box.querySelector('.bl4b_square[data-n="' + n + '"]'), "ElementFocused");
        // Python reads the right button; it needs to know which square it's over
        ext.send(KEY, { do: "hover", n: n });
    }

    function track() {
        var box = row();
        var before = [hovered, chipsFor, onBar].join();
        if (!box || box.classList.contains("bl4b_hidden")) {
            setHovered(0, box);
            chipsFor = 0;
            onBar = false;
        } else {
            var over = 0;
            var squares = box.querySelectorAll(".bl4b_square");
            for (var i = 0; i < squares.length; i++) {
                if (inside(squares[i], mouse.x, mouse.y)) over = parseInt(squares[i].getAttribute("data-n"), 10);
            }
            var bar = box.querySelector(".bl4b_actions");
            onBar = !over && !!chipsFor && !bar.classList.contains("bl4b_hidden") && inside(bar, mouse.x, mouse.y);
            setHovered(over, box);
            if (over || onBar) {
                clearTimeout(bp.chipsTimer);
                bp.chipsTimer = null;
                if (over) chipsFor = over;
            } else if (chipsFor && !bp.chipsTimer) {
                bp.chipsTimer = setTimeout(function () {
                    bp.chipsTimer = null;
                    if (!alive()) return;
                    chipsFor = 0;
                    render();
                }, CHIPS_GRACE_MS);
            }
        }
        if ([hovered, chipsFor, onBar].join() !== before) render();
    }

    function onPointer(event) {
        if (!alive()) return;
        mouse.x = event.clientX;
        mouse.y = event.clientY;
        track();
    }

    function barButton(bar, className, text, onClick) {
        var button = element("div", "bl4b_chip " + className, text);
        button.addEventListener("click", function (event) {
            event.stopPropagation();
            if (endNaming()) return;
            var n = chipsFor;
            var info = squareState(n);
            if (!info || busy) return;
            onClick(n, info);
            sound(button, "ElementClicked");
            render();
        });
        bar.appendChild(button);
        return button;
    }

    // One bar for the row, moved under whichever square it works on: straight down from the
    // square, and sliding along it never crosses another square
    function actionBar() {
        var bar = element("div", "bl4b_actions bl4b_hidden");
        barButton(bar, "bl4b_chip_save", "Save", function (n, info) {
            if (info.empty || confirmed(n, "save")) send("save", n);
        });
        barButton(bar, "bl4b_chip_name", "Name", function (n, info) {
            if (!info.empty) {
                hurry();
                ext.send(KEY, { do: "name", n: n });
            }
        });
        barButton(bar, "bl4b_chip_clear", "Clear", function (n, info) {
            if (!info.empty && confirmed(n, "clear")) send("clear", n);
        });
        bar.addEventListener("mouseenter", onPointer);
        bar.addEventListener("mouseleave", onPointer);
        return bar;
    }

    function renderBar(box) {
        var bar = box.querySelector(".bl4b_actions");
        var n = chipsFor;
        var info = n ? squareState(n) : null;
        var show = !!info && !info.empty && !namingN();
        bar.classList.toggle("bl4b_hidden", !show);
        if (!show) {
            bar.bl4bKey = "";
            return;
        }
        var asking = confirm.n === n && Date.now() < confirm.until;
        // Its texts and place only change with these (reading offsets lays the page out)
        var key = [n, asking ? confirm.what : "", box.bl4bPlaced].join("|");
        if (bar.bl4bKey === key) return;
        bar.bl4bKey = key;
        var save = bar.querySelector(".bl4b_chip_save");
        var clear = bar.querySelector(".bl4b_chip_clear");
        setText(save, asking && confirm.what === "save" ? "Sure? Save over it" : "Save");
        setText(clear, asking && confirm.what === "clear" ? "Sure? Clear it" : "Clear");
        save.classList.toggle("bl4b_asking", asking && confirm.what === "save");
        clear.classList.toggle("bl4b_asking", asking && confirm.what === "clear");
        var sq = box.querySelector('.bl4b_square[data-n="' + n + '"]');
        if (sq) {
            // Centered under the square, kept inside the row
            var left = sq.offsetLeft + sq.offsetWidth / 2 - bar.offsetWidth / 2;
            left = Math.max(0, Math.min(left, box.offsetWidth - bar.offsetWidth));
            bar.style.left = left + "px";
        }
    }

    // Right-click on a square: "Save over Build N?"
    function promptBox() {
        var box = element("div", "bl4b_prompt bl4b_hidden");
        box.appendChild(element("p", "bl4b_prompt_title"));
        box.appendChild(element("p", "bl4b_prompt_text"));
        var buttons = element("div", "bl4b_prompt_buttons");
        var yes = element("div", "bl4b_chip bl4b_prompt_yes", "Save over it");
        var no = element("div", "bl4b_chip bl4b_prompt_no", "Cancel");
        buttons.appendChild(yes);
        buttons.appendChild(no);
        box.appendChild(buttons);
        yes.addEventListener("click", function (event) {
            event.stopPropagation();
            var n = prompting;
            closePrompt();
            if (n && !busy) send("save", n);
            sound(yes, "ElementClicked");
        });
        no.addEventListener("click", function (event) {
            event.stopPropagation();
            closePrompt();
            sound(no, "ElementClicked");
        });
        return box;
    }

    function openPrompt(n) {
        var info = squareState(n);
        var box = row();
        if (!info || !box || busy || namingN()) return;
        if (info.empty) {
            // Nothing to lose: just save
            send("save", n);
            return;
        }
        prompting = n;
        var prompt = box.querySelector(".bl4b_prompt");
        prompt.querySelector(".bl4b_prompt_title").textContent = "Save over " + (info.name ? info.name + " (Build " + n + ")" : "Build " + n) + "?";
        prompt.querySelector(".bl4b_prompt_text").textContent =
            "What you have equipped now, your skills and your specializations replace what's saved in it" +
            (info.name ? ". It keeps its name." : ".");
        sound(prompt, "ElementFocused");
        render();
    }

    function closePrompt() {
        prompting = 0;
        render();
    }

    // Overwriting and clearing take a second click
    function confirmed(n, what) {
        var now = Date.now();
        if (confirm.n === n && confirm.what === what && now < confirm.until) {
            confirm.n = 0;
            return true;
        }
        confirm = { n: n, what: what, until: now + CONFIRM_MS };
        later(render, CONFIRM_MS + 50);
        return false;
    }

    function namingN() {
        return state && state.naming ? state.naming.n : 0;
    }

    // A click while a name is being typed keeps the name (like Enter) and does nothing else
    function endNaming() {
        if (!namingN()) return false;
        ext.send(KEY, { do: "name_end", keep: true });
        state.naming = null;
        render();
        return true;
    }

    function squareState(n) {
        if (!state || !state.squares) return null;
        for (var i = 0; i < state.squares.length; i++) if (state.squares[i].n === n) return state.squares[i];
        return null;
    }

    // Measuring lays the page out: every poll only while the menu is opening (it may still move),
    // then once every PLACE_MS (a window resize); styles are written only when they change
    function place(box, menu, force) {
        var now = Date.now();
        if (!force && now - (box.bl4bPlacedAt || 0) < PLACE_MS) return;
        box.bl4bPlacedAt = now;
        var cta = menu.querySelector(".mrc_cta_btn_wrapper");
        var parent = box.parentNode;
        if (!cta || !parent) return;
        var c = cta.getBoundingClientRect();
        var p = parent.getBoundingClientRect();
        var r = rem();
        // Under the button, same width; if the button is hidden (no rewards) it still has a place
        var left = (c.left - p.left) + "px";
        var top = (c.bottom - p.top + GAP_REM * r) + "px";
        var width = Math.max(c.width, 39 * r) + "px";
        var key = [left, top, width].join("|");
        if (box.bl4bPlaced === key) return;
        box.bl4bPlaced = key;
        box.style.left = left;
        box.style.top = top;
        box.style.width = width;
    }

    function caption(info) {
        if (!info) return "";
        if (info.empty) return "Empty";
        return info.name || info.action || ("Build " + info.n);
    }

    // The strip at the bottom of a square: its points per tree, in the trees' colors
    function renderTrees(strip, info) {
        var trees = info && !info.empty ? list(info.trees) : [];
        var key = JSON.stringify(trees);
        if (strip.bl4bKey === key) return;
        strip.bl4bKey = key;
        strip.textContent = "";
        trees.forEach(function (t) {
            var seg = element("div", "bl4b_tree_seg bl4b_tree_" + (t.color || "none"));
            seg.style.flex = t.points + " 1 0";
            strip.appendChild(seg);
        });
    }

    function render() {
        var box = row();
        if (!box) return;
        var blocked = state && state.blocked;
        box.classList.toggle("bl4b_blocked", !!blocked);
        var squares = box.querySelectorAll(".bl4b_square");
        for (var i = 0; i < squares.length; i++) {
            var sq = squares[i];
            var n = parseInt(sq.getAttribute("data-n"), 10);
            var info = squareState(n);
            sq.classList.toggle("bl4b_empty", !info || !!info.empty);
            sq.classList.toggle("bl4b_saved", !!info && !info.empty);
            sq.classList.toggle("bl4b_current", !!info && !!info.current);
            sq.classList.toggle("bl4b_missing", !!info && info.missing > 0);
            sq.classList.toggle("bl4b_busy", busy === n);
            var typing = namingN() === n;
            sq.classList.toggle("bl4b_naming", typing);
            sq.classList.toggle("bl4b_selected", chipsFor === n && !hovered);
            setText(sq.querySelector(".bl4b_caption"), busy === n ? "..." : typing ? state.naming.text || "Name?" : caption(info));
            renderTrees(sq.querySelector(".bl4b_trees"), info);
        }
        renderBar(box);
        renderPanel(box);
        box.classList.toggle("bl4b_prompt_open", !!prompting);
        box.querySelector(".bl4b_prompt").classList.toggle("bl4b_hidden", !prompting);
    }

    function line(parent, className, text) {
        var p = element("p", className, text);
        parent.appendChild(p);
        return p;
    }

    function renderPanel(box) {
        var panel = box.querySelector("#bl4b_panel");
        var naming = namingN();
        // Only while the mouse is on a square (or its buttons): it goes the moment it leaves
        var shownN = naming || hovered || (onBar ? chipsFor : 0);
        var info = shownN ? squareState(shownN) : null;
        box.classList.toggle("bl4b_panel_open", !!info);
        if (!info) {
            panel.classList.add("bl4b_hidden");
            return;
        }
        panel.classList.remove("bl4b_hidden");
        var caret = naming ? (Math.floor(Date.now() / 500) % 2 ? "|" : " ") : "";
        // Built again only when something it shows changed
        var key = JSON.stringify([shownN, naming ? state.naming.text : null, caret, state && state.blocked, info]);
        if (panel.bl4bKey === key) return;
        panel.bl4bKey = key;
        panel.textContent = "";
        var head = element("div", "bl4b_panel_head");
        var titleBox = element("div", "bl4b_panel_titles");
        if (naming) {
            titleBox.appendChild(element("p", "bl4b_panel_title bl4b_typing", (state.naming.text || "") + caret));
        } else {
            titleBox.appendChild(element("p", "bl4b_panel_title", info.name || "Build " + info.n));
        }
        var sub = [];
        if (info.name || naming) sub.push("Build " + info.n);
        if (info.action) sub.push(info.action);
        if (sub.length && !info.empty) titleBox.appendChild(element("p", "bl4b_panel_sub", sub.join(" · ")));
        head.appendChild(titleBox);
        if (!info.empty) head.appendChild(element("p", "bl4b_panel_saved", (info.current ? "Equipped now · " : "") + "Saved " + info.saved));
        panel.appendChild(head);
        if (naming) line(panel, "bl4b_panel_note bl4b_naming_note", "Type a name. Enter: keep · Esc: cancel · Delete: clear it");
        if (state && state.blocked) {
            line(panel, "bl4b_panel_note bl4b_bad", state.blocked);
            return;
        }
        if (info.empty) {
            line(panel, "bl4b_panel_note", "Click to save what you have equipped, your skills and your specializations here.");
            return;
        }
        var gear = element("div", "bl4b_gear");
        list(info.gear).forEach(function (g) {
            var r = element("div", "bl4b_gear_row" + (g.have ? "" : " bl4b_gone") + (g.on ? " bl4b_on" : ""));
            r.appendChild(element("p", "bl4b_gear_slot", g.slot));
            var name = element("p", "bl4b_gear_name bl4b_rarity_" + (g.rarity || "none"), g.label);
            r.appendChild(name);
            // Builds saved before names were read only have "Legendary Jakobs Shotgun": no need to repeat it
            if (g.desc && g.label.indexOf(g.desc) < 0) r.appendChild(element("p", "bl4b_gear_desc", g.desc));
            if (!g.have) r.appendChild(element("p", "bl4b_gear_flag", "not in backpack"));
            gear.appendChild(r);
        });
        panel.appendChild(gear);
        var trees = list(info.trees);
        if (trees.length) {
            line(panel, "bl4b_panel_label", "Skills");
            var text = line(panel, "bl4b_panel_text", "");
            trees.forEach(function (t, i) {
                if (i) text.appendChild(element("span", "bl4b_sep", " · "));
                text.appendChild(element("span", "bl4b_tree_text_" + (t.color || "none"), t.name + " " + t.points));
            });
        }
        if (info.specs) {
            line(panel, "bl4b_panel_label", "Specializations");
            line(panel, "bl4b_panel_text", info.specs);
        }
        var hint = info.missing > 0
            ? "Click to load. Missing items are skipped; their slots stay as they are."
            : "Click to load.";
        line(panel, "bl4b_panel_hint", hint + " Right-click: save over it. Buttons under the row: Save · Name · Clear");
    }

    function toast(t) {
        var box = document.getElementById("bl4b_toast");
        if (!box || !t) return;
        box.querySelector(".bl4b_toast_title").textContent = t.title || "";
        var details = box.querySelector(".bl4b_toast_details");
        details.textContent = "";
        list(t.details).forEach(function (d) { line(details, "bl4b_toast_line", d); });
        box.classList.toggle("bl4b_toast_error", !!t.error);
        box.classList.remove("bl4b_hidden");
        clearTimeout(bp.toastTimer);
        bp.toastTimer = setTimeout(function () { box.classList.add("bl4b_hidden"); }, t.error || (t.details && t.details.length) ? TOAST_ERROR_MS : TOAST_MS);
        sound(box, t.error ? "Error" : "ElementClicked");
    }

    // endregion
    // region Talking to Python

    function hurry() {
        fastUntil = Date.now() + FAST_FOR_MS;
    }

    function send(what, n) {
        hurry();
        var payload = { do: what, n: n };
        busy = n;
        render();
        if (what === "save") {
            var labels = slotLabels();
            scanNames(function (names) {
                for (var slot in names) {
                    if (names[slot] && labels[slot]) labels[slot].label = names[slot];
                }
                payload.labels = labels;
                ext.send(KEY, payload);
            });
        } else {
            ext.send(KEY, payload);
        }
        // In case the answer never comes (the server restarted)
        later(function () {
            if (busy === n) {
                busy = 0;
                render();
            }
        }, 6000);
    }

    // Whether the state differs from the last one (the page only draws again then)
    var lastText = null;

    function onState(snap) {
        if (!snap || !snap.state) return false;
        var text = JSON.stringify(snap.state);
        if (text === lastText) return false;
        lastText = text;
        state = snap.state;
        bp.lastState = state;
        var pr = state.prompt;
        if (pr) {
            if (seenPrompt === null) seenPrompt = pr.id;
            else if (pr.id !== seenPrompt) {
                seenPrompt = pr.id;
                openPrompt(pr.n);
            }
        }
        var t = state.toast;
        if (t) {
            if (seenToast === null) seenToast = t.id;
            else if (t.id !== seenToast) {
                seenToast = t.id;
                busy = 0;
                toast(t);
            }
        }
        return true;
    }

    function poll() {
        try {
            pollInner();
        } catch (err) {
            log("poll failed: " + err);
            later(poll, POLL_HIDDEN_MS);
        }
    }

    function pollInner() {
        var menu = equipMenu();
        var visible = shown(menu);
        if (!visible) {
            var old = row();
            if (old) old.classList.add("bl4b_hidden");
            if (hovered || chipsFor) track();
            later(poll, POLL_HIDDEN_MS);
            return;
        }
        var box = build(menu);
        var appearing = box.classList.contains("bl4b_hidden") || !box.bl4bDrawn;
        if (appearing) hurry();
        box.classList.remove("bl4b_hidden");
        place(box, menu, appearing || Date.now() < fastUntil);
        if (appearing && state) {
            box.bl4bDrawn = true;
            try {
                render();
            } catch (err) {
                log("draw failed: " + err);
            }
        }
        ext.get(KEY, true, function (snap) {
            if (!alive()) return;
            var changed = onState(snap);
            try {
                // Clicks, hovers, prompts and timers draw for themselves; a poll only draws a new
                // state (or the typing caret) and looks for the mouse when it can matter
                if (changed || appearing || namingN()) render();
                if (appearing || hovered || chipsFor || onBar) track();
            } catch (err) {
                log("draw failed: " + err);
            }
            later(poll, Date.now() < fastUntil ? POLL_FAST_MS : namingN() ? POLL_NAMING_MS : POLL_SHOWN_MS);
        });
    }

    // endregion
    // region Styles

    function loadCss() {
        ns.get(CSS_URL, function (css) {
            if (!alive()) return;
            if (css && css !== bp.css) {
                bp.css = css;
                var style = document.getElementById("bl4b_css");
                if (!style) {
                    style = document.createElement("style");
                    style.id = "bl4b_css";
                    document.head.appendChild(style);
                }
                style.textContent = css;
            }
            later(loadCss, CSS_MS);
        });
    }

    // endregion

    document.addEventListener("mousemove", onPointer);

    ext.teardowns[KEY] = function () {
        document.removeEventListener("mousemove", onPointer);
        bp.gen = 0;
        timers.forEach(clearTimeout);
        clearTimeout(bp.toastTimer);
        clearTimeout(bp.chipsTimer);
        bp.chipsTimer = null;
        var box = row();
        if (box && box.parentNode) box.parentNode.removeChild(box);
    };

    loadCss();
    poll();
    log("ready");
})();
