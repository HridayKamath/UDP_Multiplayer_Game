"use strict";
const PALETTE = ["#4ca6ff", "#ffbd4a", "#4ad394", "#c58cff", "#ff8fa3", "#6fe0e0", "#d6d96b", "#ff9a5c"];
const SCENARIOS = [
  ["baseline", "Normal network (baseline)"], ["delay", "High delay (40 ms)"],
  ["jitter", "Delay + jitter (40 ± 20 ms)"], ["loss", "Packet loss (5 % per direction)"],
  ["congestion_noqos", "Congestion, NO prioritisation (baseline)"], ["congestion_qos", "Congestion, SDN QoS prioritisation"],
];
let S = {};                       // latest state from /api/state
let driveId = null;
const colors = {}, history = {};
let lastEventKey = "", lastExpDone = -1, condInit = false, lastMove = 0;

const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const fmt = (v, d = 1) => (v === null || v === undefined) ? "—" : Number(v).toFixed(d);
function colorOf(id) { if (!(id in colors)) colors[id] = PALETTE[Object.keys(colors).length % PALETTE.length]; return colors[id]; }

let toastTimer;
function toast(msg, error) {
  const t = $("toast"); t.textContent = msg; t.className = error ? "error" : ""; t.style.display = "block";
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.style.display = "none", 4500);
}

async function act(action, data) {
  try {
    const r = await fetch("/api/action", {method: "POST", headers: {"Content-Type": "application/json"},
                                          body: JSON.stringify(Object.assign({action}, data || {}))});
    const j = await r.json();
    toast(j.msg || (j.ok ? "done" : "failed"), !j.ok);
    await poll();
    return j;
  } catch (e) { toast("request failed: " + e, true); }
}

// ------------------------------------------------------------------ polling
async function poll() {
  try {
    const r = await fetch("/api/state"); const s = await r.json();
    if (s.booting) return;
    S = s; render();
  } catch (e) {
    setPill("pNet", "bad", "Manager offline");
  }
}

function setPill(id, cls, text) { const e = $(id); e.className = "pill " + cls; e.textContent = text; }

function render() {
  const g = S.game || {players: []}, players = g.players || [];
  const dev = S.lab === "local", ctl = S.controller || {};
  setPill("pNet", S.network ? (dev ? "warn" : "ok") : "bad", S.network ? (dev ? "Dev mode (no SDN)" : "Network up") : "Network down");
  setPill("pCtl", dev ? "warn" : (ctl.ok && ctl.switch_connected ? "ok" : "bad"),
          dev ? "No controller" : (ctl.ok ? (ctl.switch_connected ? "Ryu + switch" : "Ryu, no switch") : "Ryu offline"));
  setPill("pSrv", g.server_up ? "ok" : "bad", g.server_up ? "Server running" : "Server stopped");
  setPill("pQos", dev ? "" : (S.qos === "on" ? "ok" : "warn"), dev ? "QoS n/a" : "QoS " + (S.qos === "on" ? "ON" : "OFF"));
  const b = $("busy"); b.hidden = !S.busy; b.textContent = S.busy ? "⏳ " + S.busy + " is running – other controls are locked" : "";
  renderKpis(players, g); renderArena(players); renderPlayers(players); renderRtt(players);
  renderTopo(players); renderControls(); renderFlows(); renderQueues(); renderExp(); renderTests(); renderEvents();
}

