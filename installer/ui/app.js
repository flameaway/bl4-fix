/* BL4 Direct setup page. Talks to webgui.Api through window.pywebview.api; with ?mock in the URL
   it runs against fake data (for previews in a normal browser). */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const S = {
  api: null,
  info: null,
  gameDir: "",
  status: null,
  selected: new Set(),
  filter: "all",
  search: "",
  busy: false,
  task: null,
  logCount: 0,
  friends: { host_port: 7777, allow_lan: true, friends: [], firewall_current: true },
  friendsDirty: false,
};

const MONO = {
  bl4_direct: "DC", bl4_third_person: "3P", bl4_minimap: "MM", bl4_mod_menu: "M",
  bl4_reroll: "RR", bl4_weapondex: "WD", bl4_uvh_teleport: "UV", bl4_respec: "RS",
  bl4_perf: "PF", bl4_skip_intro: "SI", bl4_builds: "BP", bl4_less_fog: "LF",
};
const monogram = (m) => MONO[m.id] || m.name.replace(/^BL4\s+/i, "").split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
const rarity = (m) => (m.required ? "legendary" : m.default ? "epic" : m.source === "ported" ? "uncommon" : "rare");
const TIER = { legendary: "Legendary", epic: "Epic", rare: "Rare", uncommon: "Uncommon" };
const TIER_SUB = { legendary: "core // always on", epic: "recommended", rare: "BL4 Direct original", uncommon: "port of Yeti's mod" };

/* ---------- boot ---------- */

async function boot() {
  const started = performance.now();
  S.api = await getApi();
  S.info = await S.api.init();
  const icon = S.info.icon || "../icon.png";
  $("#boot-icon").src = icon;
  $("#tb-icon").src = icon;
  $("#tb-version").textContent = "v" + S.info.version;
  S.gameDir = S.info.game_dir;
  S.selected = new Set(S.info.mods.filter((m) => m.required || m.default).map((m) => m.id));
  $("#count-all").textContent = S.info.mods.length;
  renderMods();
  renderRailPath();
  wire();
  const statusP = refreshStatus();
  loadFriends();
  lookupIp();
  const still = new URLSearchParams(location.search).has("still");  // previews: no boot, no motion
  if (still) document.body.classList.add("still");
  else await sleep(Math.max(0, 1150 - (performance.now() - started)));
  $("#boot").classList.add("done");
  await statusP;
  setInterval(pollRunning, 3000);
}

function getApi() {
  if (new URLSearchParams(location.search).has("mock")) return Promise.resolve(mockApi());
  return new Promise((resolve) => {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.init) return resolve(window.pywebview.api);
    window.addEventListener("pywebviewready", () => resolve(window.pywebview.api), { once: true });
  });
}

/* ---------- mods ---------- */

function renderMods() {
  const grid = $("#mods");
  grid.innerHTML = S.info.mods.map((m, i) => {
    const r = rarity(m);
    return `<div class="card ink r-${r}${m.required ? " locked" : ""}" data-id="${m.id}" style="--i:${i}" role="checkbox" tabindex="0">
      <div class="card-top">
        <div class="hex"><b>${esc(monogram(m))}</b></div>
        <div class="card-tier"><span>${TIER[r]}</span><small>${TIER_SUB[r]}</small></div>
        ${m.key ? `<span class="keycap">${esc(m.key)}</span>` : ""}
      </div>
      <div class="card-name">${esc(m.name)}</div>
      <div class="card-blurb">${esc(m.blurb)}</div>
      <div class="card-foot">
        <span class="tags"></span>
        ${m.required
          ? `<span class="lock" title="Always installed"><svg viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="9" rx="1.5"/><path d="M8 11V8a4 4 0 018 0v3"/></svg></span>`
          : `<label class="switch" onclick="event.preventDefault()"><input type="checkbox" tabindex="-1"><i></i></label>`}
      </div>
    </div>`;
  }).join("");
  updateMods();
}

