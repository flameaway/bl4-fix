// BL4 Easy Respec: right-click a skill in the Skills menu to take one point back out, or unspec
// everything at once.
//
// respec.js loads this and runs it again whenever it changes on disk, after calling
// window.Bl4Respec_Dispose to undo everything the previous copy set up.
//
// Right mouse never reaches the menus' scripts (BL4 keeps it for itself), so the mod's Python side
// watches the button and, while this page says the Skills menu is showing, bumps a counter in a
// loose file on every right click. This page polls that file, checks the hovered skill against the
// tree's rules, and either asks Python to refund the point or explains why not.
//
// Messages to Python ride on the page's sound events: ScriptEvent_Spatial(name, x, y) calls the
// skills ui_script function `name` with position {x, y}. Python hooks RemovePoint, picks out ours
// by a very negative x, and swallows them. Both numbers arrive as 32-bit floats, so every value
// stays below 2^24.
//
// The page binds its own handlers (activation, hover) when it initialises, which can be before
// this runs, so nothing here wraps them. We watch what they leave behind instead: a hidden widget
// is marked "inactive", and the cursor handler marks the hovered skill "hover_effect".
(function () {
    "use strict";

    // Undo steps for everything start() sets up, run in reverse by Bl4Respec_Dispose
    const cleanup = [];
    let disposed = false;
    window.Bl4Respec_Dispose = () => {
        disposed = true;
        for (const undo of cleanup.splice(0).reverse()) {
            try {
                undo();
            } catch (err) {
                console.error("bl4respec cleanup: " + err);
            }
        }
    };

    const CHANNEL = "RemovePoint";
    const MARK = 1 << 23;
    const OP = { Active: 1, Remove: 3, UnspecAll: 4 };
    const SIGNAL_URL = "coui://uiresources/bl4respec/right_click.txt";
    const POLL_MS = 40;
    // Python forgets the menu is showing if it doesn't hear this often (see Page in __init__.py)
    const HEARTBEAT_MS = 1000;
    const MESSAGE_MS = 2600;

    // FNV-1a folded to 24 bits; Python computes the same over each skill graph's name
    function hash24(text) {
        let h = 0x811c9dc5;
        for (let i = 0; i < text.length; i++) {
            h ^= text.charCodeAt(i);
            h = Math.imul(h, 0x01000193) >>> 0;
        }
        return ((h >>> 24) ^ (h & 0xffffff)) >>> 0;
    }

    function send(widget, op, arg, value) {
        widget.TriggerWidgetEvent("ScriptEvent_Spatial", CHANNEL, -(MARK + op * 0x10000 + arg), value);
    }

    // region Rules

    const isModifier = (node) => node.nodetype === NodeType.Augment || node.nodetype === NodeType.Capstone;
    const spentIn = (node) => (node && node.nodetype === NodeType.Passive ? node.progresspointsspent : 0);
    const tierPoints = (tier) => tier.nodes.reduce((sum, node) => sum + spentIn(node), 0);
    const tierInUse = (tier) => tier.nodes.some((node) => spentIn(node) > 0 || (isModifier(node) && node.isactivated));
    const segmentPoints = (segment) => segment.tiers.reduce((sum, tier) => sum + tierPoints(tier), 0);
    const segmentInUse = (segment) => segment.tiers.some(tierInUse);
    const skillName = (node) => node.displayname || "that skill";

    function sameNode(link, s, t, n) {
        return link.segmentindex === s && link.tierindex === t && link.nodeindex === n;
    }

    // Whether one point can come out of tree.segments[s].tiers[t].nodes[n]. Returns null if it can,
    // otherwise the reason it can't.
    function blockReason(tree, s, t, n) {
        const segment = tree.segments[s];
        const node = segment && segment.tiers[t] && segment.tiers[t].nodes[n];
        if (!node || node.nodetype === NodeType.None) return "Nothing to remove here.";
        if (isModifier(node)) return "Augments and capstones don't cost skill points.";
        if (node.progresspointsspent <= 0) {
            return node.bonuspoints > 0 ? "Those points come from your class mod." : "No points in this skill.";
        }

        // Each row unlocks at a number of points spent in the rows above it, within this trunk or branch
        let above = 0;
        for (let row = 0; row < segment.tiers.length; row++) {
            const tier = segment.tiers[row];
            const after = above - (row > t ? 1 : 0);
            if (row > t && tierInUse(tier) && after < tier.progresspoints) {
                return `Row ${row + 1} needs ${tier.progresspoints} points in the rows above it. Remove points from row ${row + 1} first.`;
            }
            above += tierPoints(tier);
        }

        // Branches unlock at a number of points spent in the trunk
        if (s === 0) {
            const trunkAfter = segmentPoints(segment) - 1;
            for (let b = 1; b < tree.segments.length; b++) {
                const branch = tree.segments[b];
                if (segmentInUse(branch) && trunkAfter < branch.progresspoints) {
                    return `The branches need ${branch.progresspoints} points in the trunk. Remove points from the branches first.`;
                }
            }
        }

        // Skills joined by a line need a point in a skill they hang from
        if (node.progresspointsspent === 1) {
            for (let cs = 0; cs < tree.segments.length; cs++) {
                const tiers = tree.segments[cs].tiers;
                for (let ct = 0; ct < tiers.length; ct++) {
                    for (let cn = 0; cn < tiers[ct].nodes.length; cn++) {
                        const child = tiers[ct].nodes[cn];
                        if (!child || !(spentIn(child) > 0 || (isModifier(child) && child.isactivated))) continue;
                        const links = Array.from(child.links || []);
                        if (!links.some((link) => sameNode(link, s, t, n))) continue;
                        const otherParent = links.some((link) => {
                            if (sameNode(link, s, t, n)) return false;
                            const parent = tree.segments[link.segmentindex]?.tiers[link.tierindex]?.nodes[link.nodeindex];
                            return spentIn(parent) > 0;
                        });
                        if (!otherParent) {
                            return `${skillName(child)} needs a point in this skill. Remove points from ${skillName(child)} first.`;
                        }
                    }
                }
            }
        }
        return null;
    }

    // endregion
    // region Messages

    function showMessage(widget, text, title = "Can't remove that point") {
        let box = widget.Bl4Respec_Box;
        if (!box) {
            box = document.createElement("div");
            box.className = "bl4respec_message bl4respec_hidden";
            box.appendChild(document.createElement("div")).className = "bl4respec_title";
            box.appendChild(document.createElement("div")).className = "bl4respec_text";
            widget.WidgetDiv.appendChild(box);
            widget.Bl4Respec_Box = box;
        }
        box.firstChild.textContent = title;
        box.lastChild.textContent = text;
        box.classList.toggle("bl4respec_hidden", false);
        clearTimeout(widget.Bl4Respec_BoxTimer);
        widget.Bl4Respec_BoxTimer = setTimeout(() => box.classList.toggle("bl4respec_hidden", true), MESSAGE_MS);
    }

    function hideMessage(widget) {
        clearTimeout(widget.Bl4Respec_BoxTimer);
        widget.Bl4Respec_Box?.classList.toggle("bl4respec_hidden", true);
    }

    // endregion
    // region Right clicks

    // The skill under the cursor as {segment, tier, node}, or null
    function hoveredSkill(widget) {
        const segments = (widget.skilltree && widget.skilltree.segments) || [];
        for (let s = 0; s < segments.length; s++) {
            for (let t = 0; t < segments[s].tiers.length; t++) {
                const nodes = segments[s].tiers[t].nodes;
                for (let n = 0; n < nodes.length; n++) {
                    if (nodes[n].querySelector('[class*="skill_node_container"].hover_effect')) {
                        return { segment: s, tier: t, node: n };
                    }
                }
            }
        }
        return null;
    }

    function onRightClick(widget) {
        const hover = hoveredSkill(widget);
        const model = widget.DataModel;
        if (!hover || !model || confirmOpen(widget)) return;
        const tree = model.trees[model.activetreeindex];
        if (!tree) return;

        const reason = blockReason(tree, hover.segment, hover.tier, hover.node);
        if (reason) {
            widget.PlaySound("InsufficientPoints");
            showMessage(widget, reason);
            return;
        }
        hideMessage(widget);
        send(widget, OP.Remove, hover.tier * 16 + hover.node, hash24(tree.segments[hover.segment].name));
        widget.PlaySound(CHANNEL);
    }

    // endregion
    // region Unspec all

    function button(parent, className, text, onClick) {
        const element = document.createElement("div");
        element.className = "bl4respec_button " + className;
        element.textContent = text;
        parent.appendChild(element);
        new CursorHandler({ element, onClick });
        return element;
    }

    function addUnspecButton(widget) {
        if (widget.Bl4Respec_Unspec || !widget.WidgetDiv) return;
        widget.Bl4Respec_Unspec = button(widget.WidgetDiv, "bl4respec_unspec", "Unspec All", () => openConfirm(widget));
    }

    const confirmOpen = (widget) => !!widget.Bl4Respec_Confirm && !widget.Bl4Respec_Confirm.classList.contains("bl4respec_hidden");

    function closeConfirm(widget) {
        widget.Bl4Respec_Confirm?.classList.toggle("bl4respec_hidden", true);
    }

    function openConfirm(widget) {
        const model = widget.DataModel;
        const spent = model ? Array.from(model.trees).reduce((sum, tree) => sum + tree.pointsspent, 0) : 0;
        if (spent <= 0) {
            widget.PlaySound("InsufficientPoints");
            showMessage(widget, "You haven't spent any skill points.", "Nothing to unspec");
            return;
        }
        hideMessage(widget);

        let overlay = widget.Bl4Respec_Confirm;
        if (!overlay) {
            overlay = document.createElement("div");
            overlay.className = "bl4respec_overlay bl4respec_hidden";
            const panel = overlay.appendChild(document.createElement("div"));
            panel.className = "bl4respec_panel";
            panel.appendChild(document.createElement("div")).className = "bl4respec_panel_title";
            panel.appendChild(document.createElement("div")).className = "bl4respec_panel_text";
            const buttons = panel.appendChild(document.createElement("div"));
            buttons.className = "bl4respec_panel_buttons";
            button(buttons, "bl4respec_cancel", "Cancel", () => closeConfirm(widget));
            button(buttons, "bl4respec_confirm", "Unspec All", () => unspecAll(widget));
            widget.WidgetDiv.appendChild(overlay);
            widget.Bl4Respec_Confirm = overlay;
        }
        overlay.querySelector(".bl4respec_panel_title").textContent = "Unspec all skills?";
        overlay.querySelector(".bl4respec_panel_text").textContent =
            `This refunds all ${spent} skill points in all three trees. Augments and capstones get unequipped.`;
        overlay.classList.toggle("bl4respec_hidden", false);
    }

    function unspecAll(widget) {
        closeConfirm(widget);
        send(widget, OP.UnspecAll, 0, 0);
        widget.PlaySound("ResetPoints");
        widget.Event_ResetPoints(); // the menu's own reset animation
    }

    // endregion
    // region Watching the page

    const state = { showing: false, lastBeat: 0, count: undefined, polling: false };

    function isShowing(widget) {
        return !!widget.WidgetDiv && !widget.WidgetDiv.classList.contains("inactive") && !!widget.DataModel
            && !widget.DataModel.isplayerstatsopened;
    }

    function poll(widget) {
        if (state.polling) return;
        state.polling = true;
        const xhr = new XMLHttpRequest();
        xhr.onreadystatechange = () => {
            if (xhr.readyState !== 4) return;
            state.polling = false;
            const ok = xhr.status === 200 || (xhr.status === 0 && xhr.responseText);
            // Python writes the file on its first right click; until then count it as zero clicks
            const count = ok ? xhr.responseText.trim() : "0";
            const last = state.count;
            state.count = count;
            if (last !== undefined && count !== last && state.showing) onRightClick(widget);
        };
        try {
            xhr.open("GET", SIGNAL_URL, true);
            xhr.send();
        } catch (err) {
            state.polling = false;
        }
    }

    function tick() {
        const widget = document.querySelector("widget-skills");
        if (!widget || typeof widget.TriggerWidgetEvent !== "function") return;
        const showing = isShowing(widget);
        const now = Date.now();
        if (showing !== state.showing || (showing && now - state.lastBeat >= HEARTBEAT_MS)) {
            if (showing !== state.showing) {
                state.count = undefined; // clicks from before the menu showed don't count
                if (showing) {
                    addUnspecButton(widget);
                } else {
                    hideMessage(widget);
                    closeConfirm(widget);
                }
            }
            state.showing = showing;
            state.lastBeat = now;
            send(widget, OP.Active, 0, showing ? 1 : 0);
        }
        if (showing) poll(widget);
    }

    function startWatching() {
        const timer = setInterval(() => {
            try {
                tick();
            } catch (err) {
                console.error("bl4respec: " + err);
            }
        }, POLL_MS);
        cleanup.push(() => clearInterval(timer));
        cleanup.push(() => {
            const widget = document.querySelector("widget-skills");
            if (!widget) return;
            clearTimeout(widget.Bl4Respec_BoxTimer);
            for (const key of ["Bl4Respec_Box", "Bl4Respec_Unspec", "Bl4Respec_Confirm"]) {
                widget[key]?.remove();
                delete widget[key];
            }
        });
    }

    // endregion
    // region Hint bar

    // Right click removes the hovered skill's point now, and Python takes it off the game's "undo
    // the last point" (controllers keep that), so swap the game's Undo hint for ours. The page
    // builds its hints and hands them to the game in one engine.trigger call, which we filter. Key
    // hints take the glyph name in lowercase, like the game's own "gamepad_facebutton_bottom".
    const UNDO_ACTION = "action_ui_skills_undo";
    const STATS_ACTION = "action_ui_show_player_stats";

    function swapHints() {
        const proto = window.Widget_Skills.prototype;
        const updateHintBar = proto.UpdateHintBar;

        const wrapped = function (...args) {
            if (this.IsUsingGamepad() || !this.DataModel || this.DataModel.isplayerstatsopened) {
                return updateHintBar.apply(this, args);
            }
            const trigger = engine.trigger;
            engine.trigger = function (name, entries, ...rest) {
                if (name === "Event_OnHintBarEntriesReceived" && Array.isArray(entries)) {
                    entries = entries.filter((entry) => !entry || entry.input_action !== UNDO_ACTION);
                    const at = entries.findIndex((entry) => entry && entry.input_action === STATS_ACTION);
                    if (!entries.some((entry) => entry && entry.loc_text === "Remove Point")) {
                        const hint = OakUtils.SetupHintEntry("rightmousebutton", "Remove Point", true, false, false);
                        entries.splice(at < 0 ? 0 : at, 0, hint);
                    }
                }
                return trigger.call(this, name, entries, ...rest);
            };
            try {
                return updateHintBar.apply(this, args);
            } finally {
                engine.trigger = trigger;
            }
        };
        proto.UpdateHintBar = wrapped;

        const refresh = () => {
            const widget = document.querySelector("widget-skills");
            if (widget && widget.DataModel) widget.UpdateHintBar();
        };
        cleanup.push(() => {
            if (proto.UpdateHintBar === wrapped) proto.UpdateHintBar = updateHintBar;
            refresh();
        });
        refresh();
    }

    // endregion

    // The menu's widget loader can run this before the skill scripts it depends on (verified)
    (function start(attempt) {
        if (disposed) return;
        if (typeof NodeType === "undefined" || !window.Widget_Skills) {
            if (attempt < 600) setTimeout(() => start(attempt + 1), 50);
            return;
        }
        startWatching();
        swapHints();
    })(0);
})();
