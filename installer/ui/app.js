/* BL4 Fix setup page, built from the game's own menus: the title screen, its main menu, Options
   rows (toggle, stepper, list button) and fonts, from game/ (tools/gen_setup_ui.py). Talks to
   webgui.Api through window.pywebview.api; with ?mock in the URL it runs on fake data (previews).
   Templates are cloned the way the in-game Mod Menu does it: data-bind and l10n attributes stripped,
   the gbx and widget custom elements turned into divs. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const params = new URLSearchParams(location.search);

const S = {
  api: null,
  info: null,
  gameDir: "",
  status: null,
  selected: new Set(),
  page: "loadout",
  busy: false,
  task: null,
  logCount: 0,
  log: [],
  friends: { host_port: 7777, allow_lan: true, firewall: false, friends: [], firewall_current: true },
  friendsDirty: false,
  firewallSaved: false, // the firewall on/off choice as saved (an install or Save applies a change)
  update: null, // what the latest release has that this app doesn't (api.update_check), once known
  publicIp: "",
  draft: { name: "", ip: "", port: "" },
  // Disk Space: the last scan's numbers for the current choice (api.space_info), the voice
  // languages to keep, whether to blank old patch data
  space: { info: null, keep: null, old: true, seq: 0 },
  // Safe mode (api.safe_mode: core.safe_mode_status): on, aside, missing, left, others, time
  safe: null,
};

/* ---------- templates ---------- */

const T = {};

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

// Game markup as live DOM: bindings and localisation ids off, custom elements as divs
function clean(root) {
  $$("script, link", root).forEach((n) => n.remove());
  const walk = (node) => {
    for (const child of [...node.children]) walk(child);
    for (const a of [...node.attributes]) {
      if (/^(data-bind|data-l10n|gbx-data-l10n|data-input|gue-|tooltip-data-key|gbx-component-)/.test(a.name)) node.removeAttribute(a.name);
    }
    const tag = node.tagName.toLowerCase();
    if (tag.includes("-") && tag !== "gbx-text-input" && tag !== "gbx-component") {
      const div = document.createElement("div");
      for (const a of node.attributes) div.setAttribute(a.name, a.value);
      div.dataset.tag = tag;
      while (node.firstChild) div.appendChild(node.firstChild);
      node.replaceWith(div);
    }
  };
  for (const child of [...root.children]) walk(child);
  return root;
}

function parse(html) {
  const t = document.createElement("template");
  t.innerHTML = html;
  return clean(t.content);
}

async function loadTemplates() {
  const get = (p) => fetch("game/" + p).then((r) => r.text());
  const [list, mm, toggle, stepper, input, options, menuMain, title, filters] = await Promise.all([
    get("components/list_button/list_button.html"),
    get("components/main_menu_button/main_menu_button.html"),
    get("components/toggle/toggle.html"),
    get("components/stepper/stepper.html"),
    get("components/text_input/text_input.html"),
    get("menu_options/menu_options.html"),
    get("menu_main/menu_main.html"),
    get("title_screen/title_screen.html"),
    get("_setup/filters.svg"),
  ]);
  T.list = parse(list).firstElementChild;
  T.mm = parse(mm).firstElementChild;
  T.toggle = parse(toggle).firstElementChild;
  T.stepper = parse(stepper).firstElementChild;
  T.input = parse(input).firstElementChild;
  const opts = parse(options);
  T.entry = $(".menu_options_regular_entry", opts);
  T.title = $(".menu_options_title_entry", opts);
  T.menuMain = parse(menuMain);
  T.titleScreen = parse(title);
  // The game's tints (Coherent colour matrices), as SVG filters the stylesheets point at
  document.body.insertAdjacentHTML("afterbegin", filters);
}

const make = (name) => T[name].cloneNode(true);

function hoverable(target, host = target) {
  target.addEventListener("mouseenter", () => host.classList.add("hover_effect"));
  target.addEventListener("mouseleave", () => host.classList.remove("hover_effect"));
}

function onClick(target, fn) {
  target.addEventListener("click", (e) => {
    e.stopPropagation();
    fn(e);
  });
}

function listButton(text, fn, cls) {
  const b = make("list");
  b.classList.add("su_btn");
  if (cls) b.classList.add(...cls.split(" "));
  $(".button_txt", b).textContent = text;
  hoverable(b);
  if (fn) onClick(b, () => { if (!b.classList.contains("su_disabled")) fn(); });
  return b;
}

/* ---------- boot: the title screen ---------- */

async function boot() {
  S.api = await getApi();
  await loadTemplates();
  $("#boot_slot").appendChild(T.titleScreen.firstElementChild);
  const still = params.has("still");
  if (still) document.body.classList.add("still");
  const started = performance.now();
  S.info = await S.api.init();
  S.gameDir = S.info.game_dir;
  S.selected = new Set(S.info.mods.filter((m) => m.required || m.default).map((m) => m.id));
  buildMenu();
  wire();
  const statusP = refreshStatus();
  loadFriends();
  lookupIp();
  if (!still) {
    await sleep(Math.max(0, 2300 - (performance.now() - started)));
    $("#boot-prompt").textContent = "ECHO-4 link established";
    await sleep(500);
  }
  $("#boot").classList.add("done");
  showPage(params.get("page") || "loadout");
  await statusP;
  setInterval(pollRunning, 3000);
  if (S.info.updated) toast(`Updated to BL4 Fix ${S.info.version}`);
  checkUpdate();
}

async function checkUpdate(manual = false) {
  const u = await S.api.update_check();
  S.update = u && u.release ? u : null;
  if (S.update && S.update.stale) toast(`A newer BL4 Fix Setup is out (${S.update.version}): Update App`);
  else if (manual) toast(S.update ? "BL4 Fix Setup is up to date" : "Couldn't reach GitHub (offline?)", !S.update);
  render();
}

function updateSummary(u) {
  const parts = [];
  if (u.version && u.version !== u.current) parts.push(`BL4 Fix Setup ${u.current} -> ${u.version}`);
  if (u.new_mods.length) parts.push("New mods: " + u.new_mods.join(", "));
  if (u.newer_mods.length) parts.push("Updated: " + u.newer_mods.join(", "));
  if (u.sdk) parts.push("A newer Python SDK");
  return parts.join("\n");
}

async function updateApp(confirmed = false) {
  const u = S.update;
  if (S.busy || !u || !u.stale) return;
  if (!u.can_apply) { S.api.open_releases(); return; }
  const size = u.size ? ` (${Math.round(u.size / 1048576)} MB)` : "";
  if (!confirmed && !(await confirmDialog("Update BL4 Fix Setup?", `${updateSummary(u)}\n\nDownloads the new app${size}, replaces this one and restarts it. Your mods, friends and settings stay as they are.`, "Update and restart", false, { okCls: "" }))) return;
  startTask("selfupdate", "Updating BL4 Fix Setup");
  if (!(await S.api.update_apply())) endTask();
}

function getApi() {
  if (params.has("mock")) return Promise.resolve(mockApi());
  return new Promise((resolve) => {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.init) return resolve(window.pywebview.api);
    window.addEventListener("pywebviewready", () => resolve(window.pywebview.api), { once: true });
  });
}

/* ---------- the main menu (title screen panel) ---------- */

const NAV = [
  { id: "update", text: "Update App", big: true }, // shown only when a newer release is out
  { id: "install", text: "Install", big: true },
  { id: "launch", text: "Launch Game", big: true },
  { divider: true },
  { id: "loadout", text: "Loadout" },
  { id: "coop", text: "Co-op" },
  { id: "system", text: "System" },
  { id: "space", text: "Disk Space" },
  { id: "quit", text: "Quit" },
];