// ------------------------------------------------------------------ live tab
function renderKpis(players, g) {
  const conn = players.filter(p => p.status === "connected");
  const rtts = conn.map(p => p.rtt_ms).filter(v => v !== null), jit = conn.map(p => p.jitter_ms).filter(v => v !== null);
  const avg = a => a.length ? a.reduce((x, y) => x + y, 0) / a.length : null;
  const rec = players.reduce((s, p) => s + p.up_received, 0), lost = players.reduce((s, p) => s + p.up_lost, 0);
  const loss = rec + lost ? 100 * lost / (rec + lost) : 0, ar = avg(rtts);
  const q = ar === null ? "" : ar < 60 ? "good" : ar < 150 ? "mid" : "poor";
  const lq = loss < 1 ? "good" : loss < 5 ? "mid" : "poor";
  const k = (label, val, sub, cls) => `<div class="kpi ${cls || ""}"><span>${label}</span><strong>${val}</strong><small>${sub || ""}</small></div>`;
  $("kpis").innerHTML =
    k("Players connected", conn.length + " / " + players.length, "registered with the server") +
    k("Average RTT", ar === null ? "—" : fmt(ar) + " ms", "client-measured round trip", q) +
    k("Average jitter", jit.length ? fmt(avg(jit), 2) + " ms" : "—", "smoothed RTT variation") +
    k("Updates lost (↑)", fmt(loss, 1) + " %", lost + " of " + (rec + lost) + " missing (seq gaps)", players.length ? lq : "") +
    k("Background traffic", g.bg ? fmt(g.bg.rate_mbps_now, 1) + " Mbit/s" : "—", "received at the server (port 10000)");
}

function renderArena(players) {
  const root = $("arenaPlayers"), seen = new Set(), blocked = S.blocked || [];
  players.forEach(p => {
    seen.add(p.id);
    let d = root.querySelector(`[data-id="${CSS.escape(p.id)}"]`);
    if (!d) { d = document.createElement("div"); d.dataset.id = p.id; d.textContent = p.id.slice(0, 3); d.style.background = colorOf(p.id); root.appendChild(d); }
    d.className = "dot" + (p.status !== "connected" ? " disconnected" : "") + (blocked.includes(p.ip) ? " blocked" : "") + (p.id === driveId ? " drive" : "");
    d.style.left = Math.max(0, Math.min(100, p.x)) + "%"; d.style.top = Math.max(0, Math.min(100, p.y)) + "%";
    d.title = `${p.id}  (${p.x}, ${p.y})  ${p.status}`;
  });
  [...root.children].forEach(c => { if (!seen.has(c.dataset.id)) c.remove(); });
  $("legend").innerHTML = players.map(p => `<span><i style="background:${colorOf(p.id)}"></i>${esc(p.id)}</span>`).join("");
}

function renderPlayers(players) {
  const blocked = S.blocked || [], procs = S.processes || {}, dev = S.lab === "local";
  $("noPlayers").hidden = players.length > 0;
  $("players").tBodies[0].innerHTML = players.map(p => {
    const isB = blocked.includes(p.ip), alive = procs[p.id] && procs[p.id].alive;
    const host = Object.keys(S.hosts || {}).find(h => S.hosts[h] === p.ip) || "";
    const lossCls = p.up_loss_pct > 5 ? "color:var(--red)" : p.up_loss_pct > 0.5 ? "color:var(--amber)" : "";
    const sb = isB ? `<button data-a="unblock" data-id="${esc(p.id)}" class="primary">Unblock</button>`
                   : `<button data-a="block" data-id="${esc(p.id)}" class="danger" ${dev ? "disabled" : ""}>Block</button>`;
    const rm = alive ? `<button data-a="player_remove" data-id="${esc(p.id)}">Remove</button>` : "";
    const dr = p.status === "connected" ? `<button data-a="drive" data-id="${esc(p.id)}">${driveId === p.id ? "Driving ✓" : "Drive"}</button>` : "";
    return `<tr><td><i class="legend" style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${colorOf(p.id)};margin-right:6px"></i><b>${esc(p.id)}</b></td>
      <td class="l">${esc(host)} ${esc(p.ip)}</td><td><span class="badge ${p.status}">${p.status}${isB ? " (blocked)" : ""}</span></td>
      <td>${p.x}, ${p.y}</td><td>${p.seq}</td><td>${p.up_received}</td><td>${p.up_lost}</td><td style="${lossCls}">${fmt(p.up_loss_pct, 1)} %</td>
      <td>${p.down_lost}</td><td>${fmt(p.rtt_ms)}</td><td>${fmt(p.jitter_ms, 2)}</td><td>${p.pps}</td><td>${rm}${dr}${sb}</td></tr>`;
  }).join("");
}