function updateMods() {
  const installed = new Set(S.status && S.status.valid ? S.status.installed : []);
  const dev = new Set(S.status && S.status.valid ? S.status.dev_mods : []);
  const known = !!(S.status && S.status.valid);
  const q = S.search.trim().toLowerCase();
  let shown = 0;
  for (const card of $$(".card", $("#mods"))) {
    const m = S.info.mods.find((x) => x.id === card.dataset.id);
    const on = S.selected.has(m.id);
    card.classList.toggle("on", on);
    card.setAttribute("aria-checked", on);
    const box = $("input", card);
    if (box) box.checked = on;
    const tags = [];
    if (m.required) tags.push(`<span class="tag always">Always on</span>`);
    else if (known && on && !installed.has(m.id)) tags.push(`<span class="tag add">+ Install</span>`);
    else if (known && !on && installed.has(m.id)) tags.push(`<span class="tag remove">- Remove</span>`);
    else if (installed.has(m.id)) tags.push(`<span class="tag installed">Installed</span>`);
    if (dev.has(m.id)) tags.push(`<span class="tag dev">Dev copy</span>`);
    const html = tags.join("");
    const tagEl = $(".tags", card);
    if (tagEl.innerHTML !== html) tagEl.innerHTML = html;
    const match =
      (S.filter === "all" || (S.filter === "on" && on) || S.filter === m.source) &&
      (!q || (m.name + " " + m.blurb + " " + m.id).toLowerCase().includes(q));
    card.hidden = !match;
    if (match) shown++;
  }
  $("#mods-empty").hidden = shown > 0;
  const n = S.selected.size;
  $("#count-on").textContent = n;
  $("#count-fill").style.width = (100 * n) / S.info.mods.length + "%";
  updateDock();
}

function toggleMod(id) {
  const m = S.info.mods.find((x) => x.id === id);
  if (!m || m.required || S.busy) return;
  if (S.selected.has(id)) S.selected.delete(id);
  else S.selected.add(id);
  const card = $(`.card[data-id="${id}"]`);
  card.classList.remove("flash");
  void card.offsetWidth;
  card.classList.add("flash");
  updateMods();
}

function preset(name) {
  if (S.busy) return;
  const mods = S.info.mods;
  S.selected = new Set(
    mods.filter((m) => m.required || name === "all" || (name === "defaults" && m.default)).map((m) => m.id),
  );
  updateMods();
}

function changes() {
  if (!S.status || !S.status.valid) return { add: [], remove: [] };
  const installed = new Set(S.status.installed);
  const mods = S.info.mods;
  return {
    add: mods.filter((m) => S.selected.has(m.id) && !installed.has(m.id)),
    remove: mods.filter((m) => !S.selected.has(m.id) && installed.has(m.id)),
  };
}

/* ---------- status ---------- */

async function refreshStatus() {
  if (!S.gameDir) {
    S.status = { valid: false };
  } else {
    setLeds(null);
    S.status = await S.api.status(S.gameDir);
  }
  if (S.status.valid) S.selected = new Set(S.status.chosen);
  renderRailPath();
  renderChecks();
  setLeds(S.status);
  updateMods();
  updateRunning(S.status.running);
}

function renderRailPath() {
  const bad = !S.gameDir || (S.status && !S.status.valid);
  const text = S.gameDir || "Not found. Pick the Borderlands 4 folder.";
  $("#rail-path").textContent = text;
  $("#rail-path").classList.toggle("bad", bad);
  $("#sys-path").textContent = text;
  $("#sys-path").classList.toggle("bad", bad);
}

const CHECK_ICON = '<svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
const CROSS_ICON = '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>';