function buildMenu() {
  const widget = T.menuMain.firstElementChild;
  const list = $(".mm_entry_list", widget);
  list.innerHTML = "";
  S.nav = {};
  for (const item of NAV) {
    if (item.divider) {
      list.appendChild(el("div", "mm_section_divider"));
      continue;
    }
    const slot = el("div", item.big ? "mm_entry_item mm_main_btn_fontsize big_text_1" : "mm_entry_item_sub mm_sub_btn_fontsize med_text_1");
    const btn = make("mm");
    $(".mm_button_txt", btn).textContent = item.text;
    // The brackets next to Game Settings hold its icons; Install shows the pending changes there
    const info = $(".mm_game_settings_info", btn);
    if (item.id === "install" || item.id === "update") {
      $$(".mm_game_settings_icon", info).forEach((n) => n.remove());
      info.appendChild(el("p", "su_mm_info", ""));
      slot.classList.add("game_settings_enable", "su_mm_install");
      if (item.id === "update") slot.hidden = true;
    } else {
      info.remove();
    }
    $$(".pip", btn).forEach((n) => n.remove());
    slot.appendChild(btn);
    slot.addEventListener("mouseenter", () => { btn.classList.add("hover_effect"); slot.classList.add("hover_effect"); });
    slot.addEventListener("mouseleave", () => { btn.classList.remove("hover_effect"); slot.classList.remove("hover_effect"); });
    onClick(slot, () => navigate(item.id));
    list.appendChild(slot);
    S.nav[item.id] = { slot, btn };
  }
  // ECHO OS version line, bottom left
  const ver = $(".menu_echo_os_ver", widget);
  if (ver) ver.textContent = `//.ECHO-4 OS  BL4 FIX ${S.info.version}`;
  $("#menu_slot").appendChild(widget);
}

function navigate(id) {
  if (id === "update") return updateApp();
  if (id === "install") return install();
  if (id === "launch") return launch();
  if (id === "quit") return quit();
  showPage(id);
}

function showPage(id) {
  S.page = id;
  for (const [key, n] of Object.entries(S.nav)) {
    n.slot.classList.toggle("su_current", key === id);
    n.btn.classList.toggle("su_current", key === id);
  }
  render();
  $("#entries").scrollTop = 0;
}

/* ---------- rows ---------- */

function row(name, parts, details, opts = {}) {
  const wrap = make("entry");
  wrap.classList.add("su_entry_wrap");
  const entry = $(".menu_options_entry", wrap);
  $(".menu_options_entry_text", wrap).textContent = name;
  const comp = $(".menu_options_entry_component", wrap);
  for (const p of parts) if (p) comp.appendChild(p);
  if (opts.status) $(".list_entry_panel", wrap).appendChild(el("p", "su_status su_status_" + opts.status.kind, opts.status.text));
  entry.addEventListener("mouseenter", () => {
    entry.classList.add("hover_effect");
    if (details) showDetails(details);
    if (opts.onHover) opts.onHover();
  });
  entry.addEventListener("mouseleave", () => {
    entry.classList.remove("hover_effect");
    showDetails(pageDetails());
  });
  if (opts.onClick) onClick(entry, opts.onClick);
  $("#entries").appendChild(wrap);
  return wrap;
}

function title(text) {
  const t = make("title");
  t.classList.add("su_title_entry");
  const label = $(".cat_divider_title", t);
  if (label) label.textContent = text;
  $("#entries").appendChild(t);
}

function toggle(value, onSet, offText = "Off", onText = "On") {
  const t = make("toggle");
  const left = $(".toggle_button_left", t);
  const right = $(".toggle_button_right", t);
  const texts = $$(".toggle_button_text", t);
  texts[0].textContent = offText;
  texts[1].textContent = onText;
  left.classList.toggle("active", !value);
  right.classList.toggle("active", !!value);
  hoverable(left);
  hoverable(right);
  if (onSet) {
    onClick(left, () => value && onSet(false));
    onClick(right, () => !value && onSet(true));
  }
  return t;
}

function stepper(text, onStep) {
  const s = make("stepper");
  $(".stepper_value", s).textContent = text;
  $(".stepper_indicator_container", s)?.remove();
  const left = $(".stepper_button_left", s);
  const right = $(".stepper_button_right", s);
  hoverable(left);
  hoverable(right);
  onClick(left, () => onStep(-1));
  onClick(right, () => onStep(1));
  return s;
}

function textInput(value, placeholder, onInput, width) {
  const t = make("input");
  t.classList.add("su_input");
  if (width) t.style.width = width;
  const input = $("input", t);
  input.value = value;
  input.placeholder = placeholder;
  input.spellcheck = false;
  input.addEventListener("focus", () => t.classList.add("focused"));
  input.addEventListener("blur", () => t.classList.remove("focused"));
  input.addEventListener("input", () => { onInput(input.value, input); t.classList.toggle("filled", !!input.value); });
  input.addEventListener("click", (e) => e.stopPropagation());
  t.classList.toggle("filled", !!value);
  return t;
}

function valueText(text, cls = "") {
  return el("p", "su_value small_text_1 " + cls, text);
}

/* ---------- details pane ---------- */

function showDetails(d) {
  $("#d-title").textContent = d.title || "";
  $("#d-meta").textContent = d.meta || "";
  $("#d-meta").className = "su_details_meta small_text_2" + (d.metaCls ? " " + d.metaCls : "");
  $("#d-body").textContent = d.body || "";
  $("#log").hidden = !d.log;
  $(".su_details").classList.toggle("su_details_long", !!d.long);
}

function pageDetails() {
  switch (S.page) {
    case "coop":
      return { title: "Co-op", meta: "Direct IP  //  No lobbies", body: "Send your IP to a friend, add theirs, and join each other's games. In game, F6 opens the BL4 Direct panel to host and join." };
    case "system":
      return { title: "Activity", meta: "Systems check", body: S.log.length ? "" : "Nothing yet. Installs, repairs and firewall changes are logged here.", log: true };
    case "space":
      return { title: "Disk Space", meta: "Optional  //  Not part of Install", body: SPACE_WHY };
    default: {
      const { add, remove } = changes();
      const known = S.status && S.status.valid;
      const meta = !known ? "" : add.length || remove.length ? `${add.length} to install  //  ${remove.length} to remove` : `${S.selected.size} mods equipped`;
      return { title: "Loadout", meta, body: "Pick the mods you want, then Install. Run this app again any time to change them. Settings and keys for every mod live in game: F7, or MODS in the pause menu." };
    }
  }
}

/* ---------- pages ---------- */

const SECTIONS = [
  { title: "Core", test: (m) => m.required },
  { title: "BL4 Fix", test: (m) => !m.required && m.source !== "ported" },
  { title: "Ports of Yeti's mods (GPL-3.0)", test: (m) => m.source === "ported" },
];

function render() {
  const entries = $("#entries");
  const scroll = entries.scrollTop;
  entries.innerHTML = "";
  $("#page-title").textContent = { loadout: "Loadout", coop: "Co-op", system: "System", space: "Disk Space" }[S.page];
  if (S.page === "loadout") renderLoadout();
  else if (S.page === "coop") renderCoop();
  else if (S.page === "space") renderSpace();
  else renderSystem();
  entries.scrollTop = scroll;
  showDetails(pageDetails());
  renderLog();
  updateMenu();
}

