"use strict";

// BL4 Instant Item Pictures: keeps every item picture the game holds loaded in the menu page.
//
// The inventory and bank lists reuse about 50 cells while scrolling and hand each one another
// item's picture many times a second. A picture the page doesn't hold yet loads again first, and
// the cell stays empty until it has (the "items disappear, then reappear" while scrolling). So we
// hold every picture: one 1 px element each, inside a 1 px box out of the way, almost transparent
// (an element that isn't drawn at all may not keep its picture). Then a cell's switch is instant.
//
// The BL4 UI Link (uilink/ui/hub.js, as `new Function("bl4linkNs", js)(ns)`) runs this file in the
// game's menu page, or the BL4 Mod Menu's extensions.js does until the link's pak has loaded; either
// runs it again when it changes, so it tears its last copy down first. Python (bl4_item_pictures/
// page.py) gives the pictures' paths: state {paths: ["/Engine/Transient.<capture>:OakInventoryThumbnail_<n>", ...]}.
// No answer (the mod is off or the server is gone) lets every picture go.

(function () {
    var ns = (typeof bl4linkNs !== "undefined" && bl4linkNs) || window.bl4ModMenu;
    if (!ns || !ns.ext) return;
    var ext = ns.ext;
    var KEY = "itempictures";
    var POLL_MS = 2000;

    if (ext.teardowns[KEY]) {
        try {
            ext.teardowns[KEY]();
        } catch (err) {}
    }
    // Survives reloads of this file; each copy takes a new, ever higher number
    var ip = ns.itemPictures || (ns.itemPictures = {});
    ip.counter = Math.max(ip.counter || 0, ip.gen || 0) + 1;
    var gen = ip.counter;
    ip.gen = gen;

    var timers = [];
    var held = {}; // path -> its element
    var count = 0;
    var box = null;

    function alive() {
        return ip.gen === gen;
    }
    function later(fn, ms) {
        var id = setTimeout(function () {
            var at = timers.indexOf(id);
            if (at >= 0) timers.splice(at, 1);
            if (alive()) fn();
        }, ms);
        timers.push(id);
        return id;
    }
    function log(text) {
        if (ns.log) ns.log("[itempictures] " + text);
    }

    function container() {
        if (box && box.parentNode) return box;
        box = document.createElement("div");
        box.style.cssText = "position:absolute;left:0;top:0;width:1px;height:1px;overflow:hidden;opacity:0.01;pointer-events:none;";
        document.body.appendChild(box);
        held = {};
        count = 0;
        return box;
    }

    function hold(path) {
        var node = document.createElement("div");
        node.style.cssText = "position:absolute;left:0;top:0;width:1px;height:1px;";
        // the same image URL the cells bind: TextureRenderTarget2D'<path>'
        node.style.backgroundImage = "url(TextureRenderTarget2D'" + path + "')";
        container().appendChild(node);
        held[path] = node;
        count++;
    }

    function release(path) {
        var node = held[path];
        if (node && node.parentNode) node.parentNode.removeChild(node);
        delete held[path];
        count--;
    }

    function apply(paths) {
        var want = {};
        for (var i = 0; i < paths.length; i++) {
            var path = paths[i];
            if (typeof path !== "string") continue;
            want[path] = true;
            if (!held[path]) hold(path);
        }
        for (var old in held) {
            if (!want[old]) release(old);
        }
    }

    function clear() {
        if (box && box.parentNode) box.parentNode.removeChild(box);
        box = null;
        held = {};
        count = 0;
    }

    function poll(done) {
        ext.get(KEY, false, function (snap) {
            if (!alive()) return;
            var paths = snap && snap.state && snap.state.paths;
            try {
                if (paths && paths.length !== undefined) apply(paths);
                else clear();
            } catch (err) {
                log("apply: " + err);
            }
            if (done) done();
        });
    }

    function loop() {
        poll(function () {
            if (alive()) later(loop, POLL_MS);
        });
    }

    ext.teardowns[KEY] = function () {
        ip.gen = 0;
        timers.forEach(clearTimeout);
        timers = [];
        clear();
    };

    loop();
    log("ready");
})();
