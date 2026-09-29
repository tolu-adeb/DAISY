/* ABG Intelligence Terminal — Markets tab, risk book, backtests, sign-in (loaded last; reuses helpers). */
"use strict";

async function loadMarkets() {
  if (!$("#mk-groups").children.length) $("#mk-groups").innerHTML = '<p class="muted">Loading markets… (the first load quotes ~30 instruments and can take a few seconds)</p>';
  // calendar and models don't depend on market data: render them straight away
  api("/api/calendar?days=21").then(renderCalendar).catch((e) => { $("#mk-calendar").innerHTML = `<p class="muted">${esc(e.message)}</p>`; });
  api("/api/models").then((models) => {
    $("#mk-models").innerHTML = modelHtml(models.grade_model, "Entry grade", "abg train grade") + modelHtml(models.risk_model, "Risk", "abg train risk");
  }).catch(() => {});
  try {
    const m = await api("/api/markets");
    const rg = m.regime;
    $("#mk-regime").innerHTML = rg ? `<b>Market regime: ${esc(rg.label)}</b><span class="muted">${esc(rg.summary.split(": ").slice(1).join(": "))}</span>
      ${(rg.notes || []).map((n) => `<span class="neg">${esc(n)}</span>`).join("")}` : '<span class="muted">Regime unavailable</span>';
    $("#mk-groups").innerHTML = m.groups.map((g) => `<section class="card"><h2>${esc(g.group)}</h2>
      ${g.group === "Rates: yields" ? curveHtml(m) : ""}
      <table><thead><tr><th>Symbol</th><th class="n">Price</th><th class="n">Day</th>${g.rows.some((r) => r.asset_class.includes("future")) ? '<th class="n">$/pt · tick</th>' : ""}</tr></thead><tbody>
      ${g.rows.map((r) => `<tr><td><a class="sym" data-sym="${esc(r.symbol)}">${esc(r.symbol)}</a><span class="mk-name">${esc(r.name)}</span></td>
        <td class="n">${r.price == null ? '<span class="muted">n/a</span>' : fmt(r.price, r.price < 5 ? 4 : 2)}</td><td class="n">${sgn(r.change_pct, 2, "%")}</td>
        ${r.asset_class.includes("future") ? `<td class="n">${fmt(r.multiplier, 0)} · ${fmt(r.tick_value, 2)}</td>` : (g.rows.some((x) => x.asset_class.includes("future")) ? "<td></td>" : "")}</tr>`).join("")}
      </tbody></table></section>`).join("");
    bindSymLinks("#mk-groups");
  } catch (e) {
    $("#mk-groups").innerHTML = `<section class="card"><h2>Markets unavailable</h2><p class="muted">${esc(e.message)}</p>
      <p class="muted">If this says "Not Found", the server is still running old code: close its window and start it again.</p></section>`;
  }
}

function renderCalendar(cal) {
  $("#mk-blackout").textContent = `entries pause ${cal.blackout.before_min} min before / ${cal.blackout.after_min} min after macro releases`;
  $("#mk-calendar").innerHTML = cal.events.length ? cal.events.map((e) => `<div class="cal-row ${e.kind === "earnings" ? "earn" : ""}">
    <span>${esc(new Date(e.ts * 1000).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }))}</span>
    <span>${esc(e.name)} ${e.detail ? `<span class="muted">· ${esc(e.detail)}</span>` : ""}${e.estimated ? ' <span class="est">(estimated)</span>' : ""}</span></div>`).join("")
    : '<p class="muted">No scheduled releases in the next 3 weeks.</p>';
}
window.loadMarkets = loadMarkets;

function curveHtml(m) {
  const pts = [["3m", m.curve["3m"]], ["5y", m.curve["5y"]], ["10y", m.curve["10y"]], ["30y", m.curve["30y"]]].filter((p) => p[1] != null);
  if (!pts.length) return "";
  const mx = Math.max(...pts.map((p) => p[1]), 0.01);
  return `<div class="curve">${pts.map((p) => `<div style="height:${Math.max(4, p[1] / mx * 80)}px"><span>${fmt(p[1], 2)}%</span></div>`).join("")}</div>
    <div class="curve-labels">${pts.map((p) => `<span>${p[0]}</span>`).join("")}</div>
    ${m.spreads["10y-3m"] != null ? `<p class="muted">10y − 3m: ${sgn(m.spreads["10y-3m"], 2, " pts")}${m.inverted ? ' <b class="neg">inverted</b>' : ""}</p>` : ""}`;
}

