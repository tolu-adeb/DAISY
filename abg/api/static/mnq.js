/* DAISY - MNQ Bot tab (docs/20): today's plan, live event log, paper record, posting pause, monitor detail.
   Live vs backtest + uptime come from parity.js (the System sub-tab). */
"use strict";
(() => {
const MQ = { lastTop: null, status: null };
const pts = (x) => (x == null ? "—" : (x > 0 ? "+" : "") + Number(x).toFixed(1));
const agoS = (ts) => { if (!ts) return "—"; const s = Math.max(0, Date.now() / 1000 - ts); return s < 60 ? "just now" : s < 5400 ? `${Math.round(s / 60)}m ago` : s < 172800 ? `${(s / 3600).toFixed(1)}h ago` : `${Math.round(s / 86400)}d ago`; };
const pill = (ok, on, off) => `<span class="pill ${ok ? "ok" : "bad"}">${ok ? on : off}</span>`;

async function loadPlan() {
  let r;
  try { r = await api("/api/alpha"); } catch (e) { $("#mq-plan").innerHTML = `<p class="muted">${esc(e.message)}</p>`; return; }
  const st = r.status || {}, bt = r.backtest, tr = r.trades || [];
  MQ.status = st;
  const paused = (st.paused || {}).on;
  const head = `<div class="card-head"><h2>Today's plan</h2><span class="muted">${st.enabled === false
    ? esc(st.hint || "bot off: set ABG_ALPHA_ENABLED=true in .env (docs/14)")
    : `feed ${esc(st.feed || "—")}${st.ratio ? `, ratio ${Number(st.ratio).toFixed(3)}` : ""}, Discord ${esc((st.discord || {}).mode || "off")}`}</span></div>`;
  const b = (st.brief || {}).ctx;
  const brief = b ? `<div class="levels">
      <div><span>Lean</span><b>${esc(b.bias_label)}</b></div><div><span>PDH</span>${fmt(b.pdh)}</div><div><span>PDL</span>${fmt(b.pdl)}</div>
      <div><span>ONH</span>${fmt(b.onh)}</div><div><span>ONL</span>${fmt(b.onl)}</div></div>
      ${(b.events || []).length ? `<p class="neg">News: ${b.events.map((e) => esc(e.time + " " + e.name)).join(", ")}</p>` : ""}`
    : '<p class="muted">The morning brief lands here at 9:25 ET on trading days.</p>';
  const ot = st.open_trade;
  const open = ot ? `<p class="open-trade"><b>Open #${ot.id}: ${esc(ot.side_label)} ${esc(ot.setup)}</b> at ${fmt(ot.entry)}, stop ${fmt(ot.stop)}, T1 ${fmt(ot.t1)}, final ${fmt(ot.final)}</p>`
    : st.done_reason ? `<p class="muted">Done for today: ${esc(st.done_reason)}</p>` : "";
  const s = bt && bt.stats;
  const btHtml = s && s.trades ? `<p><b>Backtest</b> over ${s.days} days: ${s.trades} trades, ${(s.win_rate * 100).toFixed(0)}% wins,
      avg ${Number(s.avg_r).toFixed(2)}R, ${s.net_usd_per_micro >= 0 ? "+" : "-"}$${Math.abs(s.net_usd_per_micro).toFixed(0)} per micro after costs, max DD ${fmt(s.max_dd_pts)} pts</p>`
    : '<p class="muted">No backtest saved yet: run <code>abg alpha backtest --walk-forward</code>.</p>';
  const canPause = st.enabled !== false && st.running !== false;
  const pause = `<p class="small">${paused ? `<span class="pill bad">Discord posts paused</span> <span class="muted">since ${esc((st.paused.at || "").slice(0, 16))} by ${esc(st.paused.by || "")}; trades are still tracked</span>` : '<span class="pill ok">Posting</span>'}
    <button class="btn small" id="mq-pause" ${canPause ? "" : "disabled"} title="Hold or resume the bot's Discord posts (trades keep being tracked)">${paused ? "Resume posts" : "Pause posts"}</button></p>`;
  $("#mq-plan").innerHTML = head + brief + open + btHtml + pause;
  const pb = $("#mq-pause");
  if (pb) pb.onclick = async () => {
    try { await api("/api/alpha/pause", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ on: !paused }) });
      toast(paused ? "MNQ posts resumed" : "MNQ posts paused - trades are still tracked"); loadPlan(); }
    catch (e) { toast(e.message); }
  };
  const rows = tr.slice(0, 30);
  $("#mq-trades").innerHTML = rows.length ? `<table><thead><tr><th>Date</th><th>Time</th><th>Side</th><th>Setup</th><th class="n">Entry</th><th>Exit</th><th class="n">Pts</th><th class="n">R</th></tr></thead><tbody>
      ${rows.map((t) => `<tr><td>${esc(t.date)}</td><td>${esc((t.opened || "").slice(11, 16))}</td><td>${esc(t.side_label)}</td><td>${esc(t.setup)}</td>
      <td class="n">${fmt(t.entry)}</td><td>${esc(t.exit_reason)}</td><td class="n ${t.pts > 0 ? "pos" : t.pts < 0 ? "neg" : ""}">${pts(t.pts)}</td><td class="n">${Number(t.r).toFixed(2)}</td></tr>`).join("")}
      </tbody></table>` : '<p class="muted">No paper trades yet. They appear here as the bot closes them.</p>';
}