function renderRtt(players) {
  players.forEach(p => {
    if (p.rtt_ms !== null && p.status === "connected") { (history[p.id] = history[p.id] || []).push(p.rtt_ms); if (history[p.id].length > 60) history[p.id].shift(); }
  });
  const c = $("rttChart"), g = c.getContext("2d"); g.clearRect(0, 0, c.width, c.height);
  let max = 20; Object.values(history).forEach(h => h.forEach(v => { if (v > max) max = v; }));
  max = Math.ceil(max / 20) * 20;
  g.strokeStyle = "#2b3b57"; g.fillStyle = "#93a3bb"; g.font = "10px sans-serif"; g.lineWidth = 1;
  for (let i = 0; i <= 4; i++) { const y = 12 + (c.height - 26) * i / 4; g.beginPath(); g.moveTo(34, y); g.lineTo(c.width - 6, y); g.stroke(); g.fillText(Math.round(max * (1 - i / 4)), 4, y + 3); }
  Object.keys(history).forEach(id => {
    const h = history[id]; g.strokeStyle = colorOf(id); g.lineWidth = 2; g.beginPath();
    h.forEach((v, i) => { const x = 34 + i * (c.width - 40) / 59, y = 12 + (c.height - 26) * (1 - v / max); i ? g.lineTo(x, y) : g.moveTo(x, y); });
    g.stroke();
  });
}

// ------------------------------------------------------------------ SDN tab
function renderTopo(players) {
  const blocked = S.blocked || [], hosts = S.hosts || {};
  ["client1", "client2", "client3", "client4"].forEach(h => {
    const ip = hosts[h], has = players.some(p => p.ip === ip && p.status === "connected"), isB = blocked.includes(ip);
    $("l-" + h).setAttribute("class", isB ? "blocked" : "");
    $("n-" + h).setAttribute("class", isB ? "isblocked" : has ? "hasplayers" : "");
  });
  const q = S.queues || {}, c = S.conditions || {};
  $("topoBottleneck").textContent = "bottleneck " + (c.bw || "?") + " Mbit/s";
  $("topoQ1").textContent = q[1] ? `q1 game ${fmt(q[1].mbps, 2)} Mbit/s` : "";
  $("topoQ0").textContent = q[0] ? `q0 other ${fmt(q[0].mbps, 2)} Mbit/s` : "";
  $("n-bg").setAttribute("class", S.bg && S.bg.running ? "isblocked" : "");
}

function renderControls() {
  const dev = S.lab === "local", on = S.qos === "on";
  $("qosOn").className = on ? "on" : ""; $("qosOff").className = on ? "" : "offon";
  $("qosOn").disabled = $("qosOff").disabled = dev;
  $("blockedList").innerHTML = (S.blocked || []).length
    ? S.blocked.map(ip => `<span class="chip">${esc(ip)} blocked <a href="#" data-a="unblock_ip" data-ip="${esc(ip)}" style="color:#fff">unblock</a></span>`).join("") : '<span class="muted">nobody blocked</span>';
  const gs = S.guard || {};
  if (document.activeElement !== $("guardOn")) $("guardOn").checked = !!gs.enabled;
  const c = S.conditions || {};
  $("condNow").textContent = `Active: delay ${c.delay} ms, jitter ${c.jitter} ms, loss ${c.loss} %, bottleneck ${c.bw} Mbit/s`;
  if (!condInit && S.conditions) { $("cDelay").value = c.delay; $("cJitter").value = c.jitter; $("cLoss").value = c.loss; $("cBw").value = c.bw; condInit = true; }
  $("bgNow").textContent = S.bg && S.bg.running ? `running: ${S.bg.mbps} Mbit/s` : "idle";
}