function renderLoadout() {
  const known = !!(S.status && S.status.valid);
  const installed = new Set(known ? S.status.installed : []);
  const dev = new Set(known ? S.status.dev_mods : []);
  $("#page-crumb").textContent = `${S.selected.size} of ${S.info.mods.length} equipped`;
  for (const sec of SECTIONS) {
    const mods = S.info.mods.filter(sec.test);
    if (!mods.length) continue;
    title(sec.title);
    for (const m of mods) {
      const on = S.selected.has(m.id);
      let status = null;
      if (m.required) status = { kind: "core", text: "Always on" };
      else if (known && on && !installed.has(m.id)) status = { kind: "add", text: "+ Install" };
      else if (known && !on && installed.has(m.id)) status = { kind: "remove", text: "- Remove" };
      else if (installed.has(m.id)) status = { kind: "on", text: "Installed" };
      const meta = [m.key ? `Key: ${m.key}` : "", installed.has(m.id) ? "Installed" : "Not installed", dev.has(m.id) ? "Loads from a dev folder" : ""].filter(Boolean).join("  //  ");
      const t = m.required ? toggle(true, null) : toggle(on, (v) => setMod(m.id, v));
      if (m.required) t.classList.add("su_locked");
      row(m.name, [t], { title: m.name, meta, body: m.blurb }, {
        status,
        onClick: m.required ? null : () => setMod(m.id, !S.selected.has(m.id)),
      });
    }
    if (sec.title === "Core") firewallRow();
  }
  title("Quick picks");
  const picks = el("div", "su_btn_row");
  picks.append(listButton("Everything", () => preset("all")), listButton("Core only", () => preset("none")));
  row("Select", [picks], { title: "Quick picks", body: "Everything: every mod (what a first install picks). Core only: just BL4 Direct Connect and the Mod Menu." });
}

const FIREWALL_WHY =
  "Strongly recommended if you host. Steam's own rule lets anyone on the internet reach the game's co-op port while you host: " +
  "bots that scan for open ports, or anyone who learns your IP. If the game's network code ever has a security hole, " +
  "this rule keeps everyone but your friends from reaching it at all.";

// Optional (off on a new install): some players don't want a setup app touching their firewall
function firewallRow() {
  const f = S.friends;
  const changed = f.firewall !== S.firewallSaved;
  let status = null;
  if (changed) status = { kind: f.firewall ? "add" : "remove", text: f.firewall ? "+ Rule" : "- Rule" };
  else if (!f.firewall) status = { kind: "warn", text: "Recommended" };
  row("Firewall rule for hosting", [toggle(f.firewall, setFirewall)], {
    title: "Firewall rule for hosting", meta: f.firewall ? "Only friends can reach your hosted games" : "Off: recommended on",
    metaCls: f.firewall ? "su_ok" : "su_warn",
    body: FIREWALL_WHY + "\n\nOn: one Windows Firewall rule that blocks inbound UDP on your host port from everyone except this PC, the friends on your Co-op list and (if allowed) your local network. " +
      "It only ever blocks; it never opens anything. When the rule changes, Windows asks if \"Network Command Shell\" may make changes: " +
      "that's netsh, Windows' own firewall tool, and Show more details lists the rule.\n\nOff: BL4 Fix never touches your firewall (an old BL4 Direct rule is removed).",
  }, { status, onClick: () => setFirewall(!f.firewall) });
}

// Installed: applies at once (Windows asks for admin when the rule changes). Before the first
// install there's nothing to apply to yet: the choice goes in with Install
async function setFirewall(on) {
  if (S.busy) return;
  S.friends.firewall = on;
  if (!S.status || !S.status.valid || !S.status.installed.length) { render(); return; }
  render();
  startTask("firewall", on ? "Firewall rule on" : "Firewall rule off");
  if (!(await S.api.set_firewall(S.gameDir, on))) endTask();
}

function renderCoop() {
  const f = S.friends;
  $("#page-crumb").textContent = `${f.friends.length} friend${f.friends.length === 1 ? "" : "s"}`;
  title("You");
  row("Your public IP", [valueText(S.publicIp || "Looking up...", "su_ip"), listButton("Copy", () => copy(S.publicIp), "su_small")], {
    title: "Your public IP", meta: "Send this to friends who'll join you", body: "Friends type it in BL4 Direct Connect's Join box (F6 in game) to join the game you host.",
  });
  row("Port when you host", [stepper(String(f.host_port), (d) => { f.host_port = Math.max(1024, Math.min(65535, f.host_port + d)); markFriendsDirty(); })], {
    title: "Port when you host", meta: `UDP ${f.host_port}`, body: `Hosting over the internet: forward UDP ${f.host_port} on your router to ${S.info.lan_ip || "this PC"}. Joining someone needs no setup.`,
  });
  row("Allow local network / Tailscale", [toggle(f.allow_lan, (v) => { f.allow_lan = v; markFriendsDirty(); })], {
    title: "Allow local network / Tailscale", body: "Lets anyone on your local network or VPN join the games you host, besides the friends below.",
  });
  title("Friends");
  if (!f.friends.length) row("No friends yet", [valueText("Add one below", "su_dim")], { title: "Friends", body: "Add the friends you play with: their name, public IP and the port they host on." });
  f.friends.forEach((fr, i) => {
    row(fr.name, [valueText(`${fr.address}:${fr.port}`, "su_mono"), listButton("Copy IP", () => copy(fr.address), "su_small"), listButton("Remove", () => removeFriend(i), "su_small su_danger")], {
      title: fr.name, meta: `${fr.address} : ${fr.port}`, body: "Only friends on this list (plus your local network, if allowed) get through the firewall to the games you host.",
    });
  });
  const d = S.draft;
  row("Add a friend", [
    textInput(d.name, "Name", (v) => (d.name = v), "12rem"),
    textInput(d.ip, "IP address", (v) => (d.ip = v), "15rem"),
    textInput(d.port, String(S.info.default_port), (v, input) => { input.value = v.replace(/\D/g, "").slice(0, 5); d.port = input.value; }, "7.5rem"),
    listButton("Add", addFriend, "su_small su_tiny"),
  ], { title: "Add a friend", body: "Their name, public IPv4 address (like 203.0.113.5) and the port they host on (7777 unless they changed it)." });
  title("Firewall");
  firewallRow();
  const current = f.firewall_current && !S.friendsDirty;
  const saved = f.firewall ? (current ? "Firewall matches this list" : "Changes not saved yet") : current ? "Saved (no firewall rule)" : "Changes not saved yet";
  row(saved, [listButton(f.firewall ? "Save & lock firewall" : "Save", saveFriends, "su_wide")], {
    title: "Firewall", meta: current ? "Up to date" : "Not applied", metaCls: current ? "su_ok" : "su_warn",
    body: f.firewall
      ? "Saves the list and limits your hosted games' port to this PC, your friends and (if allowed) your local network. If the game is running, press Reload friends in BL4 Direct's panel."
      : "Saves the list. The firewall rule is off, so nothing is changed in Windows Firewall (an existing BL4 Direct rule is removed).",
  }, { status: { kind: current ? "on" : "warn", text: current ? "Saved" : "Unsaved" } });
}

