/* Ported from the MNQ bot spin-off (c3.lab/mnq-bot) for DAISY's desk tabs (docs/20). Wrapped so its names stay local. */
(() => {
"use strict";
const api = window.deskApi, toast = window.deskToast;
// Swing desk -> Wide scan (docs/19.8): the setup across every liquid US stock, ranked; shadow until you switch it on.
const WD = { data: null, open: new Set() };

const pct = (x, d = 1) => x == null ? '<span class="muted">—</span>' : `<span class="${x > 0 ? "pos" : x < 0 ? "neg" : ""}">${x > 0 ? "+" : ""}${fmt(x, d)}%</span>`;
const scoreBar = (v) => v == null ? "—" : `<span class="score" style="--w:${Math.max(0, Math.min(100, v))}%" title="0 = weakest, 50 = neutral, 100 = strongest"><i></i><em>${fmt(v, 0)}</em></span>`;
const newsCell = (n) => {
  if (!n) return '<span class="muted small">not read</span>';
  const flags = (n.red_flags || []).map((f) => `<span class="flag">${esc(f)}</span>`).join(" ");
  const dot = n.score > 0 ? "pos" : n.score < 0 ? "neg" : "muted";
  return `<span class="${dot}">${n.score > 0 ? "+" : ""}${n.score ?? 0}</span> ${flags} <span class="small">${esc(n.why || "")}</span>`;
};
const statusCell = (c) => {
  const s = c.status || "";
  const cls = s === "posted" ? "ok" : s.startsWith("would post - held") ? "warn" : s.startsWith("would post") ? "ok" : s.startsWith("kept off") ? "warn" : "";
  const label = s.startsWith("would post - held") ? "would post (held)" : s.replace(/^kept off Discord: /, "held: ");
  return `<span class="pill ${cls}" title="${esc(s)}">${esc(label)}</span>`;
};

function renderWideStrip(v) {
  const r = v.run || {}, st = v.status, sb = v.scoreboard || {}, br = r.breadth || {};
  const low = br.above_50d_pct != null && br.above_50d_pct < st.min_breadth;
  $("#wd-strip").innerHTML = [
    stat("Mode", st.mode !== "posting" ? "Shadow" : (st.evidence || {}).ok ? `Posting top ${st.max_posts}` : "Posting held",
      st.mode !== "posting" ? "tracked here only; nothing posts" : (st.evidence || {}).ok ? "best few go to Discord each night" : "until the backtest clears the bar below",
      st.mode === "posting" && !(st.evidence || {}).ok ? "caution" : ""),
    stat("Stocks scanned", r.universe ? fmt(r.universe.liquid, 0) : "—", r.universe ? `liquid, of ${fmt(r.universe.common, 0)} US common stocks` : "after the first run"),
    stat("Breadth", br.above_50d_pct == null ? "—" : fmt(br.above_50d_pct, 0) + "%", `above their 50-day average${low ? `; under ${st.min_breadth}% nothing posts` : ""}`, low ? "caution" : ""),
    stat("Last scan", r.day ? r.taken : "—", r.day ? `taken of ${r.setups} setups on ${r.day.slice(5)}` : "runs after the close"),
    stat("Shadow record", sb.win_rate == null ? "—" : fmt(sb.win_rate, 1) + "%", `${sb.closed || 0} closed, ${sgn(sb.sum_R || 0, 2, "R")}; ${sb.open || 0} open, ${sb.watching || 0} working`),
  ].join("");
}

function renderRanked(v) {
  const r = v.run;
  if (!r) {
    $("#wd-ranked").innerHTML = `<p class="muted">${v.status.ready ? `The first scan runs at ${esc(v.status.time)} ET on the next trading day (or press Run now). It first downloads about six months of the market's daily bars, which takes roughly half an hour on Polygon's free plan.` : "Add ABG_POLYGON_API_KEY to .env (and ABG_FINNHUB_API_KEY for sectors, earnings and news), then restart."}</p>`;
    $("#wd-run-note").textContent = "";
    return;
  }
  $("#wd-run-note").textContent = `${r.day}, built ${(r.built || "").slice(11, 16)} ET · ${r.mode === "posting" ? `${r.posted} posted` : r.mode === "held" ? "posting held" : "shadow"} · ranked by ${r.rank === "score" ? "score" : "nearest to entry"}`;
  const rows = r.candidates || [];
  if (!rows.length) { $("#wd-ranked").innerHTML = '<p class="muted">No stock printed the setup on that session.</p>'; return; }
  $("#wd-ranked").innerHTML = `<table class="ranked"><thead><tr><th class="n">#</th><th>Stock</th><th>Score</th><th>Gate</th><th class="n" title="last close to the order, in daily ATRs: small = likely to fill">To entry</th><th class="n">20d vs SPY</th><th>Sector</th><th class="n">Volume</th><th>News</th><th>Earnings</th><th>Plan</th><th>Status</th></tr></thead><tbody>
    ${rows.map((c) => {
      const f = c.features || {}, p = c.profile || {}, n = c.news || {};
      const open = WD.open.has(c.symbol);
      const heads = (n.headlines || []).map((h) => `<li><span class="muted small">${esc(h.at || "")}</span> ${h.url ? `<a href="${esc(h.url)}" target="_blank" rel="noopener">${esc(h.headline)}</a>` : esc(h.headline)}</li>`).join("");
      return `<tr class="${c.take ? "" : "skip"}" data-wsym="${esc(c.symbol)}" tabindex="0">
        <td class="n">${c.rank || ""}</td>
        <td><b class="sym">${esc(c.symbol)}</b> <span class="muted small">${esc((c.name || "").slice(0, 28))}</span></td>
        <td>${c.take ? scoreBar(c.score) : '<span class="muted small">—</span>'}</td>
        <td>${c.take ? `<span class="tier t-${esc(c.tier)}">${esc(c.tier)}</span>` : `<span class="muted small" title="${esc((c.reasons || []).join("; "))}">skip</span>`}</td>
        <td class="n">${c.dist_atr == null ? "—" : fmt(c.dist_atr, 1) + " ATR"}</td>
        <td class="n">${pct(f.rs20)}</td>
        <td>${p.sector_etf ? `${esc(p.sector_etf)} ${pct(f.sector_rs20)}` : '<span class="muted small">—</span>'}</td>
        <td class="n">${f.vol_surge ? `×${fmt(f.vol_surge, 1)}` : "—"}</td>
        <td class="news">${c.take ? newsCell(c.news) : ""}</td>
        <td>${c.earnings ? `<span class="caution small" title="inside the hold window: half size">${esc(c.earnings.slice(5))}</span>` : ""}</td>
        <td class="small">${c.take ? `limit ${lv(c.limit)} · stop ${lv(c.stop)} · TP ${lv(c.tp)}` : ""}</td>
        <td>${c.take ? statusCell(c) : ""}</td></tr>
        ${open ? `<tr class="detail"><td></td><td colspan="11">
          <p class="small">${esc(c.note || "")}${p.industry ? ` · ${esc(p.industry)}` : ""}${p.mcap_m ? ` · $${fmt(p.mcap_m / 1000, 1)}B` : ""} · ATR ${fmt(c.atr_pct, 1)}% · risk ${fmt(c.risk_pct, 2)}%</p>
          ${c.parts ? `<p class="small muted">score parts (−1 to 1): ${Object.entries(c.parts).map(([k, x]) => `${k} ${x > 0 ? "+" : ""}${fmt(x, 2)}`).join(" · ")}</p>` : ""}
          ${heads ? `<ul class="plain heads">${heads}</ul>` : ""}${n.by ? `<p class="muted small">news read by ${esc(n.by)}</p>` : ""}</td></tr>` : ""}`;
    }).join("")}</tbody></table>
    <p class="muted small">Taken setups are ranked by ${r.rank === "score" ? "score" : "how close the last close is to the order (likeliest to fill first); in the backtest this beat the score, whose top picks did worst"}. Score: strength vs SPY over 20 days (35%) and 60 days (20%), the sector ETF vs SPY (15%), volume vs its 20-day average (10%), headlines (20%); earnings inside the hold window subtract and halve the size. Red flags and weak breadth keep an idea off Discord; it's still tracked.</p>`;
}

function renderWideBook(v) {
  const ideas = v.ideas || [];
  $("#wd-book-note").textContent = ideas.length ? `${ideas.filter((i) => i.state === "FILLED").length} open, ${ideas.filter((i) => i.state === "WATCHING").length} working` : "";
  $("#wd-book").innerHTML = ideas.length ? ideas.map((i) => i.state === "FILLED" ? posRow(i) : ordRow(i)).join("")
    : '<p class="muted">Nothing open yet. Every idea the gate takes is tracked here with the same fills and exits as the Book, so the shadow record builds up before anything posts.</p>';
}

function btRow(name, x) {
  if (!x || !x.n) return "";
  return `<tr><td>${esc(name)}</td><td class="n">${x.n}</td><td class="n">${x.never_filled}</td><td class="n">${x.closed}</td>
    <td class="n">${x.win_rate == null ? "—" : fmt(x.win_rate, 1) + "%"}</td><td class="n">${x.avg_r == null ? "—" : sgn(x.avg_r, 2)}</td><td class="n">${sgn(x.sum_r, 2)}</td></tr>`;
}
function btButton(v) {
  const job = v.status.backtest_job || {}, running = job.queued && !job.error;
  const ok = v.status.ready && !running && !document.body.classList.contains("readonly");
  return `<div class="btns"><button class="btn" id="wd-bt-run" ${ok ? "" : "disabled"}>${running ? "Backtest running…" : v.backtest ? "Run again" : "Run backtest"}</button>
    ${job.error ? `<span class="neg small">${esc(job.error)}</span>` : running ? `<span class="muted small">queued ${ago(job.queued)}; it waits for a running scan, then replays every stock since ${esc(job.start)}</span>` : ""}</div>`;
}
function wireBtButton() {
  const b = $("#wd-bt-run");
  if (b) b.onclick = async () => {
    if (!confirm("Backtest the wide scan on all the stored history? It runs inside the bot (a few minutes; longer if days are missing).")) return;
    try { await api("/api/swing/wide/backtest", { json: {} }); toast("Backtest started", "ok"); setTimeout(loadWide, 1500); } catch (e) { toast(e.message, "error"); }
  };
}
function renderBacktest(v) {
  const b = v.backtest;
  if (!b) {
    $("#wd-bt-note").textContent = "";
    $("#wd-bt").innerHTML = `<p class="muted">Replays every liquid stock since July 1 through the gate and the v1 exits, so you can see whether the setup holds up outside the channel's picks before anything posts.</p>${btButton(v)}`;
    wireBtButton();
    return;
  }
  const s = b.summary, top = Object.keys(s).find((k) => /^top_\d+_per_day$/.test(k));
  const set = b.settings || {}, ev = v.status.evidence || {}, n = (top || "top_3").split("_")[1];
  $("#wd-bt-note").textContent = `${b.start} → ${b.end}, built ${(b.built || "").slice(0, 10)}`;
  const ch = b.channel || {}, rc = s.rank_compare || {}, pu = s.posting_unseen;
  const gate = `<div class="gatebox ${ev.ok ? "ok" : ""}"><b>${ev.ok ? "Posting allowed" : "Posting held"}</b>
    <span class="small">${esc(ev.why || "")}.</span>
    <span class="small muted">Posting needs ≥ ${fmt(v.status.evidence_min_trades || 40, 0)} closed trades of what would post, ≥ ${fmt(v.status.evidence_min_win || 65, 0)}% wins and a positive total R, counted only outside the window the ranking and chase were chosen on.</span></div>`;
  const hist = v.status.history || {};
  $("#wd-bt").innerHTML = `${gate}<p class="small">${fmt(s.setups, 0)} setups in ${fmt(s.symbols, 0)} stocks; the gate took ${fmt(s.taken, 0)}.
      Settings: ranked by ${set.rank === "score" ? "score" : "nearest to entry"}${set.chase_atr ? `, chase up to ${fmt(set.chase_atr, 1)} ATR` : ", no chase"}.
      ${hist.target ? `History: ${fmt(hist.stored, 0)} of ${fmt(hist.target, 0)} sessions stored (more each night; re-run Saturdays).` : ""}</p>
    <div class="scroll"><table class="grp"><thead><tr><th></th><th class="n">Taken</th><th class="n">No fill</th><th class="n">Closed</th><th class="n">Win rate</th><th class="n">Avg R</th><th class="n">Total R</th></tr></thead><tbody>
    ${btRow("All taken", s.all_taken)}
    ${rc.entry ? btRow(`Top ${n} a day, nearest to entry${set.rank !== "score" ? " (posts)" : ""}`, rc.entry) : ""}
    ${rc.score ? btRow(`Top ${n} a day by score${set.rank === "score" ? " (posts)" : ""}`, rc.score) : top ? btRow(`Top ${n} a day by score`, s[top]) : ""}
    ${pu ? btRow(`What posts, outside ${(pu.excludes || []).join(" to ")}`, pu) : ""}
    ${s.chased ? btRow("Chased entries", s.chased) : ""}
    ${Object.entries(s.by_entry_distance || {}).filter(([k]) => k !== "cuts").map(([k, x]) => btRow(`To entry: ${k}`, x)).join("")}
    ${Object.entries(s.by_score || {}).filter(([k]) => k !== "cuts").map(([k, x]) => btRow(`Score: ${k}`, x)).join("")}
    ${btRow("Tier A", (s.by_tier || {}).A)}${btRow("Tier B", (s.by_tier || {}).B)}
    ${btRow("Earnings in the window", s.earnings_in_window)}${btRow("Outside the core 85", s.outside_core)}</tbody></table></div>
    <p class="small">For comparison, the channel's ideas: ${ch.closed ?? "—"} closed, ${ch.win_rate ?? "—"}% wins, ${sgn(ch.sum_R ?? 0, 2, "R")}.</p>
    <ul class="plain muted small">${(b.notes || []).map((n) => `<li>${esc(n)}</li>`).join("")}</ul>${btButton(v)}`;
  wireBtButton();
}

function renderWideDesk(v) {
  const st = v.status, src = st.sources || {};
  const chip = (ok, name, why) => `<span class="pill ${ok ? "ok" : ""}" title="${esc(why)}">${ok ? "✓" : "✗"} ${name}</span>`;
  const pr = st.progress || {};
  const prog = st.busy ? `<span class="small">${esc(st.busy)}${pr.step === "bars" ? ` · ${pr.done}/${pr.of} days, about ${pr.eta_min} min left` : pr.step === "history" ? ` · ${pr.done}/${pr.of} older days` : ""}</span>` : "";
  $("#wd-desk").innerHTML = `${chip(src.polygon, "Polygon", "whole-market daily bars")} ${chip(src.finnhub, "Finnhub", "sectors, earnings, headlines")} ${chip(src.claude, "Claude", "reads the headlines")}
    <span class="muted small">runs ${esc(st.time)} ET on trading days · price ≥ $${fmt(st.min_price, 0)}, ≥ $${fmt(st.min_dollar_vol_m, 0)}M a day · ${st.bars.days} sessions stored${st.bars.last ? ` (to ${st.bars.last})` : ""}</span>
    ${prog}<span class="spacer"></span>
    <button class="btn" id="wd-run" ${st.ready && !st.busy && !document.body.classList.contains("readonly") ? "" : "disabled"}>Run now</button>`;
  const b = $("#wd-run");
  if (b) b.onclick = async () => {
    if (!confirm(st.bars.days ? "Run the wide scan for the last finished session now?" : "Run the first wide scan now? It downloads about six months of the market's bars first (roughly 30 minutes); the page shows progress.")) return;
    try { await api("/api/swing/wide/run", { json: {} }); toast("Wide scan started", "ok"); setTimeout(loadWide, 1500); } catch (e) { toast(e.message, "error"); }
  };
}

async function loadWide() {
  let v;
  try { v = await api("/api/swing/wide"); } catch (e) { $("#wd-ranked").innerHTML = `<p class="muted">${esc(e.message)}</p>`; return; }
  WD.data = v;
  renderWideStrip(v); renderRanked(v); renderWideBook(v); renderBacktest(v); renderWideDesk(v);
  clearTimeout(WD.t);
  if (v.status.busy || (v.status.backtest_job || {}).queued) WD.t = setTimeout(() => { if (!document.hidden && document.querySelector('[data-panel="swing/wide"]:not([hidden])')) loadWide(); }, 5000);
}

(function () {
  const prev = LOADERS.swing;
  LOADERS.swing = (sub) => { prev(sub); if (sub === "wide") loadWide(); };
  document.addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-wsym]");
    if (!tr || e.target.closest("a")) return;
    const s = tr.dataset.wsym;
    WD.open.has(s) ? WD.open.delete(s) : WD.open.add(s);
    if (WD.data) renderRanked(WD.data);
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.matches("tr[data-wsym]")) e.target.click(); });
  const prevEv = window.onSwingEvent;
  window.onSwingEvent = (d) => { if (prevEv) prevEv(d); if (d.type === "wide" && document.querySelector('[data-panel="swing/wide"]:not([hidden])')) loadWide(); };
})();
window.loadWide = loadWide;
})();