function renderFlows() {
  const rows = (S.flows || []).map(f => `<tr style="${f.priority === 300 ? "background:rgba(255,107,107,.1)" : ""}"><td>${f.priority}</td>
    <td class="l"><span class="badge ${f.priority === 300 ? "disconnected" : "connected"}">${esc(f.label)}</span></td>
    <td class="l" style="font-family:monospace;font-size:12px">${esc(f.match)}</td><td class="l" style="font-family:monospace;font-size:12px">${esc(f.actions)}</td>
    <td>${f.packets.toLocaleString()}</td><td>${f.bytes.toLocaleString()}</td></tr>`).join("");
  $("flows").tBodies[0].innerHTML = rows || '<tr><td colspan="6" class="muted center">No OpenFlow data (network down, or local dev mode).</td></tr>';
}

function renderQueues() {
  const q = S.queues || {}, bw = (S.conditions || {}).bw || 10;
  const row = (id, name, cls) => {
    const v = q[id] || {bytes: 0, mbps: 0, drops: 0};
    return `<div class="qrow"><div class="top"><span><b>Queue ${id}</b> – ${name}</span><span>${fmt(v.mbps, 2)} Mbit/s · ${(v.bytes / 1e6).toFixed(2)} MB total</span></div>
      <div class="qbar ${cls}"><div style="width:${Math.min(100, 100 * v.mbps / bw)}%"></div></div></div>`;
  };
  $("queues").innerHTML = Object.keys(q).length ? row(1, "game traffic (priority)", "q1") + row(0, "best effort / background", "q0")
    : '<p class="muted">No queue statistics (network down or QoS not attached).</p>';
  $("queueRaw").textContent = S.queue_raw || "";
}

// ------------------------------------------------------------------ experiments tab
async function renderExp() {
  const e = S.experiment || {}, wrap = $("expProgressWrap");
  wrap.hidden = !e.running; $("expBar").style.width = e.total ? (100 * e.done / e.total) + "%" : "0";
  $("expMsg").textContent = e.running ? `Running: ${e.current}  –  ${e.done}/${e.total} runs done` : (e.msg && e.msg !== "idle" ? e.msg : "");
  if (e.done !== lastExpDone || e.running === false && lastExpDone === -2) { lastExpDone = e.done; loadResults(); }
}

async function loadResults() {
  try {
    const r = await (await fetch("/api/results")).json(), s = r.summary || [];
    $("noResults").hidden = s.length > 0;
    const cell = (it, m) => { const v = it[m]; return v ? (it.runs > 1 ? `${v.mean} <small class="muted">±${v.sd}</small>` : v.mean) : "—"; };
    $("resTable").tBodies[0].innerHTML = s.map(it => `<tr><td class="l">${esc(it.label)}</td><td>${it.runs}</td>` +
      ["rtt_avg_ms", "rtt_p95_ms", "jitter_ms", "up_loss_pct", "down_loss_pct", "bg_rx_mbps"].map(m => `<td>${cell(it, m)}</td>`).join("") + "</tr>").join("");
    $("findings").innerHTML = (r.findings || []).length ? "<ul>" + r.findings.map(t => `<li>${esc(t)}</li>`).join("") + "</ul>" : "";
    $("charts").innerHTML = (r.charts || []).join("");
  } catch (e) { /* ignore */ }
}

function runExperiments() {
  const names = [...document.querySelectorAll("#scenList input:checked")].map(i => i.value);
  act("experiment_start", {scenarios: names, duration: +$("expDur").value, repeats: +$("expRep").value});
}
async function resetResults() {
  if (!confirm("Move the current results.csv aside and start with an empty results table?")) return;
  await fetch("/api/experiment/reset", {method: "POST"}); lastExpDone = -1; loadResults(); toast("results cleared (old file kept as .bak)");
}