function renderChecks() {
  const st = S.status;
  if (!st || !st.valid) {
    $("#checks").innerHTML = `<div class="check bad" style="--i:0"><span class="mark">${CROSS_ICON}</span><b>Game</b><span class="val"><span>Borderlands 4 not found</span><small>Use Browse to pick its install folder</small></span></div>`;
    $("#conflicts").innerHTML = "";
    $("#compat").textContent = "-";
    return;
  }
  $("#checks").innerHTML = st.checks.map((c, i) => {
    const cls = c.key === "intro" ? "info" : c.ok ? "ok" : "bad";
    return `<div class="check ${cls}" style="--i:${i}"><span class="mark">${cls === "bad" ? CROSS_ICON : CHECK_ICON}</span><b>${esc(c.label)}</b>
      <span class="val"><span title="${esc(c.value)}">${esc(c.value)}</span><small title="${esc(c.hint)}">${esc(c.hint)}</small></span></div>`;
  }).join("");
  $("#conflicts").innerHTML = st.conflicts.map((c) => `<div>${esc(c)}</div>`).join("");
  $("#compat").textContent = st.compat;
}

function setLeds(st) {
  const names = [["vc", "VC++"], ["sdk", "SDK"], ["mod", "BL4 Direct"], ["fw", "Firewall"]];
  $("#dock-leds").innerHTML = names.map(([key, label]) => {
    let cls = "wait";
    if (st && st.valid) cls = st.checks.find((c) => c.key === key).ok ? "ok" : "bad";
    else if (st) cls = "bad";
    return `<div class="led ${cls}"><i></i>${label}</div>`;
  }).join("");
}

function updateDock() {
  const btn = $("#install");
  const label = $("#install-label");
  const msg = $("#dock-msg");
  const sub = $("#dock-sub");
  btn.classList.remove("ready", "calm");
  if (S.busy) {
    label.textContent = S.task === "uninstall" ? "Removing" : "Working";
    btn.disabled = true;
    return;
  }
  const st = S.status;
  if (!st) { msg.textContent = "Reading install..."; sub.textContent = ""; btn.disabled = true; return; }
  if (!st.valid) {
    msg.textContent = "Borderlands 4 not found";
    sub.textContent = "Pick the game's install folder (Change, bottom left).";
    label.textContent = "Install";
    btn.disabled = true;
    return;
  }
  btn.disabled = !!st.running;
  const { add, remove } = changes();
  const fresh = st.installed.length === 0;
  if (fresh) {
    msg.innerHTML = `Ready to install <span class="add">${S.selected.size} mods</span>`;
    sub.textContent = "Installs the Python SDK, BL4 Direct and your loadout. Takes a few seconds.";
    label.textContent = "Install";
    btn.classList.add("ready");
  } else if (add.length || remove.length) {
    const parts = [];
    if (add.length) parts.push(`<span class="add">+${add.length} new</span>`);
    if (remove.length) parts.push(`<span class="rem">-${remove.length} removed</span>`);
    msg.innerHTML = parts.join(" &nbsp; ");
    sub.textContent = [...add.map((m) => "+ " + m.name), ...remove.map((m) => "- " + m.name)].join("   ");
    label.textContent = "Apply";
    btn.classList.add("ready");
  } else if (!st.ready) {
    msg.innerHTML = `<span class="rem">Needs a repair</span>`;
    sub.textContent = st.checks.filter((c) => !c.ok && c.key !== "fw").map((c) => `${c.label}: ${c.value}`).join("   ") || "Something's off. Install fixes it.";
    label.textContent = "Repair";
    btn.classList.add("ready");
  } else {
    msg.textContent = "Locked and loaded";
    sub.textContent = `${st.installed.length} mods installed. Launch the game: F6 co-op, F7 mod settings.`;
    label.textContent = "Reinstall";
    btn.classList.add("calm");
  }
}

async function pollRunning() {
  if (document.hidden || !S.status || !S.status.valid || S.busy) return;
  updateRunning(await S.api.running());
}

function updateRunning(running) {
  if (!S.status || !S.status.valid) running = false;
  $("#running-banner").hidden = !running;
  if (S.status && S.status.valid && S.status.running !== running) {
    S.status.running = running;
    updateDock();
  }
}

async function chooseFolder() {
  if (S.busy) return;
  const picked = await S.api.browse();
  if (!picked) return;
  if (!(await S.api.is_game_dir(picked))) {
    toast("That's not a Borderlands 4 folder", true);
    return;
  }
  S.gameDir = picked;
  S.status = null;
  updateDock();
  await refreshStatus();
  loadFriends();
}

