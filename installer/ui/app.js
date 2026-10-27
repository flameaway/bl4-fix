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
  friends: { host_port: 7777, allow_lan: true, friends: [], firewall_current: true },
  friendsDirty: false,
  publicIp: "",
  draft: { name: "", ip: "", port: "" },
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
  claptrap.start();
  setInterval(pollRunning, 3000);
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
  { id: "install", text: "Install", big: true },
  { id: "launch", text: "Launch Game", big: true },
  { divider: true },
  { id: "loadout", text: "Loadout" },
  { id: "coop", text: "Co-op" },
  { id: "system", text: "System" },
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
    if (item.id === "install") {
      $$(".mm_game_settings_icon", info).forEach((n) => n.remove());
      info.appendChild(el("p", "su_mm_info", ""));
      slot.classList.add("game_settings_enable", "su_mm_install");
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
  claptrap.react("page_" + id);
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
}

function pageDetails() {
  switch (S.page) {
    case "coop":
      return { title: "Co-op", meta: "Direct IP  //  No lobbies", body: "Send your IP to a friend, add theirs, and join each other's games. In game, F6 opens the BL4 Direct panel to host and join." };
    case "system":
      return { title: "Activity", meta: "Systems check", body: S.log.length ? "" : "Nothing yet. Installs, repairs and firewall changes are logged here.", log: true };
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
  $("#page-title").textContent = { loadout: "Loadout", coop: "Co-op", system: "System" }[S.page];
  if (S.page === "loadout") renderLoadout();
  else if (S.page === "coop") renderCoop();
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
        onHover: () => claptrap.hoverMod(m),
        onClick: m.required ? null : () => setMod(m.id, !S.selected.has(m.id)),
      });
    }
  }
  title("Quick picks");
  const picks = el("div", "su_btn_row");
  picks.append(listButton("Everything", () => preset("all")), listButton("Core only", () => preset("none")));
  row("Select", [picks], { title: "Quick picks", body: "Everything: every mod (what a first install picks). Core only: just BL4 Direct Connect and the Mod Menu." });
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
  const current = f.firewall_current && !S.friendsDirty;
  row(current ? "Firewall matches this list" : "Changes not saved yet", [listButton("Save & lock firewall", saveFriends, "su_wide")], {
    title: "Firewall", meta: current ? "Up to date" : "Not applied", metaCls: current ? "su_ok" : "su_warn",
    body: "Saves the list and limits your hosted games' port to this PC, your friends and (if allowed) your local network. If the game is running, press Reload friends in BL4 Direct's panel.",
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
  title("Maintenance");
  row("Refresh", [listButton("Check again", () => !S.busy && refreshStatus(), "su_small")], { title: "Refresh", body: "Reads the install again." });
  row("Uninstall", [listButton("Uninstall...", uninstall, "su_small su_danger")], {
    title: "Uninstall", meta: "Removes every mod BL4 Fix installed", metaCls: "su_bad",
    body: "Also removes the firewall rule. Your friends list and mod settings are kept.",
  });
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
  claptrap.react(on ? "mod_on" : "mod_off");
  render();
}

function preset(name) {
  if (S.busy) return;
  S.selected = new Set(S.info.mods.filter((m) => m.required || name === "all" || (name === "defaults" && m.default)).map((m) => m.id));
  claptrap.react("preset_" + name);
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
    if (!st.installed.length) sub = `${S.selected.size} mods`;
    else if (add.length || remove.length) { text = "Apply"; sub = [add.length ? `+${add.length}` : "", remove.length ? `-${remove.length}` : ""].filter(Boolean).join(" "); }
    else if (!st.ready) text = "Repair";
    else { text = "Reinstall"; sub = "Up to date"; }
  }
  $(".mm_button_txt", n.btn).textContent = text;
  const info = $(".su_mm_info", n.btn);
  info.textContent = sub;
  info.parentElement.classList.toggle("su_empty", !sub);
  n.slot.classList.toggle("su_disabled", disabled);

  const l = S.nav.launch;
  const running = !!(st && st.valid && st.running);
  $(".mm_button_txt", l.btn).textContent = running ? "Game Running" : S.launching ? "Starting..." : "Launch Game";
  l.slot.classList.toggle("su_disabled", S.busy || !st || !st.valid || running || !!S.launching);
}

async function refreshStatus() {
  S.status = S.gameDir ? await S.api.status(S.gameDir) : { valid: false };
  if (S.status.valid) S.selected = new Set(S.status.chosen);
  render();
}

