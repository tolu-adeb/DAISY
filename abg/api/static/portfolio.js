/* ABG Intelligence Terminal — saved portfolio + live signals (loaded after app.js; reuses its helpers). */
"use strict";
const PF = { snap: null, feed: [], unread: 0, status: null, es: null, lastSignalTs: 0, rulesKinds: null };
const ago = (t) => { if (!t) return "never"; const s = Math.max(0, Date.now() / 1000 - t);
  return s < 60 ? `${Math.round(s)}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${Math.round(s / 3600)}h ago`; };
const inSecs = (s) => s == null ? "—" : s < 60 ? `${Math.round(s)}s` : `${Math.round(s / 60)}m`;
const money = (x) => x == null ? "—" : (x < 0 ? "−$" : "$") + fmt(Math.abs(x));
const sgnMoney = (x) => x == null ? '<span class="muted">—</span>' :
  `<span class="${x > 0 ? "pos" : x < 0 ? "neg" : ""}">${x > 0 ? "+" : x < 0 ? "−" : ""}$${fmt(Math.abs(x))}</span>`;
const pfVisible = () => !$("#view-portfolio").hidden;

async function loadPortfolio(risk = true) {
  try {
    const s = await api(`/api/portfolio?risk=${risk}`);
    PF.snap = s;
    renderPortfolio(s);
  } catch (e) { toast("Portfolio: " + e.message); }
}
window.loadPortfolio = loadPortfolio;

function renderPortfolio(s) {
  renderKpis(s); renderMonitor(s.monitor || PF.status); renderHoldings(s); renderWatch(s); renderRules(s); renderPfRisk(s);
  $("#pf-asof").textContent = `priced ${ago(s.as_of)}`;
}

function kpi(label, value, sub) { return `<div class="card kpi"><h2>${label}</h2><div class="v">${value}</div><div class="s">${sub || "&nbsp;"}</div></div>`; }
function renderKpis(s) {
  const t = s.totals || {}, r = s.risk || {};
  $("#pf-kpis").innerHTML =
    kpi("Market value", money(t.market_value), `${t.positions} positions · ${t.watching} watching`) +
    kpi("Today", sgnMoney(t.day_pnl), sgn(t.day_pct, 2, "%")) +
    kpi("Unrealized P&L", sgnMoney(t.unrealized_pnl), sgn(t.unrealized_pct, 2, "%") + ` on ${money(t.cost_basis)} cost`) +
    kpi("Realized P&L", sgnMoney(t.realized_pnl), "average-cost method") +
    kpi("1-day VaR (95%)", r.available ? money(r.var_95_1d_usd) : "—",
        r.available ? `${fmt(r.var_95_1d_pct)}% · vol ${fmt(r.ann_volatility_pct, 1)}%` : (r.reason || "add holdings"));
}

function renderMonitor(m) {
  if (!m) return;
  PF.status = m;
  const open = m.market?.open;
  const state = m.running ? `<span class="live"><i class="pulse"></i>Live</span>`
    : m.elsewhere ? `<span class="live"><i class="pulse"></i>Running in another window</span>`
    : `<span class="live"><i class="pulse off"></i>Monitor stopped</span>`;
  const chans = (m.channels || []).map((c) => `<span class="chan ${c.configured ? "on" : "off"}" title="${esc(c.hint || "min severity: " + (c.min_severity || ""))}">${esc(c.name)}</span>`).join(" ");
  const notif = ("Notification" in window) && Notification.permission !== "granted"
    ? `<button class="btn small" id="mon-notif">Enable pop-ups</button>` : "";
  $("#pf-monitor").innerHTML = `${state}
    <span class="muted">market ${open ? "open" : "closed" + (m.market?.next_open_et ? " · opens " + esc(m.market.next_open_et.slice(5, 16).replace("T", " ")) + " ET" : "")}</span>
    <span class="muted">quotes ${ago(m.last_quote_sweep)}${m.running ? " · next " + inSecs(m.next_quote_in_s) : ""}</span>
    <span class="muted">analysis ${ago(m.last_analysis_sweep)}${m.running ? " · next " + inSecs(m.next_analysis_in_s) : ""}</span>
    ${m.errors?.length ? `<span class="neg" title="${esc(m.errors.map((e) => e.where + ": " + e.error).join("\n"))}">${m.errors.length} recent errors</span>` : ""}
    <span class="spacer"></span>${chans}
    ${notif}
    <button class="btn small" id="mon-refresh">Refresh now</button>
    <button class="btn small" id="mon-test">Test alert</button>
    ${m.elsewhere ? "" : `<button class="btn small" id="mon-toggle">${m.running ? "Stop" : "Start"} monitor</button>`}`;
  $("#mon-refresh").onclick = async () => { busy(true); try { await api("/api/monitor/refresh", { method: "POST" }); await loadPortfolio(); } catch (e) { toast(e.message); } finally { busy(false); } };
  $("#mon-test").onclick = async () => { try { const r = await api("/api/notify/test", { method: "POST" });
    toast("Test sent:\n" + Object.entries(r).map(([k, v]) => `• ${k}: ${v}`).join("\n")); } catch (e) { toast(e.message); } };
  if ($("#mon-toggle")) $("#mon-toggle").onclick = async () => { try {
    renderMonitor(await api(`/api/monitor/${m.running ? "stop" : "start"}`, { method: "POST" })); } catch (e) { toast(e.message); } };
  if ($("#mon-notif")) $("#mon-notif").onclick = async () => { await Notification.requestPermission(); renderMonitor(PF.status); };
}