function renderSystem() {
  const st = S.status;
  $("#page-crumb").textContent = st && st.valid ? (st.ready ? "All systems go" : "Needs a repair") : "Game not found";
  title("Borderlands 4");
  row("Game folder", [valueText(S.gameDir || "Not found", "su_mono su_path"), listButton("Browse", chooseFolder, "su_small"), listButton("Open", () => S.api.open_folder(S.gameDir), "su_small")], {
    title: "Game folder", body: S.gameDir || "Pick the Borderlands 4 install folder.",
  });
  title("Systems check");
  if (!st || !st.valid) {
    row("Borderlands 4", [valueText("Not found", "su_bad")], { title: "Borderlands 4", body: "Use Browse to pick its install folder." });
  } else {
    for (const c of st.checks) {
      const cls = c.key === "intro" ? "" : c.ok ? "su_ok" : "su_bad";
      row(c.label, [valueText(c.value, cls)], { title: c.label, meta: c.value, metaCls: cls, body: c.hint }, { status: c.key === "intro" ? null : { kind: c.ok ? "on" : "remove", text: c.ok ? "OK" : "Fix" } });
    }
    for (const c of st.conflicts) row("Conflict", [valueText(c, "su_bad")], { title: "Conflict", body: c });
    row("Compatibility code", [valueText(st.compat, "su_mono"), listButton("Copy", () => copy(st.compat), "su_small")], {
      title: "Compatibility code", body: "Friends with the same code can play together. Different codes usually mean a different game patch or mod version.",
    });
  }
  title("Safety");
  safeModeRow();
  row("Safety", [valueText("Open source  //  Saves read only", "su_dim")], {
    title: "Safety", meta: "What BL4 Fix does and doesn't do", body: SAFETY_TEXT, long: true,
  });
  title("Maintenance");
  const u = S.update;
  const appParts = [valueText(S.info.version, "su_mono")];
  if (u && u.stale) appParts.push(listButton(u.can_apply ? "Update" : "Download", () => updateApp(), "su_small"));
  else appParts.push(listButton("Check now", () => !S.busy && checkUpdate(true), "su_small"));
  row("BL4 Fix Setup", appParts, {
    title: "BL4 Fix Setup", meta: !u ? "Not checked (offline?)" : u.stale ? `Update available: ${u.version} (${u.release})` : `Up to date (${u.release})`,
    metaCls: u && u.stale ? "su_warn" : u ? "su_ok" : "",
    body: (u && u.stale ? updateSummary(u) + "\n\n" : "") + "The app checks GitHub for a newer release each time it starts. Update replaces this app in place and restarts it; your mods, friends and settings stay.",
  }, { status: u && u.stale ? { kind: "warn", text: "Update" } : null });
  row("Refresh", [listButton("Check again", () => !S.busy && refreshStatus(), "su_small")], { title: "Refresh", body: "Reads the install again." });
  row("Report a problem", [listButton("Make report", bugReport, "su_small" + (st && st.valid ? "" : " su_disabled"))], {
    title: "Report a problem", meta: "For a bug, an error or a crash",
    body: REPORT_WHY,
  });
  row("Uninstall", [listButton("Uninstall...", uninstall, "su_small su_danger")], {
    title: "Uninstall", meta: "Removes every mod BL4 Fix installed", metaCls: "su_bad",
    body: "Also removes the firewall rule. Your friends list and mod settings are kept.",
  });
}

/* ---------- safety + safe mode (core.set_safe_mode) ---------- */

const SAFETY_TEXT =
  "Open source: BL4 Fix's mods are GPL-3.0, at github.com/flameaway/bl4-fix.\n" +
  "Saves: only read, never written. The one exception is a restore you ask for in Save Backups, applied after the game closes, with your old saves kept aside.\n" +
  "Internet: GitHub for update checks and the downloads you press; api.ipify.org to show your public IP for co-op; Microsoft for the VC++ runtime when it's missing. Nothing else.\n" +
  "Bug reports: stay on your PC until you attach one yourself, with names, paths and IP addresses masked.\n" +
  "Other modders' repositories: their mods aren't checked by BL4 Fix and run with the game's rights. Add only repositories you trust.\n" +
  "Safe mode sets other modders' mods aside if one breaks the game.";

const SAFE_MODE_WHY =
  "For when a mod breaks the game: sets every mod that isn't BL4 Fix's aside (not deleted) until you turn this off. BL4 Fix's mods keep working.";

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function safeModeRow() {
  const s = S.safe;
  const known = !!(S.status && S.status.valid && s && "on" in s);
  const on = known && s.on;
  const aside = known ? s.aside.length : 0;
  const others = known ? s.others.length : 0;
  let meta = "", body = SAFE_MODE_WHY + "\n(They wait in sdk_mods\\.bl4_safe_mode. Close the game first.)";
  if (!known) meta = "Pick the game folder first";
  else if (on) {
    meta = `${plural(aside, "mod")} set aside`;
    if (aside) body += "\n\nSet aside: " + s.aside.join(", ");
    if (others) body += "\n\nAdded since (these load): " + s.others.join(", ") + ". Turn safe mode off and on again to set them aside too.";
  } else {
    meta = others ? `${plural(others, "mod")} by other modders installed` : "No other modders' mods installed";
    if (others) body += "\n\nWould set aside: " + s.others.join(", ");
  }
  if (known && s.left.length) body += "\n\nStill set aside, because sdk_mods has one with the same name again: " + s.left.join(", ");
  const t = toggle(on, known ? setSafeMode : null);
  if (!known) t.classList.add("su_locked");
  row("Safe mode", [t], { title: "Safe mode", meta, metaCls: on ? "su_warn" : "", body }, {
    status: on ? { kind: "warn", text: `${aside} aside` } : null,
    onClick: known ? () => setSafeMode(!on) : null,
  });
}

async function setSafeMode(on) {
  if (S.busy || !S.status || !S.status.valid) return;
  if (S.status.running) return toast("Close Borderlands 4 first", true);
  startTask("safe_mode", on ? "Safe mode on" : "Safe mode off");
  if (!(await S.api.set_safe_mode(S.gameDir, on))) endTask();
}

/* ---------- bug reports (the Mod Menu's report.py) ---------- */

const REPORT_WHY =
  "Saves one zip with what a bug report needs: mod and game versions, the errors our mods logged, the SDK logs, " +
  "the newest game crash, your installed mods and their settings. Your Windows user name, Steam names, friends, IPs and " +
  "account ids are masked, and your saves are never included. Nothing is sent: you attach the zip to a GitHub issue yourself.\n\n" +
  "In game the same report is in the Mod Menu (F7) > Report a problem. Use this one when the game won't start.";

async function bugReport() {
  if (S.busy) return;
  if (!S.status || !S.status.valid) return toast("Pick the Borderlands 4 folder first", true);
  startTask("report", "Making a bug report");
  if (!(await S.api.bug_report(S.gameDir))) endTask();
}

async function reportDone() {
  const r = await S.api.report_result();
  if (!r || !r.path) return;
  S.api.show_report();
  const body = `Saved as ${r.name}. Explorer shows it now.\n\n` +
    "Open a new issue on GitHub (the versions are filled in for you), say what happened and drag the zip into it.";
  if (await confirmDialog("Report ready", body, "Open GitHub issue", false, { cancelText: "Close", okCls: "", backdrop: false })) S.api.open_issue();
}

/* ---------- disk space (installer/slim.py) ---------- */

const SPACE_WHY =
  "Removes the voice languages you don't use and the old patch data the game never reads. Separate from Install: " +
  "nothing changes until you press Free space.\n\n" +
  "Undo: Verify integrity of game files in Steam. Scan again after a game update.\n\n" +
  "Idea and methods from bl4-slim by nanafy (MIT).";

function gb(n) {
  return n >= 1073741824 || !n ? `${(n / 1073741824).toFixed(2)} GB` : `${(n / 1048576).toFixed(1)} MB`;
}

