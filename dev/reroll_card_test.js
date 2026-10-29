// Offline test of bl4_reroll/ui/card.js keeping the card order after a reroll: node dev/reroll_card_test.js mods/bl4_reroll/ui/card.js
const fs = require("fs");
let now = 1000;
Date.now = () => now;
let timers = [], tid = 0;
global.setTimeout = (fn, ms) => { timers.push({ id: ++tid, at: now + (ms || 0), fn }); return tid; };
global.clearTimeout = (id) => { timers = timers.filter(t => t.id !== id); };
function advance(ms) {
  const end = now + ms;
  for (;;) {
    timers.sort((a, b) => a.at - b.at || a.id - b.id);
    const t = timers[0];
    if (!t || t.at > end) break;
    timers.shift(); now = Math.max(now, t.at); t.fn();
  }
  now = end;
}
function el(cls) {
  const e = { className: cls || "", style: {}, children: [], parentNode: null, offsetHeight: 20, offsetWidth: 400, textContent: "",
    classList: { set: new Set(), toggle(c, on) { on ? this.set.add(c) : this.set.delete(c); }, add(c) { this.set.add(c); }, remove(c) { this.set.delete(c); }, contains(c) { return this.set.has(c); } },
    appendChild(c) { c.parentNode = e; e.children.push(c); return c; },
    removeChild(c) { e.children = e.children.filter(x => x !== c); c.parentNode = null; },
    querySelector(sel) { const want = sel.replace(/^\./, ""); const walk = (n) => { for (const c of n.children) { if (String(c.className).split(" ").includes(want)) return c; const f = walk(c); if (f) return f; } return null; }; return walk(e); },
    querySelectorAll() { return []; },
    getBoundingClientRect() { return { left: 0, top: 0, width: 100, height: 20, bottom: 20 }; } };
  return e;
}
// The card: rows 24 high (the 2nd 40: wrapped text), 6 apart
const HEIGHTS = [24, 40, 24];
let rowsEls = [];
function makeRows(n) {
  rowsEls = [];
  let top = 100;
  for (let i = 0; i < n; i++) {
    const r = el("item_card_secondary_stats_enhance");
    r.natTop = top; r.h = HEIGHTS[i]; top += r.h + 6;
    r.getBoundingClientRect = function () {
      const m = /translateY\((-?[\d.]+)px\)/.exec(this.style.transform || "");
      const t = this.natTop + (m ? parseFloat(m[1]) : 0);
      return { left: 0, top: t, width: 300, height: this.h, bottom: t + this.h };
    };
    r.text = el("item_card_secondary_stat_enhance_text");
    r.querySelector = (s) => s === ".item_card_secondary_stat_enhance_text" ? r.text : null;
    rowsEls.push(r);
  }
}
const host = el("item_card"); host.getBoundingClientRect = () => ({ left: 0, top: 0, width: 400, height: 500, bottom: 500 });
const tooltip = el("item_card_tooltip"); let tipH = 300; tooltip.getBoundingClientRect = () => ({ left: 0, top: 0, width: 400, height: tipH, bottom: tipH });
const card = {
  DataModel: null,
  querySelector(sel) { if (sel === ".item_card_tooltip") return tooltip; if (sel === ".item_card") return host; return null; },
  querySelectorAll(sel) { if (sel === ".item_card_secondary_stats_enhance") return rowsEls; return []; },
};
function stat(name, v) { return { value: "[secondary]" + name + "[/secondary] is increased by [secondary]+" + v + "%[/secondary]" }; }
function show(name, stats) {
  card.DataModel = { name, level: "Lvl 50", rarityident: "epic", itemtype: "enhancement", bannertype: "equipped", mousepositioned: true,
    secondary_stat_entries: stats, primary_stat_entries: [] };
  if (rowsEls.length !== stats.length) makeRows(stats.length);
}
global.document = {
  querySelectorAll: (s) => s === "widget-item-card" && card.DataModel ? [card] : [],
  createElement: () => el(), getElementById: () => null, head: el(), body: el(),
};
global.getComputedStyle = () => ({});
const sent = [];
let getCb = null;
global.window = { bl4ModMenu: { log: (t) => console.log("LOG", t), get: (u, cb) => cb(""),
  ext: { teardowns: {}, send: (k, m) => sent.push(m), get: (k, x, cb) => { getCb = cb; } } } };