/* ---------- tasks ---------- */

async function install() {
  if (S.busy || !S.status || !S.status.valid) return;
  const { remove } = changes();
  if (remove.length) {
    const ok = await confirmModal(
      "Remove mods?",
      `<p>These will be uninstalled (their settings are kept):</p><ul>${remove.map((m) => `<li>${esc(m.name)}</li>`).join("")}</ul>`,
      "Remove & apply",
      "red",
    );
    if (!ok) return;
  }
  startTask("install", `Install: ${S.selected.size} mods`);
  if (!(await S.api.install(S.gameDir, [...S.selected]))) endTask();
}

async function uninstall() {
  if (S.busy || !S.status || !S.status.valid) return;
  const ok = await confirmModal(
    "Uninstall everything?",
    `<p>Removes BL4 Direct, every mod this app installed and the firewall rule. Your friends list and mod settings are kept.</p>
     <label class="toggle-field"><span class="switch" style="--r:var(--red)"><input type="checkbox" id="rm-sdk"><i></i></span>
     <span><b>Also remove the Python SDK</b><small>Leave this off if you use other SDK mods</small></span></label>`,
    "Uninstall",
    "red",
    () => ({ removeSdk: $("#rm-sdk").checked }),
  );
  if (!ok) return;
  startTask("uninstall", "Uninstall");
  if (!(await S.api.uninstall(S.gameDir, ok.removeSdk))) endTask();
}

function startTask(name, title) {
  S.busy = true;
  S.task = name;
  S.logCount = 0;
  $("#dock").classList.add("busy");
  $("#pfill").style.width = "4%";
  $("#pline").textContent = "Starting...";
  $("#dock-msg").textContent = name === "uninstall" ? "Uninstalling..." : name === "friends" ? "Locking firewall..." : "Installing...";
  logLine(title, "head");
  updateDock();
}

function endTask() {
  S.busy = false;
  S.task = null;
  $("#dock").classList.remove("busy");
  updateDock();
}

/* Called by webgui.Api */
window.app = {
  onLog(msg) {
    logLine(msg, /^Error/.test(msg) ? "err" : /^(Done|Installed|Removed|Saved)/.test(msg) ? "ok" : "");
    if (!S.busy) return;
    S.logCount++;
    // Install logs about one line per mod plus a handful of setup lines
    const expect = S.task === "install" ? S.selected.size + 8 : 8;
    $("#pfill").style.width = Math.min(94, 4 + (90 * S.logCount) / expect) + "%";
    $("#pline").textContent = msg;
  },
  async onTaskDone(name, error) {
    $("#pfill").style.width = "100%";
    await sleep(350);
    endTask();
    if (name === "friends") {
      if (error) toast("Firewall: " + error, true);
      else toast("Friends saved, firewall locked");
      S.friendsDirty = false;
      loadFriends();
    } else if (error) {
      stamp(false, error);
    } else {
      stamp(true, name === "uninstall" ? "BL4 Direct and its mods are gone." : "Launch Borderlands 4. F6 for co-op, F7 for mod settings.");
    }
    refreshStatus();
  },
};

function logLine(msg, cls = "") {
  const log = $("#log-full");
  const div = document.createElement("div");
  if (cls) div.className = cls;
  div.textContent = msg;
  log.appendChild(div);
  while (log.childElementCount > 400) log.firstElementChild.remove();
  log.scrollTop = log.scrollHeight;
}

/* ---------- co-op ---------- */

async function lookupIp() {
  const el = $("#ip");
  el.classList.add("wait");
  const ip = await S.api.public_ip();
  el.classList.remove("wait");
  if (!ip) { el.textContent = "offline?"; return; }
  S.publicIp = ip;
  $("#rail-ip").textContent = ip;
  if (document.body.classList.contains("still")) el.textContent = ip;
  else decode(el, ip);
}

