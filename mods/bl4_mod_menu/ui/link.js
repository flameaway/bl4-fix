"use strict";

// BL4 Mod Menu through the BL4 UI Link (shared/uilink): loads loader.js in the game's menu page
// (view_status_menus, where the pause menu lives) and on the title screen (menu_main), the job
// that one <script> line in our old pause / main menu page paks (4245 / 4247) did. loader.js then
// loads menu.js. Nothing to undo when the link drops this script: menu.js keeps running and goes
// quiet without the mod's server, as it always did.

(function () {
    var ns = typeof bl4linkNs !== "undefined" ? bl4linkNs : null;
    var view = ns && ns.view;
    if (view === "menu_main") window.bl4ModMenuContext = "main";
    else if (view !== "view_status_menus") return;
    // Already running: an old page pak still mounted this session, or a re-run of this file
    if (window.bl4ModMenu && window.bl4ModMenu.loaderRunning) return;
    var xhr = new XMLHttpRequest();
    xhr.onreadystatechange = function () {
        if (xhr.readyState !== 4) return;
        // Loose coui:// files answer with status 0 or 200
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