function goAnalyze(sym) { $("#sym").value = sym; analyze(); }

function renderHoldings(s) {
  const rows = s.holdings || [];
  if (!rows.length) { $("#pf-holdings").innerHTML = '<p class="muted">No holdings yet. Record a trade below — leave price blank to use the live quote.</p>'; return; }
  const maxW = Math.max(...rows.map((h) => h.weight_pct || 0), 1);
  $("#pf-holdings").innerHTML = `<table><thead><tr><th>Symbol</th><th class="n">Shares</th><th class="n">Avg cost</th><th class="n">Price</th>
    <th class="n">Day</th><th class="n">Value</th><th>Weight</th><th class="n">Unrealized</th><th class="n">Stop</th><th class="n">Target</th>
    <th>Signal</th><th>Risk</th><th></th></tr></thead><tbody>${rows.map((h) => `<tr>
      <td><a class="sym" data-sym="${esc(h.symbol)}">${esc(h.symbol)}</a>${h.quote_error ? ` <span class="neg" title="${esc(h.quote_error)}">!</span>` : ""}</td>
      <td class="n">${fmt(h.shares, h.shares % 1 ? 4 : 0)}</td><td class="n">${fmt(h.avg_cost)}</td><td class="n">${fmt(h.price)}</td>
      <td class="n">${sgn(h.change_pct, 2, "%")}</td><td class="n">${fmt(h.market_value)}</td>
      <td class="num"><i class="wbar" style="width:${Math.round(50 * (h.weight_pct || 0) / maxW)}px"></i>${fmt(h.weight_pct, 1)}%</td>
      <td class="n">${sgnMoney(h.unrealized_pnl)}<br><small>${sgn(h.unrealized_pct, 1, "%")}</small></td>
      <td class="n"><input class="cell" type="number" step="any" data-meta="stop_loss" data-sym="${esc(h.symbol)}" value="${h.stop_loss ?? ""}" placeholder="—" aria-label="Stop for ${esc(h.symbol)}"></td>
      <td class="n"><input class="cell" type="number" step="any" data-meta="take_profit" data-sym="${esc(h.symbol)}" value="${h.take_profit ?? ""}" placeholder="—" aria-label="Target for ${esc(h.symbol)}"></td>
      <td>${h.signal_label ? `${esc(h.signal_label)} <span class="muted num">${fmt(h.signal_score, 0)}</span>` : '<span class="muted">…</span>'}</td>
      <td class="lvl lvl-${esc(h.risk_level)}">${h.risk_level ? `<span class="badge"><span class="dot"></span>${esc(h.risk_level)}</span>` : '<span class="muted">…</span>'}</td>
      <td><button class="btn small" data-sell="${esc(h.symbol)}" data-shares="${h.shares}">Sell</button></td></tr>`).join("")}</tbody></table>`;
  bindSymLinks("#pf-holdings");
  $("#pf-holdings").querySelectorAll("input[data-meta]").forEach((inp) => inp.addEventListener("change", async () => {
    const v = inp.value === "" ? null : +inp.value;
    const body = v == null ? { clear: false } : { [inp.dataset.meta]: v };
    try {
      if (v == null) {   // clearing one field: rewrite the other one explicitly
        const h = (PF.snap.holdings || []).find((x) => x.symbol === inp.dataset.sym) || {};
        await api(`/api/portfolio/positions/${inp.dataset.sym}`, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ clear: true }) });
        const other = inp.dataset.meta === "stop_loss" ? "take_profit" : "stop_loss";
        if (h[other]) await api(`/api/portfolio/positions/${inp.dataset.sym}`, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ [other]: h[other] }) });
      } else {
        await api(`/api/portfolio/positions/${inp.dataset.sym}`, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
      }
      toast(`${inp.dataset.sym} ${inp.dataset.meta.replace("_", " ")} ${v == null ? "cleared" : "set to " + fmt(v)}`);
      loadPortfolio(false);
    } catch (e) { toast(e.message); }
  }));
  $("#pf-holdings").querySelectorAll("[data-sell]").forEach((b) => b.addEventListener("click", () => {
    $("#tx-side").value = "SELL"; $("#tx-sym").value = b.dataset.sell; $("#tx-shares").value = b.dataset.shares; $("#tx-price").focus();
  }));
}

