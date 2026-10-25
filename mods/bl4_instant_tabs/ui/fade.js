// BL4 Instant Menu Tabs: skip the fade-in each ECHO menu page plays when you switch to it.
//
// Every page (inventory, skills, map, missions, ...) calls PlayAnimation("status_menu_fadein") from
// its Activate, a 0.25 s ease-in from invisible (_shared/data/shared_animations.data). We let the
// animation start as usual and then end it straight away, which jumps it to its last frame, so
// the page shows fully at once. If the game ever can't find the animation, it just plays.
//
// Run by loader.js, again each time this file changes, so it only ever wraps the game's own
// PlayAnimation (kept in ns.origPlay) and reads the on/off switch at call time.

var ns = window.bl4InstantTabs;
var FADE_IN = "status_menu_fadein";

function install() {
    if (typeof Animator === "undefined") {
        setTimeout(install, 500);
        return;
    }
    if (!ns.origPlay) ns.origPlay = Animator.prototype.PlayAnimation;
    var orig = ns.origPlay;
    Animator.prototype.PlayAnimation = function (AnimationId, Options) {
        orig.apply(this, arguments);
        if (AnimationId === FADE_IN && ns.config && ns.config.skipFade) {
            try {
                this.EndAnimation(AnimationId, Options || {});
            } catch (err) {
                // Leave the fade running
            }
        }
    };
}

install();
