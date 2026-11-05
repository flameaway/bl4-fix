// BL4 Minimap, page mode: the minimap drawn in the game's HUD page (view HUD), run there by the
// BL4 UI Link (key "bl4minimap"). The mod (page.py) runs a small server on the game's machine: we
// learn its port and token from the link's state, then long-poll it for position samples and for
// what to draw (sections cfg / tiles / slow / fast, each sent again only when it changed). Every
// frame we place everything ourselves: the map from tile PNGs (served by the mod), turned with the
// HUD compass's heading (a frame behind the camera, like the compass itself), moved to the position
// predicted from the last two samples; the icons projected around it. Mirrors widget.py's look.
// Hot reload: a new copy stops the old one (window.bl4Minimap, ever higher generations).
(function () {
    "use strict";

    var ns = (typeof bl4linkNs !== "undefined" && bl4linkNs) || null;
    var KEY = "bl4minimap";
    if (!ns || !/[?&]view=HUD/i.test(location.href)) return;

    var prev = window.bl4Minimap;
    if (prev && typeof prev.stop === "function") prev.stop();
    var me = { gen: (window.bl4MinimapGen || 0) + 1, stopped: false };
    window.bl4MinimapGen = me.gen;
    window.bl4Minimap = me;
    ns.ext.teardowns[KEY] = function () {
        if (window.bl4Minimap) window.bl4Minimap.stop();
    };

    // region Look (UI units of widget.py; px = units * cfg.ui)

    var RING = 5, RIM = 5, SQUARE_CORNER = 6, SQUARE_CLIP_INSET = 4;
    var ARROW = 44, ICON = 36, ROUTE_DOT = 14, CHEVRON = 16, CHEVRON_ASPECT = 0.8;
    var NORTH = 26, NORTH_OUTLINE = 2, NORTH_TEXT_PT = 14;
    var RING_COLOR = "rgb(101,206,255)";       // linear (0.13, 0.62, 1.0)
    var RIM_COLOR = "rgba(0,18,39,0.85)";      // linear (0, 0.006, 0.02, 0.85)
    var NORTH_FILL = "rgba(0,18,39,0.95)";
    var RADAR = "coui://uiresources/enemy_radar/assets/ui_art_asset/";
    var ARROW_URL = RADAR + "enemy_radar_circle_player_arrow.png";
    var DOT_URL = RADAR + "enemy_radar_circle_friendly.png";
    var CHEVRON_URL = "coui://uiresources/compass/assets/UI_Art_Assets/compass_vertical_indicator.png";

    // The HUD compass's heading: scrollposx = C_SLOPE * yaw + C_ZERO, wrapping over 201 (-50..151).
    // Fit to 3180 frames against the yaw of the frame it shows: p50 0.0005 deg, p90 0.0012 (game
    // 32892, 2026-11-04). The old fit (0.557, 50.9) was up to 1.1 deg off, depending on the heading,
    // and a median of compass-vs-sample pairs taken while the view was still corrected it: after a
    // turn the map sat off and slid back over about a second.
    // It's a frame behind the camera (the game collects it before the camera updates; our samples
    // land a frame late too: Gameface hands an answer over at its next frame), and the map turns
    // with it as is. Turned further by half its last frame's change, the map ran ahead whenever a
    // turn slowed, then came back: momentum and a jiggle after a flick (3170 frames, game 32892,
    // 2026-11-04: ahead of the camera by over 0.5 deg in 36 frames, up to 13.2 deg, and 20 frames
    // off by over 1 deg the other way from the frame before; as is: never ahead by more than
    // 0.3 deg, 2 such flips, at p50 1.41 / p90 6.10 deg behind while turning instead of 0.71 / 3.56).
    // Our samples predicted forward overshot on stops too (2026-11-02).
    var C_SLOPE = 201 / 360, C_ZERO = 50.5;
    var MAX_AHEAD = 150;  // ms a position may be predicted past its sample
    var MAX_SPAN = 250;   // two samples further apart than this: no prediction
    var STALE_MS = 2000;  // no answer from the feed for this long: hide, find it again

    // endregion
    // region State

    var st = {
        port: 0, token: "", q: 0, revs: {}, cfg: null, tiles: null, slow: null, fast: null, fog: null, fogKey: "",
        samples: [], vis: false, lastAnswer: 0, xhr: null, asking: false, timer: null,
        compassWarned: false, anchor: null, scale: 0, placed: {}, tileKey: "",
        routeKey: "", frameKey: "", shown: null
    };

    // endregion
    // region DOM

    function div(parent, css) {
        var d = document.createElement("div");
        d.style.cssText = "position:absolute;left:0;top:0;" + (css || "");
        if (parent) parent.appendChild(d);
        return d;
    }

    var root = div(null, "position:fixed;left:auto;right:40px;top:110px;width:240px;height:240px;pointer-events:none;display:none;");
    var rim = div(root);
    var clip = div(root, "overflow:hidden;background:#000;");
    var rot = div(clip, "width:0;height:0;");
    var map = div(rot, "width:0;height:0;");
    var routeLayer = div(map, "width:0;height:0;");
    // Fog of war: the game's explored grid as one image (unexplored tinted), over the tiles and
    // under the route, placed in the map layer by a matrix from its world square (placeFog)
    // Two images: a new revision loads into the hidden one and they swap once it has loaded (one
    // image changing its src shows nothing until the new picture is in: the map blinked)
    var fogImgs = [], fogShown = 0;
    for (var fi = 0; fi < 2; fi++) {
        var fimg = document.createElement("img");
        fimg.style.cssText = "position:absolute;left:0;top:0;transform-origin:0 0;display:none;";
        map.insertBefore(fimg, routeLayer);
        fogImgs.push(fimg);
    }
    var fogImg = fogImgs[0];  // (tiles go under both)
    var ring = div(root, "box-sizing:border-box;background:transparent;");
    var frameEl = div(root);  // icons, markers and the arrow (clips when square, like the UMG frame)
    var leaderLayer = div(frameEl, "width:0;height:0;");  // spread icons' lines to their true spots
    var iconLayer = div(frameEl, "width:0;height:0;");
    var markerLayer = div(frameEl, "width:0;height:0;");
    var arrow = div(frameEl, "background-image:url(" + ARROW_URL + ");background-size:100% 100%;");
    var badge = div(root, "box-sizing:border-box;border-radius:50%;text-align:center;color:#fff;font-family:Industry-Bold;display:none;");
    badge.textContent = "N";
    var edgeLayer = div(root, "width:0;height:0;");
    document.body.appendChild(root);

    // A pool of icon elements per layer and per picture (url + tint). An element never changes its
    // picture: Coherent loads an image again when an element switches to it and shows nothing until
    // then, so pools shared by index made icons blink while moving (an icon leaving range moved every
    // later one to another element: ~30 picture swaps a second, 2026-11-02). Unused ones are hidden
    // with visibility (kept in layout, picture kept).
    function Pool(layer) {
        this.layer = layer;
        this.byKey = {};
        this.used = {};
    }
    Pool.prototype.take = function (url, tint) {
        var key = url + "|" + (tint || "");
        var list = this.byKey[key] || (this.byKey[key] = []);
        var n = this.used[key] || 0;
        var it = list[n];
        if (!it) {
            var css = "background-size:100% 100%;";
            if (tint) {
                css += "background-color:" + tint + ";mask-image:url(" + url + ");-webkit-mask-image:url(" + url + ");" +
                    "mask-size:100% 100%;-webkit-mask-size:100% 100%;";
            } else {
                css += "background-image:url(" + url + ");";
            }
            it = { el: div(this.layer, css), size: -1, pos: "", shown: true, chev: null, chevKey: "" };
            list.push(it);
            st.made = (st.made || 0) + 1;
        }
        this.used[key] = n + 1;
        if (!it.shown) { it.el.style.visibility = "visible"; it.shown = true; st.reshows = (st.reshows || 0) + 1; }
        return it;
    };
    Pool.prototype.finish = function () {
        for (var key in this.byKey) {
            if (!Object.prototype.hasOwnProperty.call(this.byKey, key)) continue;
            var list = this.byKey[key];
            for (var i = this.used[key] || 0; i < list.length; i++) {
                var it = list[i];
                if (it.shown) { it.el.style.visibility = "hidden"; it.shown = false; }
            }
        }
        this.used = {};
    };
    var pools = { icon: new Pool(iconLayer), marker: new Pool(markerLayer), edge: new Pool(edgeLayer), leader: new Pool(leaderLayer) };

    function setSize(it, size) {
        if (it.size !== size) {
            it.el.style.width = it.el.style.height = size + "px";
            it.size = size;
        }
    }

    // Above (1) / below (-1) chevron, a child of the icon's element
    function setChevron(it, vertical, size, scale) {
        var key = vertical ? vertical + ":" + size + ":" + scale : "";
        if (key === it.chevKey) return;
        it.chevKey = key;
        if (!vertical) {
            if (it.chev) it.chev.style.display = "none";
            return;
        }
        if (!it.chev) it.chev = div(it.el, "background-image:url(" + CHEVRON_URL + ");background-size:100% 100%;");
        var w = CHEVRON * scale, h = w * CHEVRON_ASPECT;
        var gap = size / 2 + h / 2;
        var c = it.chev.style;
        c.display = "";
        c.width = w.toFixed(2) + "px";
        c.height = h.toFixed(2) + "px";
        c.transform = "translate(" + (size / 2 - w / 2).toFixed(2) + "px," + (size / 2 - vertical * gap - h / 2).toFixed(2) + "px)" +
            (vertical > 0 ? " rotate(180deg)" : "");
    }

    function place(it, x, y, size) {
        var pos = "translate(" + (x - size / 2).toFixed(1) + "px," + (y - size / 2).toFixed(1) + "px)";
        if (pos !== it.pos) {
            it.el.style.transform = pos;
            it.pos = pos;
        }
    }

    // endregion
    // region Feed

    function stop() {
        me.stopped = true;
        clearTimeout(st.timer);
        if (st.xhr) { try { st.xhr.abort(); } catch (err) {} }
        if (root.parentNode) root.parentNode.removeChild(root);
        if (window.bl4Minimap === me) window.bl4Minimap = null;
    }
    me.stop = stop;
    me.st = st;

    // The chain findFeed -> ask -> ask... goes on only from each request's answer. Gameface can drop
    // a request without ever finishing it (game 33460, 2026-11-02: the long-poll back at readyState
    // 0, no readystatechange, `asking` left set): nothing asked again and the minimap stayed hidden
    // for good. frame() starts a new chain once no answer came for STALE_MS; a late answer of the
    // old one is ignored (st.chain moved on), so there is never more than one.
    function restart(now) {
        st.chain = (st.chain || 0) + 1;
        st.kickAt = now;
        st.restarts = (st.restarts || 0) + 1;
        clearTimeout(st.timer);
        var old = st.xhr;
        st.xhr = null;
        st.asking = false;
        if (old) { try { old.abort(); } catch (err) {} }
        findFeed();
    }

    function findFeed() {
        if (me.stopped) return;
        var chain = st.chain;
        ns.ext.get(KEY, false, function (snap) {
            if (me.stopped || chain !== st.chain) return;
            var s = snap && snap.state;
            if (s && s.port && s.token) {
                if (s.port !== st.port || s.token !== st.token) {
                    st.port = s.port;
                    st.token = s.token;
                    st.q = 0;
                    st.revs = {};
                }
                st.lastAnswer = Date.now();
                ask();
            } else {
                st.timer = setTimeout(findFeed, 2000);
            }
        });
    }

    function ask() {
        if (me.stopped || st.asking) return;
        st.asking = true;
        var r = [st.revs.cfg || 0, st.revs.tiles || 0, st.revs.slow || 0, st.revs.fast || 0, st.revs.fog || 0].join(".");
        var xhr = new XMLHttpRequest(), chain = st.chain;
        st.xhr = xhr;
        xhr.onreadystatechange = function () {
            if (xhr.readyState !== 4 || chain !== st.chain) return;
            st.asking = false;
            st.xhr = null;
            if (me.stopped) return;
            if (xhr.status !== 200) {
                // Refused (status 0: the mod's feed restarted on a new port) or long silent: ask the
                // link where it is now
                if (xhr.status === 0 || Date.now() - st.lastAnswer > STALE_MS) {
                    st.port = 0;
                    st.timer = setTimeout(findFeed, 300);
                } else {
                    st.timer = setTimeout(ask, 250);
                }
                return;
            }
            st.lastAnswer = Date.now();
            st.backoff = 0;
            try {
                take(JSON.parse(xhr.responseText));
            } catch (err) {
                ns.log("minimap: " + err);
            }
            ask();
        };
        // (The `d` counters encoded: Gameface drops a request whose URL has `[` or `]` without a word,
        // readyState left at 0. A yaw-error readout put them in `made` and every poll after it died:
        // the minimap froze and hid itself, 2026-11-02/03, games 33460 and 21940)
        try {
            xhr.open("GET", "http://127.0.0.1:" + st.port + "/p?t=" + st.token + "&q=" + st.q + "&r=" + r + "&d=" + encodeURIComponent((st.made || 0) + "." + (st.reshows || 0)), true);
            xhr.send(null);
        } catch (err) {
            st.asking = false;
            st.timer = setTimeout(findFeed, 1000);
        }
    }

    function take(m) {
        if (m.q !== st.q && m.p) {
            st.q = m.q;
            var p = m.p;
            st.vis = !!p.vis;
            var s = st.samples;
            if (!s.length || s[s.length - 1].t !== p.t) {
                s.push(p);
                if (s.length > 8) s.shift();
            }
        }
        var names = ["cfg", "tiles", "slow", "fast", "fog"];
        for (var i = 0; i < names.length; i++) {
            var sec = m[names[i]];
            if (sec) {
                st.revs[names[i]] = sec.rev;
                st[names[i]] = sec.v;
                if (names[i] === "tiles") st.tileKey = "";
                if (names[i] === "slow" || names[i] === "cfg") st.routeKey = "";
                if (names[i] === "fog" || names[i] === "cfg") st.fogKey = "";
            }
        }
    }

    // endregion
    // region Position and heading

    function wrap180(a) {
        a = (a + 180) % 360;
        if (a < 0) a += 360;
        return a - 180;
    }

    function compassValue() {
        var m = window.data_def_compass_0;
        return m && typeof m.scrollposx === "number" ? m.scrollposx : null;
    }

    function rawCompassYaw(c) {
        return (c - C_ZERO) / C_SLOPE;
    }

    function predict(now) {
        var s = st.samples;
        if (!s.length) return null;
        var b = s[s.length - 1];
        if (s.length < 2) return { x: b.x, y: b.y, z: b.z, yaw: b.yaw };
        var a = s[s.length - 2];
        var span = b.t - a.t;
        var ahead = Math.min(Math.max(now - b.t, 0), MAX_AHEAD);
        if (span <= 0 || span > MAX_SPAN) ahead = 0;
        var f = span > 0 ? ahead / span : 0;
        // (The heading isn't predicted: it overshot whenever a turn stopped. It's only the fallback
        // for the compass anyway.)
        return { x: b.x + (b.x - a.x) * f, y: b.y + (b.y - a.y) * f, z: b.z, yaw: b.yaw };
    }

    // endregion
    // region Layout

    function layout(cfg) {
        var ui = cfg.ui || 1, size = cfg.size, square = cfg.square;
        var key = [size, cfg.mx, cfg.my, square, ui, cfg.icon].join("|");
        if (key === st.frameKey) return;
        st.frameKey = key;
        var r = root.style;
        r.right = cfg.mx + "px";
        r.top = cfg.my + "px";
        r.width = r.height = size + "px";
        var rimW = RIM * ui, ringW = RING * ui;
        var rs = rim.style;
        rs.left = rs.top = -rimW + "px";
        rs.width = rs.height = size + 2 * rimW + "px";
        rs.background = RIM_COLOR;
        rs.borderRadius = square ? (SQUARE_CORNER + RIM) * ui + "px" : "50%";
        var cs = clip.style, inset = square ? SQUARE_CLIP_INSET * ui : 0;
        cs.left = cs.top = inset + "px";
        cs.width = cs.height = size - 2 * inset + "px";
        cs.borderRadius = square ? "0" : "50%";
        rot.style.left = rot.style.top = size / 2 - inset + "px";
        var gs = ring.style;
        gs.width = gs.height = size + "px";
        gs.border = ringW + "px solid " + RING_COLOR;
        gs.borderRadius = square ? SQUARE_CORNER * ui + "px" : "50%";
        var fs = frameEl.style;
        fs.width = fs.height = size + "px";
        fs.overflow = square ? "hidden" : "visible";
        iconLayer.style.left = markerLayer.style.left = iconLayer.style.top = markerLayer.style.top = size / 2 + "px";
        edgeLayer.style.left = edgeLayer.style.top = size / 2 + "px";
        leaderLayer.style.left = leaderLayer.style.top = size / 2 + "px";
        var a = ARROW * cfg.icon * ui;
        var as = arrow.style;
        as.width = as.height = a + "px";
        as.left = as.top = size / 2 - a / 2 + "px";
        var n = NORTH * cfg.icon * ui;
        var bs = badge.style;
        bs.width = bs.height = n + "px";
        bs.background = NORTH_FILL;
        bs.border = NORTH_OUTLINE * ui + "px solid " + RING_COLOR;
        bs.fontSize = (NORTH_TEXT_PT * 96 / 72 * cfg.icon * ui).toFixed(1) + "px";
        bs.lineHeight = (n - 2 * NORTH_OUTLINE * ui).toFixed(1) + "px";
        st.tileKey = st.routeKey = "";
        st.lastArrow = null;
    }

    // The map layer's frame: north up, (up, right) world units, origin at st.anchor
    function northFrame(cfg, x, y) {
        var n = cfg.nyaw * Math.PI / 180, c = Math.cos(n), s = Math.sin(n);
        return { up: x * c + y * s, right: -x * s + y * c };
    }

    function placeTiles(cfg, here, scale) {
        var t = st.tiles;
        if (!t || !t.tiles) return;
        var W = t.world, inner = t.px - 2 * t.margin;
        if (!st.anchor || Math.abs(here.up - st.anchor.up) > 4 * W || Math.abs(here.right - st.anchor.right) > 4 * W ||
            st.anchor.folder !== t.folder) {
            st.anchor = { up: Math.ceil(here.up / W) * W, right: Math.floor(here.right / W) * W, folder: t.folder };
            st.tileKey = st.routeKey = st.fogKey = "";
            for (var k0 in st.placed) if (Object.prototype.hasOwnProperty.call(st.placed, k0)) map.removeChild(st.placed[k0].el);
            st.placed = {};
        }
        var reach = cfg.radius * 1.5;
        var i0 = Math.floor((here.up - reach) / W), i1 = Math.floor((here.up + reach) / W);
        var j0 = Math.floor((here.right - reach) / W), j1 = Math.floor((here.right + reach) / W);
        var key = [i0, i1, j0, j1, scale.toFixed(6), st.revs.tiles].join("|");
        if (key === st.tileKey) return;
        st.tileKey = key;
        var want = {}, side = t.px / inner * W * scale, m = t.margin / inner * W * scale;
        for (var i = i0; i <= i1; i++) {
            for (var j = j0; j <= j1; j++) {
                var name = i + "_" + j, ver = t.tiles[name];
                if (!ver) continue;
                want[name] = true;
                var have = st.placed[name];
                var src = "http://127.0.0.1:" + st.port + "/t/" + name + ".png?t=" + st.token + "&f=" + (ver.charAt(0) === "f" ? 1 : 0) +
                    "&v=" + encodeURIComponent(t.folder + ver);
                if (!have) {
                    var img = document.createElement("img");
                    img.style.cssText = "position:absolute;";
                    map.insertBefore(img, fogImg);
                    have = st.placed[name] = { el: img, src: "", geo: "" };
                }
                var geo = side.toFixed(3) + "|" + ((j * W - st.anchor.right) * scale - m).toFixed(2) + "|" + ((st.anchor.up - (i + 1) * W) * scale - m).toFixed(2);
                if (have.geo !== geo) {
                    var g = geo.split("|"), s = have.el.style;
                    s.width = s.height = g[0] + "px";
                    s.left = g[1] + "px";
                    s.top = g[2] + "px";
                    have.geo = geo;
                }
                if (have.src !== src) {
                    have.el.src = src;
                    have.src = src;
                }
            }
        }
        for (var k in st.placed) {
            if (Object.prototype.hasOwnProperty.call(st.placed, k) && !want[k]) {
                map.removeChild(st.placed[k].el);
                delete st.placed[k];
            }
        }
    }

    // The fog layer: grid pixel (u, v) is world (X, Y) = (ox + u * ex / w, oy + v * ey / h); the map
    // layer puts world (X, Y) at ((right - anchor.right), (anchor.up - up)) * scale (northFrame)
    function placeFog(cfg, scale) {
        var f = st.fog;
        if (!f || !st.anchor) {
            if (st.fogKey !== "none") { fogImgs[0].style.display = fogImgs[1].style.display = "none"; st.fogKey = "none"; }
            return;
        }
        var key = f.rev + "|" + scale.toFixed(6) + "|" + cfg.nyaw + "|" + st.anchor.up + "," + st.anchor.right;
        if (key === st.fogKey) return;
        st.fogKey = key;
        var n = cfg.nyaw * Math.PI / 180, c = Math.cos(n), s = Math.sin(n);
        var ox = f.sq[0], oy = f.sq[1], kx = f.sq[2] / f.w, ky = f.sq[3] / f.h;
        var m = [-scale * s * kx, -scale * c * kx, scale * c * ky, -scale * s * ky,
            scale * (-ox * s + oy * c - st.anchor.right), scale * (st.anchor.up - ox * c - oy * s)];
        var transform = "matrix(" + m.map(function (v) { return v.toFixed(6); }).join(",") + ")";
        for (var k = 0; k < 2; k++) {
            var style = fogImgs[k].style;
            style.width = f.w + "px";
            style.height = f.h + "px";
            style.transform = transform;
        }
        var src = "http://127.0.0.1:" + st.port + "/f.png?t=" + st.token + "&v=" + f.rev;
        var shown = fogImgs[fogShown], next = fogImgs[1 - fogShown];
        if (shown.getAttribute("src") === src) {
            shown.style.display = "block";
            return;
        }
        if (next.getAttribute("src") === src) return;  // (still loading)
        next.onload = function () {
            if (next.getAttribute("src") !== src) return;
            next.style.display = "block";
            shown.style.display = "none";
            fogShown = 1 - fogShown;
        };
        next.src = src;
    }

    // The route's dots: round, so they sit in the turned map layer at their world places (placed
    // again only when the route or the scale changes)
    function placeRoute(cfg, scale) {
        var slow = st.slow;
        var key = st.revs.slow + "|" + scale.toFixed(6) + "|" + (st.anchor ? st.anchor.up + "," + st.anchor.right : "");
        if (key === st.routeKey || !st.anchor) return;
        st.routeKey = key;
        var dots = slow && slow.route ? slow.route.d : [];
        var size = ROUTE_DOT * cfg.icon * cfg.ui, color = slow && slow.route ? slow.route.c : "#fff";
        var kids = routeLayer.childNodes, n = 0;
        for (var i = 0; i < dots.length; i++) {
            var f = northFrame(cfg, dots[i][0], dots[i][1]);
            var el = kids[n] || div(routeLayer, "background-color:" + color + ";mask-size:100% 100%;-webkit-mask-size:100% 100%;" +
                "mask-image:url(" + DOT_URL + ");-webkit-mask-image:url(" + DOT_URL + ");");
            var s = el.style;
            s.visibility = "visible";
            s.backgroundColor = color;
            s.width = s.height = size.toFixed(2) + "px";
            s.transform = "translate(" + ((f.right - st.anchor.right) * scale - size / 2).toFixed(2) + "px," +
                ((st.anchor.up - f.up) * scale - size / 2).toFixed(2) + "px)";
            n++;
        }
        for (; n < kids.length; n++) kids[n].style.visibility = "hidden";
    }

    // markers.project: (u, v) in -1..1 of the half-size, x right, y down, `yaw` up
    function project(x, y, here, c, s, radius, square) {
        var dx = x - here.x, dy = y - here.y;
        var forward = dx * c + dy * s, right = -dx * s + dy * c;
        var u = right / radius, v = -forward / radius;
        return { u: u, v: v, d: square ? Math.max(Math.abs(u), Math.abs(v)) : Math.sqrt(u * u + v * v) };
    }

    // Same scale as the map; only icons at the very edge are pulled in to fit (widget.set_layer)
    function pullIn(px, py, half, size, ringW, square) {
        var edge = Math.max(0, half - size / 2 - ringW);
        if (square) return [Math.max(-edge, Math.min(edge, px)), Math.max(-edge, Math.min(edge, py))];
        var d = Math.sqrt(px * px + py * py);
        return d > edge ? [px * edge / d, py * edge / d] : [px, py];
    }

    // endregion
    // region Spread ("Spread out stacked icons": spread.py's layout and fit, see there)

    var SPREAD_FOOT = 0.85, SPREAD_RINGS = 12, SPREAD_NEAR_RINGS = 3, SPREAD_TOUCH = 0.999;
    var LEADER = 2, LEADER_DOT = 8;  // the line's width, the true spot's dot (at 100% icon size)
    // Ring k's directions (6 k), in the order tried: straight on, then out to both sides
    var SPREAD_DIRS = [];
    for (var rk = 1; rk <= SPREAD_RINGS; rk++) {
        var dirs = [];
        for (var rt = 0; rt < 6 * rk; rt++) {
            var ra = ((rt + 1) >> 1) * 2 * Math.PI / (6 * rk) * (rt % 2 ? 1 : -1);
            dirs.push([Math.cos(ra), Math.sin(ra)]);
        }
        SPREAD_DIRS.push(dirs);
    }

    // spots: [x, y, r, key] in world units -> { offs: [[dx, dy]...], groups: [[spot...]...] }
    function spreadLayout(spots) {
        var n = spots.length, offs = [], i, j, rmax = 0;
        for (i = 0; i < n; i++) { offs.push([0, 0]); if (spots[i][2] > rmax) rmax = spots[i][2]; }
        if (n < 2 || rmax <= 0) return { offs: offs, groups: [] };
        var cell = 2 * rmax, grid = {}, cells = [];
        for (i = 0; i < n; i++) {
            var gx = Math.floor(spots[i][0] / cell), gy = Math.floor(spots[i][1] / cell), gk = gx + "," + gy;
            if (!grid[gk]) { grid[gk] = []; cells.push([gx, gy, grid[gk]]); }
            grid[gk].push(i);
        }
        var parent = [];
        for (i = 0; i < n; i++) parent.push(i);
        function root(a) {
            while (parent[a] !== a) { parent[a] = parent[parent[a]]; a = parent[a]; }
            return a;
        }
        for (var ci = 0; ci < cells.length; ci++) {
            var members = cells[ci][2];
            for (var dx = -1; dx <= 1; dx++) {
                for (var dy = -1; dy <= 1; dy++) {
                    var other = grid[(cells[ci][0] + dx) + "," + (cells[ci][1] + dy)];
                    if (!other) continue;
                    for (var oj = 0; oj < other.length; oj++) {
                        j = other[oj];
                        for (var mi = 0; mi < members.length; mi++) {
                            i = members[mi];
                            if (j <= i) continue;
                            var ex = spots[i][0] - spots[j][0], ey = spots[i][1] - spots[j][1], er = spots[i][2] + spots[j][2];
                            if (ex * ex + ey * ey < er * er * SPREAD_TOUCH) parent[root(j)] = root(i);
                        }
                    }
                }
            }
        }
        var byRoot = {}, roots = [];
        for (i = 0; i < n; i++) {
            var ri = root(i);
            if (!byRoot[ri]) { byRoot[ri] = []; roots.push(ri); }
            byRoot[ri].push(i);
        }
        var groups = [], placed = [];
        for (i = 0; i < roots.length; i++) {
            var g = byRoot[roots[i]];
            if (g.length > 1) groups.push(g);
            else placed.push([spots[g[0]][0], spots[g[0]][1], spots[g[0]][2]]);  // (alone: it stays)
        }
        if (!groups.length) return { offs: offs, groups: [] };
        function cmp(a, b) { return a < b ? -1 : a > b ? 1 : 0; }
        function first(g) {
            var best = spots[g[0]];
            for (var k = 1; k < g.length; k++) {
                var s = spots[g[k]];
                if (s[0] < best[0] || (s[0] === best[0] && s[1] < best[1])) best = s;
            }
            return best;
        }
        groups.sort(function (a, b) { var p = first(a), q = first(b); return cmp(p[0], q[0]) || cmp(p[1], q[1]); });
        for (var gi = 0; gi < groups.length; gi++) {
            var grp = groups[gi], mx = 0, my = 0;
            for (i = 0; i < grp.length; i++) { mx += spots[grp[i]][0]; my += spots[grp[i]][1]; }
            mx /= grp.length;
            my /= grp.length;
            var d2 = {};
            for (i = 0; i < grp.length; i++) {
                var gs = spots[grp[i]];
                d2[grp[i]] = (gs[0] - mx) * (gs[0] - mx) + (gs[1] - my) * (gs[1] - my);
            }
            grp.sort(function (a, b) {
                var p = spots[a], q = spots[b];
                return cmp(d2[a], d2[b]) || cmp(p[0], q[0]) || cmp(p[1], q[1]) || cmp(p[3], q[3]);
            });
            for (var gm = 0; gm < grp.length; gm++) {
                var sp = spots[grp[gm]], x = sp[0], y = sp[1], r = sp[2];
                var near = nearList(placed, x, y, r, SPREAD_NEAR_RINGS), spot = null, blocked = false;
                for (j = 0; j < near.length && !blocked; j++) blocked = near[j][0] < near[j][3];
                if (blocked) {
                    var ax = x - mx, ay = y - my;
                    var a0 = ax * ax + ay * ay > r * 0.01 * r * 0.01 ? Math.atan2(ay, ax) : -Math.PI / 2;  // (middle: ~north)
                    // The near rings first, with only the icons that reach them (most find a place there)
                    spot = ringSpot(x, y, r, a0, near, 1, SPREAD_NEAR_RINGS) ||
                        ringSpot(x, y, r, a0, nearList(placed, x, y, r, SPREAD_RINGS), SPREAD_NEAR_RINGS + 1, SPREAD_RINGS);
                }
                if (spot) {
                    placed.push([spot[0], spot[1], r]);
                    offs[grp[gm]] = [spot[0] - x, spot[1] - y];
                } else {
                    placed.push([x, y, r]);  // (free, or nowhere free near: it stays)
                }
            }
        }
        return { offs: offs, groups: groups };
    }

    // The placed icons that could touch a place on rings 1..rings around (x, y), nearest first (they
    // block most places): [distance^2, x, y, (r + their r)^2]
    function nearList(placed, x, y, r, rings) {
        var reach = (rings + 1) * r, near = [];
        for (var j = 0; j < placed.length; j++) {
            var pl = placed[j], px = pl[0] - x, py = pl[1] - y, pr = pl[2];
            if (Math.abs(px) < reach + pr && Math.abs(py) < reach + pr) near.push([px * px + py * py, pl[0], pl[1], (r + pr) * (r + pr) * SPREAD_TOUCH]);
        }
        near.sort(function (a, b) { return a[0] - b[0]; });
        return near;
    }

    // The first free place on rings first..last (k r around (x, y)), each tried from the direction a0 out
    function ringSpot(x, y, r, a0, near, first, last) {
        var ca = Math.cos(a0), sa = Math.sin(a0);
        for (var k = first - 1; k < last; k++) {
            var dirs = SPREAD_DIRS[k], dist = (k + 1) * r;
            for (var t = 0; t < dirs.length; t++) {
                var cx = x + dist * (dirs[t][0] * ca - dirs[t][1] * sa), cy = y + dist * (dirs[t][0] * sa + dirs[t][1] * ca);
                var ok = true;
                for (var m = 0; m < near.length; m++) {
                    var ex = cx - near[m][1], ey = cy - near[m][2];
                    if (ex * ex + ey * ey < near[m][3]) { ok = false; break; }
                }
                if (ok) return [cx, cy];
            }
        }
        return null;
    }

    // pts: [x, y, edge] -> the shift [x, y] that brings them all inside together
    function spreadFit(pts, square) {
        var i, tx = 0, ty = 0;
        if (square) {
            var out = [];
            for (var axis = 0; axis < 2; axis++) {
                var lo = -Infinity, hi = Infinity;
                for (i = 0; i < pts.length; i++) {
                    lo = Math.max(lo, -pts[i][2] - pts[i][axis]);
                    hi = Math.min(hi, pts[i][2] - pts[i][axis]);
                }
                out.push(lo <= hi ? Math.min(Math.max(0, lo), hi) : (lo + hi) / 2);
            }
            return out;
        }
        for (var it = 0; it < 4; it++) {
            var worst = 0, wx = 0, wy = 0;
            for (i = 0; i < pts.length; i++) {
                var x = pts[i][0] + tx, y = pts[i][1] + ty, d = Math.sqrt(x * x + y * y);
                if (d - pts[i][2] > worst) { worst = d - pts[i][2]; wx = x / d; wy = y / d; }
            }
            if (worst <= 0) break;
            tx -= worst * wx;
            ty -= worst * wy;
        }
        return [tx, ty];
    }

    // The layout for what's shown now, made again only when the icons, the spreading markers or the
    // zoom changed (st.fast is a new list at the marker rate, st.slow each time the route moves: only
    // the icons and the spreading markers in them are compared, when a new list came)
    function spreadFor(scale, iconScale) {
        var icons = st.slow && st.slow.icons ? st.slow.icons : [], marks = st.fast || [], i;
        if (icons !== st.spreadIcons) {
            st.spreadIcons = icons;
            var ikey = JSON.stringify(icons);
            if (ikey !== st.spreadIconKey) { st.spreadIconKey = ikey; st.spreadKey = ""; }
        }
        if (marks !== st.spreadFast) {
            st.spreadFast = marks;
            var rank = [], keys = [];
            for (i = 0; i < marks.length; i++) {
                rank.push(marks[i][7] ? keys.length : -1);
                if (marks[i][7]) keys.push(marks[i][0] + "," + marks[i][1] + "," + marks[i][3] + "," + marks[i][4]);
            }
            st.spreadRank = rank;  // marker -> its place among the spreading ones (-1: doesn't spread)
            keys = keys.join(";");
            if (keys !== st.spreadMarks) { st.spreadMarks = keys; st.spreadKey = ""; }
        }
        var key = scale + "|" + iconScale;
        if (key === st.spreadKey) return st.spread;
        st.spreadKey = key;
        var spots = [], iconSpot = [], foot = SPREAD_FOOT / 2 / scale;
        for (i = 0; i < icons.length; i++) {
            iconSpot.push(icons[i][0] ? spots.length : -1);
            if (icons[i][0]) spots.push([icons[i][1], icons[i][2], ICON * iconScale * foot, icons[i][0], ICON * iconScale]);
        }
        var nIcons = spots.length;
        for (i = 0; i < marks.length; i++) {
            var mk = marks[i];
            if (mk[7]) spots.push([mk[3], mk[4], mk[1] * iconScale * foot, mk[0], mk[1] * iconScale]);
        }
        var lay = spreadLayout(spots);
        st.spread = lay.groups.length ? { spots: spots, offs: lay.offs, groups: lay.groups, iconSpot: iconSpot, nIcons: nIcons, at: [] } : null;
        return st.spread;
    }

    var leaderLines = [];  // plain lines: no picture, so shared by index
    // Places each spread group (moved all together inside the rim, so they stay apart there too) and
    // the lines back to the true spots. sp.at[spot]: where a shown member goes this frame, or null.
    function spreadPlace(sp, where, c, s, radius, half, ringW, square, iconScale, ui) {
        var nLines = 0, dot = LEADER_DOT * iconScale, w = LEADER * ui;
        for (var gi = 0; gi < sp.groups.length; gi++) {
            var g = sp.groups[gi], pts = [], shown = [], k, m;
            for (k = 0; k < g.length; k++) {
                m = g[k];
                sp.at[m] = null;
                var spot = sp.spots[m], qt = project(spot[0], spot[1], where, c, s, radius, square);
                if (qt.d > 1) continue;
                var qd = project(spot[0] + sp.offs[m][0], spot[1] + sp.offs[m][1], where, c, s, radius, square);
                pts.push([qd.u * half, qd.v * half, Math.max(0, half - spot[4] / 2 - ringW)]);
                shown.push([m, qt]);
            }
            if (!pts.length) continue;
            var t = spreadFit(pts, square);
            for (k = 0; k < pts.length; k++) {
                m = shown[k][0];
                var size = sp.spots[m][4];
                var pos = sp.at[m] = pullIn(pts[k][0] + t[0], pts[k][1] + t[1], half, size, ringW, square);
                var to = pullIn(shown[k][1].u * half, shown[k][1].v * half, half, dot, ringW, square);
                var dx = to[0] - pos[0], dy = to[1] - pos[1], d = Math.sqrt(dx * dx + dy * dy), start = size / 2 * SPREAD_FOOT;
                if (d <= start) continue;  // (its true spot is under it)
                var line = leaderLines[nLines] || (leaderLines[nLines] = { el: div(leaderLayer, "background-color:" + RING_COLOR + ";transform-origin:0 50%;"), shown: false });
                // (Tenths of a pixel / degree, compared as numbers: the style is only written when one changed)
                var lx = Math.round((pos[0] + dx / d * start) * 10), ly = Math.round((pos[1] + dy / d * start - w / 2) * 10);
                var la = Math.round(Math.atan2(dy, dx) * 1800 / Math.PI), ll = Math.round((d - start) * 10), ls = line.el.style;
                if (lx !== line.x || ly !== line.y || la !== line.a) {
                    ls.transform = "translate(" + lx / 10 + "px," + ly / 10 + "px) rotate(" + la / 10 + "deg)";
                    line.x = lx; line.y = ly; line.a = la;
                }
                if (ll !== line.l) { ls.width = ll / 10 + "px"; line.l = ll; }
                if (w !== line.w) { ls.height = w + "px"; line.w = w; }
                if (!line.shown) { ls.visibility = "visible"; line.shown = true; }
                nLines++;
                var it = pools.leader.take(DOT_URL, RING_COLOR);
                setSize(it, dot);
                place(it, to[0], to[1], dot);
            }
        }
        spreadHide(nLines);
    }

    function spreadHide(nLines) {
        for (var i = nLines; i < leaderLines.length; i++) {
            if (leaderLines[i].shown) { leaderLines[i].el.style.visibility = "hidden"; leaderLines[i].shown = false; }
        }
        pools.leader.finish();
    }

    // endregion
    // region Frame

    function frame() {
        if (me.stopped) return;
        requestAnimationFrame(frame);
        var now = Date.now();
        var c0 = compassValue();
        // No answer for STALE_MS (the server answers a long-poll within 0.5 s): the chain is dead or
        // the feed moved. Start over, at most every STALE_MS (restart())
        // (backing off to every 30 s while nothing answers: each restart may leave another request
        // stuck in Gameface)
        if (now - st.lastAnswer > STALE_MS && now - (st.kickAt || 0) > Math.min(STALE_MS << (st.backoff || 0), 30000)) {
            st.backoff = Math.min((st.backoff || 0) + 1, 4);
            restart(now);
        }
        var cfg = st.cfg;
        var show = cfg && st.vis && st.samples.length && now - st.lastAnswer < STALE_MS;
        if (!show) {
            if (st.shown !== false) { root.style.display = "none"; st.shown = false; }
            return;
        }
        if (st.shown !== true) { root.style.display = ""; st.shown = true; }
        try {
            var t0 = performance.now();
            draw(cfg, now, c0);
            var took = performance.now() - t0;
            var cost = st.cost || (st.cost = { n: 0, sum: 0, max: 0 });
            cost.n++;
            cost.sum += took;
            if (took > cost.max) cost.max = took;
        } catch (err) {
            if (!st.drawErr) { st.drawErr = true; ns.log("minimap draw: " + err); }
        }
    }

    function draw(cfg, now, c0) {
        layout(cfg);
        var p = predict(now);
        var yaw = p.yaw;
        // The compass shows the frame our newest sample is from (or the one after, if that sample
        // came late): off by more than that frame's turn, it's not the heading (a game patch, the
        // compass hidden), and the samples' heading is used
        var sm = st.samples, turn = sm.length > 1 ? Math.abs(wrap180(sm[sm.length - 1].yaw - sm[sm.length - 2].yaw)) : 0;
        var ok = c0 !== null && Math.abs(wrap180(rawCompassYaw(c0) - p.yaw)) <= 1 + 2 * turn;
        if (!ok && c0 !== null && !st.compassWarned) { st.compassWarned = true; ns.log("minimap: the compass doesn't match the heading"); }
        if (ok) {
            yaw = rawCompassYaw(c0);
        }
        var mapYaw = cfg.rotate ? yaw : cfg.nyaw;
        var half = cfg.size / 2, radius = cfg.radius, scale = half / radius;
        var ui = cfg.ui, iconScale = cfg.icon * ui, ringW = RING * ui;
        var here = northFrame(cfg, p.x, p.y);
        placeTiles(cfg, here, scale);
        placeRoute(cfg, scale);
        placeFog(cfg, scale);
        if (st.anchor) {
            map.style.transform = "translate(" + (-(here.right - st.anchor.right) * scale).toFixed(2) + "px," +
                (-(st.anchor.up - here.up) * scale).toFixed(2) + "px)";
        }
        rot.style.transform = "rotate(" + wrap180(cfg.nyaw - mapYaw).toFixed(2) + "deg)";
        var arrowTurn = cfg.rotate ? 0 : wrap180(yaw - mapYaw);
        var at = arrowTurn.toFixed(1);
        if (at !== st.lastArrow) { arrow.style.transform = "rotate(" + at + "deg)"; st.lastArrow = at; }

        var r = mapYaw * Math.PI / 180, c = Math.cos(r), s = Math.sin(r), square = cfg.square;
        var where = { x: p.x, y: p.y };
        // Spread out stacked icons: where the moved ones go (sp.at), and their lines
        var sp = cfg.spread ? spreadFor(scale, iconScale) : null;
        if (sp) {
            spreadPlace(sp, where, c, s, radius, half, ringW, square, iconScale, ui);
            st.spreadShown = true;
        } else if (st.spreadShown) {
            spreadHide(0);
            st.spreadShown = false;
        }
        // The map's icons (never on the rim)
        var icons = st.slow && st.slow.icons ? st.slow.icons : [];
        var size = ICON * iconScale, i, q, it, pos, at;
        for (i = 0; i < icons.length; i++) {
            var ic = icons[i];
            if (!ic[0]) continue;
            q = project(ic[1], ic[2], where, c, s, radius, square);
            if (q.d > 1) continue;
            it = pools.icon.take(ic[0], "");
            setSize(it, size);
            at = sp && sp.iconSpot[i] >= 0 ? sp.at[sp.iconSpot[i]] : null;
            pos = at || pullIn(q.u * half, q.v * half, half, size, ringW, square);
            place(it, pos[0], pos[1], size);
            setChevron(it, ic[3], size, iconScale);
        }
        pools.icon.finish();
        // Markers; out-of-range clamped ones on the ring (the middle of the blue line)
        var marks = st.fast || [], reach = half - ringW / 2;
        for (i = 0; i < marks.length; i++) {
            var mk = marks[i];
            if (!mk[0]) continue;
            var msize = mk[1] * iconScale;
            q = project(mk[3], mk[4], where, c, s, radius, square);
            if (q.d > 1) {
                if (!mk[6]) continue;
                it = pools.edge.take(mk[0], mk[2]);
                setSize(it, msize);
                place(it, q.u / q.d * reach, q.v / q.d * reach, msize);
            } else {
                it = pools.marker.take(mk[0], mk[2]);
                setSize(it, msize);
                at = sp && st.spreadRank[i] >= 0 ? sp.at[sp.nIcons + st.spreadRank[i]] : null;
                pos = at || pullIn(q.u * half, q.v * half, half, msize, ringW, square);
                place(it, pos[0], pos[1], msize);
            }
            setChevron(it, mk[5], msize, iconScale);
        }
        pools.marker.finish();
        pools.edge.finish();
        // North marker: a far point to the north, pulled onto the rim
        if (cfg.north) {
            var n = cfg.nyaw * Math.PI / 180, far = radius * 4;
            q = project(p.x + far * Math.cos(n), p.y + far * Math.sin(n), where, c, s, radius, square);
            var bsize = NORTH * iconScale;
            var bx = q.u / q.d * reach + half - bsize / 2, by = q.v / q.d * reach + half - bsize / 2;
            var bt = "translate(" + bx.toFixed(1) + "px," + by.toFixed(1) + "px)";
            if (badge.style.display === "none") badge.style.display = "";
            if (bt !== st.lastBadge) { badge.style.transform = bt; st.lastBadge = bt; }
        } else if (badge.style.display !== "none") {
            badge.style.display = "none";
        }
    }

    // endregion

    requestAnimationFrame(frame);
    st.kickAt = Date.now();  // (frame()'s restart waits for this first look-up)
    findFeed();
})();