function bindSymLinks(root) { $(root).querySelectorAll("a.sym").forEach((a) => a.addEventListener("click", () => goAnalyze(a.dataset.sym))); }

function renderWatch(s) {
  const rows = s.watchlist || [];
  $("#pf-watch").innerHTML = rows.length ? `<table><thead><tr><th>Symbol</th><th class="n">Price</th><th class="n">Day</th><th>Signal</th><th>Risk</th><th>Setup</th><th></th></tr></thead>
    <tbody>${rows.map((w) => `<tr><td><a class="sym" data-sym="${esc(w.symbol)}">${esc(w.symbol)}</a></td><td class="n">${fmt(w.price)}</td>
      <td class="n">${sgn(w.change_pct, 2, "%")}</td><td>${esc(w.signal_label || "…")}</td>
      <td class="lvl lvl-${esc(w.risk_level)}">${w.risk_level ? `<span class="badge"><span class="dot"></span>${esc(w.risk_level)}</span>` : "…"}</td>
      <td>${esc(w.top_setup || "—")}</td><td><button class="x" title="Stop watching" data-unwatch="${esc(w.symbol)}">×</button></td></tr>`).join("")}</tbody></table>`
    : '<p class="muted">Nothing on the watchlist.</p>';
  bindSymLinks("#pf-watch");
  $("#pf-watch").querySelectorAll("[data-unwatch]").forEach((b) => b.addEventListener("click", async () => {
    try { await api(`/api/portfolio/watchlist/${b.dataset.unwatch}`, { method: "DELETE" }); loadPortfolio(false); } catch (e) { toast(e.message); } }));
}

function renderRules(s) {
  const rules = s.rules || [];
  $("#pf-rules").innerHTML = rules.length ? `<table>${rules.map((r) => `<tr><td><b>${esc(r.symbol)}</b></td>
      <td>${esc(r.description.replace(" value", ""))} <b class="num">${fmt(r.value, r.value % 1 ? 2 : 0)}</b></td>
      <td class="muted">${r.enabled ? (r.one_shot ? "one-shot" : "repeating") : "fired"}</td>
      <td><button class="x" title="Delete rule" data-rule="${r.id}">×</button></td></tr>`).join("")}</table>`
    : '<p class="muted">No custom rules. Built-in signals (setups, crosses, big moves, stops…) run automatically.</p>';
  $("#pf-rules").querySelectorAll("[data-rule]").forEach((b) => b.addEventListener("click", async () => {
    try { await api(`/api/alerts/${b.dataset.rule}`, { method: "DELETE" }); loadPortfolio(false); } catch (e) { toast(e.message); } }));
}

