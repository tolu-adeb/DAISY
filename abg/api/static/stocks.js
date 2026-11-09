/* DAISY - Stocks tab (docs/21): the four-book rule system. Today's candidates and their write-ups, the market
   regime and stress flags, capital by book, the scorecard vs the benchmark, and what the bar store holds.
   Also adds the "Stock research" sub-tab to R&D. Every value is escaped; secrets never reach the page - the
   API only says whether a webhook is set. */
"use strict";
(() => {
const api = window.deskApi, toast = window.deskToast;
const ST = { data: null, memoId: null, uni: null };
const p1 = (x, d = 1) => (x == null ? "—" : `${x > 0 ? "+" : ""}${fmt(x * 100, d)}%`);
const p0 = (x) => (x == null ? "—" : `${fmt(x * 100, 0)}%`);
const sg = (x, d = 1) => (x == null ? '<span class="muted">—</span>' : `<span class="${x > 0 ? "pos" : x < 0 ? "neg" : ""}">${p1(x, d)}</span>`);
const px = (x) => (x == null ? "—" : (Math.abs(x) >= 1000 ? fmt(x, 0) : fmt(x, 2)));
const post = (path, body) => api(path, { json: body || {} });
const SHORT = { dual_momentum: "Dual mom.", xs_momentum: "12-1 mom.", trend_pullback: "Pullback",
                mean_reversion: "Reversion", crisis_alpha: "Crisis" };
const REGIME = { bull_trend: ["trending", "ok"], bull_chop: ["choppy, rising", "warn"], risk_off: ["risk-off", "bad"],
                 crisis: ["CRISIS", "bad"], warmup: ["warming up", ""] };
const pill = (txt, cls) => `<span class="pill ${cls || ""}">${esc(txt)}</span>`;
// pitch.js exposes its small markdown renderer; the gate checklist reads better as ticks and crosses
const md = (t) => (window.deskMd ? window.deskMd((t || "").replace(/^- \[x\] /gm, "- ✓ ").replace(/^- \[ \] /gm, "- ✗ ")) : `<pre>${esc(t)}</pre>`);

// ------------------------------------------------------------------ today
function renderToday(r) {
  const st = r.status || {}, t = r.today || {}, s = t.state, cands = t.candidates || [];
  const ld = st.last_daily || {};
  const opens = cands.filter((c) => c.action === "BUY" || c.action === "REBALANCE");
  const closes = cands.filter((c) => c.action === "SELL");
  const [rw, rc] = REGIME[(s && s.regime) || ""] || ["—", ""];
  $("#sk-kpis").innerHTML = [
    stat("Signals for", s ? esc(s.date) : "—", ld.at ? `pass ran ${esc(ld.at.slice(0, 16).replace("T", " "))}` : "no daily pass yet"),
    stat("Regime", s ? pill(rw, rc) : "—", s ? `stress ${fmt(s.stress, 2)}${s.crisis ? " · crisis mode" : ""}` : ""),
    stat("To open", opens.length, `${closes.length} to close`),
    stat("Paper equity", s ? usd(s.equity).replace("+", "") : "—", s ? `${p1(s.drawdown)} from peak` : ""),
    stat("Pitches", st.pitches || 0, "every one marked vs the benchmark"),
  ].join("");

  const u = st.universe || {};
  $("#sk-desk").innerHTML = `<b>Stock desk</b>
    <span class="${st.enabled ? "pos" : "muted"}">${st.enabled ? `daily pass ${esc(st.daily_time)} weekdays` : "off (ABG_STOCKS_ENABLED)"}</span>
    <span class="${st.webhook ? "pos" : "muted"}">${st.webhook ? `Discord connected${st.tag ? ` · tag ${esc(st.tag)}` : ""}` : "no Discord webhook: pitches stay here"}</span>
    <span class="${u.seed ? "neg" : "muted"}">${u.seed ? "seed universe (survivorship-biased)" : `${esc(u.index)} · ${u.stocks} stocks`}</span>
    ${(st.errors || []).slice(-2).map((e) => `<span class="neg">${esc(e.where)}: ${esc(e.error)}</span>`).join("")}
    <span class="spacer"></span>
    <button class="btn small" id="sk-run" title="Replay the books to the latest close and write today's pitches. Never posts from here.">Run daily pass</button>
    <button class="btn small" id="sk-sweep" title="Refresh the next page of the universe">Sweep now</button>`;
  $("#sk-run").onclick = runDaily;
  $("#sk-sweep").onclick = async () => { busy(true); try { const x = await post("/api/stocks/sweep"); toast(`Sweep: ${x.ok} ok, ${x.failed} failed`); } catch (e) { toast(e.message); } finally { busy(false); } };

  if (!s) {
    $("#sk-context").innerHTML = '<p class="muted">No daily pass yet. It runs after the close on weekdays, or press <b>Run daily pass</b> once the bar store has history (Universe tab).</p>';
    $("#sk-list").innerHTML = "";
    return;
  }
  const flags = Object.entries(s.flags || {});
  const on = flags.filter(([, v]) => v).map(([k]) => k);
  const w = Object.entries(s.weights || {}).sort((a, b) => (b[1] || 0) - (a[1] || 0));
  $("#sk-context").innerHTML = `
    <div class="row two">
      <div><h3>Capital by book</h3><table><tbody>${w.map(([k, v]) => `<tr><td>${esc(SHORT[k] || k)}</td><td class="n">${p0(v)}</td>
        <td class="n muted">${usd((s.budgets || {})[k]).replace("+", "")}</td></tr>`).join("")}</tbody></table>
        <p class="muted small">Set by the Hedge allocator from how each book has done lately; every book keeps at least its floor.</p></div>
      <div><h3>Early-warning flags</h3>${flags.length ? `<p>${on.length ? on.map((k) => pill(k, "warn")).join(" ") : '<span class="muted">none on</span>'}</p>
        <p class="muted small">${on.length} of ${flags.length} on · stress ${fmt(s.stress, 2)} · crisis mode starts at half of them.</p>` : '<p class="muted">no flags available (missing inputs)</p>'}
        ${(s.warnings || []).length ? `<p class="neg small">${esc(s.warnings[0])}</p>` : ""}</div>
    </div>`;

  $("#sk-list").innerHTML = cands.length ? `<table><thead><tr><th>Action</th><th>Ticker</th><th>Book</th><th class="n">Close</th>
      <th class="n">Stop</th><th class="n">Size</th><th class="n">Rank</th><th class="n">At risk</th><th></th></tr></thead><tbody>
    ${cands.map((c) => `<tr><td>${pill(c.action, c.action === "SELL" ? "bad" : c.action === "BUY" ? "ok" : "")}</td>
      <td><b>${esc(c.symbol)}</b></td><td>${esc(SHORT[c.book] || c.book)}</td><td class="n">${px(c.price)}</td>
      <td class="n">${px(c.stop)}</td><td class="n">${c.shares ? fmt(c.shares, 0) : "—"}</td>
      <td class="n">${c.rank ? `${c.rank}/${c.of}` : "—"}</td><td class="n">${c.r_dollars ? usd(c.r_dollars).replace("+", "") : "—"}</td>
      <td><button class="btn small" data-skmemo="${esc(c.id)}">Thesis</button></td></tr>`).join("")}</tbody></table>
    <p class="muted small">Signals from the ${esc(s.date)} close, to act on at the next open. Paper only, research not advice.</p>`
    : '<p class="muted">Nothing to do: no book\'s entry conditions were met and nothing held hit an exit. Standing pat is the signal.</p>';
}

async function runDaily() {
  const b = $("#sk-run"); if (b) { b.disabled = true; b.textContent = "Running…"; }
  busy(true);
  try { const r = await post("/api/stocks/daily", { post: false }); toast(`Daily pass: ${r.opens} to open, ${r.closes} to close (not posted)`); await load(); }
  catch (e) { toast(e.message); }
  finally { busy(false); }
}

// ------------------------------------------------------------------ memo
async function showMemo(id) {
  ST.memoId = id;
  window.showView("stocks", "memo");
  const c = ((ST.data && ST.data.today && ST.data.today.candidates) || []).find((x) => x.id === id);
  $("#sk-memo-note").textContent = id;
  try {
    const r = await api(`/api/stocks/memo/${encodeURIComponent(id)}`);
    $("#sk-memo").innerHTML = md(r.markdown);
  } catch (e) {
    // Book A rebalances and closes get a digest line, not a full write-up
    $("#sk-memo").innerHTML = c ? `<h2>${esc(c.action)} ${esc(c.symbol)}</h2><p>${esc(c.what_happened)}</p>
      <h3>Exits</h3><ul>${(c.exits || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
      <p class="muted small">${esc(c.reason || "")}</p>` : `<p class="neg">${esc(e.message)}</p>`;
  }
}

// ------------------------------------------------------------------ scorecard
function renderScore(sc) {
  const a = (sc && sc.all) || {}, by = (sc && sc.by_book) || {}, rows = (sc && sc.pitches) || [];
  $("#sk-score-kpis").innerHTML = [
    stat("Pitches", a.n || 0, a.n ? `${a.marked || 0} marked so far (a pitch counts once a session has passed)` : "none yet"),
    stat("Avg return", a.avg_ret == null ? "—" : sg(a.avg_ret), ""),
    stat("Avg vs benchmark", a.avg_excess == null ? "—" : sg(a.avg_excess), ""),
    stat("Beat it", a.beat_spy == null ? "—" : p0(a.beat_spy), "of marked pitches"),
  ].join("");
  const books = Object.entries(by);
  $("#sk-score").innerHTML = (books.length ? `<table><thead><tr><th>Book</th><th class="n">Pitches</th><th class="n">Avg return</th>
      <th class="n">vs benchmark</th><th class="n">Beat it</th></tr></thead><tbody>${books.map(([b, v]) => `<tr><td>${esc(SHORT[b] || b)}</td>
      <td class="n">${v.n}</td><td class="n">${sg(v.avg_ret)}</td><td class="n">${sg(v.avg_excess)}</td><td class="n">${v.beat_spy == null ? "—" : p0(v.beat_spy)}</td></tr>`).join("")}</tbody></table>` : "")
    + (rows.length ? `<h3>Every pitch</h3><table><thead><tr><th>Signal</th><th>Ticker</th><th>Book</th><th class="n">Entry</th><th class="n">Last</th>
      <th class="n">Return</th><th class="n">Benchmark</th><th class="n">vs bench</th><th></th></tr></thead><tbody>
      ${rows.map((r) => `<tr><td>${esc(r.as_of)}</td><td><b>${esc(r.symbol)}</b></td><td>${esc(SHORT[r.book] || r.book)}</td>
        <td class="n">${px(r.price)}</td><td class="n">${px(r.last)}</td><td class="n">${sg(r.ret)}</td><td class="n">${sg(r.spy_ret)}</td>
        <td class="n">${sg(r.excess)}</td><td><button class="btn small" data-skmemo="${esc(r.id)}">Thesis</button></td></tr>`).join("")}</tbody></table>`
      : '<p class="muted">No pitches yet.</p>');
}

// ------------------------------------------------------------------ universe
async function loadUniverse() {
  let r;
  try { r = await api("/api/stocks/universe"); } catch (e) { $("#sk-uni").innerHTML = `<p class="neg">${esc(e.message)}</p>`; return; }
  ST.uni = r;
  const u = r.universe || {}, s = r.store || {}, w = r.sweep || {};
  $("#sk-uni-kpis").innerHTML = [
    stat("Universe", fmt(u.daily || u.core, 0), `${u.stocks} index + ${u.market || 0} market · ${u.etfs} ETFs`),
    stat("Index", esc(u.index || "—"), u.seed ? "seed list: survivorship-biased" : `${esc(u.source || "")} ${esc(u.as_of || "")}`, u.seed ? "neg" : ""),
    stat("Bars stored", fmt(s.bars, 0), `${s.symbols || 0} symbols · to ${esc(s.last_day || "—")}`),
    stat("Short of history", (r.missing || []).length, (r.missing || []).length ? "backfill to fill them" : "all followed names ready"),
    stat("Sweep", `${w.page}/${w.every_minutes}m`, `full cycle ≈ ${w.cycle_hours}h · last ${esc((w.last_sweep || "never").slice(0, 16).replace("T", " "))}`),
  ].join("");
  $("#sk-uni-bar").innerHTML = `<span class="muted">${s.errors ? `${s.errors} symbol(s) with a last error · ` : ""}${s.stale_symbols || 0} stale (36h+) · ${s.membership_rows ? `${s.membership_rows} membership rows` : "no point-in-time membership"}</span>
    <span class="spacer"></span>
    ${u.seed ? '<button class="btn small" id="sk-index" title="Needs an FMP key">Load S&amp;P 500 list</button>' : ""}
    <button class="btn small" id="sk-backfill" title="Fetch years of daily history for every name that is short of it">Backfill missing</button>`;
  const ib = $("#sk-index");
  if (ib) ib.onclick = async () => { busy(true); try { const x = await post("/api/stocks/universe", { index: "sp500" }); toast(`Loaded ${x.symbols} symbols`); await loadUniverse(); } catch (e) { toast(e.message); } finally { busy(false); } };
  $("#sk-backfill").onclick = async () => {
    const b = $("#sk-backfill"); b.disabled = true; b.textContent = "Backfilling… (minutes)";
    try { const x = await post("/api/stocks/backfill"); toast(`Backfill: ${x.ok} ok, ${x.failed} failed`); await loadUniverse(); }
    catch (e) { toast(e.message); } finally { b.disabled = false; b.textContent = "Backfill missing"; }
  };
  const rows = (r.symbols || []).slice().sort((a, b) => (a.bars - b.bars) || a.symbol.localeCompare(b.symbol));
  $("#sk-uni").innerHTML = `<table><thead><tr><th>Symbol</th><th class="n">Bars</th><th>Last bar</th><th class="n">Updated</th><th>Source</th><th>Note</th></tr></thead><tbody>
    ${rows.map((x) => `<tr><td><b>${esc(x.symbol)}</b></td><td class="n ${x.bars < 250 ? "neg" : ""}">${fmt(x.bars, 0)}</td><td>${esc(x.last_day || "—")}</td>
      <td class="n">${x.age_h == null ? "—" : `${fmt(x.age_h, 0)}h ago`}</td><td class="muted">${esc(x.provider || "")}</td>
      <td class="${x.error ? "neg" : "muted"} small">${x.delisted ? "parked (repeated failures)" : esc((x.error || "").slice(0, 80))}</td></tr>`).join("")}</tbody></table>`;
}

// ------------------------------------------------------------------ R&D: stock research
async function loadResearch() {
  let r;
  try { r = await api("/api/stocks/research"); } catch (e) { $("#rs-report").innerHTML = `<p class="neg">${esc(e.message)}</p>`; return; }
  const rep = r.report, v = (rep && rep.verdict) || {};
  const cls = { passes: "ok", fails: "bad" }[v.overall] || "warn";
  $("#rs-bar").innerHTML = `<b>Stock desk research</b>
    ${rep ? pill(`verdict: ${v.overall || "—"}`, cls) : '<span class="muted">no study yet</span>'}
    <span class="muted">${rep ? `${esc((rep.at || "").slice(0, 16).replace("T", " "))} · ${rep.runs} runs` : ""} · schedule: ${esc(r.schedule || "off")} (Saturdays 09:00 ET, posted to the R&amp;D channel)</span>
    ${r.busy ? `<span class="pos">running: ${esc(r.busy)}</span>` : ""}
    <span class="spacer"></span>
    <button class="btn small" id="rs-quick" ${r.busy ? "disabled" : ""} title="Ablations over 5 years - a few minutes">Quick study</button>
    <button class="btn small" id="rs-full" ${r.busy ? "disabled" : ""} title="Ablations, walk-forward and early-warning - can take a long time on 500 names">Full study</button>`;
  const start = async (scope) => {
    try { await post("/api/stocks/research", { scope }); toast(`Study started (${scope}). It never posts from here.`); setTimeout(loadResearch, 1500); }
    catch (e) { toast(e.message); }
  };
  $("#rs-quick").onclick = () => start("quick");
  $("#rs-full").onclick = () => start("full");
  $("#rs-report").innerHTML = r.markdown ? md(r.markdown)
    : '<p class="muted">No study saved yet. The R&amp;D desk runs one every Saturday when the stock desk is on, or start one above. Findings are proposals: nothing here changes a live setting.</p>';
  if (r.busy) setTimeout(() => { const v_ = document.getElementById("view-rd"); if (v_ && !v_.hidden) loadResearch(); }, 20000);
}

// ------------------------------------------------------------------ wiring
async function load(sub) {
  if (sub === "universe") return loadUniverse();
  if (sub === "memo") { if (ST.memoId) return; }
  let r;
  try { r = await api("/api/stocks"); } catch (e) { $("#sk-desk").innerHTML = `<span class="neg">${esc(e.message)}</span>`; return; }
  ST.data = r;
  renderToday(r);
  renderScore(r.scorecard);
}

document.addEventListener("click", (e) => {
  const m = e.target.closest("[data-skmemo]"); if (m) showMemo(m.dataset.skmemo);
});
LOADERS.stocks = (sub) => load(sub);
const prevRd = LOADERS.rd;
LOADERS.rd = (sub) => { prevRd(sub); if (sub === "stocks") loadResearch(); };
window.stocksRefresh = () => { const v = document.getElementById("view-stocks"); if (v && !v.hidden) load(); };
window.stocksResearchRefresh = () => { const v = document.getElementById("view-rd"); if (v && !v.hidden) loadResearch(); };
})();
