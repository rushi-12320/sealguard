/* SealGuard web UI - plain JavaScript, no build step. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, d = 1) => v === null || v === undefined || Number.isNaN(v) ? "–" : Number(v).toFixed(d);
const pct = (v, d = 1) => v === null || v === undefined ? "–" : `${Number(v).toFixed(d)}%`;
const short = h => h ? `${h.slice(0, 10)}…${h.slice(-6)}` : "–";
const when = ts => ts ? ts.replace("T", " ").replace("+00:00", "").slice(0, 19) : "–";
const sleep = ms => new Promise(r => setTimeout(r, ms));

const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

async function api(path, opts = {}) {
  const init = { method: opts.method || (opts.body ? "POST" : "GET"), headers: { "Content-Type": "application/json" } };
  if (opts.body) init.body = JSON.stringify(opts.body);
  const r = await fetch(path, init);
  if (!r.ok) {
    let m = r.statusText;
    try { const j = await r.json(); m = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* not json */ }
    throw new Error(m);
  }
  return (r.headers.get("content-type") || "").includes("json") ? r.json() : r;
}

function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = `toast on ${kind}`;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.className = "toast"), 4200);
}

function operator() { return ($("#operator").value || "").trim(); }
function needName() {
  const n = operator();
  if (!n) { toast("Enter your name in the Operator field (top right) first — every decision is signed.", "bad"); $("#operator").focus(); }
  return n || null;
}

const badge = (text, cls) => `<span class="badge ${cls}">${esc(text)}</span>`;
const VERDICT_CLS = { PASS: "b-ok", PRODUCT_1: "b-bad", PRODUCT_2: "b-bad", BOTH: "b-bad", RECAPTURE: "b-warn" };
const SEV_CLS = { high: "b-bad", medium: "b-warn", low: "b-info" };
const STATUS_CLS = { pending_approval: "b-warn", critic_blocked: "b-bad", applied: "b-ok", approved: "b-ok", rejected: "b-mute",
  superseded: "b-mute", critic_review: "b-info", open: "b-warn", acknowledged: "b-mute", completed: "b-ok", aborted: "b-bad",
  running: "b-info", match: "b-ok", diverged: "b-bad", allow: "b-ok", block: "b-bad", needs_approval: "b-warn" };
const st = s => badge(String(s || "").replace(/_/g, " "), STATUS_CLS[s] || "b-mute");
const CAUSE_CLS = { CORROBORATED_DEFECT: "b-bad", PROBABLE_DEFECT: "b-bad", BORDERLINE: "b-warn", MISALIGNMENT: "b-vio", IMAGE_QUALITY: "b-info" };

let DOMAIN = null;
let STATUS = null;

/* ---------------------------------------------------------------- navigation */
const NAV = [
  ["Operations", [["", "Command Center", "◎"], ["line", "Line Monitor", "▤"], ["incidents", "Incidents", "⚑", "incidents"]]],
  ["Agents", [["agents", "Agent Ops", "⟳"], ["calibration", "Calibration Lab", "⊿"], ["approvals", "Approvals", "✓", "approvals"]]],
  ["Governance", [["guardrails", "Guardrails", "⛨"], ["ledger", "Audit Ledger", "⛓"]]],
  ["Data", [["labels", "Labelling", "✎", "labels"], ["reports", "Reports", "▦"], ["data", "Data Sources", "⇪"]]],
];