function renderSpace() {
  const sp = S.space;
  const info = sp.info;
  const total = info ? info.voice_bytes + (sp.old ? info.old_bytes : 0) : 0;
  $("#page-crumb").textContent = info ? `${gb(info.on_disk)} on disk  //  ${gb(total)} to free` : "Not scanned yet";
  title("Borderlands 4");
  row("Size on disk", [valueText(info ? gb(info.on_disk) : "Not scanned", "su_mono"), listButton(info ? "Scan again" : "Scan", spaceScan, "su_small")], {
    title: "Scan", meta: "Changes nothing",
    body: "Reads the game's own pak and IoStore indexes (about 20 seconds) to find the voice languages installed and the old patch copies the game no longer reads.",
  });
  if (!info) return;
  title("Voice languages");
  for (const l of info.languages) {
    const keep = sp.keep.has(l.name);
    row(l.name, [valueText(gb(l.size), "su_mono"), toggle(keep, (v) => setKeep(l.name, v), "Remove", "Keep")], {
      title: l.name, meta: keep ? "Kept" : `Removing frees ${gb(l.size)}`, metaCls: keep ? "su_ok" : "su_warn",
      body: "Keep the language your game's voice setting is on (Options > Audio). Removing the one the game uses leaves the characters silent until you verify the game files in Steam.",
    }, { status: keep ? null : { kind: "remove", text: "- Remove" }, onClick: () => setKeep(l.name, !keep) });
  }
  title("Old patch data");
  const canOld = !!info.unit;
  row("Old patch copies", [valueText(canOld ? gb(info.old_bytes) : "Not on this drive", "su_mono"), canOld ? toggle(sp.old, setSpaceOld, "Keep", "Free") : null], {
    title: "Old patch copies", meta: canOld ? `${info.old_files} files` : "Needs NTFS",
    body: canOld
      ? "Byte ranges that only hold an older copy of something a later patch replaced. They're blanked in place: each file keeps its size and every byte the game reads (checked before and after), and Windows stops storing the blanked parts. In Explorer, \"Size on disk\" shows the real number."
      : "This drive can't free part of a file: it needs NTFS (and a few MB free for a test).",
  }, { status: canOld && sp.old && info.old_bytes ? { kind: "remove", text: "- Free" } : null, onClick: canOld ? () => setSpaceOld(!sp.old) : null });
  title("Free space");
  const running = !!(S.status && S.status.running);
  row(total ? `Frees ${gb(total)}` : "Nothing to free", [listButton("Free space...", spaceApply, "su_wide su_danger" + (total && !running ? "" : " su_disabled"))], {
    title: "Free space", meta: running ? "Close Borderlands 4 first" : total ? `About ${gb(info.on_disk - total)} left` : "", metaCls: running ? "su_warn" : "",
    body: "Deletes the voice languages marked Remove and blanks the old patch copies, if picked. Close the game and let Steam finish any update first.",
  });
  row("Undo", [valueText("Verify integrity in Steam", "su_dim")], {
    title: "Undo", body: "Steam > Borderlands 4 > Properties > Installed Files > Verify integrity of game files downloads everything removed here again.",
  });
}

async function spaceScan() {
  if (S.busy) return;
  if (!S.status || !S.status.valid) return toast("Pick the Borderlands 4 folder first", true);
  startTask("space_scan", "Reading the game files");
  if (!(await S.api.space_scan(S.gameDir))) endTask();
}

// Asks Python what the current choice frees; a newer request wins over a slower older one
async function spaceInfo(keep) {
  const seq = ++S.space.seq;
  const info = await S.api.space_info(keep, S.space.old);
  if (seq !== S.space.seq) return;
  S.space.info = info && info.languages ? info : null;
  if (S.space.info) S.space.keep = new Set(S.space.info.keep);
  if (S.page === "space") render();
}

function setKeep(name, on) {
  if (S.busy || !S.space.info) return;
  const keep = new Set(S.space.keep);
  if (on) keep.add(name);
  else if (keep.size <= 1) return toast("Keep at least one voice language", true);
  else keep.delete(name);
  S.space.keep = keep;
  render();
  spaceInfo([...keep]);
}

function setSpaceOld(on) {
  if (S.busy || !S.space.info) return;
  S.space.old = on;
  render();
  spaceInfo([...S.space.keep]);
}

async function spaceApply() {
  const sp = S.space;
  const info = sp.info;
  if (S.busy || !info) return;
  if (S.status && S.status.running) return toast("Close Borderlands 4 first", true);
  const drop = info.languages.filter((l) => !sp.keep.has(l.name));
  const old = sp.old && info.old_bytes;
  if (!drop.length && !old) return;
  const lines = [];
  if (drop.length) lines.push("Delete these voice languages:\n" + drop.map((l) => `  ${l.name}  (${gb(l.size)})`).join("\n"));
  if (old) lines.push(`Blank ${gb(info.old_bytes)} of old patch copies in ${info.old_files} files.`);
  lines.push(`Keeping: ${[...sp.keep].join(", ")}.\n\nUndo: Verify integrity of game files in Steam (downloads it all again).`);
  if (!(await confirmDialog("Free disk space?", lines.join("\n\n"), `Free ${gb(info.voice_bytes + (old || 0))}`))) return;
  startTask("space", "Freeing disk space");
  if (!(await S.api.space_apply(S.gameDir, [...sp.keep], !!sp.old))) { endTask(); toast("Scan again first", true); }
}

function renderLog() {
  const log = $("#log");
  log.innerHTML = "";
  for (const l of S.log.slice(-200)) log.appendChild(el("p", "su_log_line " + l.cls, l.text));
  log.scrollTop = log.scrollHeight;
}

/* ---------- state ---------- */

function setMod(id, on) {
  if (S.busy) return;
  if (on) S.selected.add(id);
  else S.selected.delete(id);
  render();
}

function preset(name) {
  if (S.busy) return;
  S.selected = new Set(S.info.mods.filter((m) => m.required || name === "all" || (name === "defaults" && m.default)).map((m) => m.id));
  render();
}

function changes() {
  if (!S.status || !S.status.valid) return { add: [], remove: [] };
  const installed = new Set(S.status.installed);
  return {
    add: S.info.mods.filter((m) => S.selected.has(m.id) && !installed.has(m.id)),
    remove: S.info.mods.filter((m) => !S.selected.has(m.id) && installed.has(m.id)),
  };
}

function updateMenu() {
  const n = S.nav && S.nav.install;
  if (!n) return;
  const st = S.status;
  let text = "Install", sub = "", disabled = false;
  if (S.busy) { text = S.task === "uninstall" ? "Removing" : "Working"; disabled = true; }
  else if (!st) { sub = "..."; disabled = true; }
  else if (!st.valid) { sub = "No game"; disabled = true; }
  else if (st.running) { sub = "Game running"; disabled = true; }
  else {
    const { add, remove } = changes();
    const fwChanged = S.friends.firewall !== S.firewallSaved;
    if (!st.installed.length) sub = `${S.selected.size} mods`;
    else if (add.length || remove.length) { text = "Apply"; sub = [add.length ? `+${add.length}` : "", remove.length ? `-${remove.length}` : ""].filter(Boolean).join(" "); }
    else if (fwChanged) { text = "Apply"; sub = "Firewall"; }
    else if (!st.ready) text = "Repair";
    else { text = "Reinstall"; sub = "Up to date"; }
  }
  $(".mm_button_txt", n.btn).textContent = text;
  const info = $(".su_mm_info", n.btn);
  info.textContent = sub;
  info.parentElement.classList.toggle("su_empty", !sub);
  n.slot.classList.toggle("su_disabled", disabled);

  const u = S.nav.update;
  u.slot.hidden = !(S.update && S.update.stale);
  if (S.update) {
    const info = $(".su_mm_info", u.btn);
    info.textContent = S.update.new_mods.length ? `${S.update.new_mods.length} new mod${S.update.new_mods.length === 1 ? "" : "s"}` : S.update.version;
  }
  u.slot.classList.toggle("su_disabled", S.busy);

  const l = S.nav.launch;
  const running = !!(st && st.valid && st.running);
  $(".mm_button_txt", l.btn).textContent = running ? "Game Running" : S.launching ? "Starting..." : "Launch Game";
  l.slot.classList.toggle("su_disabled", S.busy || !st || !st.valid || running || !!S.launching);
}

async function refreshStatus() {
  S.status = S.gameDir ? await S.api.status(S.gameDir) : { valid: false };
  if (S.status.valid) S.selected = new Set(S.status.chosen);
  S.safe = S.status.valid ? await S.api.safe_mode(S.gameDir) : null;
  render();
}

async function pollRunning() {
  if (document.hidden || !S.status || !S.status.valid || S.busy) return;
  const running = await S.api.running();
  if (running && S.launching) S.launching = false;
  if (running !== S.status.running) {
    S.status.running = running;
    render();
  }
}

async function chooseFolder() {
  if (S.busy) return;
  const picked = await S.api.browse();
  if (!picked) return;
  if (!(await S.api.is_game_dir(picked))) { toast("That's not a Borderlands 4 folder", true); return; }
  S.gameDir = picked;
  S.status = null;
  S.space.info = null;
  render();
  await refreshStatus();
  loadFriends();
}

/* ---------- tasks ---------- */