function renderPfRisk(s) {
  const r = s.risk || {};
  if (!r.available) { $("#pf-risk").innerHTML = `<h2>Portfolio risk</h2><p class="muted">${esc(r.reason || "Add holdings to see portfolio risk.")}</p>`; return; }
  const contrib = Object.entries(r.risk_contribution_pct || {}).sort((a, b) => b[1] - a[1]);
  $("#pf-risk").innerHTML = `<h2>Portfolio risk (1y daily returns, current weights)</h2>
    <div class="row two"><dl class="kv">
      <dt>Annualised volatility</dt><dd>${fmt(r.ann_volatility_pct, 2)}%</dd>
      <dt>1-day VaR 95%</dt><dd>${fmt(r.var_95_1d_pct, 2)}% · ${money(r.var_95_1d_usd)}</dd>
      <dt>1-day CVaR 95%</dt><dd>${fmt(r.cvar_95_1d_pct, 2)}% · ${money(r.cvar_95_1d_usd)}</dd>
      <dt>Diversification ratio</dt><dd>${fmt(r.diversification_ratio, 2)}</dd></dl>
    <div><div class="sec" style="font-size:12px;margin-bottom:6px">Share of portfolio risk</div><div class="bars">${contrib.map(([k, v]) =>
      `<span>${esc(k)}</span><span class="track"><i class="fill" style="width:${Math.max(0, Math.min(100, v))}%"></i></span><span class="num">${fmt(v, 1)}%</span>`).join("")}</div></div></div>`;
}

// ------------------------------------------------------------------ forms
function bindPortfolioForms() {
  $("#tx-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = { side: $("#tx-side").value, symbol: $("#tx-sym").value.trim().toUpperCase(), shares: +$("#tx-shares").value };
    if ($("#tx-price").value) body.price = +$("#tx-price").value;
    if ($("#tx-date").value) body.date = $("#tx-date").value;
    busy(true);
    try {
      const t = await api("/api/portfolio/transactions", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
      toast(`${t.side} ${t.shares} ${t.symbol} @ ${fmt(t.price)} recorded`);
      $("#tx-form").reset();
      await loadPortfolio();
    } catch (err) { toast(err.message); } finally { busy(false); }
  });
  $("#tx-history-btn").addEventListener("click", async () => {
    const box = $("#tx-history");
    if (!box.hidden) { box.hidden = true; return; }
    try {
      const txs = await api("/api/portfolio/transactions");
      box.innerHTML = `<table style="margin-top:10px"><thead><tr><th>#</th><th>Date</th><th>Side</th><th>Symbol</th><th class="n">Shares</th><th class="n">Price</th><th></th></tr></thead><tbody>${
        txs.slice().reverse().map((t) => `<tr><td>${t.id}</td><td>${esc(new Date(t.ts * 1000).toLocaleDateString())}</td>
        <td class="${t.side === "BUY" ? "pos" : "neg"}">${t.side}</td><td>${esc(t.symbol)}</td><td class="n">${fmt(t.shares, t.shares % 1 ? 4 : 0)}</td>
        <td class="n">${fmt(t.price)}</td><td><button class="x" title="Delete transaction" data-tx="${t.id}">×</button></td></tr>`).join("")}</tbody></table>`;
      box.hidden = false;
      box.querySelectorAll("[data-tx]").forEach((b) => b.addEventListener("click", async () => {
        try { await api(`/api/portfolio/transactions/${b.dataset.tx}`, { method: "DELETE" }); box.hidden = true; loadPortfolio(); } catch (err) { toast(err.message); } }));
    } catch (err) { toast(err.message); }
  });
  $("#watch-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const syms = $("#watch-sym").value.split(/[\s,]+/).map((x) => x.trim().toUpperCase()).filter(Boolean);
    try {
      for (const s of syms) await api("/api/portfolio/watchlist", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ symbol: s }) });
      $("#watch-sym").value = ""; loadPortfolio(false);
    } catch (err) { toast(err.message); }
  });
  $("#rule-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/alerts", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({
        symbol: $("#rule-sym").value.trim().toUpperCase(), kind: $("#rule-kind").value, value: +$("#rule-val").value,
        one_shot: !$("#rule-repeat").checked }) });
      $("#rule-form").reset(); loadPortfolio(false);
    } catch (err) { toast(err.message); }
  });
  $("#sig-ack").addEventListener("click", async () => { try { await api("/api/signals/ack", { method: "POST" }); } catch {} PF.unread = 0; updateBadge();
    PF.feed.forEach((s) => (s.acknowledged = true)); renderFeed(); });
}