function decode(el, text) {
  const glyphs = "0123456789#%&@$";
  let frame = 0;
  const total = 22;
  const tick = () => {
    frame++;
    el.textContent = [...text].map((ch, i) => (ch === "." || frame > (i / text.length) * total + 6 ? ch : glyphs[(Math.random() * glyphs.length) | 0])).join("");
    if (frame < total + 8) requestAnimationFrame(tick);
  };
  tick();
}

async function loadFriends() {
  S.friends = await S.api.load_friends(S.gameDir || "");
  S.friendsDirty = false;
  $("#host-port").value = S.friends.host_port;
  $("#allow-lan").checked = S.friends.allow_lan;
  renderFriends();
}

function renderFriends() {
  const list = S.friends.friends;
  $("#friend-count").textContent = list.length;
  $("#friends-empty").hidden = list.length > 0;
  $("#friends").innerHTML = list.map((f, i) => {
    const hue = [...f.name].reduce((a, c) => (a * 31 + c.charCodeAt(0)) % 360, 7);
    return `<div class="friend" style="animation-delay:${i * 40}ms" data-i="${i}">
      <div class="hex" style="--r:hsl(${hue} 85% 58%)"><b>${esc(f.name[0].toUpperCase())}</b></div>
      <div class="f-main"><span class="f-name">${esc(f.name)}</span><span class="f-addr">${esc(f.address)}<b>:${f.port}</b></span></div>
      <button class="icon-btn" data-copy="${esc(f.address)}" title="Copy IP"><svg viewBox="0 0 24 24"><rect x="8" y="8" width="12" height="12" rx="1"/><path d="M16 8V4H4v12h4"/></svg></button>
      <button class="icon-btn del" data-del="${i}" title="Remove"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
    </div>`;
  }).join("");
  const lan = S.info.lan_ip || "this PC's local IP";
  $("#forward-hint").innerHTML = `<b>Hosting over the internet?</b> Forward <code>UDP ${esc($("#host-port").value || S.friends.host_port)}</code> on your router to <code>${esc(lan)}</code>. Joining someone needs no setup.`;
  const fw = $("#fw-state");
  const current = S.friends.firewall_current && !S.friendsDirty;
  fw.className = "fw-state " + (current ? "ok" : "stale");
  $("span", fw).textContent = current ? "Firewall matches this list" : "Unsaved changes: firewall not updated";
}

function markFriendsDirty() {
  S.friendsDirty = true;
  renderFriends();
}

async function addFriend(e) {
  e.preventDefault();
  const name = $("#f-name").value.trim();
  const ip = await S.api.normalize_ip($("#f-ip").value);
  const portText = $("#f-port").value.trim();
  const port = portText ? parseInt(portText, 10) : S.info.default_port;
  let bad = false;
  for (const [el, ok] of [[$("#f-name"), !!name], [$("#f-ip"), !!ip], [$("#f-port"), port >= 1024 && port <= 65535]]) {
    el.classList.remove("err");
    if (!ok) { void el.offsetWidth; el.classList.add("err"); bad = true; }
  }
  if (bad) { toast(!ip ? "Enter an IPv4 address like 203.0.113.5" : "Check the name and port", true); return; }
  S.friends.friends = S.friends.friends.filter((f) => f.name.toLowerCase() !== name.toLowerCase());
  S.friends.friends.push({ name, address: ip, port });
  $("#f-name").value = $("#f-ip").value = $("#f-port").value = "";
  $("#f-name").focus();
  markFriendsDirty();
}

async function removeFriend(i) {
  const row = $(`.friend[data-i="${i}"]`);
  if (row) { row.classList.add("gone"); await sleep(230); }
  S.friends.friends.splice(i, 1);
  markFriendsDirty();
}

async function saveFriends() {
  if (S.busy) return;
  if (!S.status || !S.status.valid) { toast("Pick the Borderlands 4 folder first", true); return; }
  const port = parseInt($("#host-port").value, 10);
  if (!(port >= 1024 && port <= 65535)) { toast("Host port must be 1024-65535", true); return; }
  const data = { host_port: port, allow_lan: $("#allow-lan").checked, friends: S.friends.friends };
  startTask("friends", "Friends & firewall");
  if (!(await S.api.save_friends(S.gameDir, data))) endTask();
}

