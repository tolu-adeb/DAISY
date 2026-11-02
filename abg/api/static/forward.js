/* Ported from the MNQ bot spin-off (c3.lab/mnq-bot) for DAISY's desk tabs (docs/20). Wrapped so its names stay local. */
(() => {
"use strict";
const api = window.deskApi, toast = window.deskToast;
// R&D -> Forward lab (docs/17.9): daily ideas on all the data, paper-traded on unseen sessions, approved by you.
const FW = { data: null, open: new Set() };
const fwPct = (x) => x == null ? "—" : (x * 100).toFixed(1) + "%";
const fwUsd = (x) => x == null ? "—" : (x < 0 ? "−$" : "$") + Math.abs(x).toLocaleString(LOCALE, { maximumFractionDigits: 0 });
const fwChanges = (c) => Object.entries(c || {}).map(([k, v]) => `<code>${esc(k)}=${esc(Array.isArray(v) ? v.join(",") : v === "" ? "off" : v)}</code>`).join(" ");
const STATUS = { ready: ["Ready for you", "ok"], paper: ["Paper trading", ""], queued: ["Waiting for a slot", ""], approved: ["Live", "ok"], retired: ["Retired", "muted"] };

function fwVs(c, l, kind) {
  if (!c) return "—";
  const a = kind === "wr" ? c.win_rate : c.net_usd, b = kind === "wr" ? l.win_rate : l.net_usd;
  const cls = a > b ? "pos" : a < b ? "neg" : "";
  return `<span class="${cls}">${kind === "wr" ? fwPct(a) : fwUsd(a)}</span> <span class="muted small">vs ${kind === "wr" ? fwPct(b) : fwUsd(b)}</span>`;
}

function fwProgress(s, rules) {
  const f = s.forward;
  if (!f) return s.status === "queued" ? '<span class="muted small">starts when a slot frees up</span>' : "";
  const pct = Math.min(100, Math.round(100 * f.sessions / rules.min_sessions));
  return `<span class="fw-prog" title="${f.sessions} of ${rules.min_sessions} sessions needed"><i style="width:${pct}%"></i></span>
    <span class="small">${f.sessions}/${rules.min_sessions} sessions · ${f.cand.trades} trades</span>`;
}

function renderFwStrategies(v) {
  const rules = v.status.rules, list = v.strategies || [];
  $("#fw-st-note").textContent = `${v.counts.paper} paper · ${v.counts.ready} ready · ${v.counts.queued} waiting · up to ${rules.max_active} at once`;
  if (!list.length) {
    $("#fw-strategies").innerHTML = '<p class="muted">None yet. An idea lands here when it beats the live settings in the walk-forward test; paper trading starts with the next session.</p>';
    return;
  }
  $("#fw-strategies").innerHTML = `<div class="scroll"><table class="fw"><thead><tr><th>#</th><th>Strategy</th><th>Status</th><th>Unseen sessions</th><th>Win rate</th><th>Net</th><th>Walk-forward</th><th></th></tr></thead><tbody>
    ${list.map((s) => {
      const [lab, cls] = STATUS[s.status] || [s.status, ""];
      const f = s.forward, open = FW.open.has(s.id);
      const act = s.status === "ready" ? `<button class="btn" data-fw-approve="${s.id}">Approve</button> ` : "";
      const ret = ["paper", "ready", "queued"].includes(s.status) ? `<button class="btn ghost" data-fw-retire="${s.id}">Retire</button>` : "";
      const folds = (s.folds || []).map((b) => `<i class="${b.cand_wr >= b.base_wr ? "up" : "dn"}" title="${esc(b.from)} → ${esc(b.to)}: ${fwPct(b.cand_wr)} vs ${fwPct(b.base_wr)}"></i>`).join("");
      return `<tr data-fw="${s.id}" tabindex="0" class="${s.status}">
        <td>${s.id}</td><td><b>${esc(s.name)}</b>${s.origin && s.origin !== "forward lab" ? ` <span class="pill">${esc(s.origin)}</span>` : ""}${s.route === "profit" ? ' <span class="pill" title="held the win rate, clearly better profit/drawdown">profit route</span>' : ""}<br>${fwChanges(s.changes)}${s.rebased ? ' <span class="muted small">(live settings changed since it started)</span>' : ""}</td>
        <td><span class="pill ${cls}">${esc(lab)}</span>${s.retired_why ? `<br><span class="muted small">${esc(s.retired_why)}</span>` : ""}</td>
        <td>${fwProgress(s, rules)}</td>
        <td>${f ? fwVs(f.cand, f.live, "wr") : "—"}</td><td>${f ? fwVs(f.cand, f.live, "net") : "—"}</td>
        <td><span class="folds">${folds}</span><span class="small muted"> ${s.wf ? `${fwPct(s.wf.win_rate)}, net/DD ${fmt(s.wf.ratio, 1)}` : ""}</span></td>
        <td class="acts">${act}${ret}</td></tr>
        ${open ? `<tr class="detail"><td></td><td colspan="7"><p class="small">${esc(s.rationale || "")}</p>
          <p class="small muted">passed on data to ${esc(s.passed_on || "")}${s.trials_before ? ` after ${s.trials_before} ideas tested in this lab (so some passes are luck; the unseen sessions decide)` : ""}${s.fwd_start ? ` · paper since ${esc(s.fwd_start)}` : ""}</p>
          ${(s.journey || []).length ? `<ol class="journey">${s.journey.map((j) => `<li class="${j.oos ? "oos" : ""}"><span class="muted small">${j.at ? new Date(j.at * 1000).toLocaleDateString(LOCALE, { month: "short", day: "numeric" }) : ""}</span> ${esc(j.what)}${j.oos ? ' <span class="pill ok">out-of-sample from here</span>' : j.seen ? ` <span class="muted small">(saw data through ${esc(j.seen)})</span>` : ""}</li>`).join("")}</ol>` : ""}
          <ul class="plain small">${(s.checks || []).map((c) => `<li><span class="${c.pass ? "pos" : "neg"}">${c.pass ? "✓" : "✗"}</span> ${esc(c.check)}: ${esc(c.detail || "")}</li>`).join("")}</ul></td></tr>` : ""}`;
    }).join("")}</tbody></table></div>`;
}

function renderFwTrials(v) {
  const t = v.trials || [];
  $("#fw-tr-note").textContent = `${v.total_trials} tested so far`;
  $("#fw-trials").innerHTML = t.length ? `<table class="grp"><thead><tr><th>Idea</th><th class="n">Win rate</th><th class="n">Net/DD</th><th>Result</th></tr></thead><tbody>
    ${t.slice(0, 24).map((x) => `<tr><td>${esc(x.name)}${x.origin && x.origin !== "forward lab" ? ` <span class="pill">${esc(x.origin === "claude" ? "Claude" : x.origin)}</span>` : /from Claude/.test(x.rationale || "") ? ' <span class="pill">Claude</span>' : ""}</td>
      <td class="n">${fwPct((x.search || {}).win_rate)} <span class="muted small">vs ${fwPct((x.live || {}).win_rate)}</span></td>
      <td class="n">${fmt((x.search || {}).ratio, 1)} <span class="muted small">vs ${fmt((x.live || {}).ratio, 1)}</span></td>
      <td>${x.verdict === "candidate" ? '<span class="pos">passed</span>' : `<span class="muted small">failed: ${esc((x.failed || []).join(", ").replace(/_/g, " "))}</span>`}</td></tr>`).join("")}</tbody></table>`
    : '<p class="muted">Nothing tested yet. The first run is today after the close (or press Run now).</p>';
}

function renderFwRuns(v) {
  const r = v.runs || [];
  $("#fw-runs").innerHTML = r.length ? `<table class="grp"><thead><tr><th>Data to</th><th class="n">Sessions</th><th class="n">Tested</th><th class="n">Passed</th><th>Events</th><th class="n">Time</th></tr></thead><tbody>
    ${r.map((x) => `<tr><td>${esc(x.data_to)}</td><td class="n">${x.sessions}</td><td class="n">${x.tested}${x.ideas_from_claude ? ` <span class="muted small">(${x.ideas_from_claude} from Claude)</span>` : ""}</td><td class="n">${x.passed}</td>
      <td class="small">${(x.events || []).map((e) => `${esc(e.kind)} #${e.id}`).join(", ") || '<span class="muted">—</span>'}${x.timed_out ? ' <span class="caution">time budget hit</span>' : ""}</td><td class="n">${Math.round((x.seconds || 0) / 60)}m</td></tr>`).join("")}</tbody></table>`
    : '<p class="muted">No runs yet.</p>';
}

function renderFw(v) {
  FW.data = v;
  const st = v.status, rules = st.rules, d = v.data || {}, last = st.last_run;
  $("#fw-strip").innerHTML = [
    stat("Data", d.sessions ? fmt(d.sessions, 0) : "—", d.last ? `sessions, ${esc(d.first)} → ${esc(d.last)}; grows daily` : "copied on the first run"),
    stat("Ideas tested", fmt(v.total_trials, 0), (v.shared ? `${fmt(v.shared.total, 0)} across both labs` : "") + (v.lab_ideas_waiting ? ` · ${v.lab_ideas_waiting} Strategy Lab ideas queued` : "")),
    stat("Paper trading", v.counts.paper, `${v.counts.queued} waiting for a slot`),
    stat("Ready for you", v.counts.ready, v.counts.ready ? "approve below" : "needs " + rules.min_sessions + " unseen sessions", v.counts.ready ? "" : ""),
    stat("Live settings", last && last.live ? fwPct(last.live.win_rate) : "—", last && last.live ? `${last.live.trades} trades · net/DD ${fmt(last.live.ratio, 1)} on all data` : ""),
  ].join("");
  $("#fw-steps").innerHTML = `
    <li><b>Ideas.</b> ${st.per_day} a day: the Strategy Lab's candidates, holdout failures and near-misses first (up to half the day), then one-setting moves around the live settings and pairs of near-misses${st.agent ? ", and up to 4 from Claude, who reads both labs' results" : ""}. Ideas either lab clearly rejected are skipped.</li>
    <li><b>Walk-forward on all ${d.sessions || ""} sessions.</b> Beat the live settings on win rate (+1 pt) and profit/drawdown (+5%), keep 80% of the trades and 90% of the profit, win rate at least as good in ${rules.min_fold_wins} of ${rules.folds} periods, a neighbouring value also works, no edge on random data.${rules.profit_route ? ` <b>Or the profit route:</b> hold the win rate (within ${fmt(rules.profit_route.wr_slack * 100, 1)} pt) with net +${fmt(rules.profit_route.net * 100, 0)}% and profit/drawdown +${fmt(rules.profit_route.ratio * 100, 0)}%, net at least as good in ${rules.min_fold_wins} of ${rules.folds} periods.` : ""}</li>
    <li><b>Paper trading on unseen sessions.</b> Passing earns a slot (up to ${rules.max_active}); results count only from the next session on.</li>
    <li><b>Ready.</b> After ${rules.min_sessions} sessions and ${rules.min_trades} trades: at least as good as live on win rate and net, better on one, and profitable (profit-route ideas: more net and better profit/drawdown, win rate within 2 pt). Clearly behind, or not ready after ${rules.max_sessions} sessions: retired.</li>
    <li><b>You approve.</b> Approve writes the settings file (the old one is backed up); the bot uses it from the next session. Nothing is approved automatically.</li>`;
  renderFwStrategies(v); renderFwTrials(v); renderFwRuns(v);
  const ro = document.body.classList.contains("readonly");
  $("#fw-desk").innerHTML = `<span class="pill ${st.busy ? "ok" : ""}">${st.busy ? esc(st.busy) + "…" : "idle"}</span>
    <span class="muted small">runs ${esc(st.time)} ET on trading days on its own copy of the data; the Strategy Lab's holdout and ledger are untouched${st.post ? "; new paper, ready and retired strategies post to the R&D channel" : ""}</span>
    ${(st.errors || []).map((e) => `<span class="neg small">${esc(e)}</span>`).join(" ")}
    <span class="spacer"></span><button class="btn" id="fw-run" ${st.busy || ro ? "disabled" : ""}>Run now</button>`;
  $("#fw-run").onclick = async () => {
    if (!confirm(`Run the forward lab now? It extends its data and backtests about ${st.per_day} ideas (around 5–10 minutes).`)) return;
    try { await api("/api/rd/forward/run", { json: {} }); toast("Forward lab started", "ok"); setTimeout(loadFw, 1500); } catch (e) { toast(e.message, "error"); }
  };
  clearTimeout(FW.t);
  if (st.busy) FW.t = setTimeout(() => { if (!document.hidden && document.querySelector('[data-panel="rd/forward"]:not([hidden])')) loadFw(); }, 5000);
}

async function loadFw() {
  try { renderFw(await api("/api/rd/forward")); } catch (e) { $("#fw-strategies").innerHTML = `<p class="muted">${esc(e.message)}</p>`; }
}

(function () {
  const prev = LOADERS.rd;
  LOADERS.rd = (sub) => { prev(sub); if (sub === "forward") loadFw(); };
  document.addEventListener("click", async (e) => {
    const ap = e.target.closest("[data-fw-approve]"), rt = e.target.closest("[data-fw-retire]");
    if (ap) {
      const s = (FW.data.strategies || []).find((x) => String(x.id) === ap.dataset.fwApprove);
      if (!confirm(`Make #${s.id} "${s.name}" the live settings?\n\nThe bot uses it from the next session; the current file is backed up.`)) return;
      try { await api("/api/rd/forward/approve", { json: { id: s.id } }); toast(`#${s.id} approved: live from the next session`, "ok"); loadFw(); } catch (err) { toast(err.message, "error"); }
      return;
    }
    if (rt) {
      if (!confirm("Retire this strategy? It stops paper trading.")) return;
      try { await api("/api/rd/forward/retire", { json: { id: +rt.dataset.fwRetire } }); toast("Retired", "ok"); loadFw(); } catch (err) { toast(err.message, "error"); }
      return;
    }
    const row = e.target.closest("tr[data-fw]");
    if (row && !e.target.closest("button")) {
      const id = +row.dataset.fw;
      FW.open.has(id) ? FW.open.delete(id) : FW.open.add(id);
      if (FW.data) renderFwStrategies(FW.data);
    }
  });
})();
})();