function modelHtml(m, title, cmd) {
  if (!m) return `<p><b>${title}:</b> <span class="muted">not trained yet — run <code>${cmd}</code></span></p>`;
  const met = m.metrics || {}, auc = met.test_auc;
  const ok = auc != null && auc > 0.55;
  return `<p><b>${title}:</b> ${ok ? '<span class="pos">in use</span>' : '<span class="muted">trained, not better than chance yet (rules stay in charge)</span>'}
    · ${m.n} rows · test AUC ${auc == null ? "—" : fmt(auc, 3)} · trained ${ago(m.trained_at)}
    ${(m.importance || []).length ? `<br><span class="muted">top factors: ${m.importance.slice(0, 5).map((x) => esc(x.feature)).join(", ")}</span>` : ""}</p>`;
}

async function loadRiskBook() {
  try {
    const r = await api("/api/ext/risk"), eq = r.equity, L = r.limits;
    const bar = (label, used, detail) => { const u = Math.max(0, Math.min(100, used || 0));
      return `<div class="risk-bar">${label} <span class="muted">${detail}</span><div class="track"><div class="fill ${u >= 100 ? "bad" : u >= 70 ? "warn" : ""}" style="width:${u}%"></div></div></div>`; };
    let bars = bar("Portfolio heat", eq.heat_pct / L.max_heat_pct * 100, `${fmt(eq.heat_pct, 1)}% of ${L.max_heat_pct}% · $${fmt(eq.heat)}`);
    bars += bar("Open positions", eq.open / L.max_open * 100, `${eq.open} of ${L.max_open}`);
    if (eq.daily_room != null) bars += bar("Daily loss limit", Math.max(0, -eq.day_pnl) / L.prop_daily_loss_limit * 100, `today ${fmt(eq.day_pnl)} of −${fmt(L.prop_daily_loss_limit, 0)}`);
    if (eq.floor != null) bars += bar("Trailing drawdown", (L.prop_max_drawdown - eq.drawdown_room) / L.prop_max_drawdown * 100, `equity ${fmt(eq.equity)} · floor ${fmt(eq.floor)}`);
    $("#ext-risk").innerHTML = `<div class="card-head"><h2>Risk book</h2><span class="muted">${L.prop_enabled ? "prop-firm rules ON" : "prop-firm rules off"}</span></div>
      <p>Paper equity <b>${fmt(eq.equity)}</b> · today ${sgn(eq.day_pnl)} · open P&L ${sgn(eq.open_pnl)}</p><div class="risk-bars">${bars}</div>`;
  } catch {}
}

async function loadBacktests() {
  try {
    const r = await api("/api/ext/backtests");
    if (!r.backtests.length) { $("#ext-backtests").innerHTML = '<h2>Backtests</h2><p class="muted">None yet. Run <code>abg ext backtest signals.txt</code> or <code>--discord CHANNEL_ID</code>.</p>'; return; }
    $("#ext-backtests").innerHTML = `<h2>Backtests</h2><table><thead><tr><th>Run</th><th class="n">Ideas</th><th class="n">Fill</th><th class="n">Win</th><th class="n">Avg R</th><th class="n">Total R</th><th class="n">Max DD</th></tr></thead><tbody>
      ${r.backtests.map((b) => { const o = b.overall || {}; return `<tr title="${esc(b.grade_note || "")}"><td>${esc(b.name)}</td><td class="n">${o.ideas}</td><td class="n">${fmt((o.fill_rate || 0) * 100, 0)}%</td>
        <td class="n">${fmt((o.win_rate || 0) * 100, 0)}%</td><td class="n">${sgn(o.avg_r, 2, "R")}</td><td class="n">${sgn(o.total_r, 2, "R")}</td><td class="n">${fmt(o.max_drawdown_r, 2)}R</td></tr>`; }).join("")}</tbody></table>
      ${r.backtests[0].grade_note ? `<p class="muted">${esc(r.backtests[0].grade_note)}</p>` : ""}`;
  } catch {}
}

// ---- sign-in (only shown when the server has tokens configured)
window.authPrompt = async function () {
  const t = prompt("This terminal is protected. Enter your view or admin token:");
  if (!t) return;
  try { const r = await fetch("/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: t }) });
    if (r.ok) location.reload(); else toast("That token wasn't accepted."); } catch (e) { toast(e.message); }
};
(async function initAuth() {
  try {
    const me = await (await fetch("/api/auth/me")).json();
    if (!me.protected) return;
    const b = $("#auth-btn"); b.hidden = false;
    b.textContent = me.role === "admin" ? "🔓" : "🔒";
    b.title = me.role === "admin" ? "Signed in as admin (click to sign out)" : me.role === "view" ? "Read-only (click to sign in as admin)" : "Sign in";
    b.onclick = async () => { if (me.role === "admin") { await fetch("/api/auth/logout", { method: "POST" }); location.reload(); } else window.authPrompt(); };
    if (me.role !== "admin") document.body.classList.add("readonly");
    if (me.role === "none") window.authPrompt();
  } catch {}
})();

// hook into the Signals tab refresh
const _loadExt = window.loadExt;
window.loadExt = async function (sel) { await _loadExt(sel); loadRiskBook(); loadBacktests(); };