let state = null;
function reply() { if (getCb) { const cb = getCb; getCb = null; cb({ state: JSON.parse(JSON.stringify(state)) }); } }

// Game order (sorted by priority): Gun Damage 1, AR Status Effect Chance, Pistol Equip Speed
show("Retributive Prestidigital Hoarder", [stat("Gun Damage", 25), stat("Assault Rifle Status Effect Chance", 38), stat("Pistol Equip Speed", 150)]);
const item = { kind: "enhancement", level: 50, rarity: "epic", equipped: true, handle: 7, serial: "A", skills: {}, locked: [false, false, false],
  names: ["Assault Rifle Status Effect Chance", "Pistol Equip Speed", "Gun Damage"],
  plain: ["assault rifle status effect chance", "pistol equip speed", "gun damage"] };
state = { items: [item], pick: { handle: 7, line: 0 }, confirm: null, keys: { reroll: "T", pick: "G" }, costs: { all: 25, one: 50 }, eridium: 100, roll: { id: 0 } };

eval(fs.readFileSync(process.argv[2], "utf8"));
reply(); advance(100); reply(); advance(100);
const shownTexts = () => rowsEls.map((r, i) => ({ i, top: r.getBoundingClientRect().top })).sort((a, b) => a.top - b.top)
  .map(x => card.DataModel.secondary_stat_entries[x.i].value.replace(/\[\/?\w+\]/g, "").split(" is ")[0] + (rowsEls[x.i].classList.contains("bl4rr_row_pick") ? " [PICK]" : ""));
const lastHover = () => sent.filter(m => m.do === "hover").pop();
console.log("before:", shownTexts());

// Reroll line 0 (AR Status Effect Chance, shown row 1) -> Sniper Rifle Reload Speed (the game sorts it last)
const fp = lastHover().fp;
state.roll = { id: 1, fp, kind: "one", line: 0, cost: 50, equipped: true, lines: [
  { row: 1, old: "Assault Rifle Status Effect Chance", new: "Sniper Rifle Reload Speed", spins: true, changed: true, reel: ["Gun Accuracy", "SMG Fire Rate"] },
  { row: 2, old: "Pistol Equip Speed", new: "Pistol Equip Speed", spins: false, changed: false, reel: [] },
  { row: 0, old: "Gun Damage", new: "Gun Damage", spins: false, changed: false, reel: [] }] };
advance(100); reply(); advance(1500 + 900);
console.log("landed sent:", sent.some(m => m.do === "landed"));
// The swap: the game shows the new item in its own order (SR Reload Speed last)
show("Flurrying Prestidigital Hoarder", [stat("Gun Damage", 25), stat("Pistol Equip Speed", 150), stat("Sniper Rifle Reload Speed", 30)]);
advance(50);
console.log("after swap, before Python's state:", shownTexts(), "transforms:", rowsEls.map(r => r.style.transform || "-"));
item.serial = "B"; item.names = ["Sniper Rifle Reload Speed", "Pistol Equip Speed", "Gun Damage"]; item.plain = ["sniper rifle reload speed", "pistol equip speed", "gun damage"];
state.roll.applied = { ok: true, left: 50, message: "Replaced", equipped: false };
reply(); advance(100); reply(); advance(100);
console.log("after applied:", shownTexts());
console.log("hover secondary sent to Python:", JSON.parse(lastHover().fp).secondary.map(t => t.replace(/\[\/?\w+\]/g, "").split(" is ")[0]));
// Rows must sit where the game's slots were: tops 100, 130, 176 (heights 24,40,24 -> shown 24,24,40)
console.log("tops:", rowsEls.map(r => r.getBoundingClientRect().top));
advance(4000); reply(); advance(100);
console.log("after banner:", shownTexts());
// Hover away (card gone) then back: the game's order again
const saved = card.DataModel; card.DataModel = null; advance(400);
card.DataModel = saved; advance(100); reply(); advance(100);
console.log("hover away and back:", shownTexts(), "transforms:", rowsEls.map(r => r.style.transform || "-"));