// ------------------------------------------------------------------ tests + log
function renderTests() {
  const t = S.selftest || {results: []}, list = t.results || [];
  const pass = list.filter(r => r.status === "pass").length, fail = list.filter(r => r.status === "fail").length;
  $("testSummary").textContent = t.running ? `Running: ${t.current} …` : list.length ? `${pass} passed, ${fail} failed, ${list.length - pass - fail} skipped` : "Not run yet";
  $("testList").innerHTML = list.map(r => `<div class="test"><span class="badge ${r.status}">${r.status.toUpperCase()}</span>
    <div><span class="nm">${esc(r.name)}</span> <span class="rq">· ${esc(r.req)}</span></div><div class="dt">${esc(r.detail)}</div></div>`).join("");
}

function renderEvents() {
  const ev = S.events || [], key = ev.length + (ev[0] ? ev[0].t + ev[0].msg : "");
  if (key === lastEventKey) return; lastEventKey = key;
  $("events").innerHTML = ev.map(e => `<div class="ev ${e.kind}"><time>${e.t}</time><span>${esc(e.msg)}</span></div>`).join("") || '<p class="muted">No events yet.</p>';
}
async function loadLog() {
  try { const r = await (await fetch("/api/logs/" + $("logSel").value)).json(); $("logView").textContent = (r.lines || []).join("\n") || "(empty)"; } catch (e) {}
}

// ------------------------------------------------------------------ actions
function addPlayer() { act("player_add", {player: $("newId").value.trim(), host: $("newHost").value}).then(() => $("newId").value = ""); }
function preset(d, j, l, bw) { $("cDelay").value = d; $("cJitter").value = j; $("cLoss").value = l; $("cBw").value = bw; applyConditions(); }
function applyConditions() { act("conditions", {delay: +$("cDelay").value, jitter: +$("cJitter").value, loss: +$("cLoss").value, bw: +$("cBw").value}); }
function startBg() { act("bg_start", {mbps: +$("bgMbps").value, secs: +$("bgSecs").value}); }
function setGuard() { act("guard", {enabled: $("guardOn").checked, max_pps: +$("guardPps").value}); }

document.addEventListener("click", ev => {
  const b = ev.target.closest("[data-a]"); if (!b) return;
  ev.preventDefault();
  const a = b.dataset.a, id = b.dataset.id;
  if (a === "drive") { driveId = driveId === id ? null : id; $("driveHint").innerHTML = driveId ? `Driving <b>${esc(driveId)}</b> – use the arrow keys` : "Click <b>Drive</b> on a player, then use the arrow keys"; renderArena((S.game || {}).players || []); renderPlayers((S.game || {}).players || []); return; }
  if (a === "unblock_ip") return act("unblock", {ip: b.dataset.ip});
  if (a === "block" || a === "unblock") return act(a, {player: id});
  if (a === "player_remove") return act("player_remove", {player: id});
});

window.addEventListener("keydown", ev => {
  if (!driveId || /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) return;
  const m = {ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1]}[ev.key];
  if (!m) return; ev.preventDefault();
  const now = Date.now(); if (now - lastMove < 90) return; lastMove = now;
  fetch("/api/action", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({action: "player_move", player: driveId, dx: m[0], dy: m[1]})});
});

// ------------------------------------------------------------------ tabs and start
$("scenList").innerHTML = SCENARIOS.map(s => `<label><input type="checkbox" value="${s[0]}" checked> ${esc(s[1])}</label>`).join("");
document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => selectTab(t.dataset.tab)));
function selectTab(name) {
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tabpane").forEach(p => p.classList.toggle("active", p.id === "tab-" + name));
  try { localStorage.setItem("tab", name); } catch (e) {}
  if (name === "exp") loadResults(); if (name === "log") loadLog();
}
try { const t = localStorage.getItem("tab"); if (t && document.getElementById("tab-" + t)) selectTab(t); } catch (e) {}
setInterval(poll, 1000); setInterval(() => { if ($("tab-log").classList.contains("active")) loadLog(); }, 3000);
poll(); loadResults();
