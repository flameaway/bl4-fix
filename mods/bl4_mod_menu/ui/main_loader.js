"use strict";

// BL4 Mod Menu, title screen, for a game that still has our old main menu page pak (4247, retired
// in 0.2.0: ui/link.js does this through the BL4 UI Link now). It marks the page as the main menu
// and runs the normal loader, which loads menu.js.
// menu.js then puts MODS under Options (with a "!" when mod updates are waiting) instead of in
// the pause menu.

(function () {
    window.bl4ModMenuContext = "main";
    var xhr = new XMLHttpRequest();
    xhr.onreadystatechange = function () {
        if (xhr.readyState !== 4) return;
        if ((xhr.status === 200 || xhr.status === 0) && xhr.responseText) {
            try {
                new Function(xhr.responseText)();
            } catch (err) {}
        }
    };
    try {
        xhr.open("GET", "coui://uiresources/bl4modmenu/loader.js", true);
        xhr.send();
    } catch (err) {}
})();