async function install() {
  if (S.busy || !S.status || !S.status.valid) return;
  if (S.status.running) { toast("Close Borderlands 4 first", true); return; }
  if (S.update && S.update.stale) {
    // This exe's mods are older than the release's (or it lacks new ones): update it first
    const first = await confirmDialog("Update the app first?", `A newer BL4 Fix Setup is out:\n${updateSummary(S.update)}\n\nThis app would install its older copies.`, "Update first", false,
      { cancelText: "Install anyway", okCls: "", backdrop: null });
    if (first === null) return;
    if (first) return updateApp(true);
  }
  const { remove } = changes();
  // A first install with the firewall rule off: recommend it once
  if (!S.friends.firewall && !S.status.installed.length) {
    const on = await confirmDialog("Turn on the firewall rule?", FIREWALL_WHY + "\n\nYou can change this any time on the Co-op page.", "Turn it on", false,
      { cancelText: "Install without", okCls: "", backdrop: null });
    if (on === null) return;
    if (on) { S.friends.firewall = true; S.friendsDirty = true; }
  }
  if (remove.length && !(await confirmDialog("Remove mods?", "These will be uninstalled (their settings are kept):\n\n" + remove.map((m) => m.name).join("\n"), "Remove and apply"))) return;
  startTask("install", "Installing");
  S.installFirewall = S.friends.firewall;
  if (!(await S.api.install(S.gameDir, [...S.selected], S.friends.firewall))) endTask();
}

async function uninstall() {
  if (S.busy || !S.status || !S.status.valid) return;
  const ok = await confirmDialog("Uninstall everything?", "Removes every mod BL4 Fix installed and the firewall rule. Your friends list and mod settings are kept.", "Uninstall", true);
  if (!ok) return;
  startTask("uninstall", "Uninstalling");
  if (!(await S.api.uninstall(S.gameDir, ok.removeSdk))) endTask();
}

async function launch() {
  if (S.busy || S.launching || !S.status || !S.status.valid || S.status.running) return;
  const { add, remove } = changes();
  if ((add.length || remove.length || !S.status.installed.length) &&
      !(await confirmDialog("Launch without installing?", "Your loadout has changes that aren't installed yet. Launch Borderlands 4 anyway?", "Launch anyway"))) return;
  const error = await S.api.launch_game(S.gameDir);
  if (error) { toast(error, true); return; }
  // Steam takes a while: hold the button until the game shows up (pollRunning), at most 90 s
  S.launching = true;
  setTimeout(() => { S.launching = false; updateMenu(); }, 90000);
  toast("Starting Borderlands 4...");
  updateMenu();
}

function quit() {
  if (S.busy) { toast("Hang on, still working...", true); return; }
  S.api.close();
}

function startTask(name, label) {
  S.busy = true;
  S.task = name;
  S.logCount = 0;
  addLog(label, "su_head");
  if (name !== "friends" && name !== "firewall" && name !== "safe_mode") {
    $("#working-title").textContent = label;
    $("#working-line").textContent = "Starting...";
    $("#pfill").style.width = "3%";
    $("#working").hidden = false;
  }
  updateMenu();
}

function endTask() {
  S.busy = false;
  S.task = null;
  $("#working").hidden = true;
  updateMenu();
}

// Called by webgui.Api
window.app = {
  onLog(msg) {
    addLog(msg, /^Error/.test(msg) ? "su_bad" : /^(Done|Installed|Removed|Saved)/.test(msg) ? "su_ok" : "");
    if (!S.busy) return;
    S.logCount++;
    const expect = S.task === "install" ? S.selected.size + 8 : 8;
    const step = /^\[(\d+)\/(\d+)\]/.exec(msg); // Disk Space: "[i/n] file"
    $("#pfill").style.width = Math.min(94, step ? 3 + (91 * step[1]) / step[2] : 3 + (91 * S.logCount) / expect) + "%";
    $("#working-line").textContent = msg;
  },
  async onTaskDone(name, error) {
    $("#pfill").style.width = "100%";
    await sleep(400);
    endTask();
    if (name === "selfupdate") {
      if (error) celebrate(false, error);
      else celebrate(true, "Restarting with the new version...", "Updated");
      return;
    }
    if (name === "safe_mode") {
      await refreshStatus();
      const s = S.safe || {};
      const n = (s.aside || []).length;
      if (error) toast("Safe mode: " + error, true);
      else if (s.on) toast(`Safe mode on: ${n} mod${n === 1 ? "" : "s"} by other modders set aside`);
      else toast("Safe mode off: other modders' mods are back" + ((s.left || []).length ? ` (${s.left.length} kept aside, see Activity)` : ""), !!(s.left || []).length);
      return;
    }
    if (name === "report") {
      if (error) celebrate(false, error);
      else await reportDone();
      return;
    }
    if (name === "space_scan") {
      if (error) toast("Scan: " + error, true);
      else await spaceInfo(null); // the suggested languages to keep
      return;
    }
    if (name === "space") {
      const freed = error ? 0 : await S.api.space_freed();
      // Python drops its scan once files change (scan again for new numbers), keeps it after a
      // refusal (game running, Steam busy)
      await spaceInfo([...S.space.keep]);
      if (error) celebrate(false, error);
      else celebrate(true, `Freed ${gb(freed || 0)}. Undo: Verify integrity in Steam.`, "Space Freed");
      return;
    }
    if (name === "firewall") {
      // Only the switch changed on disk: keep any unsaved edits to the list
      const saved = await S.api.load_friends(S.gameDir || "");
      if (error) S.friends.firewall = saved.firewall;
      S.firewallSaved = saved.firewall;
      if (!S.friendsDirty) S.friends = saved;
      toast(error ? "Firewall: " + error : saved.firewall ? "Firewall rule on: only friends can reach your hosted games" : "Firewall rule off", !!error);
      refreshStatus();
      return;
    }
    if (name === "friends") {
      toast(error ? "Firewall: " + error : S.friends.firewall ? "Friends saved, firewall locked" : "Friends saved, firewall left alone", !!error);
      S.friendsDirty = false;
      loadFriends();
    } else if (error) {
      celebrate(false, error);
    } else {
      if (name === "install") S.firewallSaved = S.installFirewall;
      celebrate(true, name === "uninstall" ? "BL4 Fix and its mods are gone." : "In game: F6 for co-op, F7 for mod settings.", name === "uninstall" ? "Uninstalled" : "Installed");
    }
    refreshStatus();
  },
};

function addLog(text, cls = "") {
  S.log.push({ text, cls });
  if (S.log.length > 400) S.log.shift();
  if (S.page === "system") renderLog();
}

/* ---------- co-op ---------- */

async function lookupIp() {
  S.publicIp = (await S.api.public_ip()) || "Offline?";
  if (S.page === "coop") render();
}

async function loadFriends() {
  S.friends = await S.api.load_friends(S.gameDir || "");
  S.firewallSaved = S.friends.firewall;
  S.friendsDirty = false;
  if (S.page === "coop") render();
}

function markFriendsDirty() {
  S.friendsDirty = true;
  render();
}

async function addFriend() {
  const d = S.draft;
  const name = d.name.trim();
  const ip = await S.api.normalize_ip(d.ip);
  const port = d.port ? parseInt(d.port, 10) : S.info.default_port;
  if (!name) return toast("Give your friend a name", true);
  if (!ip) return toast("Enter an IPv4 address like 203.0.113.5", true);
  if (!(port >= 1024 && port <= 65535)) return toast("Port must be 1024-65535", true);
  S.friends.friends = S.friends.friends.filter((f) => f.name.toLowerCase() !== name.toLowerCase());
  S.friends.friends.push({ name, address: ip, port });
  S.draft = { name: "", ip: "", port: "" };
  markFriendsDirty();
}

function removeFriend(i) {
  S.friends.friends.splice(i, 1);
  markFriendsDirty();
}

