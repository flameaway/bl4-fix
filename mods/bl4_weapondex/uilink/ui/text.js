// BL4 UI Link: typing for our screens with the game's own text input (uilink v8, TextBox).
// Runs in the ECHO overlay page (it has the keyboard while a menu is up) and the title screen's.
// While Python has a box being typed in for this page, an invisible <input> here keeps the focus
// and sends what's typed; the screen that shows the box draws it from Python's state as before.
(function () {
    "use strict";
    var ns = bl4linkNs;
    var KEY = "bl4text";
    var IDLE_MS = 300; // polls with no box being typed in (only while this page runs: a menu is up)
    var ACTIVE_MS = 120;
    var old = window.bl4linkText;
    if (old && old.teardown) old.teardown();
    var st = { id: 0, rev: -1, timer: 0, dead: false };
    window.bl4linkText = st;

    var box = document.createElement("input");
    box.type = "text";
    box.setAttribute("aria-hidden", "true");
    box.style.cssText = "position:fixed;left:0;top:0;width:2px;height:2px;opacity:0;border:0;padding:0;" +
        "margin:0;z-index:-1;pointer-events:none;background:transparent;color:transparent;";
    document.body.appendChild(box);

    function send(kind, value) {
        if (!st.id) return;
        ns.ext.send(KEY, { kind: kind, id: st.id, value: value === undefined ? "" : value });
    }
    function caretToEnd() {
        try { box.setSelectionRange(box.value.length, box.value.length); } catch (e) { /* not focused */ }
    }
    box.addEventListener("input", function () {
        send("text", box.value);
        caretToEnd();
    });
    box.addEventListener("keydown", function (e) {
        var k = e.keyCode;
        if (k === 13) { e.preventDefault(); send("enter"); }
        else if (k === 27) { e.preventDefault(); send("esc"); }
        else if (k === 9) { e.preventDefault(); send("tab"); }
        else if (k === 46) { e.preventDefault(); box.value = ""; send("clear"); }
        else if (k >= 35 && k <= 40) { e.preventDefault(); caretToEnd(); } // End Home arrows: the caret stays at the end
        else if (k === 86 && e.ctrlKey) {
            // Ctrl+V: if the game's input doesn't paste by itself, Python reads the clipboard
            var before = box.value;
            setTimeout(function () { if (box.value === before) send("paste"); }, 60);
        }
    });

    function stopTyping() {
        if (!st.id) return;
        st.id = 0;
        st.rev = -1;
        if (document.activeElement === box) box.blur();
        box.value = "";
    }
    function poll() {
        if (st.dead) return;
        ns.ext.get(KEY, false, function (snap) {
            if (st.dead) return;
            var s = snap && snap.state;
            if (!s || !s.id || s.view !== ns.view) {
                stopTyping();
            } else {
                if (s.id !== st.id) { st.id = s.id; st.rev = -1; }
                if (s.rev !== st.rev) {
                    // Python changed the text (a new box, a filter, a paste, a clear): show its copy
                    st.rev = s.rev;
                    if (box.value !== s.text) box.value = s.text;
                }
                if (document.activeElement !== box) box.focus();
                caretToEnd();
            }
            st.timer = setTimeout(poll, st.id ? ACTIVE_MS : IDLE_MS);
        });
    }

    st.teardown = function () {
        st.dead = true;
        clearTimeout(st.timer);
        stopTyping();
        if (box.parentNode) box.parentNode.removeChild(box);
        if (window.bl4linkText === st) window.bl4linkText = null;
    };
    ns.ext.teardowns[KEY] = st.teardown;
    poll();
})();