async function pollRunning() {
  if (document.hidden || !S.status || !S.status.valid || S.busy) return;
  const running = await S.api.running();
  if (running && S.launching) S.launching = false;
  if (running !== S.status.running) {
    S.status.running = running;
    if (running) claptrap.react("running");
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
  render();
  await refreshStatus();
  loadFriends();
}

/* ---------- tasks ---------- */

async function install() {
  if (S.busy || !S.status || !S.status.valid) return;
  if (S.status.running) { claptrap.react("running"); toast("Close Borderlands 4 first", true); return; }
  const { remove } = changes();
  if (remove.length && !(await confirmDialog("Remove mods?", "These will be uninstalled (their settings are kept):\n\n" + remove.map((m) => m.name).join("\n"), "Remove and apply"))) return;
  startTask("install", "Installing");
  if (!(await S.api.install(S.gameDir, [...S.selected]))) endTask();
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
  claptrap.react("launch");
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
  if (name !== "friends") {
    $("#working-title").textContent = label;
    $("#working-line").textContent = "Starting...";
    $("#pfill").style.width = "3%";
    $("#working").hidden = false;
    claptrap.react("working");
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
    $("#pfill").style.width = Math.min(94, 3 + (91 * S.logCount) / expect) + "%";
    $("#working-line").textContent = msg;
  },
  async onTaskDone(name, error) {
    $("#pfill").style.width = "100%";
    await sleep(400);
    endTask();
    if (name === "friends") {
      toast(error ? "Firewall: " + error : "Friends saved, firewall locked", !!error);
      S.friendsDirty = false;
      loadFriends();
    } else if (error) {
      claptrap.react("failed");
      celebrate(false, error);
    } else {
      claptrap.react(name === "uninstall" ? "uninstalled" : "installed");
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
  claptrap.react("friend");
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
  if (!(await S.api.save_friends(S.gameDir, { host_port: f.host_port, allow_lan: f.allow_lan, friends: f.friends }))) endTask();
}

/* ---------- popups ---------- */

function confirmDialog(titleText, body, okText, uninstallOptions = false) {
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
    btns.append(listButton("Cancel", () => done(false)), listButton(okText, () => done(true), "su_danger"));
    $("#dialog").hidden = false;
    $("#dialog").onclick = (e) => { if (e.target.id === "dialog") done(false); };
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

/* ---------- Claptrap ---------- */

// His lines are ours, written for this app
const LINES = {
  hello: ["Hey there, minion! Er, I mean, valued Vault Hunter!", "Welcome to the ECHO-4 mod station! I'm your personal installation assistant!", "Ooh, a visitor! Let's put some mods on you!"],
  idle: ["Is it just me, or is this the best installer you've ever seen? It's me. I made it. Mostly.", "I could install these mods with my eye closed. I only have one eye. It's always open.", "Psst. You can click me. I won't bite. I don't have a mouth.", "Fun fact: I am ninety-eight percent installer, two percent dance machine.", "Take your time! I'll just stand here. Rolling in place. Forever.", "Did you know stairs are my natural enemy? Good thing installers don't have stairs."],
  poke: ["Hey! That tickles my processors!", "Ow! My chassis!", "Woohoo! Do it again!", "Minion! Personal space!", "Boop!"],
  page_loadout: ["Pick your gear! Everything looks better with mods. Especially me."],
  page_coop: ["Friends! I have so many friends. They're all imaginary, but they count!", "Co-op! Finally, someone to share loot with. And by share, I mean give to me."],
  page_system: ["Running diagnostics... Everything's fine! Probably! Don't look too closely."],
  mod_on: ["Equipped!", "Ooh, good choice!", "Locked and loaded!", "Adding it to the pile!"],
  mod_off: ["Aww. Fine. Be that way.", "Unequipped. I'll miss that one.", "Into the bin it goes!"],
  preset_all: ["EVERYTHING? Now we're talking! Maximum mods!"],
  preset_none: ["Core only? Minimalist. I respect that. I don't, but I said it."],
  working: ["Installing! Don't turn off your computer! Or me!", "Beep boop, copying files... I'm doing a little dance while I wait.", "This is the part where I look busy!"],
  installed: ["Ta-daaa! All installed! Now go shoot something!", "Done! Launch the game and press F7. You're welcome!"],
  uninstalled: ["Everything's gone. Just like my friends. Sniff."],
  failed: ["Uh oh. That wasn't supposed to happen. Check the log on the System page!"],
  running: ["Whoa, the game's running! Close Borderlands 4 first, I can't swap parts while it's moving!"],
  launch: ["To Kairos! Go get 'em, Vault Hunter! I'll guard the installer!", "Launching! Don't forget to press F7 when you get there!", "Off you go! Bring me back something shiny!"],
  friend: ["A new friend! Hi, friend! Wanna be best friends?"],
};
const MOD_LINES = {
  bl4_direct: "Direct IP co-op! No lobbies, no waiting. Just you, your friends, and me.",
  bl4_mod_menu: "The Mod Menu! F7 in game. That's where all the knobs and buttons live.",
  bl4_item_cards: "Detailed item cards! Every number, every skill, every shiny detail!",
  bl4_skip_intro: "Skip the logos and get straight to the shooting. Efficient!",
  bl4_minimap: "A minimap! Now you'll never get lost. Unlike me. I'm always lost.",
  bl4_third_person: "Third person! Finally, you can admire your own outfit.",
  bl4_weapondex: "The Kairosdex! Gotta loot 'em all!",
  bl4_reroll: "Reroll your stats for Eridium! It's like gambling, but you always win. Sometimes.",
  bl4_respec: "Right-click a skill to take a point back. If only life had that.",
  bl4_vehicles_anywhere: "Vehicles anywhere! Even indoors! Especially indoors!",
  bl4_backpack_size: "A bigger backpack! More room for loot. And snacks. Mostly loot.",
  bl4_shoot_open: "Shoot a chest to open it! Violence solves everything!",
  bl4_less_fog: "Less fog! Now you can see all the things trying to kill you.",
  bl4_perf: "Performance fixes! Smoother frames, fewer hitches. I feel faster already!",
  bl4_builds: "Build presets! Swap your whole loadout in a click.",
  bl4_uvh_teleport: "Hover a boss challenge, press T, and zoom! Straight to the action!",
  bl4_instant_tabs: "Instant menu tabs! No more waiting. I hate waiting. I'm waiting right now.",
  bl4_echo_anywhere: "Open your ECHO even when the game says no. Rebel!",
  bl4_dialog_skipper: "Skip voice lines?! Not mine, I hope! Mine are the best ones!",
  bl4_grapple_anywhere: "Grapple anywhere! Up, up and away!",
  bl4_ground_loot: "Ground loot helpers! Vacuum up everything on the floor. Including me, probably.",
  bl4_slam_tweaks: "Ground slam! Feel the impact! Feel the POWER!",
  bl4_encore_tweaks: "Big Encore machines refill you. Like a vending machine, but for being alive!",
};
const MOVES = ["ct_roll", "ct_wave", "ct_spin", "ct_peek", "ct_hop", "ct_boing", "ct_dance", "ct_sad"];

const claptrap = {
  el: null,
  lastSpoke: 0,
  pick(list) { return list[(Math.random() * list.length) | 0]; },
  start() {
    this.el = $("#claptrap");
    this.el.classList.add("ct_in");
    onClick(this.el, () => { this.act("ct_boing", 900); this.say(this.pick(LINES.poke), true); });
    setTimeout(() => this.say(this.pick(LINES.hello), true), 900);
    this.schedule();
  },
  schedule() {
    clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      if (!S.busy && !document.hidden) {
        this.act(this.pick(["ct_roll", "ct_wave", "ct_spin", "ct_peek", "ct_hop"]));
        if (Math.random() < 0.45 && performance.now() - this.lastSpoke > 15000) this.say(this.pick(LINES.idle));
      }
      this.schedule();
    }, 7000 + Math.random() * 9000);
  },
  act(cls, ms = 2400) {
    if (!this.el || document.body.classList.contains("still")) return;
    this.el.classList.remove(...MOVES);
    void this.el.offsetWidth;
    this.el.classList.add(cls);
    clearTimeout(this.actTimer);
    this.actTimer = setTimeout(() => this.el.classList.remove(cls), ms);
  },
  say(text, force = false) {
    const now = performance.now();
    if (!force && now - this.lastSpoke < 3500) return;
    this.lastSpoke = now;
    const subs = $("#subs");
    $("#sub-text").textContent = text;
    subs.classList.remove("su_show");
    void subs.offsetWidth;
    subs.classList.add("su_show");
    if (this.el) this.el.classList.add("ct_talk");
    clearTimeout(this.subTimer);
    this.subTimer = setTimeout(() => { subs.classList.remove("su_show"); if (this.el) this.el.classList.remove("ct_talk"); }, 1800 + text.length * 55);
  },
  hoverMod(m) {
    clearTimeout(this.hoverTimer);
    this.hoverTimer = setTimeout(() => { if (MOD_LINES[m.id] && Math.random() < 0.6) this.say(MOD_LINES[m.id]); }, 700);
  },
  react(event) {
    if (!this.el) return;
    const moves = { launch: "ct_spin", installed: "ct_dance", working: "ct_dance", failed: "ct_sad", uninstalled: "ct_sad", running: "ct_sad", friend: "ct_hop", mod_on: "ct_hop", preset_all: "ct_spin" };
    if (moves[event]) this.act(moves[event], event === "working" ? 60000 : 2400);
    const lines = LINES[event];
    const important = ["installed", "failed", "running", "working", "uninstalled", "launch"].includes(event);
    if (lines && (important || Math.random() < 0.5)) this.say(this.pick(lines), important);
  },
};

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
  if (demo === "talk") setTimeout(() => claptrap.say("Hey there, minion! Er, I mean, valued Vault Hunter!", true), 600);
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
    load_friends: async () => ({ host_port: 7777, allow_lan: true, firewall_current: true, friends: [
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
    minimize: async () => {}, close: async () => {}, drag: async () => {},
  };
}

boot().catch((e) => {
  document.body.insertAdjacentHTML("beforeend", `<pre style="position:fixed;left:10px;bottom:10px;color:#f66;z-index:99999;font-size:12px">${String((e && e.stack) || e)}</pre>`);
});