/* ---------- ui bits ---------- */

function showPage(name) {
  $$(".nav").forEach((n) => n.classList.toggle("on", n.dataset.page === name));
  $$(".page").forEach((p) => p.classList.toggle("on", p.id === "page-" + name));
}

function toast(text, bad = false) {
  const el = document.createElement("div");
  el.className = "toast" + (bad ? " bad" : "");
  el.textContent = text;
  $("#toasts").appendChild(el);
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 260); }, 2400);
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

function confirmModal(title, body, okText, okColor = "yellow", collect = null) {
  return new Promise((resolve) => {
    $("#modal-title").textContent = title;
    $("#modal-body").innerHTML = body;
    $("#modal-btns").innerHTML = `<button class="btn ghost" data-r="0">Cancel</button><button class="btn ${okColor}" data-r="1">${esc(okText)}</button>`;
    $("#modal").hidden = false;
    const done = (ok) => {
      const result = ok ? (collect ? collect() : true) : false;
      $("#modal").hidden = true;
      $("#modal").onclick = null;
      resolve(result);
    };
    $("#modal").onclick = (e) => {
      if (e.target.id === "modal") done(false);
      const b = e.target.closest("[data-r]");
      if (b) done(b.dataset.r === "1");
    };
  });
}

function stamp(ok, sub) {
  const el = $("#stamp");
  el.classList.toggle("fail", !ok);
  $("#stamp-word").textContent = ok ? (S.lastTask === "uninstall" ? "UNINSTALLED" : "INSTALLED!") : "FAILED";
  $("#stamp-sub").textContent = sub;
  const colors = ["var(--yellow)", "var(--orange)", "var(--teal)", "#fff", "var(--purple)"];
  $("#shards").innerHTML = ok
    ? Array.from({ length: 34 }, () => {
        const a = Math.random() * Math.PI * 2, d = 220 + Math.random() * 380;
        return `<i style="--c:${colors[(Math.random() * colors.length) | 0]};--x:${Math.cos(a) * d}px;--y:${Math.sin(a) * d}px;--rot:${(Math.random() * 720 - 360) | 0}deg;animation-delay:${(Math.random() * 120) | 0}ms"></i>`;
      }).join("")
    : "";
  el.classList.remove("out");
  el.hidden = false;
  const close = () => {
    el.classList.add("out");
    setTimeout(() => { el.hidden = true; }, 400);
    el.onclick = null;
    clearTimeout(timer);
  };
  el.onclick = close;
  const timer = document.body.classList.contains("still") ? 0 : setTimeout(close, ok ? 3200 : 7000);
}