async function loadPaper() {
  let r;
  try { r = await api("/api/alpha/paper"); } catch (e) { $("#mq-months").innerHTML = `<p class="muted">${esc(e.message)}</p>`; return; }
  const s = r.stats || {};
  const m = (s.months || []).slice(-1)[0];
  $("#mq-kpis").innerHTML = [
    stat("Paper trades", s.trades || 0, s.trades ? `${esc(s.first)} to ${esc(s.last)}` : "none recorded yet"),
    stat("Win rate", s.trades ? fmt(s.win_rate * 100, 1) + "%" : "—", s.trades ? `${s.wins} wins, ${s.losses} losses` : ""),
    stat("Net per micro", s.trades ? usd(s.net_usd) : "—", s.trades ? `${sgn(s.net_pts, 1, " pts")} after costs` : ""),
    stat("Max drawdown", s.trades ? "$" + fmt(s.max_dd_usd, 0) : "—", s.trades ? `avg ${sgn(s.avg_r, 2, "R")}` : ""),
    stat("This month", m ? usd(m.net_usd) : "—", m ? `${esc(m.month)}, ${m.trades} trades` : ""),
  ].join("");
  $("#mq-months").innerHTML = s.months && s.months.length ? `<table><thead><tr><th>Month</th><th class="n">Trades</th><th class="n">Net $ per micro</th></tr></thead><tbody>
    ${s.months.slice().reverse().map((x) => `<tr><td>${esc(x.month)}</td><td class="n">${x.trades}</td><td class="n">${sgn(x.net_usd, 0)}</td></tr>`).join("")}</tbody></table>`
    : '<p class="muted">No paper trades yet.</p>';
}

const DELIV = { "ok": "posted", "edited": "edited", "dashboard": "dashboard", "muted": "muted", "late - not posted": "late, held",
  "duplicate - not posted": "duplicate, held", "paused - not posted": "paused", "failed": "post failed", "edit failed": "edit failed" };
