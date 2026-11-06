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
    // The game's region mark (its "entering a region" message): the Bosses tab's group headers
    var ICON_REGION = "coui://uiresources/ingame_message/assets/region_icon.png";

    if (ext.teardowns[KEY]) {
        try {
            ext.teardowns[KEY]();
        } catch (err) {}
    }
    // Everything that must survive a reload lives on dex; each copy takes a new, ever higher number
    var dex = ns.dex || (ns.dex = {});
    dex.counter = Math.max(dex.counter || 0, dex.gen || 0) + 1;
    var gen = dex.counter;
    // The Collection as the last copy of this script read it may be in an older shape: read it again
    dex.coll = null;
    dex.collText = null;
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
    // The Collection (its binder): coll is the picked item, copy the copy whose card shows; ownWho
    // the holder shown ("" everyone), ownShow have (the default: the user's ask 2026-11-06) / all / missing, ownGroup and ownRev the grouping
    // and its direction, copySort how the picked item's copies are listed (copyHideGone: without the
    // sold and dropped ones).
    var KEPT = ["tab", "boss", "region", "kind", "onlyBoss", "sortBoss", "scroll",
                "coll", "copy", "copyOpen", "ownWho", "ownShow", "ownGroup", "ownRev", "ownType", "ownMaker", "ownPearl", "copySort",
                "copyHideGone", "ownLevel", "ownLevelMode", "partsFilter"];  // prettier-ignore
    var OWN_DEFAULTS = { coll: null, copy: null, copyOpen: null, ownWho: "", ownShow: "have", ownGroup: "type", ownRev: false,
                         ownType: "", ownMaker: "", ownPearl: false, copySort: "level",
                         copyHideGone: false, ownLevel: "", ownLevelMode: "" };  // prettier-ignore
    var freshView = !dex.view;
    var view = dex.view || (dex.view = {
        open: false, own: false, tab: "bosses", boss: null,
        region: "", kind: "", onlyBoss: false, sortBoss: "no",
        partsFilter: "all", scroll: { bosses: 0, collection: 0 }
    });  // prettier-ignore
    if (!view.scroll) view.scroll = { bosses: 0, collection: 0 };  // a view from before scrolls were kept
    function ownDefaults() {
        // A view from before the Items tab was merged into the Collection (2026-11-06)
        if (view.tab === "items") view.tab = "collection";
        // A view from before the binder (the Collection was a list of serials then: its pick is an entry id)
        if (view.ownGroup === undefined) view.coll = null;
        for (var k in OWN_DEFAULTS) if (Object.prototype.hasOwnProperty.call(OWN_DEFAULTS, k) && view[k] === undefined) view[k] = OWN_DEFAULTS[k];
    }
    ownDefaults();
    if (view.only !== undefined) {
        view.onlyBoss = !!view.only;
        delete view.only;
    }
    var state = null;
    var data = null;
    var ui = {};

    // region Data

    var BOSS_SORTS = [
        ["no", "Number"], ["name", "Name"], ["area", "Area"], ["beaten", "Beaten first"], ["drops", "Most drops"]
    ];  // prettier-ignore
    // (The Collection has no sort button: its binder's GROUP row, with Reverse, orders it)

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
        // The Collection tab is the account's (every holder's): a copy there is proof it was had,
        // even with "Per-character Kairosdex" on and this character never having had one. With one
        // holder picked it's that holder's: one it never had is "???" though another has it (the
        // user's ask 2026-11-06)
        if (view.tab === "collection" && dex.coll) {
            if (view.ownWho) return ownerHad(item.key);
            if (dex.coll.byKey[item.key]) return true;
        }
        if (obtainedMode()) return owned(item.key);
        if (!item.bosses) return true;
        for (var i = 0; i < item.bosses.length; i++) if (beaten(item.bosses[i])) return true;
        return false;
    }
    // "Spoiler free" (a mod option, off by default): a hidden item gives nothing away (no outline,
    // type, class or rarity: "?" and "???"), and an unbeaten boss shows no outline
    function spoilerFree() {
        return !!(state && state.spoiler_free);
    }
    function bare(item) {
        return spoilerFree() && !itemShown(item);
    }
    function rarityOf(item) {
        return bare(item) ? "hidden" : item.rarity.toLowerCase();
    }
    // A "?" where a hidden picture would be (Spoiler free)
    function unknownMark(box) {
        box.classList.add("bl4dex_unknown");
        box.appendChild(el("p", "bl4dex_unknown_mark", "?"));
        return box;
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
        var parts = [item.world ? "world drop" : "", item.reward ? "mission reward" : ""];
        if (!bare(item)) parts.push(item.type, item["class"], item.hero);
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
    function bossList() {
        var words = terms();
        var list = data.bosses.filter(function (b) {
            return bossFiltered(b) && (!words.length || matches(hayOf("b", b.id, function () { return bossHaystack(b); }), words));
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

    // region Collection data (bl4_weapondex/collection.py)
    // Every legendary and pearl had, one record per copy: Python writes them to collection.json (no
    // serials in it) and bumps state.collection.v; the shown copy's card and picture come in
    // state.collection.shown, made when it's picked. Identical copies share a serial id (sid): the
    // card and the parts screen go by that.

    var BANK = "bank";
    var VH_ART = "coui://uiresources/_shared/assets/ico_ui_art_vault_hunters/";
    // Each Vault Hunter's portrait icon, as the game's menus show it (the DLC heroes' are in their DLC)
    var CLASS_ICONS = {
        Char_ExoSoldier: VH_ART + "player_class_exo_soldier.png",
        Char_Paladin: VH_ART + "player_class_paladin.png",
        Char_DarkSiren: VH_ART + "player_class_dark_siren.png",
        Char_Gravitar: VH_ART + "player_class_gravitar.png",
        Char_RoboDealer: "cowbell://uiresources/portrait_icons/player_class_robodealer.png",
        Char_CorpoHacker: "harmonica://uiresources/corpo_hacker_icons/portrait/player_class_loveless.png"
    };
    var ICON_BANK = "coui://uiresources/_shared/assets/ico_ui_art_item_state/ico_ui_art_bank.png";
    var TYPE_ART = "coui://uiresources/_shared/assets/ico_ui_art_weapon_small/";
    var TYPE_ICONS = {
        "Pistol": "ico_ui_art_pistol_small", "SMG": "ico_ui_art_smg_small", "Assault Rifle": "ico_ui_art_assault_small",
        "Shotgun": "ico_ui_art_shotgun_small", "Sniper Rifle": "ico_ui_art_sniper_small", "Heavy": "ico_ui_art_heavy_small",
        "Shield": "ico_ui_art_shields_small", "Grenade": "ico_ui_art_gadget_small", "Repkit": "ico_ui_art_repkit_small",
        "Class Mod": "ico_ui_art_class_mod_small"
    };  // prettier-ignore

    function loadCollection() {
        var now = Date.now();
        if (dex.collLoading || now - (dex.collTried || 0) < 700) return;
        dex.collLoading = true;
        dex.collTried = now;
        ns.get(BASE + "collection.json", function (text) {
            dex.collLoading = false;
            if (!alive() || !text || !data) return;
            var raw = null;
            try {
                raw = JSON.parse(text);
            } catch (err) {}
            // Not the copy the state names yet (Python writes it a moment later): the next poll past
            // the 700 ms gap asks again, as long as the versions differ
            if (!raw || !raw.items || text === dex.collText) return;
            dex.collText = text;
            // Rows: [copy id, item key, holder, got, level, source, gone, serial id]
            var copies = [];
            var byKey = {};
            raw.items.forEach(function (r) {
                var item = data.itemByKey[r[1]];
                if (!item) return;
                var c = { id: r[0], key: r[1], char: r[2], t: r[3] || 0, level: r[4] || 1, src: r[5] || "", gone: r[6] || 0,
                          sid: r[7], item: item };  // prettier-ignore
                copies.push(c);
                (byKey[c.key] || (byKey[c.key] = [])).push(c);
            });
            var chars = {};
            Object.keys(raw.chars || {}).forEach(function (id) {
                var v = raw.chars[id];
                chars[id] = typeof v === "string" ? { name: v } : v;  // (an older copy had names only)
            });
            dex.coll = { v: raw.v, chars: chars, copies: copies, byKey: byKey, byId: {} };
            copies.forEach(function (c) {
                dex.coll.byId[c.id] = c;
            });
            if (view.open) render();
        });
    }
    function collState() {
        return (state && state.collection) || { v: 0, char: null, shown: null };
    }
    // A holder's name: the character's own (the name it was given), with its level when another
    // character has the same name
    function charName(c) {
        if (c === BANK) return "Bank";
        var chars = (dex.coll && dex.coll.chars) || {};
        var me = chars[c];
        if (!me || !me.name) return c ? "Another character" : "Unknown";
        var twin = Object.keys(chars).some(function (o) {
            return o !== c && chars[o] && chars[o].name === me.name;
        });
        return twin && me.level ? me.name + " (Lv " + me.level + ")" : me.name;
    }
    function holderIcon(c) {
        if (c === BANK) return ICON_BANK;
        var me = dex.coll && dex.coll.chars[c];
        return (me && CLASS_ICONS[me["class"]]) || null;
    }
    function holderBadge(c, cls) {
        var url = holderIcon(c);
        var b = el("div", "bl4dex_holder" + (cls ? " " + cls : ""));
        if (url) b.style.backgroundImage = "url(" + url + ")";
        else b.appendChild(el("p", "bl4dex_holder_letter", charName(c).charAt(0)));
        return b;
    }
    // The holders to pick from: every character the saves know, the one being played first, then by
    // when each was last played (its save's date), then the bank
    function holders() {
        var counts = {};
        if (!dex.coll) return [];
        dex.coll.copies.forEach(function (c) {
            if (!c.gone) counts[c.char] = (counts[c.char] || 0) + 1;
        });
        var chars = dex.coll.chars;
        var now = collState().char;
        var ids = Object.keys(chars).sort(function (a, b) {
            return (b === now) - (a === now) || (chars[b].played || 0) - (chars[a].played || 0) || cmp(charName(a), charName(b));
        });
        ids.push(BANK);
        return ids.map(function (id) {
            return { id: id, held: counts[id] || 0 };
        });
    }
    // "Lv 55  2h ago" under a holder's name ("Now" for the one being played; nothing for the bank)
    function holderLine(id) {
        var me = dex.coll && dex.coll.chars[id];
        if (!me) return "";
        var when = "";
        if (id === collState().char) when = "Now";
        else if (me.played) {
            var s = Math.max(0, Date.now() / 1000 - me.played);
            when = s < 3600 ? Math.max(1, Math.floor(s / 60)) + "m ago" : s < DAY_S ? Math.floor(s / 3600) + "h ago" : Math.floor(s / DAY_S) + "d ago";
        }
        return (me.level ? "Lv " + me.level : "") + (me.level && when ? "   " : "") + when;
    }
    function inScope(c) {
        if (view.ownWho && c.char !== view.ownWho) return false;
        var test = levelTest(levelText());
        return !test || test(c.level);
    }
    // The Level filter as one text levelTest reads: its mode (picked: Exactly, At least, At most,
    // Between) and the number typed. The mode is a button of its own (the user's ask: "<" and ">"
    // typed weren't apparent). "" with no mode or no number. (A view from before the modes kept
    // its typed text, ">50", which reads the same.)
    var LEVEL_MODES = [["", "Any"], ["eq", "Exactly"], ["ge", "At least"], ["le", "At most"], ["range", "Between"]];
    var LEVEL_OPS = { eq: "", ge: ">=", le: "<=", range: "" };
    function levelText() {
        var n = String(view.ownLevel || "");
        if (!view.ownLevelMode) return /^[<>=]/.test(n) ? n : "";
        return n ? LEVEL_OPS[view.ownLevelMode] + n : "";
    }
    // The Level filter's text as a test of a copy's level: "50", ">50", ">=50", "<40", "<=40",
    // "40-60" (either order); null for none or text that isn't one of those (typing halfway)
    var levelTests = {};
    function levelTest(text) {
        var t = String(text || "").replace(/\s+/g, "");
        if (!t) return null;
        if (levelTests[t] !== undefined) return levelTests[t];
        var m, test = null;
        if ((m = /^(\d+)-(\d+)$/.exec(t))) {
            var lo = Math.min(+m[1], +m[2]), hi = Math.max(+m[1], +m[2]);
            test = function (l) { return l >= lo && l <= hi; };
        } else if ((m = /^(>=|<=|>|<|=)?(\d+)$/.exec(t))) {
            var n = +m[2], op = m[1] || "=";
            test = op === ">" ? function (l) { return l > n; } : op === ">=" ? function (l) { return l >= n; }
                : op === "<" ? function (l) { return l < n; } : op === "<=" ? function (l) { return l <= n; }
                : function (l) { return l === n; };  // prettier-ignore
        }
        levelTests[t] = test;
        return test;
    }
    // Whether the holder picked has (or had) the item, at any level: its "???" doesn't follow the
    // Level filter. One pass over the copies per collection and holder.
    var ownerKeys = { coll: null, who: null, keys: {} };
    function ownerHad(key) {
        if (ownerKeys.coll !== dex.coll || ownerKeys.who !== view.ownWho) {
            ownerKeys = { coll: dex.coll, who: view.ownWho, keys: {} };
            dex.coll.copies.forEach(function (c) {
                if (c.char === view.ownWho) ownerKeys.keys[c.key] = true;
            });
        }
        return !!ownerKeys.keys[key];
    }
    // An item's copies in scope: held now (best level first) and gone (sold, dropped). Kept until the
    // collection or the owner changes: every redraw asked again for every item, and an item can have
    // tens of thousands of copies to sort. (Callers don't change what they get: they sort copies.)
    var copiesCache = { coll: null, who: null, byKey: {} };
    function itemCopies(key) {
        var who = view.ownWho + "|" + levelText();
        if (copiesCache.coll !== dex.coll || copiesCache.who !== who) copiesCache = { coll: dex.coll, who: who, byKey: {} };
        return copiesCache.byKey[key] || (copiesCache.byKey[key] = scopeCopies(key));
    }
    function scopeCopies(key) {
        var all = (dex.coll && dex.coll.byKey[key]) || [];
        var held = [];
        var gone = [];
        all.forEach(function (c) {
            if (inScope(c)) (c.gone ? gone : held).push(c);
        });
        held.sort(function (a, b) {
            return b.level - a.level || b.t - a.t;
        });
        return { held: held, gone: gone, all: all };
    }
    // (Short: it sits on a copy's row in the 32rem copies column)
    function acquired(c) {
        if (c.src === "pickup") return "Picked up " + day(c.t) + " " + clock(c.t);
        if (c.src === "save") return "Had by " + day(c.t) + " (save)";
        if (c.src === "backpack") return "In the backpack " + day(c.t);
        return "Before Kairosdex";
    }

    // The binder: every dex item (Show: those held, or those not held), in groups, each group's
    // tiles in order. An item is in every group it belongs to (a boss's, a holder's).
    var DAY_S = 86400;
    function dateGroup(t, nowS) {
        if (!t) return ["d5", "Before Kairosdex"];
        if (nowS - t < DAY_S) return ["d1", "Today"];
        if (nowS - t < 7 * DAY_S) return ["d2", "This week"];
        if (nowS - t < 30 * DAY_S) return ["d3", "This month"];
        return ["d4", "Earlier"];
    }
    // A class mod's Vault Hunter (dex.json: `class` and `hero` on every class mod), for its group
    // and the group's icon (that class's portrait icon); null for anything else
    var VH_ORDER = ["Siren", "Exo-Soldier", "Forgeknight", "Gravitar", "Rogue", "Hacker"];
    var VH_ICON = { "Siren": "Char_DarkSiren", "Exo-Soldier": "Char_ExoSoldier", "Forgeknight": "Char_Paladin",
                    "Gravitar": "Char_Gravitar", "Rogue": "Char_RoboDealer", "Hacker": "Char_CorpoHacker" };  // prettier-ignore
    function classModOf(item) {
        return item.type === "Class Mod" && item.hero && item["class"] ? { vh: CLASS_ICONS[VH_ICON[item["class"]]] || null } : null;
    }
    function binderGroups() {
        var words = terms();
        var by = view.ownGroup;
        var groups = {};
        var order = [];
        var nowS = Date.now() / 1000;
        function add(id, label, rank, item, extra) {
            var g = groups[id];
            if (!g) {
                g = groups[id] = { id: id, label: label, rank: rank, items: [], have: 0, extra: extra || null };
                order.push(g);
            }
            g.items.push(item);
            if (item.ownHeld) g.have++;
        }
        data.items.forEach(function (item) {
            if (view.ownPearl && item.rarity !== "Pearlescent") return;
            if (view.ownType && item.type !== view.ownType) return;
            if (view.ownMaker && (item.maker !== view.ownMaker || !itemShown(item))) return;
            var cs = itemCopies(item.key);
            item.ownHeld = cs.held.length;
            item.ownGone = cs.gone.length;
            if (view.ownShow === "have" && !cs.held.length) return;
            if (view.ownShow === "missing" && cs.held.length) return;
            if (words.length && !matches(hayOf("o", item.key + "|" + view.ownWho + "|" + levelText(), function () {
                return [itemHaystack(item)].concat(cs.held.map(function (c) { return norm(charName(c.char)); })).join("|");
            }), words)) return;
            if (by === "maker") {
                // Found with no maker (class mods) and not found yet (maker hidden) are two groups: one
                // id for both named the group after whichever came first (found class mods showed
                // under "Not found yet")
                if (!itemShown(item)) add("m|?", "Not found yet", 100, item);
                else if (classModOf(item)) add("m|cm|" + item.hero, item.hero + " class mods", 90 + VH_ORDER.indexOf(item["class"]), item, classModOf(item));
                else if (!item.maker) add("m|", "No manufacturer", 99, item);
                else add("m|" + item.maker, item.maker, data.makers.indexOf(item.maker), item, item.maker);
            } else if (by === "boss") {
                var ids = item.bosses || [];
                if (!ids.length) add("b|", "No boss drop", 1e6, item);
                ids.forEach(function (id) {
                    var b = data.bossById[id];
                    if (b) add("b|" + id, shownName(b), b.no, item, b);
                });
            } else if (by === "owner") {
                var seen = {};
                cs.held.forEach(function (c) {
                    if (seen[c.char]) return;
                    seen[c.char] = true;
                    add("o|" + c.char, charName(c.char), c.char === BANK ? 1e5 : 0, item, c.char);
                });
                if (!cs.held.length) add(cs.gone.length ? "o|had" : "o|", cs.gone.length ? "Had, not held now" : "Not found yet", cs.gone.length ? 1e6 : 2e6, item);
            } else if (by === "level") {
                // Under every level it has a copy at, the highest first (as by owner: in each holder's)
                var levels = {};
                cs.held.forEach(function (c) {
                    if (levels[c.level]) return;
                    levels[c.level] = true;
                    add("l|" + c.level, "Level " + c.level, -c.level, item);
                });
                if (!cs.held.length) add(cs.gone.length ? "l|had" : "l|", cs.gone.length ? "Had, not held now" : "Not found yet", cs.gone.length ? 1e6 : 2e6, item);
            } else if (by === "date") {
                var newest = null;
                cs.held.concat(cs.gone).forEach(function (c) {
                    if (!newest || c.t > newest.t) newest = c;
                });
                var g = newest ? dateGroup(newest.t, nowS) : ["d6", "Not found yet"];
                item.ownNewest = newest ? newest.t : -1;
                add(g[0], g[1], g[0], item);
            } else if (classModOf(item)) {
                // Class mods: a group per Vault Hunter, in the Class Mod type's place
                add("t|cm|" + item.hero, item.hero + " class mods", item.typeRank + (1 + VH_ORDER.indexOf(item["class"])) / 10, item, classModOf(item));
            } else {
                add("t|" + item.type, item.type, item.typeRank, item, item.type);
            }
        });
        var rev = view.ownRev ? -1 : 1;
        order.sort(function (a, b) {
            return rev * (a.rank < b.rank ? -1 : a.rank > b.rank ? 1 : cmp(a.label, b.label));
        });
        // In a group: by manufacturer (by type in a manufacturer's), then name; newest first by date
        var makerRank = function (item) {
            return itemShown(item) && item.maker ? data.makers.indexOf(item.maker) : 99;
        };
        var name = function (item) {
            return itemShown(item) ? item.name : "~";
        };
        order.forEach(function (g) {
            g.items.sort(function (a, b) {
                var r;
                if (by === "date") r = b.ownNewest - a.ownNewest;
                else if (by === "maker") r = a.typeRank - b.typeRank;
                else if (by === "type") r = makerRank(a) - makerRank(b);
                else r = a.typeRank - b.typeRank;
                return rev * (r || cmp(name(a), name(b)));
            });
        });
        return order;
    }

    // endregion

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
        var pictured = hasPicture(b.id) && (bossShown(b) || !spoilerFree());
        var box = el("div", cls + (pictured ? "" : " bl4dex_no_picture"));
        if (pictured) {
            var img = el("div", "bl4dex_picture" + (bossShown(b) ? "" : " bl4dex_silhouette"));
            img.style.backgroundImage = bossPictureUrl(b.id, small);
            box.appendChild(img);
        } else {
            box.appendChild(el("p", "bl4dex_picture_missing", "?"));
        }
        return box;
    }

    // The latest roll's own picture (roll.py renders it like the inventory: the game's own render
    // target). A found item waits for it (the game renders it in ~0.1-0.2 s) instead of showing the
    // baked picture first: the baked one only for items not found yet (their silhouette), or when no
    // roll picture is coming (none can be made here, no character). null: nothing to show (yet).
    function itemPictureFor(item, shown) {
        var roll = shown && state && state.roll && state.roll.item === item.key ? state.roll : null;
        if (roll && roll.picture) return "url(TextureRenderTarget2D'" + roll.picture + "')";
        if (rollPictureWait(item, shown)) return null;
        return dex.itemPictures && dex.itemPictures[item.key] ? itemPictureUrl(item.key, false) : null;
    }
    // Shown, and its roll's picture is on its way (or Python hasn't answered the click yet)
    function rollPictureWait(item, shown) {
        if (!shown || !state || !state.character) return false;
        var roll = state.roll && state.roll.item === item.key ? state.roll : null;
        return !roll || !!roll.coming;
    }

    // An item's picture (the inventory's own thumbnail render), dark until the item is found
    function itemPicture(item, shown) {
        var url = bare(item) ? null : itemPictureFor(item, shown);
        if (!url && !bare(item) && rollPictureWait(item, shown)) {
            // An empty frame (no "?"): the roll's picture is put in when it comes (patchRoll)
            var wait = el("div", "bl4dex_item_picture");
            wait.appendChild(el("div", "bl4dex_picture"));
            return wait;
        }
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

    function clock(ts) {
        var d = new Date(ts * 1000);
        return ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
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
        cullTiles();
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

    // Only the tiles near what's in sight are drawn: the page drew every tile each frame, in sight or
    // not (2289 tiles: 22 ms a frame, 11.5 with the far ones' insides off). A far tile keeps its box
    // (fixed size: the list's height and place don't move) and only its insides go. A screen above
    // and below stays drawn so tiles are there before they scroll in. Runs when the list moved or
    // was rebuilt (ui.cullTop null); a group far away whose tiles are all off is skipped.
    // Places are worked out from the fixed sizes (CSS below), not measured: the page measures places
    // from before for a frame or two after a scroll-bar move or a filter (heights one frame later,
    // places two), and the tiles that came into sight stayed undrawn until a scroll.
    var GROUP_HEAD_REM = 4.6 + 0.8;  // .bl4dex_group_head: height, margin
    var GROUP_GAP_REM = 1.2;  // .bl4dex_group margin
    var TILE_ROW_REM = 15 + 0.9;  // .bl4dex_tile: height (with padding and border here), margin
    var BOSS_ROW_REM = 16.4 + 0.9;  // .bl4dex_boss_tile
    function cullTiles() {
        var list = ui.list;
        if (ui.cullTop === list.scrollTop || !list.clientHeight || !ui.cols) return;
        var force = ui.cullTop === null;
        ui.cullTop = list.scrollTop;
        var top = list.scrollTop - list.clientHeight;
        var bottom = list.scrollTop + 2 * list.clientHeight;
        var cols = ui.cols;
        var y = 0;
        for (var g = list.firstChild; g; g = g.nextSibling) {
            var wrap = g.bl4dexWrap;
            if (!wrap) continue;
            var head = y + GROUP_HEAD_REM * ui.rem;
            var row = (g.bl4dexBoss ? BOSS_ROW_REM : TILE_ROW_REM) * ui.rem;
            var end = head + Math.ceil(wrap.bl4dexShown / cols) * row;
            y = end + GROUP_GAP_REM * ui.rem;
            var near = end >= top && head <= bottom;
            if (!near && g.bl4dexCulled && !force) continue;
            g.bl4dexCulled = !near;
            var k = 0;
            for (var t = wrap.firstChild; t; t = t.nextSibling) {
                if (t.bl4dexAway) continue;
                var at = head + Math.floor(k++ / cols) * row;
                var on = near && at + row >= top && at <= bottom;
                if (t.bl4dexOff !== !on) {
                    t.bl4dexOff = !on;
                    t.classList.toggle("bl4dex_tile_off", !on);
                }
            }
        }
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
        ui.ruler = el("div", "bl4dex_ruler");  // 10rem wide: rem in px for fitList
        frame.appendChild(ui.ruler);

        // (No Close button since 2026-11-06: the game's Esc / Back close the dex, and down in their
        // bar it couldn't be clicked: the ECHO overlay page is drawn over that strip and takes the mouse)

        // The parts screen (renderParts), over the whole frame
        ui.parts = el("div", "bl4dex_parts bl4dex_hidden");
        onClick(ui.parts, function () {});  // not through to the dex or the pause menu
        frame.appendChild(ui.parts);

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
        if (dex.partsFor) closeParts();
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
        var i = 0;
        while (i < BOSS_SORTS.length && BOSS_SORTS[i][0] !== view.sortBoss) i++;
        view.sortBoss = BOSS_SORTS[(i + 1) % BOSS_SORTS.length][0];
        view.scroll[view.tab] = 0;
        saveView();
        render("list");
    }

    // A tab switch keeps each tab's scroll; other changes (filters) start the list at the top
    function setView(changes) {
        var switching = changes.tab !== undefined && changes.tab !== view.tab;
        for (var k in changes) if (Object.prototype.hasOwnProperty.call(changes, k)) view[k] = changes[k];
        if (!switching) view.scroll[view.tab] = 0;
        saveView();
        // (The Collection's Owner chips also change the picked item's copies: whose. The others only
        // the binder: the details, with the game card to bind again, stay as they are)
        render(switching || (view.tab === "collection" && (changes.ownWho !== undefined || changes.ownLevel !== undefined ||
            changes.ownLevelMode !== undefined)) ? undefined : "list");  // prettier-ignore
    }

    // Go to one boss or item (a boss tile, a drop row): its tab, picked and scrolled into view.
    // Filters that hide it are cleared, and so is the search if it hides it. An item goes to its
    // Collection tile (the owner shown stays: Show goes to All when it has no copy there).
    function goTo(tab, id) {
        var c = { tab: tab };
        if (tab === "collection") {
            var item = data.itemByKey[id];
            c.coll = id;
            if (view.coll !== id) {
                c.copy = null;
                c.copyOpen = null;
                c.copyPage = 1;
            }
            if (dex.coll) {
                var held = itemCopies(id).held.length;
                if ((view.ownShow === "have" && !held) || (view.ownShow === "missing" && held)) c.ownShow = "all";
            }
            if (view.ownType && item.type !== view.ownType) c.ownType = "";
            if (view.ownMaker && (item.maker !== view.ownMaker || !itemShown(item))) c.ownMaker = "";
            if (view.ownPearl && item.rarity !== "Pearlescent") c.ownPearl = false;
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
        var target = tab === "collection" ? data.itemByKey[id] : data.bossById[id];
        if (words.length && !matches(tab === "collection" ? itemHaystack(target) : bossHaystack(target), words)) {
            state.search = "";
            send({ do: "search", text: "" });
        }
        reveal = id;
        saveView();
        render();
    }
    var reveal = null;  // an entry renderList scrolls into view

    // part "list": only the list's filters changed (a search letter, a filter, a sort): the header
    // stays, and the details are redrawn only if the entry they show changed
    function render(part) {
        if (!ui.root || !view.open || !data || !state) return;
        try {
            if (part !== "list") renderHeader();
            renderBar();
            renderFilters();
            renderList();
            if (part !== "list" || ui.detailsFor !== view.tab + "|" + current[view.tab]) renderDetails();
            renderParts();
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
        var stats = [["BOSSES", bossesBeaten, data.bosses.length], ["ITEMS", itemsShown, data.items.length]];
        if (dex.coll) {
            stats.push(["COPIES HELD", dex.coll.copies.filter(function (c) {
                return !c.gone;
            }).length, null]);
        }
        stats.forEach(function (p) {
            var box = el("div", "bl4dex_stat");
            box.appendChild(el("p", "bl4dex_stat_value", p[2] === null ? String(p[1]) : p[1] + " / " + p[2]));
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
        if (!ui.tabButtons || !ui.tabButtons.bosses.parentNode) {
            ui.tabs.innerHTML = "";
            ui.tabButtons = {};
            [["bosses", "Bosses"], ["collection", "Collection"]].forEach(function (t) {
                var tab = button(t[1], function () {
                    if (view.tab !== t[0]) setView({ tab: t[0] });
                }, "bl4dex_tab");
                ui.tabButtons[t[0]] = tab;
                ui.tabs.appendChild(tab);
            });
        }
        for (var t in ui.tabButtons) if (Object.prototype.hasOwnProperty.call(ui.tabButtons, t)) ui.tabButtons[t].classList.toggle("bl4dex_active", view.tab === t);
        var typing = !!state.typing;
        var text = state.search || "";
        ui.search.classList.toggle("bl4dex_typing", typing);
        ui.searchText.classList.toggle("bl4dex_placeholder", !text && !typing);
        ui.searchText.textContent = text ? text + (typing ? "_" : "") : typing ? "_   Enter or Esc to stop, Delete clears" : "Click to type a boss, area or item";
        ui.searchClear.classList.toggle("bl4dex_hidden", !text);
        var label = BOSS_SORTS[0][1];
        BOSS_SORTS.forEach(function (s) {
            if (s[0] === view.sortBoss) label = s[1];
        });
        ui.sortText.textContent = "Sort: " + label;
        ui.sort.classList.toggle("bl4dex_hidden", view.tab !== "bosses");
    }

    // A filter chip whose lit state follows the view: made once per tab, then only its class (and
    // any extra, like the grouping's arrow) changes. Making the rows again on each click reloaded
    // every icon in them, a visible flicker (user report 2026-11-05).
    function liveChip(text, isOn, fn, cls, extra) {
        var c = chip(text, isOn(), fn, cls);
        var entry = { c: c, isOn: isOn, extra: extra || null };
        ui.liveChips.push(entry);
        if (extra) extra(c, isOn());
        return c;
    }
    function updateChip(e) {
        var on = e.isOn();
        e.c.classList.toggle("bl4dex_active", on);
        if (e.extra) e.extra(e.c, on);
    }
    function updateChips() {
        ui.liveChips.forEach(updateChip);
    }

    function renderFilters() {
        // The rows are made again only with the tab (and on the Collection, its owners' counts);
        // a filter change only relights the chips
        var sig = JSON.stringify([view.tab, view.tab === "collection" && dex.coll ? dex.coll.v : null]);
        if (sig === ui.filtersSig && ui.filters.firstChild) {
            updateChips();
            return;
        }
        ui.filtersSig = sig;
        closeDrop();
        ui.filters.innerHTML = "";
        ui.liveChips = [];
        if (view.tab === "bosses") {
            // One row of drop-downs, as on the Collection (two rows of 24 chips before)
            var row = el("div", "bl4dex_chip_row");
            row.appendChild(el("p", "bl4dex_row_label", "FILTER"));
            [["Region", "region", data.regions], ["Kind", "kind", data.kinds]].forEach(function (f) {
                row.appendChild(dropChip(f[0], f[1], [{ id: "", label: "All" }].concat(f[2].map(function (v) {
                    return { id: v, label: v };
                })), ""));
            });
            row.appendChild(liveChip("Beaten only", function () {
                return view.onlyBoss;
            }, function () {
                setView({ onlyBoss: !view.onlyBoss });
            }, "bl4dex_toggle"));
            ui.filters.appendChild(row);
        } else {
            binderFilters();
        }
    }

    // The binder's filters: whose copies as chips (the main switch), then one row of drop-downs (what
    // to show, how to group, which type, which manufacturer) and Pearls only. Four rows of ~40 chips
    // were too many (user's pick 2026-11-05: "One row of dropdowns").
    function binderFilters() {
        var row0 = el("div", "bl4dex_chip_row bl4dex_owner_row");
        row0.appendChild(el("p", "bl4dex_row_label", "OWNER"));
        // The chips in one line that scrolls sideways when there are too many characters (the
        // wheel over it, or the arrows at its ends, shown only then)
        var who = el("div", "bl4dex_owner_scroll");
        var all = liveChip("Everyone", function () {
            return !view.ownWho;
        }, function () {
            if (view.ownWho) setView({ ownWho: "" });
        });
        if (dex.coll) all.appendChild(el("p", "bl4dex_chip_count", String(dex.coll.copies.filter(function (c) { return !c.gone; }).length)));
        who.appendChild(all);
        holders().forEach(function (h) {
            var c = liveChip(charName(h.id), function () {
                return view.ownWho === h.id;
            }, function () {
                setView({ ownWho: view.ownWho === h.id ? "" : h.id });
            }, "bl4dex_chip_holder");
            c.insertBefore(holderBadge(h.id), c.firstChild);
            var line = holderLine(h.id);
            if (line) {
                // The name over its level and when it was played
                var text = c.querySelector(".bl4dex_chip_text");
                var lines = el("div", "bl4dex_chip_lines");
                c.insertBefore(lines, text);
                lines.appendChild(text);
                lines.appendChild(el("p", "bl4dex_chip_sub", line));
            }
            c.appendChild(el("p", "bl4dex_chip_count", String(h.held)));
            who.appendChild(c);
        });
        // The chips over a bar that scrolls them too (drag its handle, or click the track)
        var box = el("div", "bl4dex_owner_box");
        box.appendChild(who);
        box.appendChild(sideBar(who));
        row0.appendChild(scrollArrow(who, -1));
        row0.appendChild(box);
        row0.appendChild(scrollArrow(who, 1));
        who.addEventListener("wheel", function (evt) {
            if (who.scrollWidth <= who.clientWidth) return;
            scrollRow(who, who.scrollLeft + (evt.deltaY || evt.deltaX));
            evt.preventDefault();
            evt.stopPropagation();
        });
        ui.filters.appendChild(row0);
        showArrows(who, 0);

        var row = el("div", "bl4dex_chip_row");
        row.appendChild(el("p", "bl4dex_row_label", "FILTER"));
        row.appendChild(dropChip("Show", "ownShow", [["have", "Have"], ["all", "Everything"], ["missing", "Missing"]].map(function (o) {
            return { id: o[0], label: o[1] };
        }), "have"));
        // Picking the grouping in use again reverses it; the button and its row show the direction
        row.appendChild(dropChip("Group", "ownGroup", [["type", "Type"], ["maker", "Manufacturer"], ["boss", "Boss"], ["owner", "Owner"], ["level", "Level"], ["date", "Date"]].map(function (o) {
            return { id: o[0], label: o[1] };
        }), null, function (id) {
            setView(view.ownGroup === id ? { ownRev: !view.ownRev } : { ownGroup: id, ownRev: false });
        }));
        row.appendChild(dropChip("Type", "ownType", typeOptions(), ""));
        row.appendChild(dropChip("Maker", "ownMaker", makerOptions(), ""));
        row.appendChild(liveChip("Pearls only", function () {
            return view.ownPearl;
        }, function () {
            setView({ ownPearl: !view.ownPearl });
        }, "bl4dex_toggle"));
        // Level: how to compare (a drop-down: Exactly / At least / At most / Between), then the
        // number, typed through Python's text box like the search (digits; "40-60" for Between).
        // Picking a mode starts the typing; the list follows as it's typed (patchLevel).
        row.appendChild(dropChip("Level", "ownLevelMode", LEVEL_MODES.map(function (o) {
            return { id: o[0], label: o[1] };
        }), "", function (id) {
            setView(id ? { ownLevelMode: id } : { ownLevelMode: "", ownLevel: "" });
            var typing = !!(state && state.typing_level);
            if (id && !typing) send({ do: "type", on: true, field: "level", text: view.ownLevel || "" });
            if (!id && typing) send({ do: "type", on: false });
        }));
        row.appendChild(liveChip("", function () {
            return !!(state && state.typing_level);
        }, function () {
            send({ do: "type", on: !(state && state.typing_level), field: "level", text: view.ownLevel || "" });
        }, "bl4dex_level_chip", function (c) {
            var typing = !!(state && state.typing_level);
            var typed = typing ? state.level_text || "" : String(view.ownLevel || "");
            var bad = !typing && typed && !levelTest(levelText());
            // Empty: the example stays while typing too (picking a mode starts the typing at once,
            // and "_" alone hid how Between is written: the user's report)
            var hint = view.ownLevelMode === "range" ? "e.g. 40-60" : "type a level";
            setText(c.firstChild, typed ? typed + (typing ? "_" : bad ? "  ?" : "") : hint);
            c.classList.toggle("bl4dex_level_hint", !typed);
            c.classList.toggle("bl4dex_typing", typing);
            c.classList.toggle("bl4dex_hidden", !view.ownLevelMode && !typing && !view.ownLevel);
        }));
        // Every filter back to how it starts (not the owner or the grouping: those are where you are)
        row.appendChild(chip("Reset", false, function () {
            if (state && state.typing_level) send({ do: "type", on: false });
            setView({ ownShow: OWN_DEFAULTS.ownShow, ownType: "", ownMaker: "", ownPearl: false, ownLevel: "", ownLevelMode: "" });
        }, "bl4dex_reset_chip"));
        ui.filters.appendChild(row);
    }

    // An arrow at one end of a sideways-scrolling row: a click moves it most of its width
    function scrollArrow(box, dir) {
        var a = el("div", "bl4dex_chip bl4dex_scroll_arrow bl4dex_hidden");
        a.appendChild(el("div", dir < 0 ? "bl4dex_scroll_left" : "bl4dex_scroll_right"));
        onClick(a, function () {
            scrollRow(box, box.scrollLeft + dir * Math.max(1, box.clientWidth * 0.8));
        });
        box["bl4dexArrow" + (dir < 0 ? "L" : "R")] = a;
        return a;
    }
    // A bar under a sideways-scrolling row: its handle is the part in sight
    function sideBar(box) {
        var track = el("div", "bl4dex_hbar bl4dex_hidden");  // (shown by showArrows once it's measured)
        var handle = el("div", "bl4dex_hbar_handle");
        track.appendChild(handle);
        handle.addEventListener("mousedown", function (evt) {
            evt.stopPropagation();
            ui.hdrag = { box: box, x: evt.clientX, left: handle.offsetLeft };
            handle.classList.add("bl4dex_sbar_drag");
        });
        track.addEventListener("mousedown", function (evt) {
            if (evt.target !== track) return;
            evt.stopPropagation();
            // Centre the handle on the click, and keep dragging from there
            sideTo(box, evt.clientX - track.getBoundingClientRect().left - handle.offsetWidth / 2);
            ui.hdrag = { box: box, x: evt.clientX, left: handle.offsetLeft };
            handle.classList.add("bl4dex_sbar_drag");
        });
        onClick(track, function () {});  // not through to the pause menu
        box.bl4dexBar = track;
        box.bl4dexHandle = handle;
        return track;
    }
    function sideTo(box, handleLeft) {
        var r = trackWidth(box) - box.bl4dexHandle.offsetWidth;
        if (r <= 0) return;
        scrollRow(box, Math.max(0, Math.min(r, handleLeft)) / r * (box.scrollWidth - box.clientWidth));
    }
    // Gameface doesn't clamp scrollLeft: past the end the row showed empty space and the handle ran
    // off its track
    function scrollRow(box, x) {
        box.scrollLeft = Math.max(0, Math.min(box.scrollWidth - box.clientWidth, x));
        showArrows(box);
    }
    // (The bar may measure 0 the frame it's shown: the row is as wide but for the bar's margin)
    function trackWidth(box) {
        return box.bl4dexBar.clientWidth || box.clientWidth;
    }
    listenDoc("mousemove", function (evt) {
        var d = ui.hdrag;
        if (d) sideTo(d.box, d.left + evt.clientX - d.x);
    });
    listenDoc("mouseup", function () {
        var d = ui.hdrag;
        if (!d) return;
        ui.hdrag = null;
        d.box.bl4dexHandle.classList.remove("bl4dex_sbar_drag");
    });

    // Each arrow shows while there's more that way, and the bar while anything is out of sight, its
    // handle where the row is (measured once laid out: a frame later when new)
    function showArrows(box, tries) {
        if (!box.parentNode) return;
        if (!box.clientWidth && tries < 10) {
            requestAnimationFrame(function () { if (alive()) showArrows(box, tries + 1); });
            return;
        }
        var more = box.scrollWidth - box.clientWidth;
        if (box.scrollLeft > Math.max(0, more)) box.scrollLeft = Math.max(0, more);  // (narrower now: a resize)
        // An arrow with nothing more its way is only unseen, still taking its room: taking it out
        // widened the row as it reached its end, and moved the end
        box.bl4dexArrowL.classList.toggle("bl4dex_hidden", !(more > 1));
        box.bl4dexArrowR.classList.toggle("bl4dex_hidden", !(more > 1));
        box.bl4dexArrowL.classList.toggle("bl4dex_unseen", !(box.scrollLeft > 1));
        box.bl4dexArrowR.classList.toggle("bl4dex_unseen", !(box.scrollLeft < more - 1));
        var bar = box.bl4dexBar;
        bar.classList.toggle("bl4dex_hidden", !(more > 1));
        if (more > 1) {
            var t = trackWidth(box);
            var w = Math.max(t * box.clientWidth / box.scrollWidth, 30);
            box.bl4dexHandle.style.width = w + "px";
            box.bl4dexHandle.style.left = (t - w) * box.scrollLeft / more + "px";
        }
    }

    // A filter as one button, "Type: Pistol" with the choice's icon (lit while it filters: not on
    // its plain choice), opening a panel of the choices under it. pick(id) defaults to setting
    // view[field]; the Group button shows its direction.
    function dropChip(title, field, options, plain, pick) {
        var c = el("div", "bl4dex_chip bl4dex_drop");
        var icon = el("div", "bl4dex_hidden");
        var text = el("p", "bl4dex_chip_text");
        var arrow = el("div", "bl4dex_arrow bl4dex_hidden");
        var caret = el("div", "bl4dex_drop_caret");
        c.appendChild(icon);
        c.appendChild(text);
        c.appendChild(arrow);
        c.appendChild(caret);
        var shownId = {};  // (never an option id: the first update fills the button in)
        var choose = pick || function (id) {
            var ch = {};
            ch[field] = id;
            setView(ch);
        };
        onClick(c, function () {
            if (ui.drop && ui.drop.button === c) closeDrop();
            else openDrop(c, options, field, choose);
        });
        ui.liveChips.push({ c: c, isOn: function () {
            return plain !== null && view[field] !== plain;
        }, extra: function () {
            var o = optionOf(options, view[field]);
            if (o.id !== shownId) {
                shownId = o.id;
                text.textContent = title + ": " + o.label;
                icon.className = o.icon ? o.iconCls : "bl4dex_hidden";
                icon.style.backgroundImage = o.icon || "";
            }
            arrow.classList.toggle("bl4dex_hidden", field !== "ownGroup");
            arrow.classList.toggle("bl4dex_arrow_up", !!view.ownRev);
        } });
        updateChip(ui.liveChips[ui.liveChips.length - 1]);
        return c;
    }
    // The Type and Maker drop-downs' choices (Items and Collection), with the game's icons
    function typeOptions() {
        return [{ id: "", label: "All" }].concat(data.types.map(function (t) {
            return { id: t, label: t, icon: TYPE_ICONS[t] ? "url(" + TYPE_ART + TYPE_ICONS[t] + ".png)" : null, iconCls: "bl4dex_chip_type" };
        }));
    }
    function makerOptions() {
        return [{ id: "", label: "All" }].concat(data.makers.map(function (m) {
            return { id: m, label: m, icon: makerArt(m, "logomark_small"), iconCls: "bl4dex_chip_logo" };
        }));
    }
    function optionOf(options, id) {
        for (var i = 0; i < options.length; i++) if (options[i].id === id) return options[i];
        return options[0];
    }

    // The open drop-down's panel: one at a time, under its button, over the binder. A press anywhere
    // else closes it (clicks don't bubble here: onClick stops them).
    function openDrop(button, options, field, choose) {
        closeDrop();
        var panel = el("div", "bl4dex_drop_panel");
        panel.style.left = button.offsetLeft + "px";
        panel.style.top = button.offsetTop + button.offsetHeight + "px";
        options.forEach(function (o) {
            var on = view[field] === o.id;
            var r = el("div", "bl4dex_drop_row" + (on ? " bl4dex_active" : ""));
            if (o.icon) {
                var i = el("div", o.iconCls);
                i.style.backgroundImage = o.icon;
                r.appendChild(i);
            }
            r.appendChild(el("p", "bl4dex_chip_text", o.label));
            if (on && field === "ownGroup") r.appendChild(el("div", "bl4dex_arrow" + (view.ownRev ? " bl4dex_arrow_up" : "")));
            onClick(r, function () {
                closeDrop();
                choose(o.id);
            });
            panel.appendChild(r);
        });
        ui.filters.appendChild(panel);
        button.classList.add("bl4dex_drop_open");
        ui.drop = { button: button, panel: panel };
    }
    function closeDrop() {
        var d = ui.drop;
        if (!d) return;
        ui.drop = null;
        d.button.classList.remove("bl4dex_drop_open");
        if (d.panel.parentNode) d.panel.parentNode.removeChild(d.panel);
    }
    function inside(node, parent) {
        for (; node; node = node.parentNode) if (node === parent) return true;
        return false;
    }
    listenDoc("mousedown", function (evt) {
        var d = ui.drop;
        if (d && !inside(evt.target, d.panel) && !inside(evt.target, d.button)) closeDrop();
    });

    // The entry the details show: the pick, or while a search or filter hides it, the first one
    // left (the pick itself is kept, and comes back with the filter off)
    var current = { bosses: null, collection: null };

    // What a list row shows besides its own data: rows made under the same key are kept and reused
    // (a click, a search letter or a filter used to make every row again: up to 318 items with their
    // mini pictures, a few thousand elements, on every change)
    var picturesRev = 0;
    var listStamp = 0;  // marks the rows a rebuild wants (never restarts, unlike the kept rows)
    function rowsKey() {
        // (Not the collection or the owner: the binder's tiles are kept through those and refilled,
        // fillTile. Made again, every picture blanked a moment: the screen looked shut for a second
        // on an owner switch, 2289 tiles in 275-305 ms.)
        return JSON.stringify([state.defeated, state.unlock, state.obtained, state.owned_v, state.spoiler_free, picturesRev]);  // prettier-ignore
    }
    var rows = { key: null, nodes: {}, hay: {}, tab: null, ids: null };
    function keptRows() {
        var key = rowsKey();
        if (rows.key !== key) rows = { key: key, nodes: {}, hay: {}, tab: null, ids: null };
        // (The Collection's haystacks have holder names in them: made again for a new collection)
        if (rows.coll !== dex.coll) {
            rows.coll = dex.coll;
            rows.hay = {};
        }
        return rows;
    }
    // A search haystack, kept with the rows (it changes with the same things)
    function hayOf(kind, id, make) {
        var hay = keptRows().hay;
        var k = kind + "|" + id;
        return hay[k] !== undefined ? hay[k] : (hay[k] = make());
    }

    function markSelected(tab) {
        if (ui.selected) ui.selected.classList.remove("bl4dex_selected");
        var node = (ui.entries && ui.entries[current[tab]]) || null;
        if (node) node.classList.add("bl4dex_selected");
        ui.selected = node;
    }

    // A click on a row: only the highlight and the details change, the list stays as it is
    function pick(tab, id) {
        view[tab === "bosses" ? "boss" : "coll"] = id;
        saveView();
        if (view.tab !== tab || !ui.entries || !ui.entries[id]) {
            render();
            return;
        }
        current[tab] = id;
        markSelected(tab);
        try {
            renderDetails();
            renderParts();
        } catch (err) {
            log("render failed: " + err + " " + (err && err.stack));
        }
    }

    function renderList() {
        rebuilding = true;
        fitList();
        if (view.tab === "collection") renderBinder();
        else renderBossBinder();
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

    // Puts parent's children in want's order, moving as little as possible: takes out the ones
    // not stamped for this build (a search letter keeps the order of the rest), then puts each
    // wanted one in its place; the ones already there stay put
    function arrange(parent, want, stamp) {
        var child = parent.firstChild;
        while (child) {
            var nextChild = child.nextSibling;
            if (child.bl4dexStamp !== stamp) parent.removeChild(child);
            child = nextChild;
        }
        var at = parent.firstChild;
        for (var i = 0; i < want.length; i++) {
            if (at === want[i]) at = at.nextSibling;
            else parent.insertBefore(want[i], at);
        }
    }

    // arrange for a group's tiles: the ones not wanted are hidden, not taken out. Gameface takes
    // ~60 us to take one element out of the page (2289 tiles: 135 ms, the list frozen on a filter
    // or owner click), 0.6 ms to hide them all; and a tile kept in the page shows its picture at
    // once when it's wanted again. (Whole groups are still taken out: 21 of them, 4.4 ms.)
    function arrangeTiles(wrap, want, stamp) {
        wrap.bl4dexShown = want.length;  // (cullTiles: the group's rows)
        for (var child = wrap.firstChild; child; child = child.nextSibling) {
            var away = child.bl4dexStamp !== stamp;
            if (child.bl4dexAway !== away) {
                child.bl4dexAway = away;
                child.classList.toggle("bl4dex_hidden", away);
            }
        }
        var at = wrap.firstChild;
        for (var i = 0; i < want.length; i++) {
            while (at && at.bl4dexAway) at = at.nextSibling;
            if (at === want[i]) at = at.nextSibling;
            else wrap.insertBefore(want[i], at);
        }
    }

    function setText(node, text) {
        if (node.textContent !== text) node.textContent = text;
    }

    // The Bosses tab: the Collection's binder look, a group per region (in the game's order) over a
    // wrap of boss tiles in the sort's order. Tiles and group blocks are kept like list rows and
    // moved as little as possible; a group's count and bar follow the tiles it shows.
    function renderBossBinder() {
        var list = bossList();
        var kept = keptRows();
        var byRegion = {};
        list.forEach(function (b) {
            (byRegion[b.region] || (byRegion[b.region] = [])).push(b);
        });
        var regions = data.regions.filter(function (r) {
            return byRegion[r];
        });
        var ids = [];
        regions.forEach(function (r) {
            byRegion[r].forEach(function (b) {
                ids.push(b.id);
            });
        });
        current.bosses = ids.indexOf(view.boss) >= 0 ? view.boss : ids.length ? ids[0] : null;
        var joined = ids.join("\n");
        if (ids.length && kept.tab === "bosses" && kept.ids === joined && ui.list.firstChild) {
            markSelected("bosses");
            var same = reveal !== null ? ui.entries[reveal] || null : null;
            if (!same) reveal = null;
            placeList(view.scroll.bosses || 0, same);
            return;
        }
        ui.entries = {};  // (ui.selected stays: markSelected takes its highlight off)
        kept.tab = "bosses";
        kept.ids = joined;
        var stamp = ++listStamp;
        var blocks = regions.map(function (r) {
            var bosses = byRegion[r];
            var block = kept.nodes["bossGroup|" + r] || (kept.nodes["bossGroup|" + r] = bossGroup(r));
            block.bl4dexStamp = stamp;
            var won = 0;
            var tiles = bosses.map(function (b) {
                if (beaten(b.id)) won++;
                var t = kept.nodes["bossTile|" + b.id] || (kept.nodes["bossTile|" + b.id] = bossTile(b));
                t.bl4dexStamp = stamp;
                ui.entries[b.id] = t;
                return t;
            });
            setText(block.bl4dexCount, won + " / " + bosses.length);
            block.bl4dexFill.style.width = Math.round(100 * won / bosses.length) + "%";
            setText(block.bl4dexNote, won < bosses.length ? bosses.length - won + " to beat" : "");
            arrangeTiles(block.bl4dexWrap, tiles, stamp);
            return block;
        });
        arrange(ui.list, blocks, stamp);
        ui.cullTop = null;  // (tiles moved: cullTiles looks at every one again)
        if (!ids.length) ui.list.appendChild(el("p", "bl4dex_empty", terms().length ? "Nothing matches that search." : "Nothing here yet."));
        markSelected("bosses");
        var target = reveal !== null ? ui.entries[reveal] || null : null;
        if (!target) reveal = null;
        placeList(view.scroll.bosses || 0, target);
    }

    // A region's header (the game's region mark, its name, beaten / shown, a bar) over its tiles
    function bossGroup(region) {
        var block = el("div", "bl4dex_group");
        var head = el("div", "bl4dex_group_head");
        var icon = el("div", "bl4dex_group_icon");
        icon.style.backgroundImage = "url(" + ICON_REGION + ")";
        head.appendChild(icon);
        head.appendChild(el("p", "bl4dex_group_name", region.toUpperCase()));
        block.bl4dexCount = el("p", "bl4dex_group_count");
        head.appendChild(block.bl4dexCount);
        var bar = el("div", "bl4dex_group_bar");
        block.bl4dexFill = el("div", "bl4dex_group_fill");
        bar.appendChild(block.bl4dexFill);
        head.appendChild(bar);
        block.bl4dexNote = el("p", "bl4dex_group_note");
        head.appendChild(block.bl4dexNote);
        block.appendChild(head);
        block.bl4dexWrap = el("div", "bl4dex_tiles");
        block.bl4dexBoss = true;  // (taller tiles: cullTiles)
        block.appendChild(block.bl4dexWrap);
        return block;
    }

    // A boss tile: its number and kind over its portrait (a silhouette until beaten), its name
    // ("???" until shown); beaten: BEATEN on a green foot, else dashed with the area it's in
    function bossTile(b) {
        var won = beaten(b.id);
        var t = el("div", "bl4dex_boss_tile" + (won ? "" : " bl4dex_boss_tile_open"));
        t.appendChild(el("p", "bl4dex_boss_tile_no", pad(b.no)));
        if (b.kind) t.appendChild(el("p", "bl4dex_boss_tile_kind", b.kind));
        t.appendChild(portrait(b, "bl4dex_boss_tile_picture", true));
        t.appendChild(el("p", "bl4dex_boss_tile_name" + (bossShown(b) ? "" : " bl4dex_boss_tile_locked"), shownName(b)));
        // (the group says the region: the area only when it's a place of its own)
        t.appendChild(el("p", won ? "bl4dex_boss_tile_beaten" : "bl4dex_boss_tile_where", won ? "BEATEN" : b.area === b.region ? "" : b.area));
        onClick(t, function () {
            pick("bosses", b.id);
        });
        return t;
    }

    // The binder: a header per group (its icon, name, how many of its items are held) over a wrap
    // of tiles. Tiles are kept like list rows (made again only when the collection or the holder
    // shown changes); the blocks are made again when the groups or their tiles change.
    // The tile tabs (Bosses, Collection): the list is as wide as a whole number of tile columns,
    // the details pane gets the rest (at least FIT_DETAILS). A fixed split left a strip of blank
    // list at some window sizes and a roomy, half-empty details pane at others (user asks
    // 2026-11-05). Measured only when the tab or the frame's width changed.
    var FIT_DETAILS = 96;
    var MIN_COLS = 3;  // rem: the card (56) and a 32rem copies column, with the pane's padding
    // A tile's width (Gameface counts padding and border inside it: 15.6rem drew 115 px at 7.4 px a
    // rem) and its margin. With padding and border added on top the list came out ~7rem too wide.
    var TILE_REM = 15.6 + 0.9;
    var LIST_EXTRA_REM = 0.8 + 1.2 + 2.4;  // the list's margin, the scroll bar and its margin
    function fitList(tries) {
        var bodyW = ui.list.parentNode.clientWidth;
        var rem = ui.ruler.offsetWidth / 10;
        if (!bodyW || !rem) {
            // Not laid out yet (Gameface lays new DOM out a frame later)
            if ((tries || 0) < 10) requestAnimationFrame(function () { if (alive()) fitList((tries || 0) + 1); });
            return;
        }
        var key = view.tab + "|" + bodyW + "|" + rem;
        if (ui.fitFor === key) return;
        ui.fitFor = key;
        var tile = TILE_REM * rem;
        // At least 3 columns (the user's rule): on a narrow window the details give up the room
        var cols = Math.max(MIN_COLS, Math.floor((bodyW - (LIST_EXTRA_REM + FIT_DETAILS) * rem) / tile));
        // (+ half a rem: the page rounds each tile and its gap to whole pixels; at 12.5 px a rem three
        // tiles took 621 px and the list's 620 sent the third to the next row)
        var w = Math.ceil(cols * tile + 0.5 * rem) + "px";
        ui.list.style.flex = "0 0 " + w;
        ui.list.style.width = w;
        ui.cols = cols;
        ui.rem = rem;
        ui.cullTop = null;  // (other columns: tiles in other places)
    }

    function renderBinder() {
        var kept = keptRows();
        var groups = dex.coll ? binderGroups() : [];
        // (The copies' scope too, owner and level: the same tiles then show other counts and levels,
        // and the shortcut below kept the old ones: Level 59 left every tile as it was)
        var joined = view.ownGroup + "|" + view.ownRev + "|" + view.ownWho + "|" + levelText() + "|" + groups.map(function (g) {
            return g.id + ":" + g.items.map(function (i) { return i.key; }).join(",");
        }).join("\n");
        var picked = view.coll && data.itemByKey[view.coll] ? view.coll : null;
        var first = groups.length && groups[0].items.length ? groups[0].items[0].key : null;
        // The pick stays shown while a filter hides its tile (its copies are still worth seeing)
        current.collection = picked || first;
        if (groups.length && kept.tab === "collection" && kept.ids === joined && ui.list.firstChild) {
            markSelected("collection");
            var same = reveal !== null ? ui.entries[reveal] || null : null;
            if (!same) reveal = null;
            placeList(view.scroll.collection || 0, same);
            return;
        }
        kept.tab = "collection";
        kept.ids = joined;
        ui.entries = {};
        // Blocks and tiles are kept and only moved: made again, their icons and pictures reloaded and
        // flickered on every chip click (user report 2026-11-05)
        var stamp = ++listStamp;
        var blocks = groups.map(function (g) {
            var k = "collGroup|" + view.ownGroup + "|" + g.id;
            var block = kept.nodes[k] || (kept.nodes[k] = binderGroup(g));
            block.bl4dexStamp = stamp;
            setText(block.bl4dexName, g.label.toUpperCase());
            setText(block.bl4dexCount, g.have + " / " + g.items.length);
            block.bl4dexFill.style.width = Math.round(100 * g.have / g.items.length) + "%";
            var missing = g.items.length - g.have;
            setText(block.bl4dexNote, missing ? missing + " not held" : "");
            var tiles = g.items.map(function (item) {
                // (Spoiler free hides a picture outright: a tile of its own, not refilled)
                var tk = "collection|" + g.id + "|" + item.key + (bare(item) ? "|bare" : "");
                var node = kept.nodes[tk];
                if (node) fillTile(node, item);
                else node = kept.nodes[tk] = binderTile(item, /^l\|\d+$/.test(g.id) ? +g.id.slice(2) : undefined);
                node.bl4dexStamp = stamp;
                if (!ui.entries[item.key]) ui.entries[item.key] = node;
                return node;
            });
            arrangeTiles(block.bl4dexWrap, tiles, stamp);
            return block;
        });
        arrange(ui.list, blocks, stamp);
        ui.cullTop = null;  // (tiles moved: cullTiles looks at every one again)
        if (!groups.length) {
            ui.list.appendChild(el("p", "bl4dex_empty", !dex.coll ? "Reading your collection..." : terms().length ? "Nothing matches that search."
                : view.ownShow === "have" ? "Nothing held here yet." : "Nothing here."));  // prettier-ignore
            rebuilding = false;
            return;
        }
        markSelected("collection");
        var target = reveal !== null ? ui.entries[reveal] || null : null;
        if (!target) reveal = null;
        placeList(view.scroll.collection || 0, target);
    }

    // A group's block: header (icon, name, held / all, a bar, how many not held) over its tiles. Made
    // once per group; renderBinder fills in the text and the tiles.
    function binderGroup(g) {
        var block = el("div", "bl4dex_group");
        var head = el("div", "bl4dex_group_head");
        var icon = groupIcon(g);
        if (icon) head.appendChild(icon);
        block.bl4dexName = el("p", "bl4dex_group_name");
        head.appendChild(block.bl4dexName);
        block.bl4dexCount = el("p", "bl4dex_group_count");
        head.appendChild(block.bl4dexCount);
        var bar = el("div", "bl4dex_group_bar");
        block.bl4dexFill = el("div", "bl4dex_group_fill");
        bar.appendChild(block.bl4dexFill);
        head.appendChild(bar);
        block.bl4dexNote = el("p", "bl4dex_group_note");
        head.appendChild(block.bl4dexNote);
        block.appendChild(head);
        block.bl4dexWrap = el("div", "bl4dex_tiles");
        block.appendChild(block.bl4dexWrap);
        return block;
    }

    function groupIcon(g) {
        var url = null;
        if (g.extra && g.extra.vh !== undefined) {
            // A Vault Hunter's class mods: that class's portrait icon
            var vh = el("div", "bl4dex_holder bl4dex_group_holder");
            if (g.extra.vh) vh.style.backgroundImage = "url(" + g.extra.vh + ")";
            return vh;
        }
        if (view.ownGroup === "type" && TYPE_ICONS[g.extra]) url = "url(" + TYPE_ART + TYPE_ICONS[g.extra] + ".png)";
        else if (view.ownGroup === "maker" && g.extra) url = makerArt(g.extra, "logomark_small");
        else if (view.ownGroup === "owner" && g.extra) return holderBadge(g.extra, "bl4dex_group_holder");
        else if (view.ownGroup === "boss" && g.extra) return portrait(g.extra, "bl4dex_group_boss", true);
        if (!url) return null;
        var icon = el("div", "bl4dex_group_icon");
        icon.style.backgroundImage = url;
        return icon;
    }

    // A tile: the item's picture and name; held: its manufacturer, how many copies, the best level
    // and who holds them; not held: dark, with the boss that drops it (or "Had" if it went)
    function binderTile(item, atLevel) {
        var shown = itemShown(item);
        var rarity = rarityOf(item);
        var t = el("div", "bl4dex_tile bl4dex_tile_" + rarity);
        t.bl4dexAtLevel = atLevel;  // (in a Level group: only its copies at that level count)
        if (item.maker) {
            t.bl4dexLogo = el("div", "bl4dex_tile_logo");
            t.bl4dexLogo.style.backgroundImage = makerArt(item.maker, "logomark_small");
            t.appendChild(t.bl4dexLogo);
        }
        t.bl4dexCount = el("p", "bl4dex_tile_count");
        t.appendChild(t.bl4dexCount);
        var pic = el("div", "bl4dex_tile_picture");
        var url = bare(item) ? null : itemPictureUrl(item.key, true);
        if (url) {
            t.bl4dexImg = el("div", "bl4dex_picture" + (shown ? "" : " bl4dex_silhouette"));
            t.bl4dexImg.style.backgroundImage = url;
            pic.appendChild(t.bl4dexImg);
        } else {
            unknownMark(pic);
        }
        t.appendChild(pic);
        t.bl4dexName = el("p", "bl4dex_tile_name bl4dex_name_" + rarity, shown ? item.name : "???");
        t.appendChild(t.bl4dexName);
        t.bl4dexFoot = el("div", "bl4dex_tile_foot");
        t.appendChild(t.bl4dexFoot);
        onClick(t, function () {
            if (view.coll !== item.key) {
                view.copy = null;
                view.copyOpen = null;
                view.copyPage = 1;
            }
            pick("collection", item.key);
        });
        fillTile(t, item);
        return t;
    }
    // A tile's parts that follow the collection and whose copies are shown: found or not (a
    // silhouette and "???": one holder's binder), held or not, how many, the best level, up to 4
    // holders. Only when they changed; the picture stays.
    function fillTile(t, item) {
        var cs = itemCopies(item.key);
        if (t.bl4dexAtLevel !== undefined) {
            // A Level group's tile: how many it has at that level (kept with the copies, cached alike)
            var at = t.bl4dexAtLevel;
            cs.atLevel = cs.atLevel || {};
            cs = cs.atLevel[at] || (cs.atLevel[at] = { held: cs.held.filter(function (c) { return c.level === at; }), gone: [] });
        }
        var held = cs.held.length > 0;
        var shown = itemShown(item);
        if (t.bl4dexImg) t.bl4dexImg.classList.toggle("bl4dex_silhouette", !shown);
        setText(t.bl4dexName, shown ? item.name : "???");
        if (!cs.holders) {
            cs.holders = [];
            for (var i = 0; i < cs.held.length && cs.holders.length < 4; i++) {
                if (cs.holders.indexOf(cs.held[i].char) < 0) cs.holders.push(cs.held[i].char);
            }
        }
        var sig = cs.held.length + "|" + cs.gone.length + "|" + (held ? cs.held[0].level : "") + "|" + cs.holders.join(",");
        if (t.bl4dexSig === sig) return;
        t.bl4dexSig = sig;
        t.classList.toggle("bl4dex_tile_had", !held && cs.gone.length > 0);
        t.classList.toggle("bl4dex_tile_missing", !held && !cs.gone.length);
        if (t.bl4dexLogo) t.bl4dexLogo.classList.toggle("bl4dex_hidden", !held);
        t.bl4dexCount.textContent = "×" + cs.held.length;
        t.bl4dexCount.classList.toggle("bl4dex_hidden", cs.held.length < 2);
        var foot = t.bl4dexFoot;
        while (foot.firstChild) foot.removeChild(foot.firstChild);
        if (held) {
            foot.appendChild(el("p", "bl4dex_tile_level", "L" + cs.held[0].level));
            var who = el("div", "bl4dex_tile_holders");
            cs.holders.forEach(function (c) {
                who.appendChild(holderBadge(c, "bl4dex_holder_small"));
            });
            foot.appendChild(who);
        } else if (cs.gone.length) {
            foot.appendChild(el("p", "bl4dex_tile_where", "Had: sold or dropped"));
        } else {
            var b = item.bosses && item.bosses.length ? data.bossById[item.bosses[0]] : null;
            foot.appendChild(el("p", "bl4dex_tile_where", b ? shownName(b) : item.reward ? "Mission reward" : ""));
        }
    }

    // region Hover card
    // Hovering an item in a boss's drops shows its item card beside it: the game's card widget (a
    // second one, model data_bl4dex_card_1) with the item's latest roll. Python rolls an item on its
    // first hover (no picture) and keeps that roll for later hovers.
    function hoverCard(target, item) {
        if (!itemShown(item)) return;
        target.addEventListener("mouseenter", function () {
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
        if (bare(item)) return "???";
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
        ui.ownView = null;
        // The Collection's pane stays on the page and is updated in place (ownDetails): any element
        // made new or taken out and put back shows its pictures blank for a moment in Gameface, and
        // the pane was made again several times per pick (the click, then the card's data coming
        // in): the Farm More icons flickered (user reports 2026-11-05/06). The Bosses tab's is made
        // again; the old pane goes once the new one is in.
        var old = [];
        for (var n = ui.details.firstChild; n; n = n.nextSibling) old.push(n);
        var keep = null;
        if (view.tab === "collection") {
            var own = current.collection && data.itemByKey[current.collection];
            if (own && dex.coll) keep = ownDetails(own);
            else ui.details.appendChild(el("p", "bl4dex_empty", dex.coll ? "Pick an item to see your copies." : "Reading your collection..."));
        } else {
            var b = current.bosses && data.bossById[current.bosses];
            if (b) bossDetails(b);
            else ui.details.appendChild(el("p", "bl4dex_empty", "Pick a boss to see where it is and what it drops."));
        }
        old.forEach(function (o) {
            if (o !== keep) ui.details.removeChild(o);
        });
        if (!keep) ui.ownPane = null;
        ui.details.scrollTop = scroll;
    }

    // The picked binder item: the copy shown (its own render and the game's card for it, which
    // squeezes in on hover: a click opens its parts), then every copy listed the way copySort says
    // (by level, by holder, or newest first), the ones that went (sold, dropped), and where to farm it.
    function ownPicture(c, info) {
        if (info && info.picture) return "url(TextureRenderTarget2D'" + info.picture + "')";
        if (ownPictureWait(info)) return null;
        return dex.itemPictures && dex.itemPictures[c.key] ? itemPictureUrl(c.key, false) : null;
    }
    // Its render is on its way: Python hasn't answered the click, is reading its card, or has the card
    // (the picture is shot right after it, and asked again if it was dropped)
    function ownPictureWait(info) {
        if (!state || !state.character) return false;
        return !info || info.status === "reading" || !!info.card;
    }
    function shownInfo(sid) {
        var shown = collState().shown;
        return shown && shown.id === sid ? shown : null;
    }
    // The copy whose card shows: the one picked, if it's one of these, else the best one held
    function shownCopy(cs) {
        var list = cs.held.concat(cs.gone);
        for (var i = 0; i < list.length; i++) if (list[i].id === view.copy) return list[i];
        return list.length ? list[0] : null;
    }
    function pickCopy(c) {
        view.copy = c.id;
        saveView();
        renderDetails();
    }

    // The picked item's pane: its copies, then who drops it (left); the game's card for the copy shown
    // (right). Made once and kept (renderDetails): each part is redrawn only when what it shows
    // changed, and the Farm More cards are refilled in place. Returns the pane's root.
    function ownDetails(item) {
        var cs = itemCopies(item.key);
        var c = shownCopy(cs);
        var shown = itemShown(item);
        var info = c ? shownInfo(c.sid) : null;
        // Python makes the card (and picture) of the copy shown; asked again if it lost track (a reload)
        if (c && (dex.inspecting !== c.sid || !info)) {
            dex.inspecting = c.sid;
            send({ do: "inspect", id: c.sid });
        }
        ui.ownView = { sid: c ? c.sid : null, card: JSON.stringify((info && info.card) || null), status: info ? info.status : null };
        var pane = ui.ownPane;
        if (!pane || pane.root.parentNode !== ui.details) {
            var cols = columns("own");
            pane = ui.ownPane = { root: cols.root, right: cols.right, copies: el("div", "bl4dex_copy_box"), farm: farmBox(),
                                  rightSig: null, copiesSig: null, farmSig: null };  // prettier-ignore
            cols.left.appendChild(pane.copies);
            cols.left.appendChild(pane.farm.root);
        }

        // The card on the right, at the top. No copy here (none held, or the owner picked has none):
        // a card rolled at your level instead (the old Items tab's, merged in: the user's ask 2026-11-06),
        // patched in place as rolls come (patchRoll); redrawn when it has no card to patch yet
        var sample = !c && shown;
        if (sample && dex.showing !== item.key) {
            // Python rolls one if there's none yet; its rolls are what the state reports
            dex.showing = item.key;
            send({ do: "show", item: item.key });
        }
        var rollSig = null;
        if (sample) {
            var got = rollsOf(item), last = got.rolls.length ? got.rolls[got.rolls.length - 1] : null;
            rollSig = last && last.card ? "card" : [(got.r && got.r.status) || "", got.rolls.length];
        }
        var rightSig = JSON.stringify([item.key, ui.ownView, collState().char, rollSig]);
        if (rightSig !== pane.rightSig) {
            pane.rightSig = rightSig;
            var r = pane.right;
            r.innerHTML = "";
            var buttons = el("div", "bl4dex_dropper_buttons");
            ui.take = null;
            ui.rollView = null;
            if (sample) {
                ui.rollView = { key: item.key, img: null, imgUrl: null, effects: [] };
                rollSection(r, item);
                skillSection(r, item);
                if (rollsOf(item).rolls.length) {
                    buttons.appendChild(chip("See parts", false, function () {
                        openParts(item.key);
                    }));
                }
            } else if (c) {
                if (info && info.card) {
                    cardPeek(r, info.card, function () {
                        openParts(item.key, c.sid);
                    });
                } else if (info && info.status && info.status !== "reading") {
                    r.appendChild(el("p", "bl4dex_detail_note bl4dex_bad", info.status));
                } else {
                    r.appendChild(el("p", "bl4dex_detail_note", "Reading its stats..."));
                }
                buttons.appendChild(chip("Copy serial", false, function () {
                    send({ do: "copy_serial", id: c.sid });
                }));
                // Another character's or the bank's copy (not one that went): to the one played
                // (transfer.py moves it; the user's ask 2026-11-06)
                var here = collState().char;
                if (!c.gone && here && c.char !== here && (c.char === BANK || dex.coll.chars[c.char])) {
                    var take = chip("", false, function () {
                        if (!state.taking) send({ do: "take", id: c.sid, from: c.char });
                    }, "bl4dex_take");
                    ui.take = { node: take, sid: c.sid, label: "Move to " + charName(here), shown: null };  // (the user's wording)
                    patchTake();
                    buttons.appendChild(take);
                }
            }
            if (buttons.firstChild) r.appendChild(buttons);
        }

        // The copies: a page, a sort or an opened group changes only them (the card and Farm More stay)
        var copiesSig = JSON.stringify([item.key, dex.coll.v, view.ownWho, levelText(), view.copySort, view.copyHideGone, view.copyPage, view.copyOpen,
                                        c ? c.id : null]);
        if (copiesSig !== pane.copiesSig) {
            pane.copiesSig = copiesSig;
            pane.copies.innerHTML = "";
            copyList(pane.copies, cs, c);
        }

        fillFarm(pane.farm, item, shown);
        return pane.root;
    }

    // Who drops the item: a heading, a card per boss (portrait, name, per kill and Big Encore, its
    // trip buttons) and a world-drop line. The cards are kept and refilled: their icons never leave
    // the page.
    function farmBox() {
        var root = el("div", "bl4dex_farm");
        var head = el("p", "bl4dex_section", "FARM MORE");
        var cards = el("div", "bl4dex_farm_cards");
        root.appendChild(head);
        root.appendChild(cards);
        return { root: root, head: head, cards: cards, list: [], sig: null };
    }
    function fillFarm(farm, item, shown) {
        var ids = (item.bosses || []).filter(function (id) {
            return !!data.bossById[id];
        });
        var trip = state && state.trip;
        var sig = JSON.stringify([item.key, shown, ids.map(function (id) { return [beaten(id), bossShown(data.bossById[id])]; }),
                                  uvhLabel(), trip && trip.boss, trip && trip.state, picturesRev]);  // prettier-ignore
        if (sig === farm.sig) return;
        farm.sig = sig;
        farm.root.classList.toggle("bl4dex_hidden", !ids.length);
        for (var i = 0; i < Math.max(ids.length, farm.list.length); i++) {
            var card = farm.list[i];
            if (i >= ids.length) {
                card.root.classList.add("bl4dex_hidden");
                continue;
            }
            if (!card) {
                card = farm.list[i] = farmCard();
                farm.cards.appendChild(card.root);
            }
            card.root.classList.remove("bl4dex_hidden");
            fillFarmCard(card, data.bossById[ids[i]], item, shown);
        }
    }
    function farmCard() {
        var card = { root: el("div", "bl4dex_dropper"), pic: el("div", "bl4dex_dropper_picture_slot"), name: el("p", "bl4dex_dropper_name"),
                     tag: el("p", "bl4dex_tag bl4dex_tag_beaten", "BEATEN"), area: el("p", "bl4dex_cell_sub"),
                     kill: chanceLine(ICON_KILL, "Per kill", "", " ", ""), encore: chanceLine(ICON_ENCORE, "Big Encore", "", " ", "bl4dex_chance_encore"),
                     trips: el("div", "bl4dex_farm_trips"), boss: null, picFor: null };  // prettier-ignore
        function toBoss() {
            if (card.boss) goTo("bosses", card.boss.id);
        }
        onClick(card.pic, toBoss);
        card.root.appendChild(card.pic);
        var info = el("div", "bl4dex_dropper_info");
        var head = el("div", "bl4dex_dropper_head");
        head.appendChild(card.name);
        head.appendChild(card.tag);
        onClick(head, toBoss);
        info.appendChild(head);
        info.appendChild(card.area);
        info.appendChild(card.kill);
        info.appendChild(card.encore);
        info.appendChild(card.trips);
        card.root.appendChild(info);
        card.killRate = card.kill.querySelector(".bl4dex_chance_rate");
        card.killNote = card.kill.querySelector(".bl4dex_chance_note");
        card.encoreRate = card.encore.querySelector(".bl4dex_chance_rate");
        return card;
    }
    function fillFarmCard(card, b, item, shown) {
        var drop = null;
        b.drops.forEach(function (x) {
            if (x.item === item.key) drop = x;
        });
        var bShown = bossShown(b);
        card.boss = b;
        card.root.className = "bl4dex_dropper" + (bShown ? "" : " bl4dex_locked");
        // The portrait only when its boss (or how it's shown) changed: a new picture is a new element
        var picFor = b.id + "|" + bShown + "|" + picturesRev;
        if (card.picFor !== picFor) {
            card.picFor = picFor;
            card.pic.innerHTML = "";
            card.pic.appendChild(portrait(b, "bl4dex_dropper_picture"));
        }
        setText(card.name, bShown ? b.name : "???");
        card.tag.classList.toggle("bl4dex_hidden", !beaten(b.id));
        setText(card.area, b.area === b.region ? b.region : b.area + ", " + b.region);
        setText(card.killRate, shown && drop ? pct(uvhChance(drop)) : "?");
        setText(card.killNote, uvhLabel() || " ");
        card.encore.classList.toggle("bl4dex_hidden", !b.encore);
        setText(card.encoreRate, shown && drop ? pct(drop.encore) : "?");
        card.trips.innerHTML = "";
        tripButtons(card.trips, b);
    }

    // The game's card as a button to its parts: lifts and frames on hover, sinks while pressed (the
    // transitions are on the holder's transform, not the card's own, which the dex pins)
    function cardPeek(parent, card, onOpen) {
        var peek = el("div", "bl4dex_peek");
        parent.appendChild(peek);
        gameCard(peek, card);
        var frame = el("div", "bl4dex_peek_frame");
        peek.appendChild(frame);
        var hint = el("div", "bl4dex_peek_hint");
        hint.appendChild(el("p", "bl4dex_peek_text", "SEE PARTS"));
        peek.appendChild(hint);
        peek.addEventListener("mouseenter", function () {
            // The frame and the tab on the card as it's seen: its top and bottom halves (the
            // tooltip around them is ~22 px wider each side for the frame art; one measure a hover)
            var top = peek.querySelector(".item_card_top_half");
            var bottom = peek.querySelector(".item_card_bottom_half");
            if (top && bottom) {
                var p = peek.getBoundingClientRect();
                var a = top.getBoundingClientRect();
                var b = bottom.getBoundingClientRect();
                var left = Math.min(a.left, b.left) - p.left;
                var width = Math.max(a.right, b.right) - p.left - left;
                frame.style.left = left - 6 + "px";
                frame.style.top = a.top - p.top - 6 + "px";
                frame.style.width = width + 12 + "px";
                frame.style.height = b.bottom - a.top + 12 + "px";
                hint.style.marginLeft = left + "px";
                hint.style.width = width + "px";
            }
            peek.classList.add("bl4dex_peek_on");
        });
        peek.addEventListener("mouseleave", function () {
            peek.classList.remove("bl4dex_peek_on");
            peek.classList.remove("bl4dex_peek_down");
        });
        peek.addEventListener("mousedown", function () {
            peek.classList.add("bl4dex_peek_down");
        });
        peek.addEventListener("mouseup", function () {
            peek.classList.remove("bl4dex_peek_down");
        });
        onClick(peek, onOpen);
    }

    var COPY_SORTS = [["level", "Level"], ["owner", "Owner"], ["new", "Newest"]];
    // The copies held, in groups of the same level and holder (by level or by holder), or one row
    // each, newest first. A group of one, or a row, shows that copy; a bigger group opens to its copies.
    function copyList(right, cs, shown) {
        var head = el("div", "bl4dex_section bl4dex_copies_head");
        var sorts = el("div", "bl4dex_copy_sorts");
        COPY_SORTS.forEach(function (s) {
            sorts.appendChild(chip(s[1], view.copySort === s[0], function () {
                if (view.copySort === s[0]) return;
                view.copySort = s[0];
                view.copyOpen = null;
                view.copyPage = 1;
                saveView();
                renderDetails();  // (the Collection redraws only what changed: ownDetails)
            }, "bl4dex_seg"));
        });
        head.appendChild(sorts);
        // The sold and dropped ones off the list (only an item that has some; "Hide sold / dropped"
        // was too wide for the column and went under the sorts)
        if (cs.gone.length) {
            head.appendChild(chip("Hide sold", view.copyHideGone, function () {
                view.copyHideGone = !view.copyHideGone;
                view.copyPage = 1;
                saveView();
                renderDetails();  // (the Collection redraws only what changed: ownDetails)
            }, "bl4dex_copy_hide"));
        }
        right.appendChild(head);
        if (!cs.held.length) right.appendChild(el("p", "bl4dex_detail_note", cs.gone.length ? "None held now." : "None yet."));
        // Every row's maker first (groups, an open group's copies, the ones that went), then only one
        // page of rows is built (an item can have thousands of copies)
        var rows = [];
        var by = view.copySort;
        if (by === "new") {
            cs.held.slice().sort(function (a, b) {
                return b.t - a.t || b.level - a.level;
            }).forEach(function (c) {
                rows.push(function () {
                    return copyRow(c, [c], shown, null);
                });
            });
        } else {
            var groups = {};
            var order = [];
            cs.held.forEach(function (c) {
                var k = c.level + "|" + c.char;
                if (!groups[k]) {
                    groups[k] = { key: k, level: c.level, char: c.char, copies: [] };
                    order.push(groups[k]);
                }
                groups[k].copies.push(c);
            });
            order.sort(function (a, b) {
                if (by === "owner") return cmp(charName(a.char), charName(b.char)) || b.level - a.level;
                return b.level - a.level || b.copies.length - a.copies.length || cmp(charName(a.char), charName(b.char));
            });
            order.forEach(function (g) {
                g.copies.sort(function (a, b) {
                    return b.t - a.t;
                });
                var open = g.copies.length > 1 && view.copyOpen === g.key;
                rows.push(function () {
                    return copyRow(g.copies[0], g.copies, shown, g.key);
                });
                if (open) {
                    g.copies.forEach(function (c, n) {
                        rows.push(function () {
                            var row = el("div", "bl4dex_copy_sub" + (shown && shown.id === c.id ? " bl4dex_selected" : ""));
                            row.appendChild(el("p", "bl4dex_copy_n", "Copy " + (n + 1)));
                            row.appendChild(el("p", "bl4dex_copy_when", acquired(c)));
                            onClick(row, function () {
                                pickCopy(c);
                            });
                            return row;
                        });
                    });
                }
            });
        }
        if (cs.gone.length && !view.copyHideGone) {
            rows.push(function () {
                return el("p", "bl4dex_section bl4dex_copy_row_head", "NO LONGER HELD   " + cs.gone.length);
            });
            cs.gone.slice().sort(function (a, b) {
                return b.gone - a.gone;
            }).forEach(function (c) {
                rows.push(function () {
                    return copyRow(c, [c], shown, null, true);
                });
            });
        }
        var pages = Math.max(1, Math.ceil(rows.length / COPY_PAGE));
        var page = Math.min(Math.max(1, view.copyPage || 1), pages);
        view.copyPage = page;
        var shownRows = rows.slice((page - 1) * COPY_PAGE, page * COPY_PAGE).map(function (make) {
            return make();
        });
        // With pages, every page is 10 rows tall (blank ones on the last): the pager stays put
        while (pages > 1 && shownRows.length < COPY_PAGE) shownRows.push(el("div", "bl4dex_copy bl4dex_copy_blank"));
        shownRows.forEach(function (r) {
            right.appendChild(r);
        });
        ui.pager = null;
        if (pages > 1) right.appendChild(pager(page, pages));
    }

    // Pages of the copies list (an item can have a hundred and more): ◀ Page [n] of N ▶. A click on
    // the number types one (Python's text box, digits; Enter jumps: state.page_go). Picking another
    // item or another copy sort starts at page 1.
    var COPY_PAGE = 10;
    function goPage(n) {
        view.copyPage = n;
        renderDetails();  // (the Collection redraws only what changed: ownDetails)
    }
    function pager(page, pages) {
        var bar = el("div", "bl4dex_pager");
        var prev = el("div", "bl4dex_chip bl4dex_scroll_arrow" + (page > 1 ? "" : " bl4dex_pager_off"));
        prev.appendChild(el("div", "bl4dex_scroll_left"));
        onClick(prev, function () {
            if (page > 1) goPage(page - 1);
        });
        bar.appendChild(prev);
        bar.appendChild(el("p", "bl4dex_pager_text", "Page"));
        var box = el("div", "bl4dex_pager_box");
        var num = el("p", "bl4dex_pager_num", String(page));
        box.appendChild(num);
        onClick(box, function () {
            send({ do: "type", on: !(state && state.typing_page), field: "page" });
        });
        bar.appendChild(box);
        bar.appendChild(el("p", "bl4dex_pager_text", "of " + pages));
        var next = el("div", "bl4dex_chip bl4dex_scroll_arrow" + (page < pages ? "" : " bl4dex_pager_off"));
        next.appendChild(el("div", "bl4dex_scroll_right"));
        onClick(next, function () {
            if (page < pages) goPage(page + 1);
        });
        bar.appendChild(next);
        ui.pager = { box: box, num: num, page: page, pages: pages };
        patchPager();
        return bar;
    }
    // The Level filter while it's typed (each poll): the typed text applies at once; the chip's text
    // and lit state follow (only those change)
    function patchLevel() {
        if (!state || view.tab !== "collection") return;
        var typing = !!state.typing_level;
        if (typing && (state.level_text || "") !== (view.ownLevel || "")) {
            setView({ ownLevel: state.level_text || "" });
        } else if (typing !== !!ui.levelTyping) {
            updateChips();
        }
        ui.levelTyping = typing;
    }
    // The Take button while its copy moves (each poll: only its text and a class change)
    function patchTake() {
        var t = ui.take;
        if (!t || !state) return;
        var moving = state.taking === t.sid;
        if (t.shown === moving) return;
        t.shown = moving;
        t.node.firstChild.textContent = moving ? "Moving..." : t.label;
        t.node.classList.toggle("bl4dex_active", moving);
    }
    // The number box while a page number is typed (each poll; only text and a class change)
    function patchPager() {
        var p = ui.pager;
        if (!p || !state) return;
        var typing = !!state.typing_page;
        p.box.classList.toggle("bl4dex_typing", typing);
        var text = typing ? (state.page_text || "") + "_" : String(p.page);
        if (p.num.textContent !== text) p.num.textContent = text;
    }
    // Enter on a typed page number (state.page_go moves): to that page. The first state seen only
    // says where the count is (a reload mustn't jump again).
    function followPageGo(go) {
        if (!go) return;
        var seen = dex.pageGo;
        dex.pageGo = go.id;
        if (seen === undefined || go.id === seen || !ui.pager || !go.page) return;
        goPage(Math.min(Math.max(1, go.page), ui.pager.pages));
    }

    function copyRow(c, copies, shown, groupKey, gone) {
        var many = copies.length > 1;
        var mine = shown && copies.some(function (x) {
            return x.id === shown.id;
        });
        var row = el("div", "bl4dex_copy" + (mine ? " bl4dex_selected" : "") + (gone ? " bl4dex_copy_gone" : ""));
        // (A CSS triangle: the game's font has no ▸ / ▾, they drew as boxes)
        var caret = el("div", "bl4dex_copy_caret");
        if (many) caret.appendChild(el("div", view.copyOpen === groupKey ? "bl4dex_caret_open" : "bl4dex_caret_shut"));
        row.appendChild(caret);
        row.appendChild(el("p", "bl4dex_copy_level", String(c.level)));
        row.appendChild(holderBadge(c.char, "bl4dex_holder_small"));
        row.appendChild(el("p", "bl4dex_copy_name", charName(c.char)));
        row.appendChild(el("p", "bl4dex_copy_count", many ? "×" + copies.length : ""));
        var newest = copies[0];
        row.appendChild(el("p", "bl4dex_copy_when", gone ? "Gone " + day(c.gone) : newest.t ? day(newest.t) : "Before"));
        onClick(row, function () {
            if (!many) {
                pickCopy(c);
                return;
            }
            view.copyOpen = view.copyOpen === groupKey ? null : groupKey;
            saveView();
            renderDetails();  // (the Collection redraws only what changed: ownDetails)
        });
        return row;
    }

    // Only the shown copy's picture changed (the parts screen shows it, the details don't): nothing to
    // redraw. Its card or status changed: false (the caller redraws; the card arrives once per serial)
    function patchOwn() {
        var rv = ui.ownView;
        if (!rv || view.tab !== "collection") return false;
        if (!rv.sid) return patchRoll();  // (no copy: the rolled card)
        var info = shownInfo(rv.sid);
        return JSON.stringify((info && info.card) || null) === rv.card && (info ? info.status : null) === rv.status;
    }

    // Two columns in the details pane (the boss or the copies on the left, its drops or card on the right)
    function columns(kind) {
        var cols = el("div", "bl4dex_cols bl4dex_cols_" + kind);
        var left = el("div", "bl4dex_col_left");
        var right = el("div", "bl4dex_col_right");
        cols.appendChild(left);
        cols.appendChild(right);
        ui.details.appendChild(cols);
        return { root: cols, left: left, right: right };
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
            // Not obtained yet ("Obtained items only" option): "???" and its silhouette (Spoiler free:
            // "?", and "???" for its type too)
            var got = itemShown(item);
            var row = el("div", "bl4dex_row bl4dex_row_link" + (got ? "" : " bl4dex_locked"));
            // The item's inventory picture left of its name
            var thumb = el("div", "bl4dex_drop_thumb" + (got ? "" : " bl4dex_silhouette"));
            var thumbUrl = itemPictureUrl(item.key, true);
            if (bare(item)) thumb = unknownMark(el("div", "bl4dex_drop_thumb"));
            else if (thumbUrl) thumb.style.backgroundImage = thumbUrl;
            row.appendChild(thumb);
            var name = el("div", "bl4dex_cell_name");
            name.appendChild(el("p", "bl4dex_cell_title bl4dex_name_" + rarityOf(item), got ? item.name : "???"));
            name.appendChild(el("p", "bl4dex_cell_sub", typeLine(item, true)));
            row.appendChild(name);
            row.appendChild(el("p", "bl4dex_cell_num", pct(drop.uvh0)));
            row.appendChild(el("p", "bl4dex_cell_num", pct(uvhChance(drop))));
            row.appendChild(el("p", "bl4dex_cell_num", b.encore ? pct(drop.encore) : "-"));
            onClick(row, function () {
                goTo("collection", item.key);
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
        if (!rv || !rv.holder || !rv.head || view.tab !== "collection" || current.collection !== rv.key) return false;
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

    // Waypoint / Travel to the boss's Big Encore machine (travel.py), on its farm card under a
    // Collection item and on its own page; nothing for bosses without one (open-world bosses)
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
    // region Parts

    // The parts screen (click an item's picture): the gun itself on the left (its picture, maker,
    // full name, the game's own card for this roll), and every part of the roll on the right, each
    // as its own tile: the game's render of that part alone, its slot, name and manufacturer, and
    // what it does to the item. Python measures it (bl4_weapondex/breakdown.py: the roll without that
    // one part, read by the game). Drawn with the game's own art: manufacturer logos, the item
    // card's better / worse arrows and backings, element backings, stat and skill icons, its markup.
    var CARD_ART = "coui://uiresources/item_card/assets/UI_Art_Assets/";
    var MAKER_ART = "coui://uiresources/_shared/assets/ui_art_manufacturer_logos/";
    var ICON_LEGENDARY = "coui://uiresources/_shared/assets/ico_ui_art_discovery/ico_misc_legendaries.png";
    var PARTS_CARD = 2;  // the parts screen's own game card widget (0 is the details pane's, 1 the hover card)

    function makerArt(maker, kind) {
        // kind: "logomark", "logotype", or "logomark_small" (its 44 px copy: ..._logomark_<maker>_small)
        if (!maker) return null;
        var small = kind === "logomark_small";
        return "url(" + MAKER_ART + "ui_art_manu_" + (small ? "logomark" : kind) + "_" + maker.toLowerCase() + (small ? "_small" : "") + ".png)";
    }

    // `own`: a Collection entry's id (its exact item), else the item's latest roll
    function openParts(key, own) {
        dex.partsFor = key;
        dex.partsOwn = own || null;
        dex.partsSig = null;
        send(own ? { do: "parts", item: key, own: own } : { do: "parts", item: key });
        renderParts();
    }

    function closeParts() {
        if (!dex.partsFor) return;
        dex.partsFor = null;
        dex.partsOwn = null;
        dex.partsSig = null;
        dex.partsClosedAt = Date.now();
        send({ do: "parts_close" });
        renderParts();
    }

    function partsState() {
        var p = state && state.parts;
        return p && p.item === dex.partsFor && (p.own || null) === (dex.partsOwn || null) ? p : null;
    }

    // A part with nothing to show on the card (looks, handling, zoom)
    function partQuiet(part) {
        return !part.core && !part.stats.length && !part.added.length && !part.removed.length && !(part.changed || []).length &&
            !part.skills.length && !(part.elements || []).length && !(part.variants || []).length;
    }

    // One screen, no scrolling: the parts stacked on the left, the gun in the middle with the picked
    // part's full details under it, the game's card on the right. Its three columns stay put between
    // redraws: the card is built once per roll (not on a click or when a part's picture comes in),
    // and a click only moves the highlight and redraws the details.
    function renderParts() {
        if (!ui.parts) return;
        var item = dex.partsFor && data && data.itemByKey[dex.partsFor];
        var own = dex.partsOwn;
        // Only over the item's own details, or its Collection copy's (another tab, item or copy closes it)
        var over = own ? view.tab === "collection" && !!item && current.collection === item.key && !!ui.ownView && ui.ownView.sid === own
                       : view.tab === "collection" && !!item && current.collection === item.key && itemShown(item);  // prettier-ignore
        if (!item || !view.open || !over) {
            if (dex.partsFor) closeParts();
            ui.parts.classList.add("bl4dex_hidden");
            ui.parts.innerHTML = "";
            ui.partsBuilt = null;
            return;
        }
        var p = partsState();
        var latest, rolling, picture;
        if (own) {
            // The entry's own card (and picture), as its details show them
            var info = shownInfo(own);
            latest = info && info.card ? { serial: info.serial, card: info.card, elements: info.elements || [] } : null;
            rolling = false;
            picture = info && info.picture;
        } else {
            var rolls = rollsOf(item).rolls;
            latest = rolls.length ? rolls[rolls.length - 1] : null;
            rolling = !!(state.roll && state.roll.item === item.key && state.roll.status === "rolling");
            picture = state.roll && state.roll.picture;
        }
        var sig = JSON.stringify([p, own, latest && latest.serial, !!(latest && latest.card), rolling, picture, view.partsFilter]);
        if (sig === dex.partsSig && ui.parts.firstChild) return;
        dex.partsSig = sig;
        ui.parts.classList.remove("bl4dex_hidden");
        // The frame: bar, then the three columns (made once per item)
        if (!ui.partsBuilt || ui.partsBuilt.item !== item.key || ui.partsBuilt.own !== own || !ui.partsBuilt.bar.parentNode) {
            ui.parts.innerHTML = "";
            var built = { item: item.key, own: own, bar: el("div", "bl4dex_parts_bar"), left: el("div", "bl4dex_parts_left"),
                          gun: el("div", "bl4dex_parts_gun"), card: el("div", "bl4dex_parts_card"), cardFor: null };  // prettier-ignore
            var body = el("div", "bl4dex_parts_body");
            body.appendChild(built.left);
            body.appendChild(built.gun);
            body.appendChild(built.card);
            ui.parts.appendChild(built.bar);
            ui.parts.appendChild(body);
            ui.partsBuilt = built;
        }
        var b = ui.partsBuilt;
        b.p = p;
        b.item = item.key;
        // Each piece redraws only when its own data changed: Python sends the parts in as it reads
        // them (and their pictures, and the chances), and a full redraw on each made it all flicker
        var barSig = JSON.stringify([p ? p.count || p.parts.length : 0, own, rolling, view.partsFilter]);
        if (b.barSig !== barSig) {
            var fresh = partsBar(item, p, rolling, own);
            b.bar.parentNode.replaceChild(fresh, b.bar);
            b.bar = fresh;
            b.barSig = barSig;
        }
        b.parts = p ? p.parts : [];
        var picked = pickedPart(b);
        fillPartsColumn(b.left, item, p, picked, rolling, latest);
        fillGunColumn(b, item, latest, picked, own);
        // The card: only when the roll changed
        var cardKey = latest && latest.card ? latest.serial : rolling ? "rolling" : "";
        if (b.cardFor !== cardKey) {
            b.cardFor = cardKey;
            b.card.innerHTML = "";
            if (latest && latest.card) fitCard(b.card, gameCard(b.card, latest.card, PARTS_CARD));
            else if (rolling) b.card.appendChild(el("p", "bl4dex_empty", "Rolling one at your level..."));
        }
    }

    // The picked part: kept by position while it's in this roll, else the first that does something
    function pickedPart(b) {
        var parts = b.parts || [];
        var picked = null;
        if (dex.partPick && b.p && dex.partPick.serial === b.p.serial) picked = parts[dex.partPick.at] || null;
        return picked || parts.filter(function (x) { return !partQuiet(x); })[0] || parts[0] || null;
    }

    // A click on a part: the highlight moves, the details redraw; nothing else changes
    function pickPart(at) {
        var b = ui.partsBuilt;
        if (!b || !b.p) return;
        dex.partPick = { serial: b.p.serial, at: at };
        var picked = pickedPart(b);
        var tiles = b.left.querySelectorAll(".bl4dex_part");
        for (var i = 0; i < tiles.length; i++) {
            tiles[i].classList.toggle("bl4dex_part_picked", tiles[i].bl4dexAt === at);
        }
        if (b.detail) {
            b.detail.innerHTML = "";
            if (picked) partDetail(b.detail, picked, data.itemByKey[b.item]);
            b.detailSig = detailSig(picked);
        }
    }

    // What the picked part's details show (its picture isn't among it)
    function detailSig(part) {
        return JSON.stringify(part || null, function (k, v) {
            return k === "picture" ? undefined : v;
        });
    }

    // Puts `nodes` in `col` in order, moving the ones already there instead of making them again
    function syncChildren(col, nodes) {
        for (var i = 0; i < nodes.length; i++) {
            var at = col.childNodes[i];
            if (at === nodes[i]) continue;
            if (at) col.insertBefore(nodes[i], at);
            else col.appendChild(nodes[i]);
        }
        while (col.childNodes.length > nodes.length) col.removeChild(col.lastChild);
    }

    // The parts, stacked: one row each, all the same height in either filter (sharing the
    // column's height squeezed "All parts"); the column scrolls when they don't fit. A tile is made again only
    // when its part changed (its picture or chance came in), so the rest stay still as parts arrive
    function fillPartsColumn(col, item, p, picked, rolling, latest) {
        var keep = col.scrollTop;
        var cache = col.bl4dexCache || {};
        var next = {};
        var nodes = [];
        function put(key, sig, make, into) {
            var had = cache[key];
            var node = had && had.sig === sig ? had.node : make();
            next[key] = { sig: sig, node: node };
            (into || nodes).push(node);
            return node;
        }
        if (!p || (p.status === "working" && !p.parts.length)) {
            var wait = !dex.partsOwn && (rolling || !latest) ? "Rolling one at your level..." : "Taking it apart...";
            put("wait", wait, function () { return el("p", "bl4dex_empty", wait); });
        } else {
            if (p.status && p.status !== "working") {
                put("status", p.status, function () { return el("p", "bl4dex_empty bl4dex_bad", p.status); });
            }
            var parts = p.parts;
            var random = view.partsFilter === "random";
            var shown = parts.filter(function (part) {
                return !random || !partFixed(part);
            });
            // The open drop-down (its slot's other parts) goes under its part, or its group
            var open = p.slot && p.slot.at ? p.slot.at - 1 : -1;
            var openSlot = open >= 0 && parts[open] ? parts[open].slot_n : null;
            // A slot that can pick more than one part: its parts in one group, under a header
            // saying how many it rolls ("ROLLS 1 TO 2"), one drop-down for the group
            var groups = {};
            shown.forEach(function (part) {
                if (multiSlot(part)) (groups[part.slot_n] = groups[part.slot_n] || []).push(part);
            });
            // In order: the groups, then the other random parts, then the fixed ones (every roll has
            // them) under their own heading, so the random ones never have fixed ones between them
            function rank(part) {
                return multiSlot(part) ? 0 : partFixed(part) ? 2 : 1;
            }
            shown = shown.map(function (part, i) { return { part: part, i: i }; })
                .sort(function (a, b) { return rank(a.part) - rank(b.part) || a.i - b.i; })
                .map(function (x) { return x.part; });
            var fixedHead = false;
            var placed = {};
            function tileOf(part, into) {
                var at = parts.indexOf(part);
                var grouped = multiSlot(part);
                var isOpen = grouped ? part.slot_n === openSlot : at === open;
                // (Not its picture: a picture coming in is put in its place, updateArt. Made again,
                // the tile and the column around it flashed as each picture loaded: user report)
                // (Nor its chance: the chances come in a moment after the parts, and rebuilding every
                // tile for them flashed all their pictures at once)
                var sig = JSON.stringify([p.serial, JSON.stringify(part, noArtChance), isOpen, grouped]);
                var tile = put("part" + at, sig, function () { return partTile(part, item, p, at, isOpen, grouped); }, into);
                if (tile.bl4dexArt) updateArt(tile.bl4dexArt, part.picture, part.box, p.pictures === "waiting" ? "..." : "");
                updateChance(tile.bl4dexChance, part.chance, p.chances);
                tile.classList.toggle("bl4dex_part_picked", part === picked);
            }
            function dropdown(key) {
                var slotSig = JSON.stringify([p.serial, JSON.stringify(p.slot, noArt), p.chances, p.rolls]);
                var drop = put(key, slotSig, function () { return slotDropdown(item, p); });
                (drop.bl4dexArts || []).forEach(function (a) {
                    var c = p.slot.parts[a.i];
                    if (c) updateArt(a.art, c.picture, c.box, c.maker ? "..." : "");
                });
            }
            shown.forEach(function (part) {
                if (rank(part) === 2 && !fixedHead && shown.length > 1 && rank(shown[0]) !== 2) {
                    fixedHead = true;
                    put("fixedHead", "", function () {
                        var head = el("div", "bl4dex_parts_section");
                        head.appendChild(el("p", "bl4dex_parts_section_text", "FIXED PARTS"));
                        head.appendChild(el("p", "bl4dex_parts_section_sub", "every roll of this item has these"));
                        return head;
                    });
                }
                if (!multiSlot(part)) {
                    tileOf(part);
                    if (parts.indexOf(part) === open) dropdown("slot" + open);
                    return;
                }
                if (placed[part.slot_n]) return;
                placed[part.slot_n] = true;
                var members = groups[part.slot_n];
                var first = parts.indexOf(members[0]);
                var isOpen = part.slot_n === openSlot;
                var gsig = JSON.stringify([p.serial, part.slot, part.picks, members.length, isOpen]);
                var group = put("group" + part.slot_n, gsig, function () {
                    return partGroup(part, members.length, isOpen, first);
                });
                var inner = [];
                members.forEach(function (m) {
                    tileOf(m, inner);
                });
                syncChildren(group.bl4dexTiles, inner);
                if (isOpen) dropdown("slot" + first);
            });
            var hidden = parts.length - shown.length;
            if (hidden || p.status === "working") {
                var foot = p.status === "working" ? "Reading the rest of the parts..." :
                    hidden + (hidden === 1 ? " fixed part" : " fixed parts") + " hidden (every roll of this item has " + (hidden === 1 ? "it" : "them") + ").";
                put("foot", foot, function () { return el("p", "bl4dex_parts_foot", foot); });
            }
            col.classList.toggle("bl4dex_parts_left_open", open >= 0);
        }
        col.bl4dexCache = next;
        syncChildren(col, nodes);
        col.scrollTop = keep;
    }

    // A part whose slot can pick more than one (shown in a group with the others it rolled)
    function multiSlot(part) {
        return part.slot_n !== null && part.slot_n !== undefined && !!part.picks && part.picks[1] > 1;
    }

    // A group's frame: "BODY ACCESSORY  |  ROLLS 1 TO 2  |  2 IN THIS ROLL", the drop-down's toggle,
    // then its parts (bl4dexTiles, filled by fillPartsColumn)
    function partGroup(part, count, isOpen, first) {
        var box = el("div", "bl4dex_part_group" + (isOpen ? " bl4dex_part_group_open" : ""));
        var head = el("div", "bl4dex_part_group_head");
        var lo = part.picks[0], hi = part.picks[1];
        head.appendChild(el("p", "bl4dex_part_group_slot", (part.slot || "Slot").toUpperCase()));
        head.appendChild(el("p", "bl4dex_part_group_picks", "ROLLS " + (lo === hi ? "" + hi : lo + " TO " + hi)));
        head.appendChild(el("p", "bl4dex_part_group_count", count + " IN THIS ROLL"));
        if (part.has_slot) {
            var toggle = el("div", "bl4dex_part_group_toggle");
            toggle.appendChild(el("p", "bl4dex_part_group_toggle_text", isOpen ? "HIDE OPTIONS" : "OTHER OPTIONS"));
            toggle.appendChild(caret(isOpen));
            head.appendChild(toggle);
            onClick(head, function () {
                toggleSlot(first);
            });
        }
        box.appendChild(head);
        box.bl4dexTiles = el("div", "bl4dex_part_group_tiles");
        box.appendChild(box.bl4dexTiles);
        return box;
    }

    // A small triangle drawn by CSS (the game's font has no arrow glyphs: they showed as a box)
    function caret(up) {
        return el("div", "bl4dex_caret " + (up ? "bl4dex_caret_up" : "bl4dex_caret_down"));
    }

    // A part every roll of this item has (its slot has no other pick for it), so not a random one.
    // Unknown until the chances are in: shown till then.
    function partFixed(part) {
        return part.chance !== null && part.chance !== undefined && part.chance >= 0.9995;
    }

    // Opens (or closes) a part's drop-down: every part its slot can roll, with what each does
    function toggleSlot(at) {
        var p = partsState();
        if (!p) return;
        // A group's parts share one drop-down: asked for by its first part
        var part = p.parts[at];
        if (part && multiSlot(part)) {
            for (var i = 0; i < p.parts.length; i++) {
                if (p.parts[i].slot_n === part.slot_n) {
                    at = i;
                    break;
                }
            }
        }
        var isOpen = p.slot && p.slot.at === at + 1;
        pickPart(at);
        send({ do: "part_slot", at: isOpen ? null : at + 1 });
    }

    // Back, the title and part count, the filters, the arrows' legend, Roll another
    function partsBar(item, p, rolling, own) {
        var bar = el("div", "bl4dex_parts_bar");
        bar.appendChild(button("Back", closeParts, "bl4dex_parts_back"));
        var title = el("div", "bl4dex_parts_title");
        title.appendChild(el("p", "bl4dex_parts_title_text", "PARTS"));
        var count = p ? p.count || p.parts.length : 0;
        if (count) title.appendChild(el("p", "bl4dex_parts_title_count", count + (own ? " on this item" : " in this roll") + "  |  click one for its details"));
        bar.appendChild(title);
        var filters = el("div", "bl4dex_parts_filters");
        // (kept for every item, and between sessions: saveView)
        filters.appendChild(chip("All parts", view.partsFilter !== "random", function () {
            view.partsFilter = "all";
            saveView();
            renderParts();
        }));
        filters.appendChild(chip("Random parts", view.partsFilter === "random", function () {
            view.partsFilter = "random";
            saveView();
            renderParts();
        }));
        bar.appendChild(filters);
        var legend = el("div", "bl4dex_parts_legend");
        legend.appendChild(arrow(true));
        legend.appendChild(el("p", "bl4dex_parts_legend_text", "better"));
        legend.appendChild(arrow(false));
        legend.appendChild(el("p", "bl4dex_parts_legend_text", "worse"));
        bar.appendChild(legend);
        if (!own) {
            bar.appendChild(chip(rolling ? "Rolling..." : "Roll another", rolling, function () {
                if (!rolling) send({ do: "roll", item: item.key, count: 1 });
            }, "bl4dex_parts_roll"));
        }
        return bar;
    }

    // The middle: the gun's picture (a Collection entry's own), the maker's logo with the full name
    // beside it, rarity and type, the level; under it the picked part in full
    // (made again only when what it shows changed: the picture, the elements, the level, the pick)
    function fillGunColumn(b, item, latest, picked, own) {
        var col = b.gun;
        // (own is the copy's page serial id, not a copy id: byId is by copy id and never had it, so
        // the copy's own picture never showed here)
        var e = own && dex.coll ? (dex.coll.byKey[item.key] || []).filter(function (c) {
            return c.sid === own;
        })[0] : null;
        var url = e ? ownPicture(e, shownInfo(own)) : bare(item) ? null : itemPictureFor(item, true);
        var level = latest && latest.card && latest.card.level ? String(latest.card.level).replace(/^Lvl\s*/i, "") : "";
        // The roll's own name (some legendaries change it with a part: "Upgraded Finnity XXX-L")
        var name = latest && latest.card && latest.card.name || item.name;
        var gunSig = JSON.stringify([item.key, own, url, latest && latest.elements, level, name]);
        if (b.gunSig === gunSig && b.detail && b.detail.parentNode === col) {
            var dsig = detailSig(picked);
            if (b.detailSig !== dsig) {
                b.detail.innerHTML = "";
                if (picked) partDetail(b.detail, picked, item);
                b.detailSig = dsig;
            }
            return;
        }
        b.gunSig = gunSig;
        col.innerHTML = "";
        var pic;
        if (e) {
            pic = el("div", "bl4dex_item_picture" + (url ? "" : " bl4dex_no_picture"));
            var img = el("div", "bl4dex_picture");
            if (url) img.style.backgroundImage = url;
            pic.appendChild(img);
        } else {
            pic = itemPicture(item, true);
        }
        pic.classList.add("bl4dex_parts_gun_picture");
        col.appendChild(pic);
        var head = el("div", "bl4dex_parts_gun_head");
        var line = el("div", "bl4dex_parts_gun_line");
        var logo = makerArt(item.maker, "logotype");
        if (logo) {
            var type = el("div", "bl4dex_parts_gun_logotype");
            type.style.backgroundImage = logo;
            line.appendChild(type);
        }
        line.appendChild(el("p", "bl4dex_parts_gun_name bl4dex_name_" + rarityOf(item), name));
        head.appendChild(line);
        var under = el("div", "bl4dex_parts_gun_line");
        var names = el("div", "bl4dex_parts_gun_names");
        var kind = [item.rarity, item.type].filter(Boolean).join(" ");
        if (item["class"]) kind += "  |  " + item["class"] + (item.hero ? " (" + item.hero + ")" : "");
        names.appendChild(el("p", "bl4dex_parts_gun_kind", kind));
        if (latest && latest.elements && latest.elements.length) {
            names.appendChild(el("p", "bl4dex_parts_gun_facts", latest.elements.join(" / ") + " element"));
        }
        under.appendChild(names);
        if (level) {
            var badge = el("div", "bl4dex_parts_level");
            badge.appendChild(el("p", "bl4dex_parts_level_word", "LEVEL"));
            badge.appendChild(el("p", "bl4dex_parts_level_value", level));
            under.appendChild(badge);
        }
        head.appendChild(under);
        col.appendChild(head);
        var detail = el("div", "bl4dex_parts_detail");
        if (picked) partDetail(detail, picked, item);
        col.appendChild(detail);
        b.detail = detail;
        b.detailSig = detailSig(picked);
    }

    // The picked part in full: slot, name, maker, then everything it does
    function partDetail(d, part, item) {
        var head = el("div", "bl4dex_parts_detail_head");
        var titles = el("div", "bl4dex_part_titles");
        titles.appendChild(el("p", "bl4dex_part_slot_text", part.slot.toUpperCase() + (part.core ? "  |  CORE PART" : "")));
        titles.appendChild(el("p", "bl4dex_parts_detail_name", part.name));
        titles.appendChild(makerLine(part, item));
        head.appendChild(titles);
        var mark = makerArt(part.maker, "logomark");
        if (mark) {
            var badge = el("div", "bl4dex_parts_detail_mark");
            badge.style.backgroundImage = mark;
            head.appendChild(badge);
        }
        d.appendChild(head);
        var body = el("div", "bl4dex_part_body");
        if (part.core) body.appendChild(el("p", "bl4dex_part_note", "The item's base numbers come from this part (it can't do without it):"));
        part.stats.forEach(function (s) {
            body.appendChild(statRow(s));
        });
        (part.elements || []).forEach(function (e) {
            var row = el("div", "bl4dex_part_element");
            // The game's element icon (its card backing art was stretched across the whole row)
            row.appendChild(markupElement("bl4dex_part_element_icon", "[" + e.toLowerCase() + "_icon]"));
            row.appendChild(el("p", "bl4dex_part_element_text", "Gives it " + e + " damage"));
            body.appendChild(row);
        });
        (part.skills || []).forEach(function (s) {
            var row = el("div", "bl4dex_part_skill bl4dex_part_skill_" + (s.color || "none"));
            var icon = el("div", "bl4dex_part_skill_icon");
            if (s.icon) icon.style.backgroundImage = "url(Texture2D'" + s.icon + "')";
            row.appendChild(icon);
            row.appendChild(el("p", "bl4dex_part_skill_points", (s.points > 0 ? "+" : "") + s.points));
            row.appendChild(el("p", "bl4dex_part_skill_name", s.name));
            row.appendChild(arrow(s.points > 0));
            body.appendChild(row);
        });
        part.added.forEach(function (t) {
            body.appendChild(perkRow(t.text, "adds", true));
        });
        (part.variants || []).forEach(function (t) {
            body.appendChild(perkRow(t, "adds", true));
        });
        (part.changed || []).forEach(function (t) {
            body.appendChild(perkRow(t.text, "changes", null));
        });
        part.removed.forEach(function (t) {
            body.appendChild(perkRow(t.text, "takes away", false));
        });
        if (partQuiet(part)) body.appendChild(el("p", "bl4dex_part_note", "No change to the card's numbers (looks, handling or zoom)."));
        if (part.note) body.appendChild(el("p", "bl4dex_part_data", "Game data: " + part.note));
        d.appendChild(body);
    }

    //  [logo] Jakobs (licensed part)   /   Shared part (any manufacturer)
    function makerLine(part, item) {
        var maker = el("div", "bl4dex_part_maker");
        var small = makerArt(part.maker, "logomark");
        if (small) {
            var icon = el("div", "bl4dex_part_maker_icon");
            icon.style.backgroundImage = small;
            maker.appendChild(icon);
        }
        var licensed = part.maker && item.maker && part.maker !== item.maker;
        maker.appendChild(el("p", "bl4dex_part_maker_text" + (part.maker ? "" : " bl4dex_part_maker_none"),
            part.maker ? part.maker + (licensed ? "  (licensed)" : "") : "Shared (any maker)"));
        return maker;
    }

    // A perk text's title: what comes before " - " ("[jakobs_icon]Jakobs-Licensed Accessory")
    function perkTitle(text) {
        var at = text.indexOf(" - ");
        return at > 0 ? text.slice(0, at) : text.length > 40 ? text.slice(0, 40) + "..." : text;
    }

    // A part's own render, centred and zoomed on where the part is drawn in it (`box`: [left, top,
    // right, bottom] as shares of the render, breakdown.py _measure). The game frames the whole item,
    // so a part alone sits where it does on the item. The render is 2:1, so is the frame (its height
    // from its width: padding), and the frame shows a 2:1 window of the render around the part.
    // A part's picture in its tile: made once, swapped in place when it comes in (or comes again,
    // breakdown._heal) and only when its picture or framing changed
    function artHolder(pic) {
        var holder = { pic: pic, node: el("p", "bl4dex_part_picture_none", ""), key: null };
        pic.insertBefore(holder.node, pic.firstChild);
        return holder;
    }
    function updateArt(holder, path, box, wait) {
        var key = path ? path + "|" + JSON.stringify(box || null) : "-" + wait;
        if (holder.key === key) return;
        holder.key = key;
        var node = path ? partPicture(path, box) : el("p", "bl4dex_part_picture_none", wait);
        holder.pic.replaceChild(node, holder.node);
        holder.node = node;
    }
    function noArt(k, v) {
        return k === "picture" || k === "box" ? undefined : v;
    }
    function noArtChance(k, v) {
        return k === "picture" || k === "box" || k === "chance" ? undefined : v;
    }
    // A part tile's chance ("37% of rolls"), swapped in place when it comes in
    function updateChance(holder, chance, chances) {
        var key = JSON.stringify([chance, chance == null ? chances : 0]);
        if (holder.key === key) return;
        holder.key = key;
        var node = chanceText(chance, chances);
        if (holder.node) holder.parent.replaceChild(node, holder.node);
        else holder.parent.appendChild(node);
        holder.node = node;
    }

    var PART_PAD = 1.2;  // room around the part
    var PART_ZOOM_MIN = 0.35;  // at most ~3x (the render is 512x256: more is blurry)
    function partPicture(path, box) {
        var url = "url(TextureRenderTarget2D'" + path + "')";
        if (!box) {
            var whole = el("div", "bl4dex_picture");
            whole.style.backgroundImage = url;
            return whole;
        }
        var s = Math.min(1, Math.max(PART_ZOOM_MIN, Math.max(box[2] - box[0], box[3] - box[1]) * PART_PAD));
        var cx = (box[0] + box[2]) / 2, cy = (box[1] + box[3]) / 2;
        var frame = el("div", "bl4dex_part_frame");
        var img = el("div", "bl4dex_part_zoomed");
        img.style.backgroundImage = url;
        // (Its height from padding, like the frame's: a % height came out 0 in Gameface, which
        // measures it against the frame's 0 content height, and the part showed, then went blank
        // when its box came in. Gameface takes this padding % of the image's own width (565 px wide
        // came out 807 tall at 50/s %): the render is 2:1, so 50%.)
        img.style.width = 100 / s + "%";
        img.style.height = "0";
        img.style.paddingTop = "50%";
        // Placed by translate, in shares of its own size (a % top was off: measured against the
        // frame's 0 content height too): the part's centre (cx, cy) on the frame's centre, which
        // is s/2 of the image's size from its corner
        img.style.left = "0";
        img.style.top = "0";
        img.style.transform = "translate(" + 100 * (s / 2 - cx) + "%, " + 100 * (s / 2 - cy) + "%)";
        frame.appendChild(img);
        return frame;
    }

    // A part with no model of its own: icons instead (a class mod skill's, a stat augment's; Texture2D
    // paths from the dex)
    function iconPicture(icons) {
        var box = el("div", "bl4dex_part_picture_icons");
        icons.forEach(function (path) {
            var icon = el("div", "bl4dex_part_picture_icon_one");
            icon.style.backgroundImage = "url(Texture2D'" + path + "')";
            box.appendChild(icon);
        });
        return box;
    }

    // One part as a compact tile: the game's picture of it alone, its slot, name and maker, and a
    // line of what it does (in full in the middle when picked)
    function partTile(part, item, p, at, open, grouped) {
        var tile = el("div", "bl4dex_part" + (part.core ? " bl4dex_part_core" : "") + (partQuiet(part) ? " bl4dex_part_quiet" : "") +
            (open && !grouped ? " bl4dex_part_open" : ""));
        tile.bl4dexAt = at;
        onClick(tile, function () {
            pickPart(at);
        });
        var pic = el("div", "bl4dex_part_picture");
        var skillIcons = (part.skills || []).map(function (s) { return s.icon; }).filter(Boolean);
        if (skillIcons.length) {
            // A skill part (class mods): the skill's own icon, not a render of the whole item
            pic.appendChild(iconPicture(skillIcons));
        } else if (part.maker) {
            // Its own picture, put in and swapped in place by updateArt (fillPartsColumn)
            tile.bl4dexArt = artHolder(pic);
            updateArt(tile.bl4dexArt, part.picture, part.box, p.pictures === "waiting" ? "..." : "");
        } else if ((part.elements || []).length) {
            // An element part has no model of its own: the game's element icon
            pic.appendChild(markupElement("bl4dex_part_picture_icon", part.elements.map(function (e) {
                return "[" + e.toLowerCase() + "_icon]";
            }).join(" ")));
        } else if (part.icon) {
            // A shared part with no model (a grenade's stat augment): the icon its card text has
            pic.appendChild(iconPicture([part.icon]));
        } else {
            pic.appendChild(el("p", "bl4dex_part_picture_none", ""));
        }
        var mark = makerArt(part.maker, "logomark");
        if (mark) {
            var badge = el("div", "bl4dex_part_maker_mark");
            badge.style.backgroundImage = mark;
            pic.appendChild(badge);
        }
        // A random part: its picture opens the drop-down of the other parts it could have been
        if (part.has_slot && !partFixed(part)) {
            pic.classList.add("bl4dex_part_picture_open");
            if (!grouped) {
                var more = el("div", "bl4dex_part_picture_more");
                more.appendChild(el("p", "bl4dex_part_picture_more_text", open ? "HIDE OPTIONS" : "OTHER OPTIONS"));
                more.appendChild(caret(open));
                pic.appendChild(more);
            }
            onClick(pic, function () {
                toggleSlot(at);
            });
        }
        tile.appendChild(pic);

        // A row: who it is (slot, name, maker), what it does, and its tally of better / worse
        var info = el("div", "bl4dex_part_info");
        var titles = el("div", "bl4dex_part_who");
        titles.appendChild(el("p", "bl4dex_part_slot_text", part.slot.toUpperCase()));
        titles.appendChild(el("p", "bl4dex_part_name", part.name));
        titles.appendChild(makerLine(part, item));
        info.appendChild(titles);
        info.appendChild(effectChips(part));
        var good = part.stats.filter(function (s) { return s.good === true; }).length + part.added.length + part.skills.length +
            (part.variants || []).length;
        var bad = part.stats.filter(function (s) { return s.good === false; }).length + part.removed.length;
        var top = el("div", "bl4dex_part_tally");
        var counts = el("div", "bl4dex_part_counts");
        if (part.core) counts.appendChild(el("p", "bl4dex_tag bl4dex_part_tag", "CORE"));
        else {
            if (good) counts.appendChild(leanCount(true, good));
            if (bad) counts.appendChild(leanCount(false, bad));
        }
        top.appendChild(counts);
        tile.bl4dexChance = { parent: top, node: null, key: null };  // (updateChance)
        updateChance(tile.bl4dexChance, part.chance, p.chances);
        info.appendChild(top);
        tile.appendChild(info);
        return tile;
    }

    // What a part does, short, one line each: "^ Damage +12%", "^ Jakobs-Licensed Accessory".
    // (Boxed chips wrapped their text inside the box in the game's UI and overlapped.)
    function effectChips(part) {
        var box = el("div", "bl4dex_part_chips");
        function add(text, good, markup) {
            var line = el("div", "bl4dex_part_line" + (good === true ? " bl4dex_part_good" : good === false ? " bl4dex_part_bad" : ""));
            if (good === true || good === false) line.appendChild(arrow(good));
            else line.appendChild(el("div", "bl4dex_part_arrow"));
            line.appendChild(markup ? markupElement("bl4dex_part_line_text", text) : el("p", "bl4dex_part_line_text", text));
            box.appendChild(line);
        }
        if (part.core) {
            add("Sets the base numbers", null);
        } else {
            part.stats.forEach(function (s) {
                var value = s.change === "added" ? s.to : s.change === "removed" ? "No " + s.label : s.pct || s.to;
                add(s.change === "removed" ? value : s.label + "  " + value, s.good);
            });
        }
        (part.elements || []).forEach(function (e) {
            add(e + " damage", true);
        });
        (part.skills || []).forEach(function (s) {
            add((s.points > 0 ? "+" : "") + s.points + " " + s.name, s.points > 0);
        });
        part.added.concat((part.variants || []).map(function (t) { return { text: t }; })).forEach(function (t) {
            add(perkTitle(t.text), true, true);
        });
        (part.changed || []).forEach(function (t) {
            add("Changes " + perkTitle(t.text), null, true);
        });
        part.removed.forEach(function (t) {
            add("Loses " + perkTitle(t.text), false, true);
        });
        if (partQuiet(part)) add("No change on the card", null);
        return box;
    }

    // The game's card in the right column, shrunk to fit its height if it's taller (no scrolling)
    function fitCard(col, holder, tries) {
        tries = tries || 0;
        later(function () {
            if (!holder.parentNode || !col.parentNode) return;
            var room = col.clientHeight - 8;
            var card = holder.firstChild ? holder.firstChild.getBoundingClientRect().height : 0;
            if (room > 0 && card > 0) {
                var scale = Math.min(1, room / card);
                holder.style.transformOrigin = "top center";
                holder.style.transform = scale < 1 ? "scale(" + scale.toFixed(3) + ")" : "none";
            }
            if (tries < 12) fitCard(col, holder, tries + 1);  // the card fills in over a few frames
        }, tries ? 150 : 50);
    }

    // "37%" over "of rolls" (exact, or simulated by the game's rules), "Always" for a part every roll
    // has, or "..." while it's being worked out. A slot that picks several parts has each one on
    // more rolls, so a slot's chances add up to its picks (1 to 2 a roll: 150%), not 100%.
    function chanceText(chance, status) {
        var box = el("div", "bl4dex_part_chance");
        if (chance === null || chance === undefined) {
            box.appendChild(el("p", "bl4dex_part_chance_value bl4dex_part_chance_wait", status === "done" ? "" : "..."));
            return box;
        }
        var pct = chance * 100;
        if (pct >= 99.95) {
            box.appendChild(el("p", "bl4dex_part_chance_value bl4dex_part_chance_always", "Always"));
            return box;
        }
        // (a part it has is never 0%: it rolled, it's only rarer than the simulated rolls could see)
        var text = pct >= 10 ? pct.toFixed(0) + "%" : pct >= 0.1 ? pct.toFixed(1) + "%" : "<0.1%";
        box.appendChild(el("p", "bl4dex_part_chance_value", text));
        box.appendChild(el("p", "bl4dex_part_chance_word", "of rolls"));
        return box;
    }

    // A part's drop-down: every part its slot can roll, the likeliest first, each with what it does
    // to this roll in its place (its effects read by Python, a few a frame) and the chance a roll
    // has it; the one(s) in this roll marked
    function slotDropdown(item, p) {
        var slot = p.slot;
        var box = el("div", "bl4dex_slot_drop");
        var head = el("div", "bl4dex_slot_head");
        var total = (slot.parts || []).length + (slot.more || 0);
        head.appendChild(el("p", "bl4dex_slot_title", (slot.slot || "Slot").toUpperCase() + ":  " + total + (total === 1 ? " PART" : " PARTS") + " IT CAN ROLL"));
        var picks = slot.picks ? slot.picks + (slot.picks === "1" ? " part" : " parts") + " from this slot per roll" : "";
        var mine = p.parts[slot.at - 1];
        var multi = slot.picks && slot.picks !== "1";
        var sub = picks + (multi ? " (so the chances add up to more than 100%)" : "") + ".  Effects: each one in place of your " +
            (mine ? mine.name : "part") + ".";
        head.appendChild(el("p", "bl4dex_slot_sub", sub));
        box.appendChild(head);
        if (slot.status) {
            box.appendChild(el("p", "bl4dex_empty bl4dex_bad", slot.status));
            return box;
        }
        var arts = box.bl4dexArts = [];  // (their pictures: updateArt, by the slot's part index)
        slot.parts.forEach(function (c) {
            var row = el("div", "bl4dex_slot_part" + (c.rolled ? " bl4dex_slot_rolled" : ""));
            var pic = el("div", "bl4dex_part_picture");
            if (c.icon) {
                pic.appendChild(iconPicture([c.icon]));
            } else {
                var art = artHolder(pic);
                updateArt(art, c.picture, c.box, c.maker ? "..." : "");
                arts.push({ art: art, i: slot.parts.indexOf(c) });
            }
            row.appendChild(pic);
            var who = el("div", "bl4dex_part_who");
            who.appendChild(el("p", "bl4dex_part_slot_text", c.rolled ? "IN THIS ROLL" : (c.note ? c.note.toUpperCase() : "")));
            who.appendChild(el("p", "bl4dex_part_name", c.name));
            who.appendChild(makerLine(c, item));
            row.appendChild(who);
            if (c.effect) {
                row.appendChild(effectChips(c.effect));
            } else {
                var wait = el("div", "bl4dex_part_chips");
                wait.appendChild(el("p", "bl4dex_part_line_text", c.effect_status === "bad" ? "The game won't take it with this roll's other parts." : "Reading..."));
                row.appendChild(wait);
            }
            row.appendChild(chanceText(c.chance, slot.known ? "done" : p.chances));
            box.appendChild(row);
        });
        var notes = [];
        if (slot.more) notes.push(slot.more + " less likely " + (slot.more === 1 ? "one" : "ones") + " not listed");
        if (slot.never) notes.push(slot.never + " more " + (slot.never === 1 ? "part" : "parts") + " of this slot can't roll on this item (the game's tag rules)");
        if (!slot.known) notes.push("working out the chances...");
        if (notes.length) box.appendChild(el("p", "bl4dex_parts_foot", notes.join(";  ") + "."));
        return box;
    }

    //  ^ 3   (green) / v 1 (red)
    function leanCount(better, n) {
        var box = el("div", "bl4dex_part_count " + (better ? "bl4dex_part_good" : "bl4dex_part_bad"));
        box.appendChild(arrow(better));
        box.appendChild(el("p", "bl4dex_part_count_text", String(n)));
        return box;
    }

    // The item card's comparison arrow: green up (better), red down (worse)
    function arrow(better) {
        var a = el("div", "bl4dex_part_arrow " + (better ? "bl4dex_part_better" : "bl4dex_part_worse"));
        a.style.backgroundImage = "url(" + CARD_ART + "item_card_compare_arrow_1_" + (better ? "green" : "red") + ".png)";
        return a;
    }

    //  [icon] Damage        1,785 x 3  >  2,006 x 3   +12%  ^
    function statRow(s) {
        var tone = s.good === true ? " bl4dex_part_good" : s.good === false ? " bl4dex_part_bad" : "";
        var row = el("div", "bl4dex_part_stat" + tone);
        var backing = el("div", "bl4dex_part_stat_backing");
        if (s.good !== null) {
            backing.style.borderImageSource = "url(" + CARD_ART + "item_card_comparison_backing_" + (s.good ? "good" : "bad") + ".png)";
        }
        row.appendChild(backing);
        var icon = el("div", "bl4dex_part_stat_icon");
        if (s.icon) icon.style.backgroundImage = "url(Texture2D'" + s.icon + "')";
        row.appendChild(icon);
        // Label above, numbers under it: long ones ("480 DMG/s | 6% Chance") fit the tile
        var text = el("div", "bl4dex_part_stat_text");
        row.appendChild(text);
        text.appendChild(el("p", "bl4dex_part_stat_label", s.label));
        var values = el("div", "bl4dex_part_stat_values");
        if (s.change === "base") {
            values.appendChild(el("p", "bl4dex_part_stat_to", s.to));
        } else {
            values.appendChild(el("p", "bl4dex_part_stat_from", s.change === "added" ? "none" : s.from));
            values.appendChild(el("p", "bl4dex_part_stat_sep", ">"));
            values.appendChild(el("p", "bl4dex_part_stat_to", s.change === "removed" ? "none" : s.to));
        }
        text.appendChild(values);
        row.appendChild(el("p", "bl4dex_part_stat_pct", s.pct || ""));
        if (s.good !== null) row.appendChild(arrow(s.good));
        else row.appendChild(el("div", "bl4dex_part_arrow"));
        return row;
    }

    // A card text the part brings (green), takes away (red) or changes the numbers of
    function perkRow(text, what, good) {
        var row = el("div", "bl4dex_part_perk" + (good === true ? " bl4dex_part_good" : good === false ? " bl4dex_part_bad" : ""));
        var icon = el("div", "bl4dex_part_perk_icon");
        if (good === null) icon.style.backgroundImage = "url(" + ICON_LEGENDARY + ")";
        else icon.appendChild(arrow(good));
        row.appendChild(icon);
        var texts = el("div", "bl4dex_part_perk_texts");
        texts.appendChild(el("p", "bl4dex_part_perk_what", what.toUpperCase()));
        texts.appendChild(markupElement("bl4dex_effect bl4dex_part_perk_text", text));
        row.appendChild(texts);
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
            // Quicker while typing, or while a picture is on its way (a roll's, or the parts screen's)
            var soon = state && (state.typing || (state.roll && state.roll.coming) || (state.parts && state.parts.pictures === "waiting"));
            schedule(!view.open ? POLL_IDLE_MS : soon ? POLL_TYPING_MS : POLL_OPEN_MS);
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
            if (!view.scroll || typeof view.scroll !== "object") view.scroll = { bosses: 0, collection: 0 };
            ownDefaults();
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
        followPageGo(next.page_go);
        if (view.open) {
            patchPager();
            patchTake();
            patchLevel();
        }
        var coll = next.collection;
        // (differs, not "is newer": a hot reload of the mod starts its count again)
        if (coll && data && (!dex.coll || dex.coll.v !== coll.v)) loadCollection();
        if (!view.open) return;
        // Only the shown item's roll (or Collection entry) changed: patch it in place (patchRoll,
        // patchOwn), else redraw it all
        var base = JSON.stringify([next.defeated, next.unlock, next.character, next.outdated, next.fills, next.owned_v, next.obtained, next.spoiler_free, next.uvh, next.trip,
                                   coll ? coll.char : null]);  // prettier-ignore
        var rest = JSON.stringify(next.roll || null) + "|" + JSON.stringify(coll ? coll.shown : null);
        var typed = JSON.stringify([next.search, next.typing]);
        var sig = base + "|" + typed + "|" + rest;
        if (sig !== dex.sig) {
            var known = !!dex.sig && base === dex.sigBase;
            var onlyTyped = known && rest === dex.sigRest;
            var onlyRoll = known && typed === dex.sigTyped;
            dex.sig = sig;
            dex.sigBase = base;
            dex.sigRest = rest;
            dex.sigTyped = typed;
            // A search letter or the typing mark: the list only (its rows are kept)
            if (onlyTyped) render("list");
            else if (!(onlyRoll && (view.tab === "collection" ? patchOwn() : patchRoll()))) render();
        }
        // Python has a parts screen open the page doesn't show (the page was reloaded, or it was
        // opened from Python): show it, on its item. Not just after Back (Python may not know yet).
        var asked = next.parts && next.parts.item;
        var askedOwn = next.parts && next.parts.own;
        if (asked && !dex.partsFor && Date.now() - (dex.partsClosedAt || 0) > 3000 && data.itemByKey[asked]) {
            if (askedOwn) {
                if (view.tab === "collection" && ui.ownView && ui.ownView.sid === askedOwn) {
                    dex.partsFor = asked;
                    dex.partsOwn = askedOwn;
                    dex.partsSig = null;
                }
            } else {
                if (view.tab !== "collection" || current.collection !== asked) goTo("collection", asked);
                dex.partsFor = asked;
                dex.partsSig = null;
            }
        }
        // The parts screen follows its own state (it redraws only when that changed)
        if (dex.partsFor) renderParts();
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
                if (changed) {
                    picturesRev++;  // the kept list rows show old pictures: make them again
                    render();
                }
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
        b.dataset.inputElem = "true";  // so the game's D-pad nav stops on it (menu.js)
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
        window.removeEventListener("resize", onResize);
    };

    // A new window size: fit the tile list again (fitList measures only on a change, so it's cheap)
    function onResize() {
        if (alive() && view.open && ui.list) fitList();
    }
    window.addEventListener("resize", onResize);

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
