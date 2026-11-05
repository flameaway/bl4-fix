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
    var pools = { icon: new Pool(iconLayer), marker: new Pool(markerLayer), edge: new Pool(edgeLayer) };

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
        // The map's icons (never on the rim)
        var icons = st.slow && st.slow.icons ? st.slow.icons : [];
        var size = ICON * iconScale, i, q, it, pos;
        for (i = 0; i < icons.length; i++) {
            var ic = icons[i];
            if (!ic[0]) continue;
            q = project(ic[1], ic[2], where, c, s, radius, square);
            if (q.d > 1) continue;
            it = pools.icon.take(ic[0], "");
            setSize(it, size);
            pos = pullIn(q.u * half, q.v * half, half, size, ringW, square);
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
                pos = pullIn(q.u * half, q.v * half, half, msize, ringW, square);
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