async function loadEvents() {
  let r;
  try { r = await api("/api/alpha/events?limit=150"); } catch (e) { $("#mq-events").innerHTML = `<p class="muted">${esc(e.message)}</p>`; return; }
  const evs = r.events || [];
  $("#mq-ev-note").textContent = evs.length ? `most recent first, ${evs[0].day || ""}` : "no events yet";
  const top = evs.length ? evs[0].at : null;
  $("#mq-events").innerHTML = evs.length ? evs.map((e, i) => {
    const t = (e.ts || "").slice(11, 16) || new Date(e.at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
    const cls = e.delivered === "ok" ? "ok" : (e.delivered || "").includes("not posted") || e.delivered === "paused - not posted" ? "late" : (e.delivered || "").includes("fail") ? "failed" : "";
    return `<div class="ev${i === 0 && MQ.lastTop !== null && top !== MQ.lastTop ? " new" : ""}"><time>${esc(t)}</time><span>${esc(e.title || e.type)}</span><span class="d ${cls}">${esc(DELIV[e.delivered] || e.delivered || "")}</span></div>`;
  }).join("") : '<p class="muted">The bot writes here from 9:10 ET on trading days: brief, ideas, signals, management, day close.</p>';
  MQ.lastTop = top;
}

async function loadMonitor() {
  let st;
  try { st = await api("/api/monitor"); } catch (e) { $("#mq-bar").innerHTML = `<span class="neg">${esc(e.message)}</span>`; return; }
  const a = st.alpha || {}, other = st.elsewhere, stream = st.stream || {};
  const here = !!st.running;
  const fresh = here || (other && (Date.now() / 1000 - (other.heartbeat || 0)) < 180);
  $("#mq-bar").innerHTML = `${pill(fresh, here ? "Monitor running here" : "Monitor running in another window", "Monitor not running")}
    ${a.enabled === false ? '<span class="pill bad">Bot off (ABG_ALPHA_ENABLED)</span>' : `<span>Day ${esc(a.day || "—")}, last bar ${esc((a.last_bar || "—").slice(11, 16))}</span>`}
    ${pill(stream.connected, "Price stream connected", stream.enabled === false ? "Price stream off" : "Price stream idle")}
    <span>Discord ${esc(((a.discord || {}).mode) || "off")}</span>
    ${st.note ? `<span class="muted">${esc(st.note)}</span>` : ""}`;
  const errs = (st.errors || []).concat(a.errors || []).slice(-5).map((e) => `<li class="neg">${esc(e.where || "")} ${esc(e.error || "")}</li>`).join("");
  const up = st.uptime || {};
  $("#mq-detail").innerHTML = `<dl class="kvs">
    <dt>Process</dt><dd>${here ? `this window, started ${agoS(st.started_at)}` : other ? `pid ${esc(other.pid)} on ${esc(other.host)}, heartbeat ${agoS(other.heartbeat)}` : "no monitor holds the lock"}</dd>
    <dt>Keep-awake</dt><dd>${up.keep_awake && up.keep_awake.on ? "on (market hours)" : "idle"}; autostart ${up.autostart === true ? "registered" : up.autostart === false ? "not registered (run deploy\\windows-autostart.ps1)" : "unknown"}</dd>
    <dt>Feeds</dt><dd>${a.ratio ? `MNQ/QQQ ratio ${fmt(a.ratio, 4)} (${esc(a.ratio_src || "")})` : "—"}</dd>
    ${(a.notes || []).length ? `<dt>Bot notes</dt><dd>${a.notes.map(esc).join("<br>")}</dd>` : ""}
    ${errs ? `<dt>Recent errors</dt><dd><ul class="plain">${errs}</ul></dd>` : ""}</dl>`;
}

LOADERS.mnq = (sub) => {
  loadMonitor();
  if (sub === "live") { loadPlan(); loadEvents(); loadPaper(); }
  if (sub === "trades") { loadPlan(); loadPaper(); }
};
window.mnqRefresh = (d) => {
  loadEvents(); loadMonitor();
  if (["signal", "t1", "final", "stopped", "breakeven", "closed", "failed", "brief"].includes(d.type)) { loadPlan(); loadPaper(); }
};
setInterval(() => { const v = $("#view-mnq"); if (v && !v.hidden && !document.hidden) { loadMonitor(); loadEvents(); } }, 20000);
})();