function renderNav() {
  const route = location.hash.replace(/^#\/?/, "").split("?")[0];
  const counts = STATUS ? { incidents: STATUS.open_incidents, approvals: STATUS.pending_proposals, labels: STATUS.unlabelled } : {};
  $("#nav").innerHTML = NAV.map(([g, items]) => `<div class="grp">${g}</div>` + items.map(([r, label, ico, cnt]) =>
    `<a href="#/${r}" class="${route === r ? "on" : ""}"><span class="ico">${ico}</span>${label}${cnt && counts[cnt] ?
      `<span class="badge ${cnt === "incidents" ? "b-bad" : cnt === "approvals" ? "b-warn" : "b-info"}">${counts[cnt]}</span>` : ""}</a>`).join("")).join("");
}

async function refreshStatus() {
  try { STATUS = await api("/api/overview"); } catch (e) { $("#status-strip").innerHTML = `<span class="chip bad"><span class="dot"></span>API offline</span>`; return; }
  const k = STATUS.kpis;
  const chip = (cls, text) => `<span class="chip ${cls}"><span class="dot"></span>${text}</span>`;
  $("#status-strip").innerHTML = [
    chip(k.pass_pct >= 85 ? "ok" : "warn", `Line pass ${pct(k.pass_pct)}`),
    chip(STATUS.policy.agents_paused ? "bad" : "ok", STATUS.policy.agents_paused ? "Agents PAUSED" : "Agents active"),
    chip(STATUS.ledger.ok ? "ok" : "bad", STATUS.ledger.ok ? `Ledger verified · ${STATUS.ledger.count}` : `Ledger BROKEN @${STATUS.ledger.broken_at}`),
    chip("violet", `Thresholds v${k.threshold_version}`),
    ...(STATUS.read_only ? [chip("warn", "Public demo · read-only")] : []),
    chip(STATUS.pending_proposals ? "warn" : "", `${STATUS.pending_proposals} awaiting approval`),
    chip(STATUS.llm ? "ok" : "", STATUS.llm ? "AI summary on" : "AI summary off"),
  ].join("");
  $("#side-foot").innerHTML = `Merkle root<br><code>${esc(short(STATUS.ledger.merkle_root))}</code><br>
    <span class="small">Last run: ${STATUS.last_run ? esc(STATUS.last_run.id) : "none"}</span>`;
  renderNav();
}

/* ---------------------------------------------------------------- live events */
function startEvents() {
  let last = 0;
  const show = e => {
    const txt = {
      agent_step: `<b>${esc(e.agent)}</b> ${esc(e.title)}`,
      run: `<b>orchestrator</b> run ${esc(e.run_id)} ${esc(e.status)}`,
      incident: `<b>incident</b> [${esc(e.severity)}] ${esc(e.title)}`,
      proposal: `<b>proposal</b> ${esc(e.title)}`,
      decision: `<b>human</b> ${esc(e.by)} ${esc(e.decision)} ${esc(e.id)}`,
      captures: `<b>line</b> ${esc(e.count)} new captures`,
      killswitch: `<b>kill switch</b> agents ${e.paused ? "paused" : "resumed"} by ${esc(e.by)}`,
    }[e.kind] || esc(e.kind);
    $("#ticker").innerHTML = `${esc(when(e.ts).slice(11))} · ${txt}`;
  };
  try {
    const es = new EventSource(`/api/events/stream?last_id=${last}`);
    es.onmessage = m => {
      const e = JSON.parse(m.data); last = e.id; show(e);
      if (["run", "decision", "killswitch", "captures"].includes(e.kind)) refreshStatus();
    };
  } catch { /* no SSE */ }
  api("/api/events/recent").then(l => l.length && show(l[l.length - 1])).catch(() => {});
}

/* ---------------------------------------------------------------- drawer */
function openDrawer(html) {
  $("#drawer").innerHTML = `<div class="row" style="justify-content:flex-end"><button class="icon-btn" id="dclose" aria-label="Close">✕</button></div>${html}`;
  $("#drawer").classList.add("on"); $("#drawer-bg").classList.add("on"); $("#drawer").setAttribute("aria-hidden", "false");
  $("#dclose").onclick = closeDrawer;
}
function closeDrawer() { $("#drawer").classList.remove("on"); $("#drawer-bg").classList.remove("on"); $("#drawer").setAttribute("aria-hidden", "true"); }

/* ---------------------------------------------------------------- pouch map */
const SEAL = 24;
const POUCH = { P1: [16, 16, 230, 250], P2: [274, 16, 230, 250] };
function regionRects() {
  const out = {};
  for (const [p, [x, y, w, h]] of Object.entries(POUCH)) {
    out[`${p}_top`] = [x, y, w, SEAL]; out[`${p}_bottom`] = [x, y + h - SEAL, w, SEAL];
    out[`${p}_left`] = [x, y + SEAL, SEAL, h - 2 * SEAL]; out[`${p}_right`] = [x + w - SEAL, y + SEAL, SEAL, h - 2 * SEAL];
  }
  return out;
}
const FILL = { FAIL: "#ef5b5b", NOT_INSPECTABLE: "#e8b339", PASS: "rgba(47,191,155,.45)", GT: "#ef5b5b", none: "#2a333e" };
function pouchSVG(states = {}, { edit = false, marked = [] } = {}) {
  const rects = regionRects();
  const seen = DOMAIN ? DOMAIN.location_uvs : {};
  let s = `<svg class="pouch-map ${edit ? "edit" : ""}" viewBox="0 0 520 282" role="img" aria-label="Seal regions of product 1 and product 2">`;
  for (const [p, [x, y, w, h]] of Object.entries(POUCH)) {
    s += `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="10" fill="#11161c" stroke="#313b47"/>`;
    s += `<rect x="${x + 44}" y="${y + 44}" width="${w - 88}" height="${h - 88}" rx="16" fill="#171d24" stroke="#262e38" stroke-dasharray="4 4"/>`;
    s += `<text x="${x + w / 2}" y="${y + h / 2 + 4}" text-anchor="middle" fill="#5d6773" font-size="13">${p === "P1" ? "Product 1" : "Product 2"}</text>`;
  }
  for (const [loc, [x, y, w, h]] of Object.entries(rects)) {
    const isMarked = marked.includes(loc);
    const fill = isMarked ? FILL.GT : FILL[states[loc] || "none"];
    const vertical = h > w;
    const cx = x + w / 2, cy = y + h / 2;
    const label = (seen[loc] || []).join(" · ");
    s += `<rect class="seal" data-loc="${loc}" x="${x}" y="${y}" width="${w}" height="${h}" rx="4" fill="${fill}" stroke="${isMarked ? "#fff" : "#0d1014"}" stroke-width="${isMarked ? 2 : 1}"><title>${loc} — seen by ${label}</title></rect>`;
    s += `<text x="${cx}" y="${cy + 3.5}" text-anchor="middle" fill="#e6e9ed" font-size="10" pointer-events="none" ${vertical ? `transform="rotate(-90 ${cx} ${cy})"` : ""}>${loc.split("_")[1]} · ${label}</text>`;
  }
  return s + `</svg>`;
}
const RANK = { FAIL: 3, NOT_INSPECTABLE: 2, PASS: 1 };
function regionStates(images) {  // worst result across the cameras that see a region
  const out = {};
  for (const i of images) if ((RANK[i.decision] || 0) > (RANK[out[i.region]] || 0)) out[i.region] = i.decision;
  return out;
}

/* ---------------------------------------------------------------- charts */
function lineChart(points, series, { h = 220, bars = null } = {}) {
  const W = 760, H = h, L = 40, R = 40, T = 10, B = 24;
  const ys = series.flatMap(s => points.map(p => p[s.key])).filter(v => v !== null && v !== undefined);
  if (!points.length || !ys.length) return `<div class="empty">No data in window</div>`;
  const ymax = Math.max(...ys) * 1.08, ymin = 0;
  const x = i => L + (i * (W - L - R)) / Math.max(1, points.length - 1);
  const y = v => T + (H - T - B) * (1 - (v - ymin) / (ymax - ymin || 1));
  let s = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img">`;
  for (let k = 0; k <= 4; k++) {
    const v = ymin + (k * (ymax - ymin)) / 4;
    s += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="#262e38"/><text x="${L - 6}" y="${y(v) + 3}" text-anchor="end">${fmt(v, 0)}</text>`;
  }
  if (bars) {
    const bmax = Math.max(5, ...points.map(p => p[bars.key] || 0));
    const bw = Math.max(2, (W - L - R) / points.length - 2);
    points.forEach((p, i) => {
      const v = p[bars.key] || 0; if (!v) return;
      const bh = (H - T - B) * 0.45 * (v / bmax);
      s += `<rect x="${x(i) - bw / 2}" y="${H - B - bh}" width="${bw}" height="${bh}" fill="${bars.color}" opacity=".55"><title>${bars.name}: ${fmt(v)}%</title></rect>`;
    });
    bars.name = `${bars.name} (bar height, max ${fmt(bmax, 0)}%)`;
  }
  for (const se of series) {
    const pts = points.map((p, i) => p[se.key] === null || p[se.key] === undefined ? null : `${x(i)},${y(p[se.key])}`).filter(Boolean);
    s += `<polyline fill="none" stroke="${se.color}" stroke-width="${se.w || 2}" ${se.dash ? `stroke-dasharray="${se.dash}"` : ""} points="${pts.join(" ")}"/>`;
  }
  const ticks = [0, Math.floor(points.length / 2), points.length - 1];
  for (const i of [...new Set(ticks)]) s += `<text x="${x(i)}" y="${H - 6}" text-anchor="middle">${esc(when(points[i].ts).slice(11, 16))}</text>`;
  return s + `</svg><div class="legend">${series.map(se => `<span><i style="background:${se.color}"></i>${se.name}</span>`).join("")}${bars ? `<span><i style="background:${bars.color}"></i>${bars.name}</span>` : ""}</div>`;
}

function histChart(hist, lines) {
  const W = 760, H = 240, L = 40, R = 10, T = 10, B = 24;
  if (!hist.edges.length) return `<div class="empty">No labelled data for this UV yet</div>`;
  const n = hist.good.length, lo = hist.edges[0], hi = hist.edges[n];
  const sq = v => Math.sqrt(v);
  const ymax = sq(Math.max(1, ...hist.good, ...hist.defect));
  const bw = (W - L - R) / n;
  const x = v => L + ((v - lo) / (hi - lo)) * (W - L - R);
  const y = v => T + (H - T - B) * (1 - sq(v) / ymax);
  let s = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Score histogram">`;
  for (let k = 0; k < n; k++) {
    const gx = L + k * bw;
    if (hist.good[k]) s += `<rect x="${gx + 1}" y="${y(hist.good[k])}" width="${bw - 2}" height="${H - B - y(hist.good[k])}" fill="#2fbf9b" opacity=".6"><title>${hist.good[k]} good · ${hist.edges[k]}–${hist.edges[k + 1]}</title></rect>`;
    if (hist.defect[k]) s += `<rect x="${gx + bw * 0.25}" y="${y(hist.defect[k])}" width="${bw * 0.5}" height="${H - B - y(hist.defect[k])}" fill="#ef5b5b"><title>${hist.defect[k]} defect · ${hist.edges[k]}–${hist.edges[k + 1]}</title></rect>`;
  }
  for (const ln of lines) {
    if (ln.v === null || ln.v === undefined) continue;
    s += `<line x1="${x(ln.v)}" x2="${x(ln.v)}" y1="${T}" y2="${H - B}" stroke="${ln.color}" stroke-width="2" ${ln.dash ? `stroke-dasharray="${ln.dash}"` : ""}/>`;
  }
  for (let k = 0; k <= 5; k++) { const v = lo + (k * (hi - lo)) / 5; s += `<text x="${x(v)}" y="${H - 6}" text-anchor="middle">${fmt(v, 0)}</text>`; }
  return s + `</svg><div class="legend"><span><i style="background:#2fbf9b"></i>good regions (√ scale)</span><span><i style="background:#ef5b5b"></i>labelled defect regions</span>${lines.filter(l => l.v !== null && l.v !== undefined).map(l => `<span><i style="background:${l.color}"></i>${l.name} <b class="mono">${fmt(l.v, 1)}</b></span>`).join("")}</div>`;
}

/* ---------------------------------------------------------------- markdown */
function md(text) {
  const lines = esc(text).split("\n");
  let html = "", inList = false, table = [];
  const inline = s => s.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`(.+?)`/g, "<code>$1</code>");
  const flushTable = () => {
    if (!table.length) return;
    const rows = table.filter(r => !/^\|\s*-/.test(r)).map(r => r.replace(/^\||\|$/g, "").split("|").map(c => c.trim()));
    html += `<div class="tbl-wrap"><table><thead><tr>${rows[0].map(c => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${rows.slice(1).map(r => `<tr>${r.map(c => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
    table = [];
  };
  for (const ln of lines) {
    if (ln.startsWith("|")) { if (inList) { html += "</ul>"; inList = false; } table.push(ln); continue; }
    flushTable();
    if (ln.startsWith("- ")) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${inline(ln.slice(2))}</li>`; continue; }
    if (inList) { html += "</ul>"; inList = false; }
    if (ln.startsWith("## ")) html += `<h2>${inline(ln.slice(3))}</h2>`;
    else if (ln.startsWith("# ")) html += `<h1>${inline(ln.slice(2))}</h1>`;
    else if (ln.trim()) html += `<p>${inline(ln)}</p>`;
  }
  flushTable(); if (inList) html += "</ul>";
  return `<div class="md">${html}</div>`;
}

/* ---------------------------------------------------------------- views */
const V = {};
const view = () => $("#view");
const errCard = e => `<div class="card"><h2 class="bad-t">Could not load</h2><p class="muted">${esc(e.message || e)}</p></div>`;

function stepsHTML(steps) {
  return `<div class="steps">${steps.map(s => `<div class="step ${esc(s.status)}"><div class="ag">${esc(s.agent)}</div>
    <div>${st(s.status === "ok" ? "ok" : s.status)}</div><div>${esc(s.title)}
    ${s.citations && s.citations.length ? `<div class="small muted">cites: ${s.citations.slice(0, 8).map(c => `<a href="#" data-cap="${esc(c)}">${esc(c)}</a>`).join(", ")}${s.citations.length > 8 ? ` +${s.citations.length - 8}` : ""}</div>` : ""}
    <details><summary>details</summary><pre>${esc(JSON.stringify(s.detail, null, 2))}</pre></details></div></div>`).join("")}</div>`;
}

const AGENTS = ["planner", "triage", "drift", "calibration", "critic", "reporter", "auditor"];
const pipelineHTML = (done = [], on = null) => `<div class="pipeline">${AGENTS.map((a, i) =>
  `${i ? '<span class="arrow">→</span>' : ""}<span class="pipe ${done.includes(a) ? "done" : ""} ${on === a ? "on" : ""}">${a}</span>`).join("")}</div>`;

/* Command Center */
V[""] = async () => {
  const o = STATUS || await api("/api/overview");
  const k = o.kpis;
  const rejects = (k.verdicts.PRODUCT_1 || 0) + (k.verdicts.PRODUCT_2 || 0) + (k.verdicts.BOTH || 0);
  const worst = Object.entries(k.per_uv).filter(([, v]) => v.headroom_pct !== null).sort((a, b) => a[1].headroom_pct - b[1].headroom_pct)[0];
  const sel = store.get("trendUV", worst ? worst[0] : "UV1");
  const [incs, run] = await Promise.all([api("/api/incidents"), o.last_run ? api(`/api/runs/${o.last_run.id}`) : null]);
  const kpi = (l, v, n, cls = "") => `<div class="card kpi"><div class="lbl">${l}</div><div class="val ${cls}">${v}</div><div class="note">${n}</div></div>`;
  view().innerHTML = `<h1>Command Center</h1><p class="sub">Last ${esc(o.window.since ? Math.round((new Date(o.window.until) - new Date(o.window.since)) / 36e5) : "")} h of the seal line · ${esc(when(o.window.until))} UTC</p>
  <div class="grid g6">
    ${kpi("Captures", k.captures, `${k.unlabelled} unlabelled`)}
    ${kpi("Pass rate", pct(k.pass_pct), "of inspectable captures", k.pass_pct >= 85 ? "ok-t" : "warn-t")}
    ${kpi("Rejects", rejects, `P1 ${k.verdicts.PRODUCT_1 || 0} · P2 ${k.verdicts.PRODUCT_2 || 0} · both ${k.verdicts.BOTH || 0}`, "bad-t")}
    ${kpi("Recapture", pct(k.recapture_pct), `${k.verdicts.RECAPTURE || 0} not inspectable`, k.recapture_pct > 3 ? "warn-t" : "")}
    ${kpi("Open incidents", o.open_incidents, "raised by agents", o.open_incidents ? "bad-t" : "ok-t")}
    ${kpi("Awaiting approval", o.pending_proposals, `${o.blocked_proposals} blocked by Critic`, o.pending_proposals ? "warn-t" : "")}
  </div>
  <h3 class="mt2">Camera health</h3>
  <div class="grid g5">${DOMAIN.uvs.map(uv => {
    const u = k.per_uv[uv]; const hr = u.headroom_pct;
    const col = hr === null ? "var(--muted)" : hr < 15 ? "var(--bad)" : hr < 30 ? "var(--warn)" : "var(--ok)";
    return `<a class="card tight" href="#/calibration?uv=${uv}" style="text-decoration:none;color:inherit">
      <div class="row" style="justify-content:space-between"><b>${uv}</b><span class="mono muted">thr ${fmt(u.threshold, 1)}</span></div>
      <div class="small muted mt">headroom</div><div style="font-size:20px;font-weight:650;color:${col}">${pct(hr)}</div>
      <div class="gauge"><span style="width:${Math.max(0, Math.min(100, hr || 0))}%;background:${col}"></span></div>
      <div class="small muted mt">good μ ${fmt(u.good_mean_recent)} · p99 ${fmt(u.good_p99_recent)}<br>fail ${pct(u.fail_pct)} · over-exposed ${pct(u.overexposed_pct)}</div></a>`;
  }).join("")}</div>
  <div class="grid g3 mt">
    <div class="card span2"><div class="row" style="justify-content:space-between"><h2>Good-score trend vs threshold</h2>
      <select id="tuv">${DOMAIN.uvs.map(u => `<option ${u === sel ? "selected" : ""}>${u}</option>`).join("")}</select></div><div id="trend"></div></div>
    <div class="card"><h2>Latest agent run</h2>${run ? `${pipelineHTML(AGENTS)}
      <p class="small mt"><span class="mono">${esc(run.id)}</span> · ${st(run.status)} ${(run.summary && run.summary.audit) ? badge(run.summary.audit.verdict, run.summary.audit.verdict === "AUDIT_PASSED" ? "b-ok" : "b-bad") : ""}</p>
      <p class="small muted">${esc(when(run.started_at))} · ${run.steps.length} steps<br>in <span class="hash">${esc(short(run.input_hash))}</span><br>out <span class="hash">${esc(short(run.output_hash))}</span></p>` : '<p class="muted">No run yet.</p>'}
      <div class="row mt"><button class="btn primary" id="runnow">Run agents now</button><a href="#/agents">Agent Ops →</a></div></div>
  </div>
  <div class="card mt"><div class="row" style="justify-content:space-between"><h2>Open incidents</h2><a href="#/incidents">All incidents →</a></div>
    ${incs.filter(i => i.status === "open").slice(0, 6).map(i => `<div class="find ${i.severity === "high" ? "fail" : "warn"}">${badge(i.severity, SEV_CLS[i.severity])} ${esc(i.title)} <span class="muted small">· ${i.evidence.length} captures · ${esc(when(i.created_at))}</span></div>`).join("") || '<p class="muted">Nothing open.</p>'}</div>`;
  const drawTrend = async uv => {
    store.set("trendUV", uv);
    const pts = await api(`/api/trend/${uv}`);
    $("#trend").innerHTML = lineChart(pts, [{ key: "good_mean", name: "good mean", color: "#2fbf9b" }, { key: "good_p95", name: "good p95", color: "#5aa9e6", dash: "4 3" },
      { key: "threshold", name: "threshold", color: "#ef5b5b", w: 2.5 }], { bars: { key: "overexposed_pct", name: "over-exposed %", color: "#e8b339" } });
  };
  $("#tuv").onchange = e => drawTrend(e.target.value);
  $("#runnow").onclick = () => { location.hash = "#/agents?run=1"; };
  drawTrend(sel);
};

/* Line Monitor */
V.line = async () => {
  let offset = 0, auto = null;
  view().innerHTML = `<h1>Line Monitor</h1><p class="sub">Every capture (two pouches) with the result of each UV camera. Click a row for region scores, triage and ground truth.</p>
  <div class="card"><div class="row"><select id="fv"><option value="">All verdicts</option>${["PASS", "PRODUCT_1", "PRODUCT_2", "BOTH", "RECAPTURE"].map(v => `<option>${v}</option>`).join("")}</select>
    <label class="small"><input type="checkbox" id="fu"> unlabelled only</label><input id="fq" placeholder="search capture id">
    <span style="flex:1"></span><button class="btn" id="tick">Simulate 10 captures</button><button class="btn" id="auto">▶ Live feed</button></div>
    <div class="tbl-wrap mt"><table><thead><tr><th>Capture</th><th>Time (UTC)</th><th>Verdict</th>${DOMAIN.uvs.map(u => `<th>${u}</th>`).join("")}<th>Ground truth</th></tr></thead><tbody id="rows"></tbody></table></div>
    <div class="row mt"><span class="muted small" id="cnt"></span><span style="flex:1"></span><button class="btn sm" id="more">Load more</button></div></div>`;
  const load = async (append = false) => {
    const qs = new URLSearchParams({ limit: 60, offset });
    if ($("#fv").value) qs.set("verdict", $("#fv").value);
    if ($("#fu").checked) qs.set("unlabelled", "true");
    if ($("#fq").value.trim()) qs.set("q", $("#fq").value.trim());
    const d = await api(`/api/captures?${qs}`);
    const html = d.items.map(c => `<tr class="click" data-cap="${esc(c.id)}"><td class="mono">${esc(c.id)}</td><td class="small">${esc(when(c.ts))}</td>
      <td>${badge(c.verdict, VERDICT_CLS[c.verdict])}</td>${DOMAIN.uvs.map(u => `<td><span class="cell c-${c.uv[u] || "none"}" title="${u}: ${c.uv[u] || "–"}"></span></td>`).join("")}
      <td class="small">${c.gt_source ? esc(DOMAIN.defect_labels[c.defect_type] || c.defect_type) + (c.gt_locations ? ` <span class="muted">${esc(c.gt_locations)}</span>` : "") : '<span class="badge b-info">unlabelled</span>'}</td></tr>`).join("");
    $("#rows").innerHTML = append ? $("#rows").innerHTML + html : (html || `<tr><td colspan="9" class="empty">No captures</td></tr>`);
    $("#cnt").textContent = `${Math.min(offset + d.items.length, d.total)} of ${d.total}`;
    $("#more").disabled = offset + d.items.length >= d.total;
  };
  const reset = () => { offset = 0; load(); };
  $("#fv").onchange = reset; $("#fu").onchange = reset;
  $("#fq").oninput = () => { clearTimeout(V._q); V._q = setTimeout(reset, 300); };
  $("#more").onclick = () => { offset += 60; load(true); };
  $("#tick").onclick = async () => { const r = await api("/api/sim/tick", { body: { n: 10 } }); toast(`+${r.generated} captures`, "ok"); reset(); };
  $("#auto").onclick = () => {
    if (auto) { clearInterval(auto); auto = null; $("#auto").textContent = "▶ Live feed"; return; }
    $("#auto").textContent = "■ Stop feed";
    auto = setInterval(async () => { if (!$("#auto")) { clearInterval(auto); return; } await api("/api/sim/tick", { body: { n: 2 } }); reset(); }, 4000);
  };
  V.cleanup = () => auto && clearInterval(auto);
  await load();
};

async function captureDrawer(id) {
  try {
    const d = await api(`/api/captures/${encodeURIComponent(id)}`);
    const c = d.capture, states = regionStates(d.images);
    const t = d.triage;
    const locs = (c.gt_locations || "").split(",").filter(Boolean);
    openDrawer(`<h1 class="mono">${esc(c.id)}</h1><p class="sub">${esc(when(c.ts))} UTC · ${esc(c.source)} · ${badge(c.verdict, VERDICT_CLS[c.verdict])}</p>
      <h3>Model result per seal region</h3>${pouchSVG(states)}
      <div class="legend mt"><span><i style="background:${FILL.PASS}"></i>pass</span><span><i style="background:${FILL.FAIL}"></i>fail</span><span><i style="background:${FILL.NOT_INSPECTABLE}"></i>not inspectable</span></div>
      ${t ? `<div class="card tight mt"><h3>Triage agent</h3>${badge(t.cause.replace(/_/g, " "), CAUSE_CLS[t.cause])} <span class="small">${esc(t.reason)}</span></div>` : ""}
      <div class="card tight mt"><h3>Ground truth</h3>${c.gt_source ? `${esc(DOMAIN.defect_labels[c.defect_type] || c.defect_type)} ${locs.length ? `at ${esc(locs.join(", "))}` : ""} <span class="muted small">(${esc(c.gt_source)}${c.gt_by ? " · " + esc(c.gt_by) : ""})</span>
        <div class="row mt">${Object.entries(d.uv_ground_truth).map(([u, v]) => `<span>${u} ${badge(v, v === "FAIL" ? "b-bad" : "b-ok")}</span>`).join("")}</div>` : '<span class="muted">Not labelled yet.</span>'}
        <div class="mt"><a href="#/labels?id=${encodeURIComponent(c.id)}" id="tolabel">${c.gt_source ? "Relabel" : "Label"} this capture →</a></div></div>
      <h3 class="mt">Images</h3><div class="tbl-wrap"><table><thead><tr><th>UV</th><th>Region</th><th class="num">Score</th><th class="num">Thr</th><th class="num">×thr</th><th>Decision</th><th>Exposure</th></tr></thead>
      <tbody>${d.images.map(i => `<tr><td>${i.uv}</td><td>${esc(i.region)}</td><td class="num">${fmt(i.score, 1)}</td><td class="num">${fmt(i.threshold, 1)}</td><td class="num">${i.threshold ? fmt(i.score / i.threshold, 2) : "–"}</td>
        <td>${badge(i.decision, i.decision === "FAIL" ? "b-bad" : i.decision === "PASS" ? "b-ok" : "b-warn")}</td><td class="small ${i.exposure !== "OK" ? "warn-t" : "muted"}">${esc(i.exposure)}${i.sat_pct !== null ? ` · ${fmt(i.sat_pct)}% sat` : ""}</td></tr>`).join("")}</tbody></table></div>`);
    $("#tolabel").onclick = closeDrawer;
  } catch (e) { toast(e.message, "bad"); }
}

/* Incidents */
V.incidents = async () => {
  const list = await api("/api/incidents");
  view().innerHTML = `<h1>Incidents</h1><p class="sub">Raised by the Triage and Drift agents. Each one cites the captures it is based on. Acknowledging is signed with your name.</p>
  <div class="card tbl-wrap"><table><thead><tr><th>Severity</th><th>Title</th><th>UV</th><th class="num">Evidence</th><th>Raised</th><th>Status</th><th></th></tr></thead><tbody>
  ${list.map(i => `<tr class="click" data-inc="${esc(i.id)}"><td>${badge(i.severity, SEV_CLS[i.severity])}</td><td>${esc(i.title)}<div class="small muted">${esc(i.kind)} · ${esc(i.id)}</div></td><td>${esc(i.uv || "–")}</td>
    <td class="num">${i.evidence.length}</td><td class="small">${esc(when(i.created_at))}</td><td>${st(i.status)}</td>
    <td>${i.status === "open" ? `<button class="btn sm" data-ack="${esc(i.id)}">Acknowledge</button>` : ""}</td></tr>`).join("") || `<tr><td colspan="7" class="empty">No incidents</td></tr>`}</tbody></table></div>`;
  $$("[data-ack]").forEach(b => b.onclick = async ev => {
    ev.stopPropagation(); const by = needName(); if (!by) return;
    await api(`/api/incidents/${b.dataset.ack}/ack`, { body: { by } }); toast("Acknowledged", "ok"); refreshStatus(); V.incidents();
  });
  $$("[data-inc]").forEach(r => r.onclick = () => {
    const i = list.find(x => x.id === r.dataset.inc);
    openDrawer(`<h1>${esc(i.title)}</h1><p class="sub">${badge(i.severity, SEV_CLS[i.severity])} ${esc(i.kind)} · ${esc(i.id)} · run ${esc(i.run_id)}</p>
      ${i.detail.action ? `<div class="find warn"><b>Recommended action:</b> ${esc(i.detail.action)}</div>` : ""}
      <h3 class="mt">Evidence (${i.evidence.length} captures)</h3><div class="small">${i.evidence.map(c => `<a href="#" data-cap="${esc(c)}">${esc(c)}</a>`).join(", ")}</div>
      <h3 class="mt">Detail</h3><pre class="mono" style="white-space:pre-wrap">${esc(JSON.stringify(i.detail, null, 2))}</pre>`);
  });
};

/* Agent Ops */
V.agents = async (params) => {
  view().innerHTML = `<h1>Agent Ops</h1><p class="sub">Seven deterministic agents. They read data and propose; the guardrails decide what they may do, and people approve what changes production. Every run is hashed so it can be replayed.</p>
  <div class="card"><div class="row" style="justify-content:space-between">${pipelineHTML()}<div class="row">
    <label class="small muted">window <input id="wh" type="number" min="1" max="168" value="12" style="width:70px"> h</label>
    <button class="btn primary" id="go">Run agent cycle</button></div></div>
    <div id="live" class="mt"><p class="muted small">Planner builds the plan → Triage classifies every reject → Drift watches score drift and exposure → Calibration proposes thresholds → Critic challenges proposals → Reporter writes the shift report → Auditor verifies ledger, citations and approvals.</p></div></div>
  <div class="card mt"><h2>Run history</h2><div class="tbl-wrap"><table><thead><tr><th>Run</th><th>Kind</th><th>Started</th><th>Status</th><th>Audit / replay</th><th>Input hash</th><th>Output hash</th><th></th></tr></thead><tbody id="runs"></tbody></table></div></div>`;
  const loadRuns = async () => {
    const rs = await api("/api/runs");
    $("#runs").innerHTML = rs.map(r => `<tr><td class="mono">${esc(r.id)}${r.replay_of ? `<div class="small muted">of ${esc(r.replay_of)}</div>` : ""}</td><td>${esc(r.kind)}</td><td class="small">${esc(when(r.started_at))}</td>
      <td>${st(r.status)}</td><td>${r.audit ? badge(r.audit, r.audit === "AUDIT_PASSED" ? "b-ok" : "b-bad") : r.match !== null && r.match !== undefined ? badge(r.match ? "identical" : "diverged", r.match ? "b-ok" : "b-bad") : ""}</td>
      <td class="hash">${esc(short(r.input_hash))}</td><td class="hash">${esc(short(r.output_hash))}</td>
      <td class="row" style="gap:6px;flex-wrap:nowrap"><button class="btn sm" data-view="${esc(r.id)}">Steps</button>${r.kind === "cycle" && r.output_hash ? `<button class="btn sm" data-replay="${esc(r.id)}">Replay</button>` : ""}</td></tr>`).join("") || `<tr><td colspan="8" class="empty">No runs</td></tr>`;
    $$("[data-view]").forEach(b => b.onclick = async () => {
      const r = await api(`/api/runs/${b.dataset.view}`);
      openDrawer(`<h1 class="mono">${esc(r.id)}</h1><p class="sub">${esc(r.kind)} · ${st(r.status)} · ${esc(when(r.started_at))}<br><span class="hash">input ${esc(r.input_hash)}<br>output ${esc(r.output_hash)}</span></p>${r.kind === "replay" && r.summary ? `<div class="find ${r.summary.match ? "pass" : "fail"}">${esc(r.summary.explanation)}</div>` : ""}${stepsHTML(r.steps || [])}`);
    });
    $$("[data-replay]").forEach(b => b.onclick = async () => {
      b.disabled = true; b.textContent = "Replaying…";
      try {
        const r = await api(`/api/runs/${b.dataset.replay}/replay`, { method: "POST" });
        openDrawer(`<h1>Replay ${r.match ? '<span class="ok-t">identical</span>' : '<span class="bad-t">diverged</span>'}</h1>
          <p class="sub">${esc(r.explanation)}</p><div class="card tight"><table><tbody>
          <tr><td>Original run</td><td class="mono">${esc(r.replay_of)}</td></tr><tr><td>Replay run</td><td class="mono">${esc(r.replay_id)}</td></tr>
          <tr><td>Input hash</td><td class="hash">${esc(r.original_input_hash)}<br>${esc(r.replay_input_hash)} ${r.input_match ? badge("same data", "b-ok") : badge("data changed", "b-warn")}</td></tr>
          <tr><td>Output hash</td><td class="hash">${esc(r.original_output_hash)}<br>${esc(r.replay_output_hash)} ${r.match ? badge("same decisions", "b-ok") : badge("different", "b-bad")}</td></tr>
          ${r.differing_sections.length ? `<tr><td>Differs in</td><td>${esc(r.differing_sections.join(", "))}</td></tr>` : ""}</tbody></table></div>`);
        loadRuns();
      } catch (e) { toast(e.message, "bad"); b.disabled = false; b.textContent = "Replay"; }
    });
  };
  $("#go").onclick = async () => {
    const btn = $("#go"); btn.disabled = true; btn.textContent = "Running…";
    $("#live").innerHTML = pipelineHTML([], "planner");
    try {
      const r = await api("/api/agents/run", { body: { window_hours: Number($("#wh").value) || 12 } });
      const done = [];
      $("#live").innerHTML = `<div id="pl"></div><div class="mt" id="ls"></div>`;
      for (let k = 0; k < r.steps.length; k++) {
        const s = r.steps[k];
        if (!done.includes(s.agent) && AGENTS.includes(s.agent)) { const prev = AGENTS.indexOf(s.agent); AGENTS.slice(0, prev).forEach(a => !done.includes(a) && done.push(a)); }
        $("#pl").innerHTML = pipelineHTML(done, s.agent);
        $("#ls").innerHTML = stepsHTML(r.steps.slice(0, k + 1));
        await sleep(90);
      }
      $("#pl").innerHTML = pipelineHTML(AGENTS);
      $("#ls").insertAdjacentHTML("afterbegin", `<div class="find ${r.status === "completed" ? "pass" : "fail"} mb"><b>${esc(r.run_id)}</b> ${st(r.status)} ${r.audit ? badge(r.audit.verdict, r.audit.verdict === "AUDIT_PASSED" ? "b-ok" : "b-bad") : ""}
        · output hash <span class="hash">${esc(short(r.output_hash))}</span> · <a href="#/approvals">review proposals →</a></div>`);
      refreshStatus(); loadRuns();
    } catch (e) { $("#live").innerHTML = errCard(e); }
    btn.disabled = false; btn.textContent = "Run agent cycle";
  };
  await loadRuns();
  if (params.get("run")) { history.replaceState(null, "", "#/agents"); $("#go").click(); }
};

/* Calibration Lab */
V.calibration = async (params) => {
  const uv = params.get("uv") || store.get("calUV", "UV1");
  store.set("calUV", uv);
  const d = await api(`/api/calibration/${uv}`);
  const rec = d.recommendation;
  const maxv = d.histogram.edges.length ? d.histogram.edges[d.histogram.edges.length - 1] : (d.current * 2);
  view().innerHTML = `<h1>Calibration Lab</h1><p class="sub">Labelled good and defect region scores per UV. Drag the threshold to see exactly which pouches would be falsely rejected or would escape. Nothing here changes production — threshold changes go through the agents, the Critic and a human approval.</p>
  <div class="row mb">${DOMAIN.uvs.map(u => `<a class="btn ${u === uv ? "primary" : ""}" href="#/calibration?uv=${u}" style="text-decoration:none">${u}</a>`).join("")}</div>
  <div class="split"><div class="card"><h2>${uv} score distribution</h2><div id="hist"></div>
    <div class="mt"><label class="fld">What-if threshold: <b id="tv" class="mono" style="color:var(--text)"></b><input type="range" id="thr" min="1" max="${Math.ceil(maxv)}" step="0.5"></label></div></div>
    <div><div class="card"><h2>What-if result</h2><div id="wi"></div></div>
    <div class="card mt"><h2>Recommendation</h2>${rec ? `<table><tbody>
      <tr><td>Current threshold</td><td class="num mono">${fmt(d.current, 2)}</td></tr><tr><td>Recommended</td><td class="num mono amber">${fmt(rec.threshold, 2)}</td></tr>
      <tr><td>Method</td><td class="small">${esc(rec.method)}</td></tr><tr><td>Labelled good / defect regions</td><td class="num">${rec.n_good} / ${rec.n_defect}</td></tr>
      <tr><td>Good p99 / p99.5 / max</td><td class="num mono">${fmt(rec.good_p99, 1)} / ${fmt(rec.good_p995, 1)} / ${fmt(rec.good_max, 1)}</td></tr>
      <tr><td>Lowest defect score</td><td class="num mono">${fmt(rec.defect_min, 1)}</td></tr><tr><td>AUROC</td><td class="num mono">${rec.auroc ?? "–"}</td></tr></tbody></table>
      <p class="small muted mt">The Calibration agent caps each step at the policy limit and the Critic blocks any proposal that lets a labelled defect pass or hides camera drift.</p>` : '<p class="muted">Not enough labelled data.</p>'}</div></div></div>`;
  let cur = d.whatif.threshold;
  const drawHist = (wi) => $("#hist").innerHTML = histChart(d.histogram, [{ v: d.current, name: "current", color: "#8b95a1" },
    { v: rec && rec.threshold, name: "recommended", color: "#f2a93b", dash: "5 4", row: 1 }, { v: wi, name: "what-if", color: "#e6e9ed", row: 2 }]);
  const showWI = (w) => {
    const c0 = d.whatif_current;
    const delta = (a, b, goodDown = true) => { const x = a - b; if (!x) return ""; const good = goodDown ? x < 0 : x > 0; return ` <span class="${good ? "ok-t" : "bad-t"}">(${x > 0 ? "+" : ""}${x} vs current)</span>`; };
    $("#wi").innerHTML = `<div class="grid g2"><div class="kpi"><div class="lbl">False rejects (good regions failing)</div><div class="val ${w.false_rejects ? "warn-t" : "ok-t"}">${w.false_rejects}</div><div class="note">${pct(w.false_reject_pct, 2)} of ${w.n_good}${delta(w.false_rejects, c0.false_rejects)}</div></div>
      <div class="kpi"><div class="lbl">Escapes (labelled defects passing)</div><div class="val ${w.escapes ? "bad-t" : "ok-t"}">${w.escapes}</div><div class="note">detection ${pct(w.detection_pct, 1)} of ${w.n_defect}${delta(w.escapes, c0.escapes)}</div></div></div>
      ${w.escape_examples.length ? `<div class="find fail mt small"><b>Would escape:</b> ${w.escape_examples.map(x => `<a href="#" data-cap="${esc(x.split("/")[0])}">${esc(x)}</a>`).join(", ")}</div>` : ""}
      ${w.false_reject_examples.length ? `<div class="find warn mt small"><b>Would be falsely rejected:</b> ${w.false_reject_examples.slice(0, 12).map(x => `<a href="#" data-cap="${esc(x.split("/")[0])}">${esc(x)}</a>`).join(", ")}${w.false_rejects > 12 ? ` +${w.false_rejects - 12}` : ""}</div>` : ""}`;
  };
  $("#thr").value = cur; $("#tv").textContent = fmt(cur, 1); drawHist(cur); showWI(d.whatif);
  $("#thr").oninput = e => {
    const v = Number(e.target.value); $("#tv").textContent = fmt(v, 1); drawHist(v);
    clearTimeout(V._c); V._c = setTimeout(async () => { const r = await api(`/api/calibration/${uv}?threshold=${v}`); showWI(r.whatif); }, 120);
  };
};

/* Approvals */
V.approvals = async () => {
  const [ps, vers] = await Promise.all([api("/api/proposals"), api("/api/thresholds")]);
  const open = ps.filter(p => ["pending_approval", "critic_blocked", "critic_review"].includes(p.status));
  const closed = ps.filter(p => !open.includes(p));
  const card = p => {
    const pl = p.payload || {};
    const th = p.kind === "threshold_change";
    return `<div class="card mt"><div class="row" style="justify-content:space-between"><div><b>${esc(p.title)}</b><div class="small muted">${esc(p.id)} · ${esc(p.kind.replace("_", " "))} · ${esc(when(p.created_at))} · run ${esc(p.run_id)}</div></div>${st(p.status)}</div>
      ${th ? `<div class="grid g4 mt"><div class="kpi"><div class="lbl">Threshold</div><div class="val mono" style="font-size:18px">${fmt(pl.current, 2)} → ${fmt(pl.proposed, 2)}</div><div class="note">${pl.change_pct > 0 ? "+" : ""}${fmt(pl.change_pct, 1)}%${pl.clamped ? ` · capped (wanted ${fmt(pl.recommended, 2)})` : ""}</div></div>
        <div class="kpi"><div class="lbl">Escapes now → after</div><div class="val" style="font-size:18px">${pl.whatif_current.escapes} → ${pl.whatif_proposed.escapes}</div><div class="note">labelled defect regions</div></div>
        <div class="kpi"><div class="lbl">False rejects now → after</div><div class="val" style="font-size:18px">${pl.whatif_current.false_rejects} → ${pl.whatif_proposed.false_rejects}</div><div class="note">${pct(pl.whatif_proposed.false_reject_pct, 2)} after</div></div>
        <div class="kpi"><div class="lbl">Evidence</div><div class="val" style="font-size:18px">${p.evidence.length}</div><div class="note">captures cited · <a href="#/calibration?uv=${esc(p.uv)}">open lab</a></div></div></div>`
        : `<p class="small">${esc(pl.reason || "")} · ${p.evidence.length} captures cited</p>`}
      <h3 class="mt">Critic findings</h3>${(p.critic || []).map(f => `<div class="find ${f.result}">${badge(f.result, f.result === "pass" ? "b-ok" : f.result === "warn" ? "b-warn" : "b-bad")} <b>${esc(f.check)}</b> <span class="small muted">${esc(f.detail)}</span></div>`).join("") || '<p class="muted small">Not assessed yet.</p>'}
      ${p.status === "pending_approval" || p.status === "critic_blocked" ? `<div class="row mt"><input data-note="${esc(p.id)}" placeholder="decision note (optional)" style="flex:1;min-width:200px">
        ${p.status === "pending_approval" ? `<button class="btn good" data-dec="approve" data-id="${esc(p.id)}">Approve${th ? " & apply" : ""}</button>` : `<span class="small bad-t">Blocked by the Critic — it cannot be approved.</span>`}
        <button class="btn danger" data-dec="reject" data-id="${esc(p.id)}">Reject</button></div>` : ""}</div>`;
  };
  view().innerHTML = `<h1>Approvals</h1><p class="sub">Agents can only propose. A named person approves, the guardrails re-check at apply time, and every decision is sealed in the ledger.</p>
  <h2>Open (${open.length})</h2>${open.map(card).join("") || '<div class="card empty">Nothing waiting. Run the agents to get fresh proposals.</div>'}
  <div class="card mt2"><h2>Threshold versions</h2><div class="tbl-wrap"><table><thead><tr><th>Version</th><th>Created</th><th>By</th>${DOMAIN.uvs.map(u => `<th class="num">${u}</th>`).join("")}<th>Change</th><th></th></tr></thead><tbody>
    ${vers.map((v, i) => `<tr><td>v${v.version} ${i === 0 ? badge("active", "b-ok") : ""}</td><td class="small">${esc(when(v.created_at))}</td><td>${esc(v.created_by)}</td>${DOMAIN.uvs.map(u => `<td class="num mono">${fmt(v.values[u], 2)}</td>`).join("")}
      <td class="small">${v.changes.map(c => `${esc(c.uv)} ${esc(c.proposal_id || c.note)}`).join("<br>")}</td><td>${i > 0 ? `<button class="btn sm" data-rb="${v.version}">Roll back to v${v.version}</button>` : ""}</td></tr>`).join("")}</tbody></table></div></div>
  <div class="card mt"><h2>Decided</h2><div class="tbl-wrap"><table><thead><tr><th>Proposal</th><th>Status</th><th>By</th><th>When</th><th>Note</th></tr></thead><tbody>
    ${closed.map(p => `<tr><td>${esc(p.title)}<div class="small muted">${esc(p.id)}</div></td><td>${st(p.status)}</td><td>${esc(p.decided_by || "–")}</td><td class="small">${esc(when(p.decided_at))}</td><td class="small">${esc(p.decision_note || "")}</td></tr>`).join("") || `<tr><td colspan="5" class="empty">None yet</td></tr>`}</tbody></table></div></div>`;
  $$("[data-dec]").forEach(b => b.onclick = async () => {
    const by = needName(); if (!by) return;
    const note = ($(`[data-note="${b.dataset.id}"]`) || {}).value || "";
    try {
      const r = await api(`/api/proposals/${b.dataset.id}/decision`, { body: { decision: b.dataset.dec, by, note } });
      if (r.ok) toast(r.status === "applied" ? `Applied — thresholds now v${r.version}` : `Proposal ${r.status}${r.work_order ? " · " + r.work_order : ""}`, "ok");
      else toast(`Blocked by guardrails: ${(r.reasons || []).join("; ")}`, "bad");
    } catch (e) { toast(e.message, "bad"); }
    refreshStatus(); V.approvals();
  });
  $$("[data-rb]").forEach(b => b.onclick = async () => {
    const by = needName(); if (!by) return;
    try { const r = await api("/api/thresholds/rollback", { body: { version: Number(b.dataset.rb), by, note: "manual rollback" } });
      toast(r.ok ? `Rolled back — now v${r.version}` : `Blocked: ${(r.reasons || []).join("; ")}`, r.ok ? "ok" : "bad"); } catch (e) { toast(e.message, "bad"); }
    refreshStatus(); V.approvals();
  });
};

/* Guardrails */
V.guardrails = async () => {
  const [{ policy }, log] = await Promise.all([api("/api/policy"), api("/api/guard-log?limit=150")]);
  const num = [["max_threshold_change_pct", "Max threshold step (%)"], ["min_good_samples", "Min labelled good regions"], ["min_defect_samples", "Min labelled defect regions"],
    ["max_escapes_allowed", "Max escapes allowed"], ["max_false_reject_pct", "Max false-reject rate (%)"], ["max_proposals_per_uv_per_day", "Max proposals / UV / 24 h"],
    ["max_steps_per_run", "Step budget per run"], ["data_window_hours", "Agent data window (h)"], ["exposure_alert_pct", "Over-exposure alert (%)"],
    ["headroom_alert_pct", "Headroom alert (%)"], ["drift_sigma_alert", "Drift alert (σ)"]];
  const tierCls = { auto: "b-ok", human: "b-info", human_approval: "b-warn", admin: "b-vio", forbidden: "b-bad" };
  view().innerHTML = `<h1>Guardrails</h1><p class="sub">Rules enforced in code on every action — agents cannot talk their way past them. Unknown actions are denied by default; forbidden actions cannot be enabled from here.</p>
  <div class="grid g3"><div class="card"><h2>Kill switch</h2><p class="small muted">Pauses every agent write immediately (reads still allowed). Signed and sealed in the ledger.</p>
    <div class="kpi"><div class="val ${policy.agents_paused ? "bad-t" : "ok-t"}">${policy.agents_paused ? "PAUSED" : "ACTIVE"}</div></div>
    <button class="btn ${policy.agents_paused ? "good" : "danger"} mt" id="kill">${policy.agents_paused ? "Resume agents" : "Pause all agents"}</button></div>
    <div class="card span2"><div class="row" style="justify-content:space-between"><h2>Red-team suite</h2><button class="btn primary" id="rt">Run attacks</button></div>
      <p class="small muted">17 attacks: self-approval, forbidden actions, oversized steps, drift masking, prompt injection in data, runaway loops, ledger tampering… Runs against the policy engine without touching production data.</p><div id="rtres"></div></div></div>
  <div class="grid g2 mt"><div class="card"><h2>Policy limits</h2><div class="grid g2">${num.map(([k, l]) => `<label class="fld">${l}<input type="number" step="any" data-pk="${k}" value="${esc(policy[k])}"></label>`).join("")}</div>
    <button class="btn primary mt" id="save">Save policy (admin)</button></div>
    <div class="card"><h2>Permission tiers</h2><table><tbody>${Object.entries(policy.permissions).map(([a, t]) => `<tr><td class="mono">${esc(a)}</td><td>${badge(t.replace("_", " "), tierCls[t] || "b-mute")}</td></tr>`).join("")}</tbody></table>
      <p class="small muted mt">auto = agent may do it · human = only a person · human approval = agent proposes, named person approves · admin = policy owner · forbidden = never.</p></div></div>
  <div class="card mt"><div class="row" style="justify-content:space-between"><h2>Guard decisions</h2><select id="gf"><option value="">all</option><option>block</option><option>needs_approval</option><option>allow</option></select></div>
    <div class="tbl-wrap"><table><thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Target</th><th>Decision</th><th>Reason</th></tr></thead><tbody id="gl"></tbody></table></div></div>`;
  const drawLog = f => $("#gl").innerHTML = log.filter(r => !f || r.decision === f).slice(0, 120).map(r => `<tr><td class="small">${esc(when(r.ts))}</td><td class="mono">${esc(r.actor)}</td><td class="mono">${esc(r.action)}</td><td class="small">${esc(r.target)}</td><td>${st(r.decision)}</td><td class="small muted">${esc(r.reasons.join("; "))}</td></tr>`).join("") || `<tr><td colspan="6" class="empty">No decisions</td></tr>`;
  drawLog(""); $("#gf").onchange = e => drawLog(e.target.value);
  $("#kill").onclick = async () => {
    const by = needName(); if (!by) return;
    const r = await api("/api/policy/killswitch", { body: { paused: !policy.agents_paused, by } });
    toast(r.ok ? (policy.agents_paused ? "Agents resumed" : "All agents paused") : (r.reasons || []).join("; "), r.ok ? "ok" : "bad"); refreshStatus(); V.guardrails();
  };
  $("#save").onclick = async () => {
    const by = needName(); if (!by) return;
    const changes = {}; $$("[data-pk]").forEach(i => { if (String(policy[i.dataset.pk]) !== i.value) changes[i.dataset.pk] = Number(i.value); });
    if (!Object.keys(changes).length) return toast("Nothing changed");
    const r = await api("/api/policy", { method: "PUT", body: { changes, by } });
    toast(r.ok ? `Policy saved: ${Object.keys(r.diff).join(", ")}` : (r.reasons || []).join("; "), r.ok ? "ok" : "bad"); V.guardrails();
  };
  $("#rt").onclick = async () => {
    $("#rt").disabled = true;
    const r = await api("/api/redteam", { method: "POST" });
    $("#rtres").innerHTML = `<p><b class="${r.passed === r.total ? "ok-t" : "bad-t"}">${r.passed} / ${r.total} attacks stopped</b></p><div class="tbl-wrap"><table><thead><tr><th></th><th>Attack</th><th>Expected</th><th>Actual</th></tr></thead><tbody>
      ${r.cases.map(c => `<tr><td>${badge(c.passed ? "held" : "FAILED", c.passed ? "b-ok" : "b-bad")}</td><td><b>${esc(c.name)}</b><div class="small muted">${esc(c.attack)}</div></td><td class="small">${esc(c.expected)}</td><td class="small mono">${esc(c.actual)}</td></tr>`).join("")}</tbody></table></div>`;
    $("#rt").disabled = false;
  };
};

/* Audit Ledger */
V.ledger = async () => {
  let offset = 0, type = "";
  view().innerHTML = `<h1>Audit Ledger</h1><p class="sub">Append-only, SHA-256 hash-chained record of every agent step outcome, guardrail decision, human approval, label and data import. Editing any entry breaks the chain from that point.</p>
  <div class="grid g2"><div class="card" id="vcard"></div><div class="card"><h2>Tamper test</h2><p class="small muted">Edits one sealed entry in an in-memory copy and re-verifies — the real ledger is never touched.</p>
    <div class="row"><input id="tseq" type="number" min="0" placeholder="entry seq (blank = middle)" style="width:220px"><button class="btn" id="tamper">Simulate tampering</button></div><div id="tres" class="mt"></div></div></div>
  <div class="card mt"><div class="row" style="justify-content:space-between"><h2>Entries</h2><select id="lt"><option value="">all event types</option></select></div>
    <div class="tbl-wrap"><table><thead><tr><th class="num">Seq</th><th>Time (UTC)</th><th>Event</th><th>Actor</th><th>Target</th><th>Payload</th><th>Entry hash</th></tr></thead><tbody id="lrows"></tbody></table></div>
    <div class="row mt"><span class="small muted" id="lcnt"></span><span style="flex:1"></span><button class="btn sm" id="lprev">Newer</button><button class="btn sm" id="lnext">Older</button></div></div>`;
  const verify = async () => {
    const v = await api("/api/ledger/verify");
    $("#vcard").innerHTML = `<h2>Chain verification</h2><div class="kpi"><div class="val ${v.ok ? "ok-t" : "bad-t"}">${v.ok ? "VERIFIED" : "BROKEN"}</div><div class="note">${v.count} entries${v.ok ? "" : ` · broken at seq ${v.broken_at}: ${esc(v.reason)}`}</div></div>
      <p class="small mt">Merkle root<br><span class="hash">${esc(v.merkle_root)}</span></p><p class="small">Head<br><span class="hash">${esc(v.head)}</span></p><button class="btn" id="reverify">Verify again</button>`;
    $("#reverify").onclick = verify;
  };
  const load = async () => {
    const qs = new URLSearchParams({ limit: 50, offset }); if (type) qs.set("event_type", type);
    const d = await api(`/api/ledger?${qs}`);
    if ($("#lt").options.length === 1) $("#lt").innerHTML += d.types.map(t => `<option>${esc(t)}</option>`).join("");
    $("#lrows").innerHTML = d.items.map(e => `<tr><td class="num mono">${e.seq}</td><td class="small">${esc(when(e.ts))}</td><td>${badge(e.event_type, /GUARD|BLOCK|REDTEAM/.test(e.event_type) ? "b-vio" : /HUMAN|THRESHOLD|LABEL|POLICY/.test(e.event_type) ? "b-warn" : "b-mute")}</td>
      <td class="mono small">${esc(e.actor)}</td><td class="small">${esc(e.target)}</td><td class="small mono" style="max-width:360px;word-break:break-word">${esc(e.payload.length > 160 ? e.payload.slice(0, 160) + "…" : e.payload)}</td><td class="hash">${esc(short(e.entry_hash))}</td></tr>`).join("");
    $("#lcnt").textContent = `${offset + 1}–${offset + d.items.length} of ${d.total}`;
    $("#lprev").disabled = offset === 0; $("#lnext").disabled = offset + d.items.length >= d.total;
  };
  $("#lt").onchange = e => { type = e.target.value; offset = 0; load(); };
  $("#lprev").onclick = () => { offset = Math.max(0, offset - 50); load(); };
  $("#lnext").onclick = () => { offset += 50; load(); };
  $("#tamper").onclick = async () => {
    const s = $("#tseq").value;
    const r = await api(`/api/ledger/tamper-demo${s !== "" ? `?seq=${Number(s)}` : ""}`);
    $("#tres").innerHTML = r.error ? `<p class="muted">${esc(r.error)}</p>` : `<div class="find pass">Original chain: ${r.original_chain.ok ? "verified" : "broken"}</div>
      <div class="find fail">After editing seq ${r.tampered_seq} (${esc(r.event_type)}): <b>broken at seq ${r.after_tampering.broken_at}</b> — ${esc(r.after_tampering.reason)}</div><p class="small muted">${esc(r.explanation)}</p>`;
  };
  await Promise.all([verify(), load()]);
};

/* Labelling */
V.labels = async (params) => {
  const q = await api("/api/captures?unlabelled=true&limit=200");
  let id = params.get("id") || (q.items[0] && q.items[0].id);
  view().innerHTML = `<h1>Labelling Workbench</h1><p class="sub">Mark where the seal defect is, once per capture. The UV1–UV5 ground truth follows from the camera layout, so you never judge five images separately. Model results stay hidden until you have decided.</p>
  <div class="split"><div class="card" id="lab"></div>
    <div class="card"><div class="row" style="justify-content:space-between"><h2>Queue (${q.total} unlabelled)</h2><input id="lq" placeholder="go to capture id" style="width:170px"></div>
      <div class="tbl-wrap" style="max-height:620px;overflow:auto"><table><tbody>${q.items.map(c => `<tr class="click" data-pick="${esc(c.id)}"><td class="mono">${esc(c.id)}</td><td>${badge(c.verdict, VERDICT_CLS[c.verdict])}</td><td class="small">${esc(when(c.ts))}</td></tr>`).join("") || `<tr><td class="empty">Queue empty — everything is labelled.</td></tr>`}</tbody></table></div></div></div>`;
  $$("[data-pick]").forEach(r => r.onclick = () => { id = r.dataset.pick; draw(); });
  $("#lq").onchange = e => { id = e.target.value.trim(); draw(); };
  let marked = [], dtype = "burn_through", reveal = false, loaded = null, d = null;
  const draw = async () => {
    if (!id) { $("#lab").innerHTML = '<p class="empty">Nothing to label.</p>'; return; }
    if (loaded !== id) {
      try { d = await api(`/api/captures/${encodeURIComponent(id)}`); } catch (e) { $("#lab").innerHTML = errCard(e); return; }
      loaded = id; reveal = false;
      const g = d.capture;
      marked = g.gt_source ? (g.gt_locations || "").split(",").filter(Boolean) : [];
      dtype = g.gt_source ? (g.defect_type || "good") : "burn_through";
    }
    const c = d.capture;
    const uvGT = {}; DOMAIN.uvs.forEach(u => uvGT[u] = dtype !== "good" && marked.some(m => DOMAIN.uv_regions[u].includes(m)) ? "FAIL" : "PASS");
    const states = reveal ? regionStates(d.images) : {};
    $("#lab").innerHTML = `<div class="row" style="justify-content:space-between"><h2 class="mono">${esc(c.id)}</h2>${c.gt_source ? badge(`labelled by ${c.gt_by || c.gt_source}`, "b-ok") : badge("unlabelled", "b-info")}</div>
      <p class="small muted">Click the seal side(s) where the defect is visible.</p>${pouchSVG(states, { edit: true, marked: dtype === "good" ? [] : marked })}
      <div class="row mt"><label class="fld">Defect type<select id="dt">${DOMAIN.defect_types.map(t => `<option value="${t}" ${t === dtype ? "selected" : ""}>${esc(DOMAIN.defect_labels[t])}</option>`).join("")}</select></label>
        <label class="small"><input type="checkbox" id="ni" ${c.not_inspectable ? "checked" : ""}> not inspectable (exclude)</label>
        <label class="small"><input type="checkbox" id="rv" ${reveal ? "checked" : ""}> show model result</label></div>
      <h3 class="mt">Derived ground truth per camera</h3><div class="row">${DOMAIN.uvs.map(u => `<span>${u} ${badge(uvGT[u], uvGT[u] === "FAIL" ? "b-bad" : "b-ok")}</span>`).join("")}</div>
      <label class="fld mt">Note (stored as data, never executed)<textarea id="nt">${esc(c.gt_note || "")}</textarea></label>
      <div class="row mt"><button class="btn primary" id="sv">Save label</button><button class="btn" id="clr">Clear marks</button><span class="small muted">Signed as: ${esc(operator() || "— enter your name above")}</span></div>
      ${reveal && d.triage ? `<div class="find warn mt small">Triage agent says ${badge(d.triage.cause.replace(/_/g, " "), CAUSE_CLS[d.triage.cause])} ${esc(d.triage.reason)}</div>` : ""}`;
    $$("#lab rect.seal").forEach(r => r.onclick = () => {
      const loc = r.dataset.loc; marked = marked.includes(loc) ? marked.filter(x => x !== loc) : [...marked, loc];
      if (marked.length && dtype === "good") dtype = "burn_through";
      draw();
    });
    $("#dt").onchange = e => { dtype = e.target.value; if (dtype === "good") marked = []; draw(); };
    $("#rv").onchange = e => { reveal = e.target.checked; draw(); };
    $("#clr").onclick = () => { marked = []; draw(); };
    $("#sv").onclick = async () => {
      const by = needName(); if (!by) return;
      try {
        const r = await api(`/api/captures/${encodeURIComponent(c.id)}/label`, { body: { defect_type: dtype, locations: dtype === "good" ? [] : marked, by, note: $("#nt").value, not_inspectable: $("#ni").checked } });
        toast(r.flags && r.flags.length ? `Saved. Note contained instruction-like text — flagged and ignored (${r.flags.length}).` : "Label saved and sealed in the ledger", r.flags && r.flags.length ? "bad" : "ok");
        marked = []; refreshStatus();
        const next = q.items.findIndex(x => x.id === c.id);
        id = q.items[next + 1] ? q.items[next + 1].id : c.id; V.labels(new URLSearchParams(`id=${id}`));
      } catch (e) { toast(e.message, "bad"); }
    };
  };
  await draw();
};

/* Reports */
V.reports = async (params) => {
  const [list, table] = await Promise.all([api("/api/reports"), api("/api/reports/defect-uv")]);
  const groups = {}; table.forEach(r => (groups[r.defect] = groups[r.defect] || []).push(r));
  view().innerHTML = `<h1>Reports</h1><p class="sub">Shift reports are written by the Reporter agent and sealed by the Auditor. The defect × UV table compares each camera's prediction with ground truth per image — the format of your customer report.</p>
  <div class="card"><div class="row" style="justify-content:space-between"><h2>Defect type × inspection ROI</h2><a class="btn primary" href="/api/reports/defect-uv.xlsx?by=${encodeURIComponent(operator() || "web")}" style="text-decoration:none">Download Excel</a></div>
    <div class="tbl-wrap"><table><thead><tr><th>Defect</th><th>ROI</th><th>Category</th><th class="num">Total</th><th class="num">Pass</th><th class="num">Fail</th><th class="num">Overkill</th><th class="num">Escape</th><th class="num">Not insp.</th><th class="num">Yield</th><th class="num">Detectability</th></tr></thead><tbody>
    ${Object.entries(groups).map(([dname, rows]) => rows.map((r, i) => `<tr><td>${i === 0 ? `<b>${esc(dname)}</b>` : ""}</td><td>${esc(r.uv)}</td><td>${esc(r.category)}</td><td class="num">${r.total}</td><td class="num">${r.pass}</td><td class="num">${r.fail}</td>
      <td class="num ${r.overkill ? "warn-t" : ""}">${r.overkill}</td><td class="num ${r.escape ? "bad-t" : ""}">${r.escape}</td><td class="num muted">${r.not_inspectable}</td>
      <td class="num">${r.yield !== null ? pct(r.yield * 100, 2) : ""}</td><td class="num">${r.detectability !== null ? pct(r.detectability * 100, 2) : ""}</td></tr>`).join("")).join("") || `<tr><td colspan="11" class="empty">No labelled data</td></tr>`}</tbody></table></div></div>
  <div class="split mt"><div class="card" id="rview"><p class="muted">Select a shift report.</p></div>
    <div class="card"><h2>Shift reports</h2><table><tbody>${list.map(r => `<tr class="click" data-rep="${esc(r.id)}"><td>${esc(r.title)}<div class="small muted">${esc(r.id)} · ${esc(r.run_id)}</div></td><td>${r.audit ? badge(r.audit, r.audit === "AUDIT_PASSED" ? "b-ok" : "b-bad") : ""}</td></tr>`).join("") || `<tr><td class="empty">No reports — run the agents.</td></tr>`}</tbody></table></div></div>`;
  const show = async rid => {
    const r = await api(`/api/reports/${rid}`);
    $("#rview").innerHTML = `${md(r.body)}<h3 class="mt">Auditor</h3>${(r.audit.findings || []).map(f => `<div class="find ${f.ok ? "pass" : "fail"}">${badge(f.ok ? "ok" : "fail", f.ok ? "b-ok" : "b-bad")} ${esc(f.check)} <span class="small muted">${esc(f.detail)}</span></div>`).join("")}
      <p class="small mt">Report SHA-256 <span class="hash">${esc(r.sha256)}</span></p><p class="small muted">${r.citations.length} cited captures: ${r.citations.slice(0, 30).map(c => `<a href="#" data-cap="${esc(c)}">${esc(c)}</a>`).join(", ")}</p>`;
  };
  $$("[data-rep]").forEach(r => r.onclick = () => show(r.dataset.rep));
  if (list.length) show(params.get("id") || list[0].id);
};

/* Data Sources */
V.data = async () => {
  const h = await api("/api/health");
  view().innerHTML = `<h1>Data Sources</h1><p class="sub">Use the built-in line simulator, or import real results from your inspection scripts (uv_inspection_results.csv).</p>
  <div class="grid g2"><div class="card"><h2>Line simulator</h2><p class="small muted">Deterministic scenario: five defect types, UV3 lamp ageing (score drift), a UV5 over-exposure burst, pouch misalignment, faint channeling that escapes. ${h.captures} captures stored.</p>
    <div class="row"><input type="number" id="sn" value="60" min="1" max="500" style="width:90px"><button class="btn primary" id="sim">Generate captures</button></div><div id="simres" class="mt"></div></div>
    <div class="card"><h2>Import CSV</h2><p class="small muted">Needs <code>capture_id</code> plus per-image columns <code>&lt;UV&gt;_&lt;region&gt;_decision</code>, <code>_score</code>, <code>_threshold</code> (optional <code>_exposure</code>), e.g. <code>UV2_P1_top_score</code>. Regions: ${esc(DOMAIN.locations.join(", "))}.</p>
      <div class="grid g2"><label class="fld">File<input type="file" id="cf" accept=".csv,text/csv"></label><label class="fld">Source name<input id="cs" value="test_set"></label>
      <label class="fld">Folder defect type (optional)<select id="cd"><option value="">unknown — label later</option>${DOMAIN.defect_types.map(t => `<option value="${t}">${esc(DOMAIN.defect_labels[t])}</option>`).join("")}</select></label></div>
      <button class="btn primary mt" id="imp">Import</button><pre id="prev" class="mono small muted" style="white-space:pre-wrap;max-height:160px;overflow:auto"></pre><div id="impres"></div></div></div>`;
  let text = "";
  $("#cf").onchange = async e => { const f = e.target.files[0]; if (!f) return; text = await f.text(); $("#prev").textContent = text.split("\n").slice(0, 4).join("\n"); $("#cs").value = f.name.replace(/\.csv$/i, ""); };
  $("#sim").onclick = async () => { const r = await api("/api/sim/tick", { body: { n: Number($("#sn").value) || 60 } }); $("#simres").innerHTML = `<div class="find pass">Added ${r.generated} captures: ${esc(JSON.stringify(r.verdicts))}</div>`; refreshStatus(); };
  $("#imp").onclick = async () => {
    if (!text) return toast("Choose a CSV file first", "bad");
    const by = needName(); if (!by) return;
    try { const r = await api("/api/ingest/csv", { body: { content: text, source: $("#cs").value || "csv", defect_type: $("#cd").value || null, by } });
      $("#impres").innerHTML = `<div class="find pass">Imported ${r.added} captures (${r.skipped_duplicates} duplicates skipped). ${r.needs_labels ? `<a href="#/labels">${r.needs_labels} need labels →</a>` : ""}</div>`; refreshStatus();
    } catch (e) { $("#impres").innerHTML = `<div class="find fail">${esc(e.message)}</div>`; }
  };
};

/* ---------------------------------------------------------------- router */
async function route() {
  if (V.cleanup) { V.cleanup(); V.cleanup = null; }
  closeDrawer();
  const [path, qs] = location.hash.replace(/^#\/?/, "").split("?");
  const fn = V[path] || V[""];
  renderNav();
  view().innerHTML = `<p class="muted">Loading…</p>`;
  try { await fn(new URLSearchParams(qs || "")); } catch (e) { view().innerHTML = errCard(e); }
  view().focus({ preventScroll: true });
  $("#side").classList.remove("on");
}

document.addEventListener("click", e => {
  const a = e.target.closest("[data-cap]");
  if (a) { e.preventDefault(); captureDrawer(a.dataset.cap); return; }
});
document.addEventListener("keydown", e => { if (e.key === "Escape") closeDrawer(); });
$("#drawer-bg").onclick = closeDrawer;
$("#menu").onclick = () => $("#side").classList.toggle("on");
$("#operator").value = store.get("operator", "");
$("#operator").oninput = e => store.set("operator", e.target.value.trim());
window.addEventListener("hashchange", route);

(async () => {
  try { DOMAIN = await api("/api/domain"); } catch (e) { view().innerHTML = errCard(e); return; }
  await refreshStatus();
  startEvents();
  route();
  setInterval(refreshStatus, 15000);
})();
