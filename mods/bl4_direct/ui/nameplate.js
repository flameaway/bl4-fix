// BL4 Direct Connect: teammate names over their heads (HUD page, run by the BL4 UI Link, key
// "bl4dcnames", only while we're a client; GitHub issue #3).
//
// On a direct-IP client the game leaves another player's overhead nameplate entry without a name
// (`name` and `platform` "", while `isnametagvisible` is on), so nothing shows over their head.
// Python publishes {names: {characterIndex: {n: name, id: PlayerId}}}; the entry's `uniqueid` is
// that character's object index. On a check every 250 ms while there are names to show, an entry
// the game left nameless gets our own name element inside its name container (styled like the
// game's) and the platform icon the game's teammate panel shows for that PlayerId; both go again
// once the game names the entry itself, the player leaves, or the script stops.
(function () {
    "use strict";

    var ns = (typeof bl4linkNs !== "undefined" && bl4linkNs) || null;
    var KEY = "bl4dcnames";
    var POLL_MS = 2000;
    var FIX_MS = 250;

    // Retire an older copy (hot reload, or the link starting the script again)
    if (window.Bl4Dc_Names && window.Bl4Dc_Names.stop) window.Bl4Dc_Names.stop();

    var me = { names: {}, any: false, stopped: false, widget: null, timers: [], count: 0, looping: false };
    window.Bl4Dc_Names = me;

    function later(fn, ms) {
        var id = setTimeout(function () {
            var at = me.timers.indexOf(id);
            if (at >= 0) me.timers.splice(at, 1);
            if (!me.stopped) fn();
        }, ms);
        me.timers.push(id);
    }

    // The player's platform as the game's own teammate panel has it ("steam", ...): the panel
    // reads it from the PlayerState, which a direct-IP client does have
    function platformOf(playerId) {
        var model = window.data_def_buddy_info_0, list = model && model.buddyinfo;
        for (var i = 0; list && i < list.length; i++) {
            if (list[i].playerid === playerId) return list[i].platformicon || "";
        }
        return "";
    }

    // Our name element under the entry's name container and the platform class on its platform
    // icon, or neither (who = null)
    function show(entryEl, who) {
        var node = entryEl.bl4dcName, icon = entryEl.bl4dcIcon;
        var platform = who ? platformOf(who.id) : "";
        if (icon && (icon.cls !== platform || !icon.el.isConnected)) {
            icon.el.classList.remove(icon.cls);
            entryEl.bl4dcIcon = icon = null;
        }
        if (!who) {
            if (node) {
                if (node.parentNode) node.parentNode.removeChild(node);
                entryEl.bl4dcName = null;
            }
            return;
        }
        // The containers exist only while the game shows the name tag (data-bind-if), and are
        // made again when that flips, which drops our child: check where ours is every time
        var box = entryEl.querySelector(".overhead_nameplate_name_cntr");
        if (!box) return;
        if (!node || node.parentNode !== box) {
            node = document.createElement("div");
            node.className = "overhead_nameplate_name bold temporary_font_size bl4dc_name";
            box.appendChild(node);
            entryEl.bl4dcName = node;
        }
        if (node.textContent !== who.n) node.textContent = who.n;
        if (platform && !icon) {
            var el = entryEl.querySelector('.overhead_nameplate_icon_container [name="Platform Icon"]');
            if (el) {
                el.classList.add(platform);
                entryEl.bl4dcIcon = { el: el, cls: platform };
            }
        } else if (icon && !icon.el.classList.contains(icon.cls)) {
            icon.el.classList.add(icon.cls);  // the game's binding rewrote the classes
        }
    }

    me.fix = function (widget) {
        var model = widget.DataModel;
        var list = model && model.overheadnameplates;
        if (!list) return;
        var kids = widget.children;
        // Counted afresh each pass: the game drops an entry's element (and ours with it) when the
        // entry goes, e.g. a player out of range
        var count = 0;
        for (var i = 0; i < kids.length; i++) {
            var entry = i < list.length ? list[i] : null;
            var who = null;
            if (me.any && entry && !entry.bisnpc && entry.isnametagvisible && !entry.name) {
                who = me.names[entry.uniqueid] || null;
            }
            if (who || kids[i].bl4dcName || kids[i].bl4dcIcon) show(kids[i], who);
            if (kids[i].bl4dcName || kids[i].bl4dcIcon) count++;
        }
        me.count = count;
    };

    me.stop = function () {
        me.stopped = true;
        for (var i = 0; i < me.timers.length; i++) clearTimeout(me.timers[i]);
        me.timers = [];
        me.any = false;
        var widget = me.widget;
        if (widget && me.count > 0) {
            for (var k = 0; k < widget.children.length; k++) show(widget.children[k], null);
        }
        if (window.Bl4Dc_Names === me) window.Bl4Dc_Names = null;
    };

    if (ns) ns.ext.teardowns[KEY] = function () { me.stop(); };

    // The game's widget (one in the HUD page); looked up again if it was replaced
    function widget() {
        var w = me.widget;
        if (!w || !w.isConnected) w = me.widget = document.querySelector("widget-overhead-nameplate");
        return w;
    }

    // Every FIX_MS while there's a name to show or one of ours is still up, else it stops until
    // the next poll brings names. (The widget's own Event_DataUpdated isn't called as the entries
    // update, checked live 2026-11-07, so this can't ride on it.)
    function loop() {
        me.looping = false;
        if (!me.any && me.count <= 0) return;
        var w = widget();
        if (w) {
            try {
                me.fix(w);
            } catch (err) {
                console.error("bl4dc nameplate: " + err);
            }
        }
        me.looping = true;
        later(loop, FIX_MS);
    }

    function poll() {
        if (!ns) return;
        ns.ext.get(KEY, false, function (snap) {
            if (me.stopped) return;
            var names = snap && snap.state && snap.state.names;
            if (names) {
                me.names = names;
                me.any = false;
                for (var k in names) if (Object.prototype.hasOwnProperty.call(names, k)) me.any = true;
                if (!me.looping) loop();
            }
            later(poll, POLL_MS);
        });
    }

    poll();
})();