async function saveFriends() {
  if (S.busy) return;
  if (!S.status || !S.status.valid) return toast("Pick the Borderlands 4 folder first", true);
  const f = S.friends;
  startTask("friends", "Friends & firewall");
  if (!(await S.api.save_friends(S.gameDir, { host_port: f.host_port, allow_lan: f.allow_lan, firewall: f.firewall, friends: f.friends }))) endTask();
}

/* ---------- popups ---------- */

// opts: cancelText, okCls (the OK button's class, default su_danger), backdrop (what a click
// outside the dialog resolves to, default false)
function confirmDialog(titleText, body, okText, uninstallOptions = false, opts = {}) {
  return new Promise((resolve) => {
    $("#dialog-title").textContent = titleText;
    const b = $("#dialog-body");
    b.innerHTML = "";
    b.appendChild(el("p", "su_dialog_text", body));
    let removeSdk = false;
    if (uninstallOptions) {
      const opt = el("div", "su_dialog_opt");
      const redraw = () => {
        opt.innerHTML = "";
        opt.append(el("p", "su_dialog_opt_label", "Also remove the Python SDK (leave this off if you use other SDK mods)"), toggle(removeSdk, (v) => { removeSdk = v; redraw(); }, "Keep", "Remove"));
      };
      redraw();
      b.appendChild(opt);
    }
    const btns = $("#dialog-btns");
    btns.innerHTML = "";
    const done = (ok) => { $("#dialog").hidden = true; resolve(ok ? { removeSdk } : false); };
    btns.append(listButton(opts.cancelText || "Cancel", () => done(false)), listButton(okText, () => done(true), opts.okCls ?? "su_danger"));
    $("#dialog").hidden = false;
    $("#dialog").onclick = (e) => {
      if (e.target.id !== "dialog") return;
      if ("backdrop" in opts) { $("#dialog").hidden = true; resolve(opts.backdrop); } else done(false);
    };
  });
}

function celebrate(ok, sub, word) {
  const c = $("#celebrate");
  const close = () => { c.classList.add("su_out"); setTimeout(() => (c.hidden = true), 400); c.onclick = null; };
  c.classList.toggle("su_fail", !ok);
  $("#cel-word").textContent = ok ? word : "Failed";
  $("#cel-sub").textContent = sub;
  const btns = $("#cel-btns");
  btns.innerHTML = "";
  if (ok && word === "Installed") btns.append(listButton("Launch Borderlands 4", () => { close(); launch(); }, "su_wide"), listButton("Close", () => close()));
  c.hidden = false;
  c.classList.remove("su_out");
  c.onclick = close;
  // With the Launch button up, stay until clicked
  if (!params.has("still") && !btns.children.length) setTimeout(close, ok ? 3800 : 8000);
}

function toast(text, bad = false) {
  const t = el("div", "su_toast" + (bad ? " su_toast_bad" : ""), text);
  $("#toasts").appendChild(t);
  setTimeout(() => { t.classList.add("su_out"); setTimeout(() => t.remove(), 300); }, 2600);
}

async function copy(text) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  toast("Copied " + text);
}

/* ---------- wiring ---------- */

function wire() {
  // Windows drags the window (webgui.Api.drag), not pywebview's drag region
  $("#tb-drag").addEventListener("mousedown", (e) => { if (e.button === 0) S.api.drag(); });
  $("#btn-min").onclick = () => S.api.minimize();
  $("#btn-close").onclick = quit;
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#dialog").hidden) $("#dialog").click();
    if (e.key === "F5" || (e.ctrlKey && e.key.toLowerCase() === "r")) e.preventDefault();
  });
  document.addEventListener("contextmenu", (e) => { if (!e.target.closest("input")) e.preventDefault(); });
}

/* ---------- mock (browser preview) ---------- */