function wire() {
  // Windows drags the window itself (webgui.Api.drag), not pywebview's drag region
  $("#tb-drag").addEventListener("mousedown", (e) => { if (e.button === 0) S.api.drag(); });
  $("#btn-min").onclick = () => S.api.minimize();
  $("#btn-close").onclick = () => {
    if (S.busy) toast("Hang on, still working...", true);
    else S.api.close();
  };
  $$(".nav").forEach((n) => (n.onclick = () => showPage(n.dataset.page)));
  $("#mods").addEventListener("click", (e) => {
    const card = e.target.closest(".card");
    if (card) toggleMod(card.dataset.id);
  });
  $("#mods").addEventListener("keydown", (e) => {
    const card = e.target.closest(".card");
    if (card && (e.key === " " || e.key === "Enter")) { e.preventDefault(); toggleMod(card.dataset.id); }
  });
  $("#filters").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    S.filter = chip.dataset.filter;
    $$(".chip", $("#filters")).forEach((c) => c.classList.toggle("on", c === chip));
    updateMods();
  });
  $("#search").addEventListener("input", (e) => { S.search = e.target.value; updateMods(); });
  $$("[data-preset]").forEach((b) => (b.onclick = () => preset(b.dataset.preset)));
  $("#install").onclick = () => { S.lastTask = "install"; install(); };
  $("#uninstall").onclick = () => { S.lastTask = "uninstall"; uninstall(); };
  $("#rail-browse").onclick = chooseFolder;
  $("#sys-browse").onclick = chooseFolder;
  $("#sys-open").onclick = () => S.api.open_folder(S.gameDir);
  $("#sys-refresh").onclick = () => { if (!S.busy) refreshStatus(); };
  $("#compat-copy").onclick = () => copy($("#compat").textContent);
  $("#log-clear").onclick = () => { $("#log-full").innerHTML = ""; };
  $("#ip-copy").onclick = () => copy(S.publicIp);
  $("#rail-ipcard").onclick = () => copy(S.publicIp);
  $("#add-friend").addEventListener("submit", addFriend);
  $("#friends").addEventListener("click", (e) => {
    const c = e.target.closest("[data-copy]");
    if (c) copy(c.dataset.copy);
    const d = e.target.closest("[data-del]");
    if (d) removeFriend(+d.dataset.del);
  });
  $$(".stepper button").forEach((b) => (b.onclick = () => {
    const v = parseInt($("#host-port").value, 10) || S.info.default_port;
    $("#host-port").value = Math.max(1024, Math.min(65535, v + +b.dataset.step));
    markFriendsDirty();
  }));
  $("#host-port").addEventListener("input", (e) => { e.target.value = e.target.value.replace(/\D/g, ""); markFriendsDirty(); });
  $("#f-port").addEventListener("input", (e) => { e.target.value = e.target.value.replace(/\D/g, ""); });
  $("#allow-lan").addEventListener("change", markFriendsDirty);
  $("#save-friends").onclick = saveFriends;
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#modal").hidden) $("#modal").click();
  });
  // A frameless window: no browser context menu or reload keys
  document.addEventListener("contextmenu", (e) => { if (!e.target.closest("input, .log, .path-box, code, .ip")) e.preventDefault(); });
  document.addEventListener("keydown", (e) => { if (e.key === "F5" || (e.ctrlKey && e.key.toLowerCase() === "r")) e.preventDefault(); });
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
    ["bl4_reroll", "Reroll", "Reroll class mod and enhancement stats for Eridium.", "mods", 0, 0, "T / G"],
    ["bl4_weapondex", "Weapondex", "A dex of the bosses you've beaten and their legendaries (F8).", "mods", 0, 0, "F8"],
    ["bl4_uvh_teleport", "UVH Boss Teleport", "Hover a UVH boss challenge and press T to go there.", "mods", 0, 0, "T"],
    ["bl4_minimap", "Minimap", "A minimap with the ECHO map's art, fog, icons and route (N).", "mods", 0, 0, "N"],
    ["bl4_third_person", "Third Person", "H: over-the-shoulder camera with aiming.", "mods", 0, 0, "H"],
    ["bl4_vehicles_anywhere", "Vehicles Anywhere", "Summon and drive where vehicles aren't allowed.", "mods", 0, 0, ""],
    ["bl4_backpack_size", "Backpack Size", "Set the backpack item limit.", "mods", 0, 0, ""],
    ["bl4_shoot_open", "Shoot to Open", "Shoot a container or chest to open it.", "mods", 0, 0, ""],
    ["bl4_less_fog", "Less Fog", "Thin out the haze over the world.", "mods", 0, 0, ""],
    ["bl4_perf", "Performance Fixes", "Fewer hitches: no DLSS reload after menus, cheaper mod hooks.", "mods", 0, 0, ""],
    ["bl4_dialog_skipper", "Dialog Skipper", "Skip voice lines by key or automatically.", "ported", 0, 0, ""],
    ["bl4_grapple_anywhere", "Grapple Anywhere", "Grapple to any ledge you aim at.", "ported", 0, 0, ""],
    ["bl4_ground_loot", "Ground Loot Helpers", "Bring the loot lying around to you, or clear it.", "ported", 0, 0, ""],
    ["bl4_slam_tweaks", "Slam Tweaks", "A fast Pre-Sequel style ground slam.", "ported", 0, 0, ""],
    ["bl4_encore_tweaks", "Encore Tweaks", "Big Encore machines refill health, ammo and skills.", "ported", 0, 0, ""],
  ].map(([id, name, blurb, source, required, def, key]) => ({ id, name, blurb, source, required: !!required, default: !!def, key }));
  const installed = ["bl4_direct", "bl4_mod_menu", "bl4_item_cards", "bl4_minimap", "bl4_third_person", "bl4_perf", "bl4_dialog_skipper"];
  const fresh = new URLSearchParams(location.search).get("mock") === "fresh";
  const page = new URLSearchParams(location.search).get("page");
  if (page) setTimeout(() => showPage(page), 0);
  const demo = new URLSearchParams(location.search).get("demo");
  if (demo === "stamp") setTimeout(() => stamp(true, "Launch Borderlands 4. F6 for co-op, F7 for mod settings."), 400);
  if (demo === "modal") setTimeout(() => $("#install").click(), 400);
  const ok = (key, label, value, hint, good = true) => ({ key, label, ok: good, value, hint });
  return {
    init: async () => ({ version: "0.5.0", sdk_version: "0.3", icon: "", game_dir: "C:\\Program Files (x86)\\Steam\\steamapps\\common\\Borderlands 4", default_port: 7777, lan_ip: "192.168.1.42", mods }),
    status: async () => {
      await sleep(300);
      return {
        valid: true, ready: !fresh, running: false, installed: fresh ? [] : installed,
        chosen: fresh ? ["bl4_direct", "bl4_mod_menu", "bl4_item_cards"] : [...installed, "bl4_reroll"].filter((x) => x !== "bl4_perf"),
        dev_mods: [], conflicts: [], compat: "BL4-5554-8f3a2c-0.5.0",
        checks: [
          ok("vc", "VC++ Runtime", "14.44", "needs 14.44+ (installed for you)"),
          ok("sdk", "Python SDK", fresh ? "missing" : "0.3", "needs 0.3", !fresh),
          ok("mod", "BL4 Direct", fresh ? "missing" : "0.5.0", "this app: 0.5.0", !fresh),
          ok("fw", "Firewall", "locked", "UDP 7777: this PC, LAN, 2 friends"),
          ok("intro", "Skip Intro", "off", ""),
        ],
      };
    },
    running: async () => false,
    browse: async () => null,
    is_game_dir: async () => true,
    open_folder: async () => {},
    public_ip: async () => { await sleep(900); return "203.0.113.57"; },
    load_friends: async () => ({ host_port: 7777, allow_lan: true, firewall_current: true, friends: [
      { name: "Moxxi", address: "198.51.100.23", port: 7777 }, { name: "Zane", address: "203.0.113.9", port: 7778 }, { name: "Amara", address: "192.0.2.144", port: 7777 }] }),
    normalize_ip: async (t) => (/^\d+\.\d+\.\d+\.\d+$/.test(t.trim()) ? t.trim() : ""),
    install: async () => {
      (async () => {
        const lines = ["Python SDK 0.3 already installed.", ...[...S.selected].map((m) => `Installed ${m}.`), "Firewall: UDP 7777 only accepts this PC, your local network/VPN", "Done. In game: F6 for the BL4 Direct panel, F7 for mod settings."];
        for (const l of lines) { await sleep(120); app.onLog(l); }
        app.onTaskDone("install", null);
      })();
      return true;
    },
    uninstall: async () => { setTimeout(() => app.onTaskDone("uninstall", null), 800); return true; },
    save_friends: async () => { setTimeout(() => app.onTaskDone("friends", null), 600); return true; },
    minimize: async () => {}, close: async () => {}, drag: async () => {},
  };
}

boot().catch((e) => {
  document.body.insertAdjacentHTML("beforeend", `<pre style="position:fixed;inset:auto 10px 10px;color:#f66;z-index:999">${esc(e && e.stack || e)}</pre>`);
});