// ------------------------------------------------------------------ live feed
function updateBadge() { const b = $("#sig-badge"); b.hidden = PF.unread <= 0; b.textContent = PF.unread > 99 ? "99+" : PF.unread; }

function renderFeed(flashId) {
  $("#sig-feed").innerHTML = PF.feed.length ? PF.feed.slice(0, 80).map((s) => `<div class="sig ${esc(s.severity)} ${s.acknowledged ? "" : "unread"} ${s.id === flashId ? "flash" : ""}">
      <div class="meta"><span class="sev ${esc(s.severity)}">${esc(s.severity)}</span><span>${esc(new Date(s.ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }))}</span>
        <span>${esc(s.kind.replace(/_/g, " "))}</span>${s.symbol && s.symbol !== "PORTFOLIO" && s.symbol !== "TEST" ? `<a class="sym" data-sym="${esc(s.symbol)}">${esc(s.symbol)}</a>` : ""}</div>
      <div class="t">${s.direction === "bullish" ? '<span class="pos">▲</span> ' : s.direction === "bearish" ? '<span class="neg">▼</span> ' : ""}${esc(s.title)}</div>
      <div class="m">${esc(s.message)}</div></div>`).join("")
    : '<p class="muted">No signals yet. The monitor fires on changes: new setups, RSI/MACD/MA crosses, big moves, level breaks, stops & targets, risk-level changes and your custom rules.</p>';
  bindSymLinks("#sig-feed");
}

function onSignal(s) {
  if (s.id != null && PF.feed.some((x) => x.id === s.id)) return;
  PF.feed.unshift({ ...s, acknowledged: false });
  PF.lastSignalTs = Math.max(PF.lastSignalTs, s.ts || 0);
  if (!pfVisible() || document.hidden) { PF.unread++; updateBadge(); }
  renderFeed(s.id);
  if ("Notification" in window && Notification.permission === "granted" && (s.severity !== "info" || s.test)) {
    try { const n = new Notification(s.title, { body: s.message, tag: `abg-${s.id ?? s.ts}`, requireInteraction: s.severity === "critical" });
      n.onclick = () => { window.focus(); showView("portfolio"); }; } catch {}
  }
}

async function loadFeed() {
  try {
    const rows = await api("/api/signals?limit=80");
    PF.feed = rows;
    PF.lastSignalTs = rows.length ? rows[0].ts : 0;
    PF.unread = rows.filter((r) => !r.acknowledged).length;
    updateBadge(); renderFeed();
  } catch {}
}

function connectStream() {
  if (!("EventSource" in window)) return;
  PF.es = new EventSource("/api/signals/stream");
  PF.es.addEventListener("signal", (e) => onSignal(JSON.parse(e.data)));
  PF.es.addEventListener("status", (e) => { const st = JSON.parse(e.data); if (pfVisible()) renderMonitor(st); else PF.status = st; });
  PF.es.addEventListener("snapshot", (e) => {
    const snap = JSON.parse(e.data);
    if (!PF.snap) return;
    PF.snap = { ...snap, risk: PF.snap.risk, monitor: PF.status };   // keep the (slower) risk block from the last full load
    if (pfVisible()) renderPortfolio(PF.snap);
  });
}

// Fallback for a monitor running in another window (no SSE from this server): poll saved signals.
setInterval(async () => {
  if (PF.status?.running) return;
  try {
    const rows = await api(`/api/signals?limit=20&since=${PF.lastSignalTs}`);
    rows.reverse().forEach(onSignal);
  } catch {}
}, 45000);
setInterval(() => { if (pfVisible() && PF.status) renderMonitor(PF.status); }, 15000);   // keep "x s ago" fresh

(async function initPortfolio() {
  try {
    const a = await api("/api/alerts");
    $("#rule-kind").innerHTML = Object.entries(a.kinds).map(([k, v]) => `<option value="${esc(k)}" title="${esc(v)}">${esc(k.replace("_", " "))}</option>`).join("");
  } catch {}
  bindPortfolioForms();
  await loadFeed();
  connectStream();
  try { PF.status = await api("/api/monitor"); } catch {}
})();