function mockApi() {
  const mods = [
    ["bl4_direct", "BL4 Direct Connect", "Direct IP co-op (F6 in game).", "mods", 1, 0, "F6"],
    ["bl4_mod_menu", "Mod Menu", "Settings for every mod: MODS in the pause menu or F7.", "mods", 1, 0, "F7"],
    ["bl4_item_cards", "Detailed Item Cards", "Class mod skills with numbers, firmware, drop sources and Big Encore travel on item cards.", "mods", 0, 1, ""],
    ["bl4_skip_intro", "Skip Intro", "No 2K / Gearbox logo movies at launch.", "mods", 0, 0, ""],
    ["bl4_instant_tabs", "Instant Menu Tabs", "The ECHO menu opens and switches pages at once.", "mods", 0, 0, ""],
    ["bl4_echo_anywhere", "ECHO Anywhere", "Open the ECHO menu when the game refuses (falling, ...).", "mods", 0, 0, ""],
    ["bl4_respec", "Easy Respec", "Right-click a skill to take a point back.", "mods", 0, 0, ""],
    ["bl4_builds", "Build Presets", "Five build slots in the inventory: gear, skills and specs.", "mods", 0, 0, ""],
    ["bl4_reroll", "Reroll", "Reroll class mod and enhancement stats, or whole legendaries and pearls, for Eridium.", "mods", 0, 0, "T / G"],
    ["bl4_weapondex", "Kairosdex", "A dex of the bosses you've beaten and their legendaries (F8).", "mods", 0, 0, "F8"],
    ["bl4_damage_meter", "Carnage Meter", "Every player's damage and top damage sources (F9), and a pop-up when a boss dies.", "mods", 0, 0, "F9"],
    ["bl4_uvh_teleport", "UVH Boss Teleport", "Hover a UVH boss challenge and press T to go there.", "mods", 0, 0, "T"],
    ["bl4_minimap", "Minimap", "A minimap with the ECHO map's art, fog, icons and route (N).", "mods", 0, 0, "N"],
    ["bl4_third_person", "Third Person", "H: over-the-shoulder camera with aiming.", "mods", 0, 0, "H"],
    ["bl4_vehicles_anywhere", "Vehicles Anywhere", "Summon and drive where vehicles aren't allowed.", "mods", 0, 0, ""],
    ["bl4_backpack_size", "Backpack Size", "Set the backpack, bank and Lost Loot limits.", "mods", 0, 0, ""],
    ["bl4_shoot_open", "Shoot to Open", "Shoot a container or chest to open it.", "mods", 0, 0, ""],
    ["bl4_less_fog", "Less Fog", "Thin out the haze over the world.", "mods", 0, 0, ""],
    ["bl4_tidy_pause", "Tidy Pause Menu", "Move the pause menu's News panel to the right, or hide it.", "mods", 0, 0, ""],
    ["bl4_perf", "Performance Fixes", "Fewer hitches: no DLSS reload after menus, cheaper mod hooks.", "mods", 0, 0, ""],
    ["bl4_dialog_skipper", "Dialog Skipper", "Skip voice lines by key or automatically.", "ported", 0, 0, ""],
    ["bl4_grapple_anywhere", "Grapple Anywhere", "Grapple to any ledge you aim at.", "ported", 0, 0, ""],
    ["bl4_ground_loot", "Ground Loot Helpers", "Bring the loot lying around to you, or clear it.", "ported", 0, 0, ""],
    ["bl4_slam_tweaks", "Slam Tweaks", "A fast Pre-Sequel style ground slam.", "ported", 0, 0, ""],
    ["bl4_encore_tweaks", "Encore Tweaks", "Big Encore machines refill health, ammo and skills.", "ported", 0, 0, ""],
  ].map(([id, name, blurb, source, required, , key]) => ({ id, name, blurb, source, required: !!required, default: true, key }));
  const installed = ["bl4_direct", "bl4_mod_menu", "bl4_item_cards", "bl4_minimap", "bl4_third_person", "bl4_perf", "bl4_dialog_skipper"];
  const fresh = params.get("mock") === "fresh";
  const demo = params.get("demo");
  if (demo === "celebrate") setTimeout(() => celebrate(true, "In game: F6 for co-op, F7 for mod settings.", "Installed"), 600);
  if (demo === "dialog") setTimeout(() => uninstall(), 600);
  if (demo === "working") setTimeout(() => { startTask("install", "Installing"); app.onLog("Installing Python SDK 0.3 (was: none)..."); app.onLog("Installed bl4_minimap."); }, 600);
  if (demo === "hover") setTimeout(() => { $$(".menu_options_entry")[3].classList.add("hover_effect"); S.nav.coop.btn.classList.add("hover_effect"); S.nav.coop.slot.classList.add("hover_effect"); $$(".su_btn")[0]?.classList.add("hover_effect"); }, 600);
  if (demo === "report") setTimeout(() => { showPage("system"); bugReport(); }, 600);
  // &demo=safety / safemode: the System page with that row hovered (its details pane showing)
  if (demo === "safety" || demo === "safemode") {
    const name = demo === "safety" ? "Safety" : "Safe mode";
    const hover = (tries) => {
      if (S.info && S.page !== "system") showPage("system");
      const entry = $$(".menu_options_entry").find((e) => $(".menu_options_entry_text", e)?.textContent === name);
      if (entry && S.safe) entry.dispatchEvent(new MouseEvent("mouseenter"));
      else if (tries) setTimeout(() => hover(tries - 1), 200);
    };
    setTimeout(() => hover(30), 600);
  }
  if (demo === "space") setTimeout(() => spaceScan().then(() => sleep(3000)).then(() => setKeep("German", true)).then(() => sleep(300)).then(() => { if (params.has("bottom")) $("#entries").scrollTop = 99999; }), 600);
  const ok = (key, label, value, hint, good = true) => ({ key, label, ok: good, value, hint });
  return {
    init: async () => ({ version: "0.5.0", sdk_version: "0.3", icon: "", game_dir: "C:\\Program Files (x86)\\Steam\\steamapps\\common\\Borderlands 4", default_port: 7777, lan_ip: "192.168.1.42", mods }),
    status: async () => {
      await sleep(200);
      return {
        valid: true, ready: !fresh, running: params.has("running"), installed: fresh ? [] : installed,
        chosen: fresh ? mods.map((m) => m.id) : [...installed, "bl4_reroll"].filter((x) => x !== "bl4_perf"),
        dev_mods: [], conflicts: [], compat: "BL4-5554-8f3a2c-0.5.0",
        checks: [
          ok("vc", "VC++ Runtime", "14.44", "needs 14.44+ (installed for you)"),
          ok("sdk", "Python SDK", fresh ? "missing" : "0.3", "needs 0.3", !fresh),
          ok("mod", "BL4 Direct Connect", fresh ? "missing" : "0.5.0", "this app: 0.5.0", !fresh),
          ok("fw", "Firewall", "locked", "UDP 7777: this PC, LAN, 2 friends"),
          ok("intro", "Skip Intro", "off", ""),
        ],
      };
    },
    running: async () => params.has("running"),
    browse: async () => null,
    is_game_dir: async () => true,
    open_folder: async () => {},
    public_ip: async () => { await sleep(300); return "203.0.113.57"; },
    load_friends: async () => ({ host_port: 7777, allow_lan: true, firewall: !params.has("nofw"), firewall_current: true, friends: [
      { name: "Moxxi", address: "198.51.100.23", port: 7777 }, { name: "Zane", address: "203.0.113.9", port: 7778 }, { name: "Amara", address: "192.0.2.144", port: 7777 }] }),
    normalize_ip: async (t) => (/^\d+\.\d+\.\d+\.\d+$/.test(t.trim()) ? t.trim() : ""),
    install: async () => {
      (async () => {
        const lines = ["Python SDK 0.3 already installed.", ...[...S.selected].map((m) => `Installed ${m}.`), "Firewall: UDP 7777 only accepts this PC, your local network/VPN", "Done. In game: F6 for the BL4 Direct panel, F7 for mod settings."];
        for (const l of lines) { await sleep(150); app.onLog(l); }
        app.onTaskDone("install", null);
      })();
      return true;
    },
    uninstall: async () => { setTimeout(() => app.onTaskDone("uninstall", null), 800); return true; },
    save_friends: async () => { setTimeout(() => app.onTaskDone("friends", null), 600); return true; },
    launch_game: async () => "",
    update_check: async () => (params.has("update")
      ? { stale: true, release: "v2026.11.01", version: "0.5.3", current: "0.5.2", new_mods: ["Instant Fast Travel"], newer_mods: ["Minimap", "Reroll"], sdk: false, size: 104000000, can_apply: true }
      : { stale: false, release: "v2026.10.27", version: "0.5.2", current: "0.5.2", new_mods: [], newer_mods: [], sdk: false, size: 0, can_apply: true }),
    update_apply: async () => { setTimeout(() => app.onTaskDone("selfupdate", null), 800); return true; },
    open_releases: async () => {},
    set_firewall: async () => { setTimeout(() => app.onTaskDone("firewall", null), 600); return true; },
    space_scan: async () => {
      (async () => { for (const l of ["Reading 1080 game files...", "Working out the old patch data...", "Done."]) { await sleep(300); app.onLog(l); } app.onTaskDone("space_scan", null); })();
      return true;
    },
    space_info: async (keep, old) => {
      const G = 1073741824;
      const langs = [["English(US)", 7.85], ["French(France)", 8.47], ["German", 8.77], ["Italian", 8.7], ["Spanish(Spain)", 9.46], ["Japanese", 8.63], ["Korean", 8.57], ["Chinese(Simplified)", 8.38]]
        .map(([name, s]) => ({ name, size: s * G }));
      keep = keep || ["English(US)"];
      const drop = langs.filter((l) => !keep.includes(l.name));
      return { on_disk: 189.78 * G, languages: langs, keep, old, voice_bytes: drop.reduce((a, l) => a + l.size, 0), voice_files: drop.length * 72,
        old_bytes: old ? (71.5 + (8 - drop.length - 1) * 1.4) * G : 0, old_files: 209, unit: params.has("fat") ? 0 : 65536 };
    },
    space_apply: async () => {
      (async () => { for (let i = 1; i <= 6; i++) { await sleep(250); app.onLog(`[${i}/6] pakchunk0-Windows_${i}_P.ucas`); } app.onTaskDone("space", null); })();
      return true;
    },
    space_freed: async () => 132.48 * 1073741824,
    bug_report: async () => {
      (async () => { for (const l of ["Collecting versions, errors, logs and the newest crash...", "Done. Report saved: C:\\...\\sdk_mods\\.bug_reports\\bl4fix-report-20261029-181203.zip"]) { await sleep(300); app.onLog(l); } app.onTaskDone("report", null); })();
      return true;
    },
    report_result: async () => ({ path: "C:\\...\\bl4fix-report-20261029-181203.zip", name: "bl4fix-report-20261029-181203.zip", url: "https://github.com/flameaway/bl4-fix/issues/new?template=bug.yml" }),
    show_report: async () => {},
    open_issue: async () => {},
    safe_mode: async () => (params.has("safe")
      ? { on: true, aside: ["PersistentLoot.sdkmod", "better_hud"], missing: [], left: [], others: [], time: 1793000000 }
      : { on: false, aside: [], missing: [], left: [], others: ["PersistentLoot.sdkmod", "better_hud"], time: 0 }),
    set_safe_mode: async () => { setTimeout(() => { app.onLog("Safe mode on: 2 mods by other modders set aside (2 now). BL4 Fix's mods keep working."); app.onTaskDone("safe_mode", null); }, 400); return true; },
    minimize: async () => {}, close: async () => {}, drag: async () => {},
  };
}

boot().catch((e) => {
  document.body.insertAdjacentHTML("beforeend", `<pre style="position:fixed;left:10px;bottom:10px;color:#f66;z-index:99999;font-size:12px">${String((e && e.stack) || e)}</pre>`);
});
