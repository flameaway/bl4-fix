"use strict";

// Kairosdex: a dex of BL4's bosses and the legendaries they drop, as a screen in the game's menu
// page (the page the pause menu lives in).
//
// The BL4 UI Link (or, until its pak loads, the BL4 Mod Menu) runs this file and runs it again
// when it changes, so it tears its last copy down first. The dex itself is dex.json next to this file (made from the
// game's data by tools/gen_weapondex.py); Python (bl4_weapondex/screen.py) serves what changes
// through GET /ext?k=weapondex: the bosses beaten on this profile (any character), the dev unlock, the search
// text (typed keys are read in Python, since no key events reach page scripts) and requests to
// open or close. Clicks go back as {op: "ext", ext: "weapondex", do: ...}.
//
// A boss is hidden ("???", no drops) until it's beaten; an item is hidden until one of the bosses
// that drop it is beaten. Items no boss drops are always shown. "Unlock everything" shows it all.

(function () {
    // eslint-disable-next-line no-undef
    var ns = (typeof bl4linkNs !== "undefined" && bl4linkNs) || window.bl4ModMenu;
    if (!ns || !ns.ext) return;
    var ext = ns.ext;
    var KEY = "weapondex";
    var BASE = "coui://uiresources/bl4weapondex/";
    var POLL_OPEN_MS = 100;
    var POLL_TYPING_MS = 40;
    var POLL_IDLE_MS = 1000;
    var CSS_MS = 2000;
    var PICTURES_MS = 3000;
    // Boss pictures (bl4_weapondex/bake.py): <id>.png, listed in index.json
    var PICTURES = BASE + "bosses/";
    // Item pictures, as the inventory draws them (bl4_weapondex/item_bake.py): <key>.png + index.json
    var ITEM_PICTURES = BASE + "items/";
    // Each picture's quarter-size copy (128 px), used for every thumbnail; listed in small/index.json
    var SMALL = "small/";
    var TOAST_MS = 4000;
    // The game's own icons: per kill (a skull with a target) and Big Encore (the map's boss replay)
    var ICON_KILL = "coui://uiresources/_shared/assets/ico_ui_art_discovery/ico_menu_target_dummies.png";
    var ICON_ENCORE = "coui://uiresources/_shared/assets/ico_ui_art_discovery/ico_boss_replay.png";

    if (ext.teardowns[KEY]) {
        try {
            ext.teardowns[KEY]();
        } catch (err) {}
    }
    // Everything that must survive a reload lives on dex; each copy takes a new, ever higher number
    var dex = ns.dex || (ns.dex = {});
    dex.counter = Math.max(dex.counter || 0, dex.gen || 0) + 1;
    var gen = dex.counter;
    dex.gen = gen;

    var timers = [];
    function alive() {
        return dex.gen === gen;
    }
    function later(fn, ms) {
        var id = setTimeout(function () {
            // Fired: drop its id, or the list grows by one per poll for as long as the page lives
            var at = timers.indexOf(id);
            if (at >= 0) timers.splice(at, 1);
            if (alive()) fn();
        }, ms);
        timers.push(id);
        return id;
    }
    function log(text) {
        if (ns.log) ns.log("[weapondex] " + text);
    }

    // What the screen shows: tab, picks, filters, sorts, and each tab's list scroll. It outlives this
    // script (dex) and is mirrored to Python (saveView), so it also outlives the page itself.
    var KEPT = ["tab", "boss", "item", "region", "kind", "type", "maker", "onlyBoss", "onlyItem", "sortBoss", "sortItem", "scroll"];
    var freshView = !dex.view;
    var view = dex.view || (dex.view = {
        open: false, own: false, tab: "bosses", boss: null, item: null,
        region: "", kind: "", type: "", maker: "", onlyBoss: false, onlyItem: false, sortBoss: "no", sortItem: "type",
        scroll: { bosses: 0, items: 0 }
    });  // prettier-ignore
    if (!view.scroll) view.scroll = { bosses: 0, items: 0 };  // a view from before scrolls were kept
    if (view.only !== undefined) {
        view.onlyBoss = view.onlyItem = !!view.only;
        delete view.only;
    }
    var state = null;
    var data = null;
    var ui = {};

    // region Data

    var BOSS_SORTS = [
        ["no", "Number"], ["name", "Name"], ["area", "Area"], ["beaten", "Beaten first"], ["drops", "Most drops"]
    ];  // prettier-ignore
    var ITEM_SORTS = [
        ["type", "Type"], ["name", "Name"], ["maker", "Manufacturer"], ["rarity", "Rarity"], ["chance", "Best chance"]
    ];  // prettier-ignore

    function index(raw) {
        var d = { bosses: raw.bosses, items: [], itemByKey: {}, bossById: {}, regions: raw.regions,
                  types: raw.types, makers: raw.makers, kinds: [] };  // prettier-ignore
        raw.bosses.forEach(function (b) {
            d.bossById[b.id] = b;
            if (b.kind && d.kinds.indexOf(b.kind) < 0) d.kinds.push(b.kind);
        });
        d.kinds.sort();
        Object.keys(raw.items).forEach(function (key) {
            var item = raw.items[key];
            item.key = key;
            item.typeRank = raw.types.indexOf(item.type);
            item.best = 0;
            d.items.push(item);
            d.itemByKey[key] = item;
        });
        raw.bosses.forEach(function (b) {
            b.drops.forEach(function (drop) {
                var item = d.itemByKey[drop.item];
                if (item) item.best = Math.max(item.best, drop.uvh0 || 0, drop.uvh5 || 0, drop.encore || 0);
            });
        });
        return d;
    }

    function loadData(done) {
        ns.get(BASE + "dex.json", function (text) {
            if (!alive()) return;
            if (!text) {
                later(function () {
                    loadData(done);
                }, 2000);
                return;
            }
            try {
                data = index(JSON.parse(text));
            } catch (err) {
                log("bad dex.json: " + err);
                return;
            }
            loadTravel();
            done();
        });
    }

    // Bosses with a Big Encore machine to set a waypoint on or travel to (travel.json, next to dex.json)
    function loadTravel() {
        ns.get(BASE + "travel.json", function (text) {
            if (!alive() || !data) return;
            if (!text) {
                // Not published yet (Python copies it at register): look again
                later(loadTravel, 3000);
                return;
            }
            try {
                data.travel = JSON.parse(text).bosses || {};
            } catch (err) {
                data.travel = {};
                log("bad travel.json: " + err);
            }
            render();
        });
    }
    function canTravel(b) {
        return !!(data.travel && data.travel[b.id]);
    }

    function beaten(id) {
        return !!(state && state.defeated && state.defeated[id]);
    }
    function unlocked() {
        return !!(state && state.unlock);
    }
    function bossShown(b) {
        return unlocked() || beaten(b.id);
    }
    // "Obtained items only" (a mod option, on by default): an item shows once the player has had one
    // (owned.py: the saves' backpacks, gear and bank, and the live backpack), else "???"
    function obtainedMode() {
        return !!(state && state.obtained !== false);
    }
    function owned(key) {
        if (!state || !state.owned) return false;
        if (dex.ownedV !== state.owned_v || !dex.ownedSet) {
            dex.ownedV = state.owned_v;
            dex.ownedSet = {};
            for (var i = 0; i < state.owned.length; i++) dex.ownedSet[state.owned[i]] = true;
        }
        return !!dex.ownedSet[key];
    }
    function itemShown(item) {
        if (unlocked()) return true;
        if (obtainedMode()) return owned(item.key);
        if (!item.bosses) return true;
        for (var i = 0; i < item.bosses.length; i++) if (beaten(item.bosses[i])) return true;
        return false;
    }

    // Lowercase letters and digits only, so "ol thrash", "olthrash" and "Ol' Thrash" all match
    function norm(text) {
        return String(text || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
    }
    function terms() {
        var raw = (state && state.search) || "";
        return raw.split(/\s+/).map(norm).filter(function (t) {
            return !!t;
        });
    }
    function matches(haystack, words) {
        for (var i = 0; i < words.length; i++) if (haystack.indexOf(words[i]) < 0) return false;
        return true;
    }

    function bossHaystack(b) {
        var parts = [b.area, b.region, b.kind];
        if (bossShown(b)) {
            parts.push(b.name);
            b.drops.forEach(function (drop) {
                var item = data.itemByKey[drop.item];
                if (item) parts.push(item.name);
            });
        }
        return parts.map(norm).join("|");
    }

    function itemHaystack(item) {
        var parts = [item.type, item["class"], item.hero, item.world ? "world drop" : "", item.reward ? "mission reward" : ""];
        if (itemShown(item)) {
            parts.push(item.name, item.maker, item.rarity);
            (item.bosses || []).forEach(function (id) {
                var b = data.bossById[id];
                if (!b) return;
                parts.push(b.area, b.region);
                if (bossShown(b)) parts.push(b.name);
            });
        }
        return parts.map(norm).join("|");
    }

    function bossFiltered(b) {
        if (view.region && b.region !== view.region) return false;
        if (view.kind && b.kind !== view.kind) return false;
        return !(view.onlyBoss && !beaten(b.id));
    }
    function itemFiltered(item) {
        if (view.type && item.type !== view.type) return false;
        if (view.maker && (item.maker !== view.maker || !itemShown(item))) return false;
        return !(view.onlyItem && !itemShown(item));
    }

    function bossList() {
        var words = terms();
        var list = data.bosses.filter(function (b) {
            return bossFiltered(b) && (!words.length || matches(bossHaystack(b), words));
        });
        var by = view.sortBoss;
        list.sort(function (a, b) {
            if (by === "name") return cmp(shownName(a), shownName(b)) || a.no - b.no;
            if (by === "area") return cmp(a.region + "|" + a.area, b.region + "|" + b.area) || a.no - b.no;
            if (by === "beaten") return (beaten(b.id) ? 1 : 0) - (beaten(a.id) ? 1 : 0) || a.no - b.no;
            if (by === "drops") return b.drops.length - a.drops.length || a.no - b.no;
            return a.no - b.no;
        });
        return list;
    }

    function itemList() {
        var words = terms();
        var list = data.items.filter(function (item) {
            return itemFiltered(item) && (!words.length || matches(itemHaystack(item), words));
        });
        var by = view.sortItem;
        list.sort(function (a, b) {
            var an = itemShown(a) ? a.name : "~", bn = itemShown(b) ? b.name : "~";
            if (by === "name") return cmp(an, bn);
            if (by === "maker") return cmp(itemShown(a) ? a.maker || "~" : "~", itemShown(b) ? b.maker || "~" : "~") || a.typeRank - b.typeRank || cmp(an, bn);
            if (by === "rarity") return cmp(b.rarity, a.rarity) || a.typeRank - b.typeRank || cmp(an, bn);
            if (by === "chance") return b.best - a.best || cmp(an, bn);
            return a.typeRank - b.typeRank || cmp(an, bn);
        });
        return list;
    }

    function cmp(a, b) {
        a = String(a).toLowerCase();
        b = String(b).toLowerCase();
        return a < b ? -1 : a > b ? 1 : 0;
    }

    function hasPicture(id) {
        return !!(dex.pictures && dex.pictures[id]);
    }

    // A picture's address: its small copy for a thumbnail once that's at least as new as the picture
    // (a backfill or a bake may not have made it yet), else the full one. null: no picture.
    function pictureUrl(folder, full, smallIndex, key, small) {
        var version = full && full[key];
        if (!version) return null;
        var smallVersion = small && smallIndex && smallIndex[key];
        if (smallVersion && smallVersion >= version) return "url(" + folder + SMALL + key + ".png?v=" + smallVersion + ")";
        return "url(" + folder + key + ".png?v=" + version + ")";
    }
    function bossPictureUrl(id, small) {
        return pictureUrl(PICTURES, dex.pictures, dex.smallPictures, id, small);
    }
    function itemPictureUrl(key, small) {
        return pictureUrl(ITEM_PICTURES, dex.itemPictures, dex.smallItemPictures, key, small);
    }

    // A boss's picture: its photo, a dark silhouette of it until it's beaten, or an empty frame.
    // small: a thumbnail (its 128 px copy)
    function portrait(b, cls, small) {
        var box = el("div", cls + (hasPicture(b.id) ? "" : " bl4dex_no_picture"));
        if (hasPicture(b.id)) {
            var img = el("div", "bl4dex_picture" + (bossShown(b) ? "" : " bl4dex_silhouette"));
            img.style.backgroundImage = bossPictureUrl(b.id, small);
            box.appendChild(img);
        } else {
            box.appendChild(el("p", "bl4dex_picture_missing", "?"));
        }
        return box;
    }

    // The latest roll's own picture (roll.py renders it like the inventory: the game's own render
    // target), else the baked one; null with neither
    function itemPictureFor(item, shown) {
        var roll = shown && state && state.roll && state.roll.item === item.key ? state.roll.picture : null;
        if (roll) return "url(TextureRenderTarget2D'" + roll + "')";
        return dex.itemPictures && dex.itemPictures[item.key] ? itemPictureUrl(item.key, false) : null;
    }

    // An item's picture (the inventory's own thumbnail render), dark until the item is found
    function itemPicture(item, shown) {
        var url = itemPictureFor(item, shown);
        var box = el("div", "bl4dex_item_picture" + (url ? "" : " bl4dex_no_picture"));
        if (url) {
            var img = el("div", "bl4dex_picture" + (shown ? "" : " bl4dex_silhouette"));
            img.style.backgroundImage = url;
            box.appendChild(img);
        } else {
            box.appendChild(el("p", "bl4dex_picture_missing", "?"));
        }
        return box;
    }

    // The game's text markup ([secondary]...[/secondary], icons) drawn by the game's own renderer.
    // It makes a <p> per paragraph, so it goes in a div.
    function markupElement(cls, text) {
        var box = el("div", cls);
        if (typeof MarkupMgr !== "undefined") {
            box.innerHTML = MarkupMgr.ResolveMarkupText(text, { attributes: [{ name: "max_num_glyph", value: 1 }] });
        } else {
            box.textContent = String(text).replace(/\[\/?[a-z_]+\]/g, "");
        }
        return box;
    }

    function shownName(b) {
        return bossShown(b) ? b.name : "???";
    }

    function pct(value) {
        if (!value) return "-";
        var p = value * 100;
        var text = p >= 10 ? String(Math.round(p)) : p >= 1 ? String(Math.round(p * 10) / 10) : String(Math.round(p * 100) / 100);
        return text + "%";
    }

    // The UVH level the chances are shown at: the player's, or UVH 1 below that (or while unknown)
    function shownUvh() {
        var u = state && state.uvh;
        return u && u > 1 ? u : 1;
    }
    // The game's chance grows in a straight line with the UVH level (checked against every chance
    // formula in the game's data, UVH 0-10: tools/gen_drops.py Loot.chance)
    function uvhChance(drop) {
        if (!drop || drop.uvh0 === undefined) return 0;
        var top = drop.uvh5 === undefined ? drop.uvh0 : drop.uvh5;
        return drop.uvh0 + (top - drop.uvh0) * shownUvh() / 5;
    }
    function uvhLabel() {
        return "UVH " + shownUvh();
    }
    function uvhNote() {
        var u = state && state.uvh;
        if (u === null || u === undefined) return "Chance per kill.";
        return u >= 1 ? "Chance per kill at your UVH level (UVH " + u + ")." : "Chance per kill. You're not in UVH yet: UVH 1 shown.";
    }

    function pad(no) {
        return "#" + ("00" + no).slice(-3);
    }

    function day(ts) {
        var d = new Date(ts * 1000);
        return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2);
    }

    // endregion
    // region DOM helpers

    function el(tag, cls, text) {
        var e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined) e.textContent = text;
        return e;
    }

    function onClick(target, fn) {
        target.addEventListener("click", function (evt) {
            evt.stopPropagation();
            fn(evt);
        });
    }

    // Document listeners, removed when this copy is torn down
    var docListeners = [];
    function listenDoc(type, fn) {
        document.addEventListener(type, fn);
        docListeners.push([type, fn]);
    }

    // A scroll bar beside a list (the page draws none): drag the handle, or click the track to jump
    function scrollbar(list) {
        var track = el("div", "bl4dex_sbar");
        var handle = el("div", "bl4dex_sbar_handle");
        track.appendChild(handle);
        var drag = null;
        function range() {
            return track.clientHeight - handle.offsetHeight;
        }
        function scrollTo(handleTop) {
            var r = range();
            if (r <= 0) return;
            var t = Math.max(0, Math.min(r, handleTop));
            list.scrollTop = t / r * (list.scrollHeight - list.clientHeight);
            syncScrollbar();
        }
        handle.addEventListener("mousedown", function (evt) {
            evt.stopPropagation();
            drag = { y: evt.clientY, top: handle.offsetTop };
            handle.classList.add("bl4dex_sbar_drag");
        });
        track.addEventListener("mousedown", function (evt) {
            if (evt.target !== track) return;
            evt.stopPropagation();
            // Centre the handle on the click, and keep dragging from there
            var top = evt.clientY - track.getBoundingClientRect().top - handle.offsetHeight / 2;
            scrollTo(top);
            drag = { y: evt.clientY, top: handle.offsetTop };
            handle.classList.add("bl4dex_sbar_drag");
        });
        onClick(track, function () {});  // not through to the pause menu
        listenDoc("mousemove", function (evt) {
            if (drag) scrollTo(drag.top + evt.clientY - drag.y);
        });
        listenDoc("mouseup", function () {
            drag = null;
            handle.classList.remove("bl4dex_sbar_drag");
        });
        list.addEventListener("scroll", function () {
            syncScrollbar();
            rememberScroll();
        });
        ui.sbar = track;
        ui.sbarHandle = handle;
        return track;
    }

    function syncScrollbar() {
        var list = ui.list;
        var track = ui.sbar;
        if (!list || !track) return;
        var total = list.scrollHeight;
        var shown = list.clientHeight;
        var fits = total <= shown + 1;
        track.classList.toggle("bl4dex_sbar_off", fits);
        if (fits) return;
        var h = Math.max(track.clientHeight * shown / total, 40);
        var top = (track.clientHeight - h) * list.scrollTop / (total - shown);
        ui.sbarHandle.style.height = h + "px";
        ui.sbarHandle.style.top = top + "px";
    }

    // Wheel scrolling doesn't always fire scroll events here: keep the handle (and the kept
    // position) in step anyway
    function watchScroll() {
        if (view.open) {
            syncScrollbar();
            rememberScroll();
        }
        later(watchScroll, 100);
    }

    // The list's position in this tab, unless the list is hidden or being rebuilt (it reads 0 then)
    var rebuilding = false;
    function rememberScroll() {
        if (!view.open || rebuilding || !ui.list || ui.root.classList.contains("bl4dex_hidden")) return;
        if (!ui.list.firstChild) return;
        var top = ui.list.scrollTop;
        if (view.scroll[view.tab] !== top) {
            view.scroll[view.tab] = top;
            saveView();
        }
    }

    // Python keeps a copy of the view (the page may be rebuilt when the game's menus close)
    var saveTimer = null;
    function saveView() {
        if (saveTimer !== null) clearTimeout(saveTimer);
        saveTimer = setTimeout(function () {
            saveTimer = null;
            if (!alive()) return;
            var kept = {};
            KEPT.forEach(function (k) {
                kept[k] = view[k];
            });
            ext.send(KEY, { do: "view", view: kept });
        }, 300);
    }

    // The game's list button (same as the mod menu uses), or a plain one if its template isn't there
    function button(text, fn, cls) {
        var t = ns.templates && ns.templates.button;
        var b = t ? t.cloneNode(true) : el("div", "list_btn");
        b.classList.add("bl4dex_btn");
        if (cls) b.classList.add(cls);
        var label = b.querySelector(".button_txt");
        if (!label) {
            label = el("p", "button_txt");
            b.appendChild(label);
        }
        label.textContent = text;
        b.addEventListener("mouseenter", function () {
            b.classList.add("hover_effect");
        });
        b.addEventListener("mouseleave", function () {
            b.classList.remove("hover_effect");
        });
        if (fn) onClick(b, fn);
        return b;
    }

    function chip(text, active, fn, cls) {
        var c = el("div", "bl4dex_chip" + (active ? " bl4dex_active" : "") + (cls ? " " + cls : ""));
        c.appendChild(el("p", "bl4dex_chip_text", text));
        onClick(c, fn);
        return c;
    }

    function send(payload) {
        ext.send(KEY, payload);
        pollSoon();
    }

    // endregion
    // region Screen

    function build() {
        var root = el("div", "bl4dex_root bl4dex_hidden");
        root.appendChild(el("div", "fill_content menu_bg_nostandin bl4dex_bg"));
        root.appendChild(el("div", "bl4dex_shade"));
        var frame = el("div", "bl4dex_frame");

        var header = el("div", "bl4dex_header");
        header.appendChild(el("p", "bl4dex_title", "KAIROSDEX"));
        ui.progress = el("div", "bl4dex_progress");
        header.appendChild(ui.progress);
        ui.notice = el("p", "bl4dex_notice");
        header.appendChild(ui.notice);
        frame.appendChild(header);

        var bar = el("div", "bl4dex_bar");
        ui.tabs = el("div", "bl4dex_tabs");
        bar.appendChild(ui.tabs);
        ui.search = el("div", "bl4dex_search");
        ui.searchText = el("p", "bl4dex_search_text");
        ui.search.appendChild(el("p", "bl4dex_search_label", "SEARCH"));
        ui.search.appendChild(ui.searchText);
        ui.searchClear = el("div", "bl4dex_search_clear");
        ui.searchClear.appendChild(el("p", "bl4dex_chip_text", "Clear"));
        ui.search.appendChild(ui.searchClear);
        onClick(ui.search, function () {
            send({ do: "type", on: !(state && state.typing) });
        });
        onClick(ui.searchClear, function () {
            send({ do: "search", text: "" });
            if (state) state.search = "";
            render();
        });
        bar.appendChild(ui.search);
        ui.sort = el("div", "bl4dex_chip bl4dex_sort");
        ui.sortText = el("p", "bl4dex_chip_text");
        ui.sort.appendChild(ui.sortText);
        onClick(ui.sort, cycleSort);
        bar.appendChild(ui.sort);
        frame.appendChild(bar);

        ui.filters = el("div", "bl4dex_filters");
        frame.appendChild(ui.filters);

        var body = el("div", "bl4dex_body");
        ui.list = el("div", "bl4dex_list");
        body.appendChild(ui.list);
        body.appendChild(scrollbar(ui.list));
        ui.details = el("div", "bl4dex_details");
        body.appendChild(ui.details);
        frame.appendChild(body);

        var footer = el("div", "bl4dex_footer");
        footer.appendChild(button("Close", close, "bl4dex_close"));
        ui.hint = el("p", "bl4dex_hint");
        footer.appendChild(ui.hint);
        frame.appendChild(footer);

        ui.toast = el("p", "bl4dex_toast bl4dex_hidden");
        frame.appendChild(ui.toast);
        root.appendChild(frame);
        // Clicks on empty space mustn't reach the pause menu underneath; they also stop typing
        onClick(root, function () {
            if (state && state.typing) send({ do: "type", on: false });
        });
        document.body.appendChild(root);
        ui.root = root;
    }

    function open(own, boss) {
        if (!ui.root) return;
        view.open = true;
        view.own = !!own;
        var jump = boss && data && data.bossById[boss] ? boss : null;
        // From the status menus (inventory etc.) their tab bar is drawn above this page: start below it
        var pause = document.querySelector('[data-input-id="menu_pause_list"]');
        var overPause = !!pause && pause.getBoundingClientRect().height > 0;
        ui.root.classList.toggle("bl4dex_below_tabs", !overPause);
        ui.root.classList.remove("bl4dex_hidden");
        dex.sig = null;
        if (jump && state) goTo("bosses", jump);
        else render();
        pollSoon();
    }

    function hide() {
        view.open = false;
        if (ui.root) ui.root.classList.add("bl4dex_hidden");
        if (state && state.typing) send({ do: "type", on: false });
    }

    // Close (button or the open key): also close the game's menu if we opened it for this
    function close() {
        var own = view.own;
        hide();
        if (!own) return;
        // Closes the pause menu we opened without showing it on the way out (menu.js)
        if (ns.closeGameMenu) ns.closeGameMenu();
        else ext.http("POST", "/action", JSON.stringify({ op: "close_menus" }), null);
    }

    function cycleSort() {
        var sorts = view.tab === "bosses" ? BOSS_SORTS : ITEM_SORTS;
        var field = view.tab === "bosses" ? "sortBoss" : "sortItem";
        var i = 0;
        while (i < sorts.length && sorts[i][0] !== view[field]) i++;
        view[field] = sorts[(i + 1) % sorts.length][0];
        view.scroll[view.tab] = 0;
        saveView();
        render();
    }

    // A tab switch keeps each tab's scroll; other changes (filters) start the list at the top
    function setView(changes) {
        var switching = changes.tab !== undefined && changes.tab !== view.tab;
        for (var k in changes) if (Object.prototype.hasOwnProperty.call(changes, k)) view[k] = changes[k];
        if (!switching) view.scroll[view.tab] = 0;
        saveView();
        render();
    }

    // Go to one boss or item (a drop or "dropped by" row): its tab, picked and scrolled into view.
    // Filters that hide it are cleared, and so is the search if it hides it.
    function goTo(tab, id) {
        var c = { tab: tab };
        if (tab === "items") {
            c.item = id;
            if (!itemFiltered(data.itemByKey[id])) {
                c.type = "";
                c.maker = "";
                c.onlyItem = false;
            }
        } else {
            c.boss = id;
            if (!bossFiltered(data.bossById[id])) {
                c.region = "";
                c.kind = "";
                c.onlyBoss = false;
            }
        }
        for (var k in c) if (Object.prototype.hasOwnProperty.call(c, k)) view[k] = c[k];
        var words = terms();
        var target = tab === "items" ? data.itemByKey[id] : data.bossById[id];
        if (words.length && !matches(tab === "items" ? itemHaystack(target) : bossHaystack(target), words)) {
            state.search = "";
            send({ do: "search", text: "" });
        }
        reveal = id;
        saveView();
        render();
    }
    var reveal = null;  // an entry renderList scrolls into view

    function render() {
        if (!ui.root || !view.open || !data || !state) return;
        try {
            renderHeader();
            renderBar();
            renderFilters();
            renderList();
            renderDetails();
        } catch (err) {
            log("render failed: " + err + " " + (err && err.stack));
        }
    }

    function renderHeader() {
        var bossesBeaten = data.bosses.filter(function (b) {
            return beaten(b.id);
        }).length;
        var itemsShown = data.items.filter(itemShown).length;
        ui.progress.innerHTML = "";
        [["BOSSES", bossesBeaten, data.bosses.length], ["ITEMS", itemsShown, data.items.length]].forEach(function (p) {
            var box = el("div", "bl4dex_stat");
            box.appendChild(el("p", "bl4dex_stat_value", p[1] + " / " + p[2]));
            box.appendChild(el("p", "bl4dex_stat_label", p[0]));
            ui.progress.appendChild(box);
        });
        var notes = [];
        if (unlocked()) notes.push("Unlock everything is on");
        if (!state.character) notes.push("No character loaded");
        if (state.outdated) notes.push("The game was updated since this dex was made: chances may be off");
        ui.notice.textContent = notes.join("   |   ");
    }

    function renderBar() {
        ui.tabs.innerHTML = "";
        [["bosses", "Bosses"], ["items", "Items"]].forEach(function (t) {
            var tab = button(t[1], function () {
                if (view.tab !== t[0]) setView({ tab: t[0] });
            }, "bl4dex_tab");
            if (view.tab === t[0]) tab.classList.add("bl4dex_active");
            ui.tabs.appendChild(tab);
        });
        var typing = !!state.typing;
        var text = state.search || "";
        ui.search.classList.toggle("bl4dex_typing", typing);
        ui.searchText.classList.toggle("bl4dex_placeholder", !text && !typing);
        ui.searchText.textContent = text ? text + (typing ? "_" : "") : typing ? "_" : "Click to type a boss, area or item";
        ui.searchClear.classList.toggle("bl4dex_hidden", !text);
        var sorts = view.tab === "bosses" ? BOSS_SORTS : ITEM_SORTS;
        var current = view.tab === "bosses" ? view.sortBoss : view.sortItem;
        var label = sorts[0][1];
        sorts.forEach(function (s) {
            if (s[0] === current) label = s[1];
        });
        ui.sortText.textContent = "Sort: " + label;
        ui.hint.textContent = typing
            ? "Typing: Enter or Esc to stop, Delete clears."
            : "Click the search box to type. Click a drop to see the item, a boss to see its drops.";
    }

    function chipRow(label, values, current, field) {
        var row = el("div", "bl4dex_chip_row");
        row.appendChild(el("p", "bl4dex_row_label", label));
        row.appendChild(chip("All", !current, function () {
            var c = {};
            c[field] = "";
            setView(c);
        }));
        values.forEach(function (v) {
            row.appendChild(chip(v, current === v, function () {
                var c = {};
                c[field] = current === v ? "" : v;
                setView(c);
            }));
        });
        return row;
    }

    function renderFilters() {
        ui.filters.innerHTML = "";
        if (view.tab === "bosses") {
            ui.filters.appendChild(chipRow("REGION", data.regions, view.region, "region"));
            var row = chipRow("KIND", data.kinds, view.kind, "kind");
            row.appendChild(chip("Beaten only", view.onlyBoss, function () {
                setView({ onlyBoss: !view.onlyBoss });
            }, "bl4dex_toggle"));
            ui.filters.appendChild(row);
        } else {
            ui.filters.appendChild(chipRow("TYPE", data.types, view.type, "type"));
            var row2 = chipRow("MAKER", data.makers, view.maker, "maker");
            row2.appendChild(chip("Found only", view.onlyItem, function () {
                setView({ onlyItem: !view.onlyItem });
            }, "bl4dex_toggle"));
            ui.filters.appendChild(row2);
        }
    }

    // The entry the details show: the pick, or while a search or filter hides it, the first one
    // left (the pick itself is kept, and comes back with the filter off)
    var current = { bosses: null, items: null };

    function renderList() {
        rebuilding = true;
        ui.list.innerHTML = "";
        ui.entries = {};
        var list = view.tab === "bosses" ? bossList() : itemList();
        if (!list.length) {
            ui.list.appendChild(el("p", "bl4dex_empty", terms().length ? "Nothing matches that search." : "Nothing here yet."));
        }
        var ids = list.map(function (x) {
            return view.tab === "bosses" ? x.id : x.key;
        });
        var picked = view[view.tab === "bosses" ? "boss" : "item"];
        current[view.tab] = ids.indexOf(picked) >= 0 ? picked : ids.length ? ids[0] : null;
        list.forEach(view.tab === "bosses" ? addBossEntry : addItemEntry);
        // A jump stays pending until a scroll to it finishes (a refresh may rebuild the list first)
        var target = reveal !== null ? ui.entries[reveal] || null : null;
        if (!target) reveal = null;
        placeList(view.scroll[view.tab] || 0, target);
    }

    // Puts the rebuilt list back at its kept position, or scrolls an entry into view. The page lays
    // a rebuilt list out a frame or so later (until then entries measure 0 and the list can't
    // scroll), so this tries again each frame until the layout is there.
    var placeJob = 0;
    function placeList(top, target) {
        var job = ++placeJob;
        var tries = 0;
        var tab = view.tab;
        function step() {
            if (!alive() || job !== placeJob || view.tab !== tab) return;
            var laidOut = !target || target.offsetHeight > 0;
            var want = top;
            if (target && laidOut) {
                // Centre it, unless it's already in sight where the list was
                var at = target.getBoundingClientRect().top - ui.list.getBoundingClientRect().top + ui.list.scrollTop;
                var inSight = at >= top && at + target.offsetHeight <= top + ui.list.clientHeight;
                if (!inSight) want = Math.max(0, at - (ui.list.clientHeight - target.offsetHeight) / 2);
            }
            ui.list.scrollTop = want;
            var landed = Math.abs(ui.list.scrollTop - want) <= 1;
            if ((!laidOut || !landed) && tries++ < 20) {
                later(step, 16);
                return;
            }
            view.scroll[tab] = ui.list.scrollTop;
            rebuilding = false;
            syncScrollbar();
            if (target) {
                reveal = null;
                saveView();
            }
        }
        step();
    }

    function addBossEntry(b) {
        var shown = bossShown(b);
        var e = el("div", "bl4dex_entry bl4dex_boss_entry" + (shown ? "" : " bl4dex_locked") + (current.bosses === b.id ? " bl4dex_selected" : ""));
        ui.entries[b.id] = e;
        e.appendChild(portrait(b, "bl4dex_thumb", true));
        e.appendChild(el("p", "bl4dex_no", pad(b.no)));
        var main = el("div", "bl4dex_entry_main");
        main.appendChild(el("p", "bl4dex_entry_name", shown ? b.name : "???"));
        main.appendChild(el("p", "bl4dex_entry_sub", b.area === b.region ? b.region : b.area + ", " + b.region));
        e.appendChild(main);
        var side = el("div", "bl4dex_entry_side");
        if (beaten(b.id)) side.appendChild(el("p", "bl4dex_tag bl4dex_tag_beaten", "BEATEN"));
        if (b.kind) side.appendChild(el("p", "bl4dex_tag", b.kind));
        e.appendChild(side);
        onClick(e, function () {
            view.boss = b.id;
            saveView();
            render();
        });
        ui.list.appendChild(e);
    }

    function addItemEntry(item) {
        var shown = itemShown(item);
        var cls = "bl4dex_entry bl4dex_item_entry" + (shown ? "" : " bl4dex_locked") + (current.items === item.key ? " bl4dex_selected" : "");
        var e = el("div", cls);
        ui.entries[item.key] = e;
        e.appendChild(el("div", "bl4dex_rarity_bar bl4dex_rarity_" + item.rarity.toLowerCase()));
        var main = el("div", "bl4dex_entry_main");
        var nameRow = el("div", "bl4dex_entry_name_row");
        nameRow.appendChild(el("p", "bl4dex_entry_name bl4dex_name_" + item.rarity.toLowerCase(), shown ? item.name : "???"));
        miniItem(nameRow, item, shown);
        main.appendChild(nameRow);
        main.appendChild(el("p", "bl4dex_entry_sub", typeLine(item, shown)));
        e.appendChild(main);
        // World drop, then the bosses that drop it at the far right
        var side = el("div", "bl4dex_entry_side");
        if (item.world) side.appendChild(el("p", "bl4dex_tag", "World drop"));
        miniBosses(side, item);
        e.appendChild(side);
        onClick(e, function () {
            view.item = item.key;
            saveView();
            render();
        });
        hoverCard(e, item, true);
        ui.list.appendChild(e);
    }

    // The item's own small picture, right of its name (a silhouette until found)
    function miniItem(row, item, shown) {
        var url = itemPictureUrl(item.key, true);
        if (!url) return;
        var pic = el("div", "bl4dex_mini_item" + (shown ? "" : " bl4dex_silhouette"));
        pic.style.backgroundImage = url;
        row.appendChild(pic);
    }

    // Small pictures of the bosses that drop an item, at the entry's right end (a silhouette until beaten)
    var MINI_MAX = 3;
    function miniBosses(row, item) {
        var ids = item.bosses || [];
        if (!ids.length) return;
        var box = el("div", "bl4dex_mini_bosses");
        for (var i = 0; i < ids.length && i < MINI_MAX; i++) {
            var b = data.bossById[ids[i]];
            if (b) box.appendChild(portrait(b, "bl4dex_mini_boss", true));
        }
        if (ids.length > MINI_MAX) box.appendChild(el("p", "bl4dex_mini_more", "+" + (ids.length - MINI_MAX)));
        row.appendChild(box);
    }

    // region Hover card
    // Hovering an item (in the list or a boss's drops) shows its item card beside it: the game's
    // card widget (a second one, model data_bl4dex_card_1) with the item's latest roll. Python
    // rolls an item on its first hover (no picture) and keeps that roll for later hovers.

    // fromList: an Items list entry, which only shows the card with the "Item card on hover in the
    // item list" option on (read at hover time, so toggling it needs no redraw); a boss's drop rows always do
    function hoverCard(target, item, fromList) {
        if (!itemShown(item)) return;
        target.addEventListener("mouseenter", function () {
            if (fromList && !(state && state.list_hover)) return;
            dex.hover = { key: item.key, target: target };
            send({ do: "hover", item: item.key });
            updateHover();
        });
        target.addEventListener("mouseleave", function () {
            endHover(target);
        });
    }

    function endHover(target) {
        if (dex.hover && dex.hover.target === target) {
            dex.hover = null;
            send({ do: "hover", item: null });
            updateHover();
        }
    }

    // A click redraws the list: the row under the mouse is replaced by a new one, which gets no
    // mouseleave (it never had a mouseenter), so the card stayed up. Check the mouse against the row.
    listenDoc("mousemove", function (evt) {
        var h = dex.hover;
        if (!alive() || !h || !h.target.parentNode) return;
        var b = h.target.getBoundingClientRect();
        if (evt.clientX < b.left || evt.clientX >= b.right || evt.clientY < b.top || evt.clientY >= b.bottom) endHover(h.target);
    });

    function updateHover() {
        if (!ui.root) return;
        var box = ui.hoverBox;
        if (!box || box.parentNode !== ui.root) {
            box = ui.hoverBox = el("div", "bl4dex_hover_card bl4dex_hidden");
            ui.root.appendChild(box);
        }
        var h = dex.hover;
        if (h && !h.target.parentNode) {
            // The page redrew (a roll finished): the same item's new row
            var again = (ui.entries && ui.entries[h.key]) || (ui.dropRows && ui.dropRows[h.key]);
            if (again && again.parentNode) h.target = again;
        }
        var info = state && state.hover;
        var card = h && info && info.item === h.key ? info.card : null;
        if (!h || !card || !h.target.parentNode || !view.open) {
            box.classList.add("bl4dex_hidden");
            dex.hoverShown = null;
            return;
        }
        var shownKey = h.key + "|" + JSON.stringify(card);
        if (dex.hoverShown !== shownKey) {
            dex.hoverShown = shownKey;
            box.innerHTML = "";
            gameCard(box, card, 1);
        }
        box.classList.remove("bl4dex_hidden");
        positionHover(box, h.target, 0);
    }

    // Beside the hovered row: right of a list entry, left of a drop row in the details pane;
    // kept inside the screen once its height is known (Gameface lays it out a frame later)
    function positionHover(box, target, tries) {
        if (!alive() || !target.parentNode || box.classList.contains("bl4dex_hidden")) return;
        var root = ui.root.getBoundingClientRect();
        var row = target.getBoundingClientRect();
        var b = box.getBoundingClientRect();
        var inList = ui.list && ui.list.contains(target);
        var width = b.width || root.width * 0.3;
        var left = inList ? row.right - root.left + 12 : row.left - root.left - width - 12;
        var top = row.top - root.top - 40;
        if (b.height) top = Math.max(0, Math.min(top, root.height - b.height));
        box.style.left = Math.round(left) + "px";
        box.style.top = Math.round(top) + "px";
        if (!b.height && tries < 10) requestAnimationFrame(function () { positionHover(box, target, tries + 1); });
    }

    // endregion

    function typeLine(item, shown) {
        var parts = [item.type];
        if (item["class"]) parts.push(item["class"] + (item.hero ? " (" + item.hero + ")" : ""));
        if (shown && item.maker) parts.push(item.maker);
        if (shown && item.rarity === "Pearlescent") parts.push("Pearlescent");
        return parts.join("  |  ");
    }

    // endregion
    // region Details

    function renderDetails() {
        // Same entry as last time (a refresh): keep where its details were scrolled; another starts at the top
        var showing = view.tab + "|" + current[view.tab];
        var scroll = showing === ui.detailsFor ? ui.details.scrollTop : 0;
        ui.detailsFor = showing;
        ui.rollView = null;
        ui.details.innerHTML = "";
        if (view.tab === "bosses") {
            var b = current.bosses && data.bossById[current.bosses];
            if (b) bossDetails(b);
            else ui.details.appendChild(el("p", "bl4dex_empty", "Pick a boss to see where it is and what it drops."));
        } else {
            var item = current.items && data.itemByKey[current.items];
            if (item) itemDetails(item);
            else ui.details.appendChild(el("p", "bl4dex_empty", "Pick an item to see who drops it."));
        }
        ui.details.scrollTop = scroll;
    }

    // Two columns in the details pane (the boss or item on the left, its drops or card on the right)
    function columns(kind) {
        var cols = el("div", "bl4dex_cols bl4dex_cols_" + kind);
        var left = el("div", "bl4dex_col_left");
        var right = el("div", "bl4dex_col_right");
        cols.appendChild(left);
        cols.appendChild(right);
        ui.details.appendChild(cols);
        return { left: left, right: right };
    }

    function bossDetails(b) {
        var shown = bossShown(b);
        var cols = columns("boss");
        var d = cols.left;
        d.appendChild(portrait(b, "bl4dex_portrait"));
        d.appendChild(el("p", "bl4dex_detail_no", pad(b.no) + (b.kind ? "   " + b.kind.toUpperCase() : "")));
        d.appendChild(el("p", "bl4dex_detail_title", shown ? b.name : "???"));
        d.appendChild(el("p", "bl4dex_detail_meta", b.area === b.region ? b.region : b.area + ", " + b.region));
        var status = beaten(b.id)
            ? (state.defeated[b.id] === 1 ? "Beaten (from your save)" : "Beaten " + day(state.defeated[b.id]))
            : "Not beaten yet" + (unlocked() ? " (shown by Unlock everything)" : "");
        d.appendChild(el("p", "bl4dex_detail_status" + (beaten(b.id) ? " bl4dex_good" : ""), status));
        tripButtons(d, b);

        d = cols.right;
        d.appendChild(el("p", "bl4dex_section bl4dex_section_top", "DEDICATED DROPS"));
        if (!shown) {
            d.appendChild(el("p", "bl4dex_detail_note", "Beat this boss to see its " + b.drops.length + " dedicated drops."));
            return;
        }
        var table = el("div", "bl4dex_table");
        var head = el("div", "bl4dex_row bl4dex_row_head");
        head.appendChild(el("div", "bl4dex_drop_thumb bl4dex_drop_thumb_head"));
        ["Item", "Normal", uvhLabel(), "Big Encore"].forEach(function (h, i) {
            head.appendChild(el("p", i ? "bl4dex_cell_num" : "bl4dex_cell_name", h));
        });
        table.appendChild(head);
        b.drops.forEach(function (drop) {
            var item = data.itemByKey[drop.item];
            if (!item) return;
            // Not obtained yet ("Obtained items only" option): "???" and its silhouette
            var got = itemShown(item);
            var row = el("div", "bl4dex_row bl4dex_row_link" + (got ? "" : " bl4dex_locked"));
            // The item's inventory picture left of its name
            var thumb = el("div", "bl4dex_drop_thumb" + (got ? "" : " bl4dex_silhouette"));
            var thumbUrl = itemPictureUrl(item.key, true);
            if (thumbUrl) thumb.style.backgroundImage = thumbUrl;
            row.appendChild(thumb);
            var name = el("div", "bl4dex_cell_name");
            name.appendChild(el("p", "bl4dex_cell_title bl4dex_name_" + item.rarity.toLowerCase(), got ? item.name : "???"));
            name.appendChild(el("p", "bl4dex_cell_sub", typeLine(item, true)));
            row.appendChild(name);
            row.appendChild(el("p", "bl4dex_cell_num", pct(drop.uvh0)));
            row.appendChild(el("p", "bl4dex_cell_num", pct(uvhChance(drop))));
            row.appendChild(el("p", "bl4dex_cell_num", b.encore ? pct(drop.encore) : "-"));
            onClick(row, function () {
                goTo("items", item.key);
            });
            hoverCard(row, item);
            ui.dropRows = ui.dropRows || {};
            ui.dropRows[item.key] = row;
            table.appendChild(row);
        });
        d.appendChild(table);
        d.appendChild(el("p", "bl4dex_detail_note", uvhNote() + " Class mods: you get the one for your class."));
    }

    // Card stats of real rolls (bl4_weapondex/roll.py): the game drops the item out of sight at your
    // level, Python reads its stats and removes it. Tiles like the card's stat row: the latest roll's
    // value, and the range over every roll under it.

    function rollsOf(item) {
        var r = state && state.roll && state.roll.item === item.key ? state.roll : null;
        var rolls = ((r && r.rolls) || []).map(function (one) {
            return Array.isArray(one) ? { lines: one, fills: {} } : one;  // rolls from before fills
        });
        return { r: r, rolls: rolls };
    }

    // The latest roll's card, then the roll buttons, then the ranges over the rolls (one column)
    function rollSection(d, item) {
        var got = rollsOf(item), r = got.r, rolls = got.rolls;
        var head = rollHead(item, r, rolls);
        var latest = rolls.length ? rolls[rolls.length - 1] : null;
        if (latest && latest.card) {
            // The roll buttons above the game's own item card for the latest roll (on the right); with
            // several rolls, each stat's range under it
            head.classList.add("bl4dex_stats_head_card");
            d.appendChild(head);
            var holder = gameCard(d, latest.card);
            var after = el("div", "bl4dex_roll_after");
            rollAfter(after, r, rolls);
            d.appendChild(after);
            // What patchRoll changes in place on the next roll (a rebuild flashed the card and picture)
            if (ui.rollView && ui.rollView.key === item.key) {
                ui.rollView.head = head;
                ui.rollView.after = after;
                ui.rollView.holder = holder;
                ui.rollView.card = JSON.stringify(latest.card);
            }
            return;
        }
        d.appendChild(head);
        if (rolls.length) {
            var last = rolls[rolls.length - 1].lines;
            var grid = el("div", "bl4dex_stat_grid");
            last.forEach(function (line) {
                var tile = el("div", "bl4dex_stat_tile");
                var icon = el("div", "bl4dex_stat_icon");
                if (line.icon) icon.style.backgroundImage = "url(Texture2D'" + line.icon + "')";
                tile.appendChild(icon);
                var text = el("div", "bl4dex_stat_text");
                text.appendChild(el("p", "bl4dex_stat_value", line.text));
                var sub = line.label || "";
                if (rolls.length > 1) {
                    var lo = null, hi = null;
                    rolls.forEach(function (one) {
                        one.lines.forEach(function (l) {
                            if (l.key !== line.key) return;
                            if (lo === null || l.value < lo.value) lo = l;
                            if (hi === null || l.value > hi.value) hi = l;
                        });
                    });
                    if (lo && hi && lo.text !== hi.text) sub += "  " + lo.text + " - " + hi.text;
                }
                text.appendChild(el("p", "bl4dex_stat_label", sub));
                tile.appendChild(text);
                grid.appendChild(tile);
            });
            d.appendChild(grid);
        } else if (!(r && r.status)) {
            d.appendChild(el("p", "bl4dex_detail_note", "Roll one at your level to see its card stats."));
        }
        if (r && r.status && r.status !== "rolling") d.appendChild(el("p", "bl4dex_detail_note bl4dex_bad", r.status));
    }

    // "STATS AT YOUR LEVEL" and the roll buttons
    function rollHead(item, r, rolls) {
        var head = el("div", "bl4dex_stats_head");
        head.appendChild(el("p", "bl4dex_section bl4dex_stats_title", "STATS AT YOUR LEVEL" + (rolls.length > 1 ? "  (" + rolls.length + " ROLLS)" : "")));
        var buttons = el("div", "bl4dex_roll_buttons");
        if (r && r.status === "rolling") {
            buttons.appendChild(chip("Rolling...", true, function () {}));
        } else {
            buttons.appendChild(chip(rolls.length ? "Roll again" : "Roll one", false, function () {
                send({ do: "roll", item: item.key, count: 1 });
            }));
            buttons.appendChild(chip("Roll 10", false, function () {
                send({ do: "roll", item: item.key, count: 10 });
            }));
            if (rolls.length) {
                buttons.appendChild(chip("Clear", false, function () {
                    send({ do: "roll_clear", item: item.key });
                }));
            }
        }
        head.appendChild(buttons);
        return head;
    }

    // Under the card: an error, the ranges over several rolls
    function rollAfter(d, r, rolls) {
        if (r && r.status && r.status !== "rolling") d.appendChild(el("p", "bl4dex_detail_note bl4dex_bad", r.status));
        if (rolls.length > 1) rangeList(d, rolls);
    }

    // A roll changed (rolling, its result, its picture, a refine) while the details show that item's
    // card: change what depends on it in place. Rebuilding the pane took the card widget out and
    // hid it until it was bound again, and made a new picture element: both flashed, two or three
    // times per roll. False when it can't (another item, no card yet): the caller renders it all.
    function patchRoll() {
        var rv = ui.rollView;
        if (!rv || !rv.holder || !rv.head || view.tab !== "items" || current.items !== rv.key) return false;
        var item = data.itemByKey[rv.key];
        var widget = cardWidget(0);
        if (!item || !widget || widget.bl4dexHolder !== rv.holder || !rv.holder.parentNode) return false;
        var got = rollsOf(item), r = got.r, rolls = got.rolls;
        var latest = rolls.length ? rolls[rolls.length - 1] : null;
        if (!latest || !latest.card) return false;
        var head = rollHead(item, r, rolls);
        head.classList.add("bl4dex_stats_head_card");
        rv.head.parentNode.replaceChild(head, rv.head);
        rv.head = head;
        rv.after.innerHTML = "";
        rollAfter(rv.after, r, rolls);
        // Effect numbers come from the latest roll
        rv.effects.forEach(function (e) {
            var text = effectText(item, e.text);
            if (text === e.shown) return;
            var node = markupElement("bl4dex_effect", text);
            e.node.parentNode.replaceChild(node, e.node);
            e.node = node;
            e.shown = text;
        });
        var url = itemPictureFor(item, true);
        if (rv.img && url && url !== rv.imgUrl) {
            rv.img.style.backgroundImage = url;
            rv.imgUrl = url;
        }
        // The card: a new model into the widget where it is
        var card = JSON.stringify(latest.card);
        if (card !== rv.card) {
            rv.card = card;
            widget.bl4dexCard = latest.card;
            if (setCardModel(latest.card, widget.bl4dexModel)) {
                later(function () { refreshWidget(widget, latest.card, 0); }, 0);
            }
        }
        return true;
    }

    // Each stat's lowest and highest value over the rolls (only the ones that vary)
    function rangeList(d, rolls) {
        var last = rolls[rolls.length - 1].lines;
        var shown = false;
        last.forEach(function (line) {
            var lo = null, hi = null;
            rolls.forEach(function (one) {
                (one.lines || []).forEach(function (l) {
                    if (l.key !== line.key) return;
                    if (lo === null || l.value < lo.value) lo = l;
                    if (hi === null || l.value > hi.value) hi = l;
                });
            });
            if (!lo || !hi || lo.text === hi.text) return;
            if (!shown) {
                d.appendChild(el("p", "bl4dex_section", "RANGE OVER " + rolls.length + " ROLLS"));
                shown = true;
            }
            var row = el("div", "bl4dex_range_row");
            row.appendChild(el("p", "bl4dex_range_label", line.label || line.key.replace(/^uistat_/, "").replace(/_/g, " ")));
            row.appendChild(el("p", "bl4dex_range_value", lo.text + "  -  " + hi.text));
            d.appendChild(row);
        });
    }

    // The game's own item card widget, bound to our data model (python card.py) instead of the
    // game's. Made the way WidgetMgr.AttachInstance makes the game's: a clone of the card archetype,
    // its bindings pointed at "data_<ident>_<n>". It's a real Widget_ItemCard, so the card script and
    // other mods' card hooks (Item Cards' skill details) work on it too. If the archetype isn't
    // there, a copy of the card's markup is bound instead (no card script).
    var CARD_HTML = "coui://uiresources/item_card/item_card.html";
    var CARD_IDENT = "bl4dex_card";
    var CARD_MODEL = "data_bl4dex_card_0";

    // inst: 0 the details pane's card, 1 the hover card (each its own widget and model)
    function cardModelName(inst) {
        return "data_" + CARD_IDENT + "_" + (inst || 0);
    }

    function cardWidget(inst) {
        inst = inst || 0;
        var MODEL = cardModelName(inst);
        dex.cardWidgets = dex.cardWidgets || {};
        if (inst === 0 && dex.cardWidget && !dex.cardWidgets[0]) dex.cardWidgets[0] = dex.cardWidget;
        if (dex.cardWidgets[inst]) return dex.cardWidgets[inst];
        var maps = typeof WidgetMgr !== "undefined" && WidgetMgr && WidgetMgr.ArchetypeDivMap;
        var arch = maps && maps.get && maps.get("Widget_ItemCard");
        if (!arch) return null;
        var w = arch.cloneNode(true);
        if (w.constructor !== arch.constructor) return null;
        w.classList.add("widget");
        if (typeof FixupDataBindValues === "function") {
            FixupDataBindValues(w, MODEL, CARD_IDENT);
        } else {
            var nodes = [w].concat(Array.prototype.slice.call(w.querySelectorAll("*")));
            nodes.forEach(function (node) {
                for (var i = 0; i < node.attributes.length; i++) {
                    var a = node.attributes[i];
                    if (a.name.indexOf("data-bind") === 0 && a.value.indexOf("$data") >= 0) {
                        node.setAttribute(a.name, a.value.split("$data").join(MODEL));
                    }
                }
            });
        }
        w.dataset.WidgetIdent = CARD_IDENT;
        w.dataset.ClassIdent = "Widget_ItemCard";
        w.dataset.DataModelId = MODEL;
        w.dataset.WidgetInstId = CARD_IDENT + "_" + inst;
        w.dataset.InstId = inst;
        w.bl4dexModel = MODEL;
        dex.cardWidgets[inst] = w;
        return w;
    }

    // What the game's DataUpdated event does for its cards; the card positions itself on screen
    // when not mouse positioned, so undo that and stop its per-frame repositioning
    function refreshWidget(w, card, tries) {
        // A newer card took the widget: its own refresh runs; this one's retries would unhide it early
        if (!alive() || !w.parentNode || (w.bl4dexCard && w.bl4dexCard !== card)) return;
        var MODEL = w.bl4dexModel || CARD_MODEL;
        w.DataModel = window[MODEL];
        w.DamageType1 = null;
        w.DamageType2 = null;
        try {
            w.Event_DataUpdated();
        } catch (err) {
            log("card update failed: " + err);
        }
        if (w.ClearCurrentRAF) w.ClearCurrentRAF();
        // In the dex's flow, not where the HUD or a tooltip would put it
        place(w);
        w.classList.remove("inactive");
        var tip = w.querySelector(".item_card_tooltip");
        if (tip) {
            tip.classList.add("tooltip_active");
            place(tip);
        }
        // Shown once its bindings show our data: the card's name reads the model's. Until then (the
        // markup's placeholders, 9,999) push the model again and look a moment later. This also
        // covers the element icons, a component that may not be ready on the first pass.
        dex.cardStats = dex.cardStats || { bound: 0, gaveUp: 0, tries: [] };
        if (tries > 0 && cardBound(w, card)) {
            w.parentNode.classList.remove("bl4dex_card_pending");
            w.parentNode.style.minHeight = "";
            later(function () {
                var h = Math.round(w.getBoundingClientRect().height);
                if (h) w.bl4dexHeight = h;
            }, 50);
            dex.cardStats.bound++;
            dex.cardStats.tries = dex.cardStats.tries.concat([tries]).slice(-20);
            return;
        }
        if (tries < 30) {
            later(function () {
                if (window[MODEL]) engine.updateWholeModel(window[MODEL]);
                engine.synchronizeModels();
                refreshWidget(w, card, tries + 1);
            }, tries ? 50 : 16);
        } else {
            w.parentNode.classList.remove("bl4dex_card_pending");
            w.parentNode.style.minHeight = "";
            log("card never showed its data");
            dex.cardStats.gaveUp++;
        }
    }

    function cardBound(w, card) {
        var name = w.querySelector('[data-bind-value="{{' + (w.bl4dexModel || CARD_MODEL) + '.name}}"]');
        return !!name && name.textContent.trim() === String(card.name).trim();
    }

    function place(node) {
        node.style.position = "relative";
        node.style.left = "0px";
        node.style.top = "0px";
        node.style.right = "auto";
        node.style.bottom = "auto";
        node.style.transform = "none";
        node.style.margin = "0px";
    }

    function cardMarkup(done) {
        if (dex.cardMarkup !== undefined) {
            done(dex.cardMarkup);
            return;
        }
        ns.get(CARD_HTML, function (text) {
            var start = text ? text.indexOf('<div name="Container"') : -1;
            var end = text ? text.lastIndexOf("</gbx-tooltip>") : -1;
            dex.cardMarkup = start >= 0 && end > start ? text.slice(start, end).split("$data").join(CARD_MODEL) : null;
            if (!dex.cardMarkup) log("item card markup not found");
            done(dex.cardMarkup);
        });
    }

    function setCardModel(model, name) {
        name = name || CARD_MODEL;
        if (typeof engine === "undefined" || !engine.createJSModel) return false;
        if (!window[name]) {
            engine.createJSModel(name, model);
        } else {
            var m = window[name];
            for (var k in model) m[k] = model[k];
            engine.updateWholeModel(m);
        }
        engine.synchronizeModels();
        return true;
    }

    function gameCard(d, card, inst) {
        var holder = el("div", "bl4dex_game_card");
        d.appendChild(holder);
        var widget = cardWidget(inst);
        if (widget) {
            // Hidden until our data is bound (the markup's placeholders, 9,999, show otherwise).
            // The page rebuilds this pane on every change, taking the card out and back in, and its
            // bindings only pick the model up again on a model update: so attach first, then push
            holder.classList.add("bl4dex_card_pending");
            widget.classList.remove("hud_master_perspective");
            // The widget is one node moved between holders. Moved into a holder Gameface hasn't laid
            // out yet, it drew a frame at the screen's top left (the holder's unset 0,0; dev/ui_live
            // probe 2026-10-24): take it out now, keep its height, and attach once the holder has a box
            if (widget.bl4dexHeight) holder.style.minHeight = widget.bl4dexHeight + "px";
            if (widget.parentNode) widget.parentNode.removeChild(widget);
            widget.bl4dexHolder = holder;
            widget.bl4dexCard = card;
            attachCard(holder, widget, card, 0);
            return holder;
        }
        if (inst) return holder;  // the hover card needs the real widget
        if (!setCardModel(card)) return holder;
        cardMarkup(function (markup) {
            if (!markup || !alive() || !setCardModel(card)) return;
            holder.innerHTML = '<div class="item_card_tooltip tooltip_container tooltip_text_shadow tooltip_active">' + markup + "</div>";
            engine.synchronizeModels();
            presentElements(holder, card, 0);
        });
        return holder;
    }

    function attachCard(holder, widget, card, tries) {
        if (!alive() || widget.bl4dexHolder !== holder || !holder.parentNode) return;
        if (!holder.getBoundingClientRect().width && tries < 10) {
            requestAnimationFrame(function () { attachCard(holder, widget, card, tries + 1); });
            return;
        }
        holder.appendChild(widget);
        if (!setCardModel(card, widget.bl4dexModel)) return;
        later(function () { refreshWidget(widget, card, 0); }, 0);
    }

    // The element row isn't data-bound: the game's card script (Widget_ItemCard.UpdateDamageType /
    // SetDamageTypePresentation) sets it up, so do what it does. The icons are a component that
    // loads a moment later: retry for a while.
    function presentElements(holder, card, tries) {
        if (!alive() || !holder.parentNode || !card.showelement1) return;
        var backing = holder.querySelector(".ic_element_backing");
        var text = holder.querySelector(".item_card_element_text");
        if (backing) backing.className = "ic_element_backing " + card.damagetype1;
        if (text) {
            text.className = "item_card_element_text " + card.damagetype1;
            if (typeof MarkupMgr !== "undefined") text.innerHTML = MarkupMgr.ResolveMarkupText(card.elementtext);
            else text.textContent = card.elementtext;
        }
        var done = true;
        [[".item_card_element1", card.damagetype1, true],
         [".item_card_element2", card.damagetype2, card.showelement2 && card.damagetype2 && card.damagetype2 !== card.damagetype1]].forEach(function (slot) {
            var icon = holder.querySelector(slot[0] + " elemental-damagetype");
            if (!icon || typeof icon.SetElement !== "function") {
                done = false;
                return;
            }
            try {
                if (slot[2] && slot[1]) {
                    icon.SetElement(slot[1]);
                    if (icon.SetFrame) icon.SetFrame(true);
                    if (icon.Show) icon.Show();
                } else if (icon.Hide) {
                    icon.Hide();
                }
            } catch (err) {}
        });
        if (!done && tries < 20) later(function () { presentElements(holder, card, tries + 1); }, 100);
    }

    // An effect text with its numbers: from the latest roll where it has them, otherwise the
    // placeholder ("{dmg}") is dropped
    function effectText(item, text) {
        var rolls = rollsOf(item).rolls;
        var fills = {};
        var level = state && state.fills && state.fills.item === item.key ? state.fills.fills : null;
        Object.keys(level || {}).forEach(function (k) {
            fills[k] = level[k];
        });
        var rolled = rolls.length ? rolls[rolls.length - 1].fills || {} : {};
        Object.keys(rolled).forEach(function (k) {
            fills[k] = rolled[k];
        });
        return text.replace(/\s*\{([A-Za-z0-9_]+)\}/g, function (all, name) {
            return fills[name] !== undefined ? (all.charAt(0) === "{" ? "" : " ") + fills[name] : "";
        });
    }

    // A legendary class mod's skills: the ones its body can roll (up to how many points), or with
    // the latest roll, the points it got (skills it didn't roll dimmed)
    // A class mod's every possible skill at its best roll (the card above shows the latest roll's)
    function skillSection(d, item) {
        var skills = item.skills || [];
        if (!skills.length) return;
        d.appendChild(el("p", "bl4dex_section", "SKILLS IT CAN ROLL"));
        skills.forEach(function (s) {
            var row = el("div", "bl4dex_skill");
            var name = el("div", "bl4dex_skill_head");
            name.appendChild(el("p", "bl4dex_skill_name", s.name));
            name.appendChild(el("p", "bl4dex_skill_points", "up to +" + s.max));
            row.appendChild(name);
            if (s.desc) row.appendChild(markupElement("bl4dex_effect bl4dex_skill_desc", s.desc));
            d.appendChild(row);
        });
    }

    function itemDetails(item) {
        var shown = itemShown(item);
        var cols = columns("item");
        var d = cols.left;
        var right = cols.right;
        // The picture above the name block; the item card to their right
        var picture = itemPicture(item, shown);
        d.appendChild(picture);
        // What a roll changes in place (patchRoll); rollSection adds the card's parts
        var rv = { key: item.key, img: null, imgUrl: null, effects: [] };
        if (shown) {
            rv.img = picture.querySelector(".bl4dex_picture");
            rv.imgUrl = itemPictureFor(item, shown);
            ui.rollView = rv;
        }
        var names = el("div", "bl4dex_item_names");
        names.appendChild(el("p", "bl4dex_detail_no", item.rarity.toUpperCase()));
        names.appendChild(el("p", "bl4dex_detail_title bl4dex_name_" + item.rarity.toLowerCase(), shown ? item.name : "???"));
        names.appendChild(el("p", "bl4dex_detail_meta", typeLine(item, shown)));
        if (shown) {
            // Its unique effect, then its red text (the card on the right repeats both)
            (item.effects || []).forEach(function (text) {
                var filled = effectText(item, text);
                var node = markupElement("bl4dex_effect", filled);
                rv.effects.push({ node: node, text: text, shown: filled });
                names.appendChild(node);
            });
            (item.red || []).forEach(function (text) {
                names.appendChild(el("p", "bl4dex_red_text", text));
            });
        }
        d.appendChild(names);
        // With a roll, the game's card below shows the red text, effects and skills itself
        var rolledCard = shown && (function () {
            var rolled = rollsOf(item).rolls;
            return rolled.length && rolled[rolled.length - 1].card;
        })();
        // Tell Python which item this is (it may not be the pick: a filter can hide that): its rolls
        // are what the state then reports, and it rolls one if there's none yet
        if (shown && dex.showing !== item.key) {
            dex.showing = item.key;
            send({ do: "show", item: item.key });
        }
        if (shown && rolledCard) {
            // A class mod's possible skills stay below its card (the card lists the rolled ones)
            rollSection(right, item);
            skillSection(right, item);
        } else if (shown) {
            // No card yet (it's being rolled): its texts where the card goes
            var left = d;
            d = right;
            // Texts the card picks by the rolled parts: the latest roll's, else how many there are
            var variants = item.variants || [];
            if (variants.length) {
                var rolled = rollsOf(item).rolls;
                var on = rolled.length ? rolled[rolled.length - 1].variants || [] : [];
                dex.showVariants = dex.showVariants || {};
                var all = !!dex.showVariants[item.key];
                if (on.length && !all) {
                    on.forEach(function (i) {
                        if (variants[i]) d.appendChild(markupElement("bl4dex_effect", effectText(item, variants[i].text)));
                    });
                } else {
                    var row = el("div", "bl4dex_variant_row");
                    row.appendChild(el("p", "bl4dex_detail_note", variants.length === 1
                        ? "Plus a part effect only some rolls have."
                        : "Plus one of " + variants.length + " part effects, depending on the parts it rolls."));
                    row.appendChild(chip(all ? "Hide" : "Show all", all, function () {
                        dex.showVariants[item.key] = !all;
                        render();
                    }));
                    d.appendChild(row);
                    if (all) {
                        variants.forEach(function (v) {
                            d.appendChild(markupElement("bl4dex_effect bl4dex_variant", effectText(item, v.text)));
                        });
                    }
                }
            }
            rollSection(right, item);
            skillSection(d, item);
            d = left;
        }
        var sources = [];
        if (item.world) sources.push("World drop: can drop from anything.");
        if (item.reward) sources.push("Mission reward: some missions give it.");
        if (!item.bosses && !sources.length) sources.push("Not in any boss's or world drop table (an event, vendor or activity reward).");
        sources.forEach(function (s) {
            d.appendChild(el("p", "bl4dex_detail_note", s));
        });
        if (!item.bosses) return;
        d.appendChild(el("p", "bl4dex_section", "DROPPED BY"));
        if (!shown) {
            d.appendChild(el("p", "bl4dex_detail_note", "Beat a boss that drops it to see it here."));
        }
        item.bosses.forEach(function (id) {
            var b = data.bossById[id];
            if (b) d.appendChild(dropperCard(b, item, shown));
        });
        d.appendChild(el("p", "bl4dex_detail_note", uvhNote()));
    }

    // A boss that drops the item: its picture, its chances (per kill at the player's UVH level, in its
    // Big Encore) and Waypoint / Travel to its Big Encore machine, as the item cards' keys do
    function dropperCard(b, item, shown) {
        var drop = null;
        b.drops.forEach(function (x) {
            if (x.item === item.key) drop = x;
        });
        var bShown = bossShown(b);
        var card = el("div", "bl4dex_dropper" + (bShown ? "" : " bl4dex_locked"));
        var pic = portrait(b, "bl4dex_dropper_picture");
        onClick(pic, function () {
            goTo("bosses", b.id);
        });
        card.appendChild(pic);
        var info = el("div", "bl4dex_dropper_info");
        var head = el("div", "bl4dex_dropper_head");
        head.appendChild(el("p", "bl4dex_dropper_name", bShown ? b.name : "???"));
        if (beaten(b.id)) head.appendChild(el("p", "bl4dex_tag bl4dex_tag_beaten", "BEATEN"));
        onClick(head, function () {
            goTo("bosses", b.id);
        });
        info.appendChild(head);
        info.appendChild(el("p", "bl4dex_cell_sub", b.area === b.region ? b.region : b.area + ", " + b.region));
        info.appendChild(chanceLine(ICON_KILL, "Per kill", shown && drop ? pct(uvhChance(drop)) : "?", uvhLabel(), ""));
        if (b.encore) {
            info.appendChild(chanceLine(ICON_ENCORE, "Big Encore", shown && drop ? pct(drop.encore) : "?", "", "bl4dex_chance_encore"));
        }
        tripButtons(info, b);
        card.appendChild(info);
        return card;
    }

    // Waypoint / Travel to the boss's Big Encore machine (travel.py), on its card under an item and
    // on its own page; nothing for bosses without one (open-world bosses)
    function tripButtons(parent, b) {
        if (!canTravel(b)) return;
        var buttons = el("div", "bl4dex_dropper_buttons");
        buttons.appendChild(chip("Waypoint", false, function () {
            send({ do: "waypoint", boss: b.id });
        }));
        buttons.appendChild(chip("Travel", false, function () {
            send({ do: "travel", boss: b.id });
        }));
        parent.appendChild(buttons);
        var trip = state && state.trip;
        if (trip && trip.boss === b.id && trip.state === "pinned") {
            parent.appendChild(el("p", "bl4dex_dropper_pinned", "Waypoint set on its Big Encore machine"));
        }
    }

    //  [icon] Per kill     4.2%   UVH 3
    function chanceLine(icon, label, rate, note, cls) {
        var row = el("div", "bl4dex_chance" + (cls ? " " + cls : ""));
        var ico = el("div", "bl4dex_chance_icon");
        ico.style.backgroundImage = "url(" + icon + ")";
        row.appendChild(ico);
        row.appendChild(el("p", "bl4dex_chance_label", label));
        row.appendChild(el("p", "bl4dex_chance_rate", rate));
        if (note) row.appendChild(el("p", "bl4dex_chance_note", note));
        return row;
    }

    // endregion
    // region Toast

    var toastTimer = null;
    function toast(text) {
        if (!ui.toast || !text) return;
        ui.toast.textContent = text;
        ui.toast.classList.remove("bl4dex_hidden");
        if (toastTimer !== null) clearTimeout(toastTimer);
        toastTimer = setTimeout(function () {
            toastTimer = null;
            if (ui.toast) ui.toast.classList.add("bl4dex_hidden");
        }, TOAST_MS);
    }

    // endregion
    // region Link

    var pollTimer = null;
    function schedule(ms) {
        if (pollTimer !== null) clearTimeout(pollTimer);
        pollTimer = setTimeout(function () {
            pollTimer = null;
            if (alive()) poll();
        }, ms);
    }
    function pollSoon() {
        schedule(20);
    }

    // done (optional): called once this poll has answered
    function poll(done) {
        ext.get(KEY, view.open, function (snap) {
            if (!alive()) return;
            if (snap && snap.state) onState(snap.state);
            schedule(!view.open ? POLL_IDLE_MS : state && state.typing ? POLL_TYPING_MS : POLL_OPEN_MS);
            if (done) done();
        });
    }

    // The page just woke up (one of the game's menus opened): the Mod Menu hides it until this
    // poll answers, so F8's screen shows without the pause menu first (menu.js, ns.wake)
    function onWake(done) {
        if (pollTimer !== null) {
            clearTimeout(pollTimer);
            pollTimer = null;
        }
        poll(done);
    }
    (ns.wake || (ns.wake = { hooks: {} })).hooks[KEY] = onWake;

    function onState(next) {
        var first = state === null;
        state = next;
        if (freshView && next.view) {
            freshView = false;
            KEPT.forEach(function (k) {
                if (next.view[k] !== undefined && next.view[k] !== null) view[k] = next.view[k];
            });
            if (!view.scroll || typeof view.scroll !== "object") view.scroll = { bosses: 0, items: 0 };
            if (view.open) render();
        }
        freshView = false;
        if (first && dex.lastOpenId === undefined) {
            // Fresh page: only act on requests made from now on (or a fresh one, just below)
            dex.lastClose = next.close;
            dex.lastMenusClosed = next.menus_closed;
            dex.lastToast = next.toast.id;
        }
        if (next.close !== dex.lastClose) {
            dex.lastClose = next.close;
            if (view.open) close();
        }
        if (next.menus_closed !== dex.lastMenusClosed) {
            // The game's menus closed since we last looked (we may have slept through it)
            dex.lastMenusClosed = next.menus_closed;
            if (view.open) hide();
        }
        if (next.open.id !== dex.lastOpenId) {
            dex.lastOpenId = next.open.id;
            if (next.open.fresh) {
                open(next.open.own, next.open.boss);
                // Python keeps a request fresh until we say we've shown it (the page may take a
                // while to wake or reload its scripts)
                send({ do: "opened", id: next.open.id });
            }
        }
        var trip = next.trip || { id: 0 };
        if (first && dex.lastTrip === undefined) dex.lastTrip = trip.id;
        if (trip.id !== dex.lastTrip) {
            dex.lastTrip = trip.id;
            // There (same map): close the dex and the game's menu. Off to another map: its loading
            // screen takes the menus down
            if (view.open && trip.state === "arrived") {
                hide();
                if (ns.closeGameMenu) ns.closeGameMenu();
                else ext.http("POST", "/action", JSON.stringify({ op: "close_menus" }), null);
            } else if (view.open && trip.state === "travelling") {
                hide();
            }
        }
        if (next.toast.id !== dex.lastToast) {
            dex.lastToast = next.toast.id;
            if (view.open) toast(next.toast.text);
        }
        if (!view.open) return;
        // Only the shown item's roll changed: patch its card in place (patchRoll), else redraw it all
        var base = JSON.stringify([next.defeated, next.unlock, next.search, next.typing, next.character, next.outdated, next.fills, next.owned_v, next.obtained, next.uvh, next.trip]);
        var sig = base + "|" + JSON.stringify(next.roll || null);
        if (sig !== dex.sig) {
            var onlyRoll = !!dex.sig && base === dex.sigBase;
            dex.sig = sig;
            dex.sigBase = base;
            if (!(onlyRoll && patchRoll())) render();
        }
        updateHover();
    }

    // endregion
    // region Stylesheet and pause menu entry

    function checkCss() {
        ns.get(BASE + "dex.css", function (css) {
            if (!alive()) return;
            if (css && css !== dex.cssText) {
                dex.cssText = css;
                var style = document.getElementById("bl4dex_css");
                if (!style) {
                    style = el("style");
                    style.id = "bl4dex_css";
                    document.head.appendChild(style);
                }
                style.textContent = css;
            }
            later(checkCss, CSS_MS);
        });
    }

    // The lists of boss and item pictures; re-read every few seconds so a running bake shows up as it goes
    function readIndex(text) {
        // {id: version} (a list of ids from older bakes)
        var map = {};
        try {
            var index = JSON.parse(text);
            if (Array.isArray(index)) {
                index.forEach(function (id) {
                    map[id] = 1;
                });
            } else {
                map = index;
            }
        } catch (err) {}
        return map;
    }
    // dex.<field>: the full pictures and their small copies (small/, picture.py), for thumbnails
    var PICTURE_INDEXES = [
        { url: PICTURES + "index.json", field: "pictures" },
        { url: PICTURES + SMALL + "index.json", field: "smallPictures" },
        { url: ITEM_PICTURES + "index.json", field: "itemPictures" },
        { url: ITEM_PICTURES + SMALL + "index.json", field: "smallItemPictures" }
    ];
    function checkPictures() {
        var changed = false;
        function next(i) {
            if (!alive()) return;
            if (i === PICTURE_INDEXES.length) {
                if (changed) render();
                later(checkPictures, PICTURES_MS);
                return;
            }
            var entry = PICTURE_INDEXES[i];
            ns.get(entry.url, function (text) {
                if (text && text !== dex[entry.field + "Text"]) {
                    dex[entry.field + "Text"] = text;
                    dex[entry.field] = readIndex(text);
                    changed = true;
                }
                next(i + 1);
            });
        }
        next(0);
    }

    // A "Kairosdex" entry in the pause menu, under the Mod Menu's "Mods" (or Options)
    function injectPauseEntry() {
        var list = document.querySelector('[data-input-id="menu_pause_list"]');
        if (!list || list.querySelector(".bl4dex_pause_entry")) return;
        var options = list.querySelector('div[name="Options Button"]');
        var optionsBtn = options && options.firstElementChild;
        if (!options || !optionsBtn) return;
        var after = list.querySelector(".bl4mm_pause_entry") || options;
        var entry = el("div", options.className + " bl4dex_pause_entry");
        var slot = el("div", optionsBtn.className);
        var b = button("Kairosdex", function () {
            open(false, null);
        });
        var theirs = optionsBtn.querySelector(".button_txt");
        var label = b.querySelector(".button_txt");
        if (theirs && label) label.className = theirs.className;
        // Their button is sized by its tag, which our div copy doesn't match: copy the size over
        var native = options.querySelector(".list_btn");
        if (native) {
            var cs = getComputedStyle(native);
            if (cs.width && cs.width !== "auto") b.style.width = cs.width;
            if (cs.height && cs.height !== "auto") b.style.height = cs.height;
            b.style.position = "relative";
            b.style.flex = "0 0 auto";
        }
        b.classList.remove("bl4dex_btn");
        slot.appendChild(b);
        entry.appendChild(slot);
        after.parentNode.insertBefore(entry, after.nextSibling);
    }

    function watchPauseMenu() {
        try {
            injectPauseEntry();
        } catch (err) {
            log("pause entry: " + err);
        }
        later(watchPauseMenu, 500);
    }

    // endregion

    ext.teardowns[KEY] = function () {
        dex.gen = 0;
        if (ns.wake && ns.wake.hooks[KEY] === onWake) delete ns.wake.hooks[KEY];
        timers.forEach(clearTimeout);
        if (pollTimer !== null) clearTimeout(pollTimer);
        if (toastTimer !== null) clearTimeout(toastTimer);
        if (saveTimer !== null) clearTimeout(saveTimer);
        var nodes = document.querySelectorAll(".bl4dex_root, .bl4dex_pause_entry");
        for (var i = 0; i < nodes.length; i++) nodes[i].parentNode.removeChild(nodes[i]);
        docListeners.forEach(function (l) {
            document.removeEventListener(l[0], l[1]);
        });
    };

    var wasOpen = view.open;
    view.open = false;
    checkCss();
    checkPictures();
    loadData(function () {
        try {
            build();
        } catch (err) {
            log("build failed: " + err + " " + (err && err.stack));
            return;
        }
        watchPauseMenu();
        watchScroll();
        log("ready: " + data.bosses.length + " bosses, " + data.items.length + " items");
        poll();
        // A reload keeps the screen open
        if (wasOpen) open(view.own, null);
    });
})();
