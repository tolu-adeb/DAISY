/* DAISY - Pitch Desk tab (docs/20): factor screen, memos with sources, scorecard vs SPY. */
"use strict";
(() => {
const PT = { data: null, memo: null };
const p1 = (x) => (x == null ? "—" : `${x > 0 ? "+" : ""}${fmt(x * 100, 1)}%`);
const sg = (x) => (x == null ? '<span class="muted">—</span>' : `<span class="${x > 0 ? "pos" : x < 0 ? "neg" : ""}">${p1(x)}</span>`);
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

// tiny markdown renderer for memos (headings, bold, italics, tables, lists, links) - text is escaped first
function md(text) {
  const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\*(.+?)\*/g, "<i>$1</i>").replace(/`(.+?)`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (m, t, u) => `<a href="${u}" target="_blank" rel="noopener noreferrer">${t}</a>`)
    .replace(/(^|\s)(https?:\/\/[^\s<]+)/g, (m, sp, u) => `${sp}<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`);
  const out = [];
  let list = false, table = null;
  const flush = () => { if (list) { out.push("</ul>"); list = false; } if (table) { out.push(table + "</tbody></table>"); table = null; } };
  for (const line of (text || "").split("\n")) {
    if (/^\|/.test(line)) {
      const cells = line.split("|").slice(1, -1).map((c) => c.trim());
      if (cells.every((c) => /^-+$/.test(c))) continue;
      if (!table) { table = `<table><thead><tr>${cells.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>`; continue; }
      table += `<tr>${cells.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`; continue;
    }
    if (table) { out.push(table + "</tbody></table>"); table = null; }
    if (/^- /.test(line)) { if (!list) { out.push("<ul>"); list = true; } out.push(`<li>${inline(line.slice(2))}</li>`); continue; }
    flush();
    const h = line.match(/^(#{1,3}) (.*)/);
    if (h) out.push(`<h${h[1].length + 1}>${inline(h[2])}</h${h[1].length + 1}>`);
    else if (line.trim()) out.push(`<p>${inline(line)}</p>`);
  }
  flush();
  return out.join("");
}

function renderStatus(st, sc) {
  const a = (sc && sc.all) || {};
  $("#pt-kpis").innerHTML = [
    stat("Pitches", a.n || 0, a.n ? `${a.marked} marked to market` : "none yet"),
    stat("Avg vs SPY", a.avg_excess == null ? "—" : sg(a.avg_excess), "since each pitch"),
    stat("Beat SPY", a.beat_spy == null ? "—" : fmt(a.beat_spy * 100, 0) + "%", "of marked pitches"),
    stat("LONG calls", ((sc && sc.by_verdict && sc.by_verdict.LONG) || {}).n || 0,
      (sc && sc.by_verdict && sc.by_verdict.LONG && sc.by_verdict.LONG.avg_excess != null) ? `avg ${p1(sc.by_verdict.LONG.avg_excess)} vs SPY` : "fair value 15%+ above price and top third"),
    stat("Universe", st.universe, st.last_screen ? `screened ${esc(st.last_screen.slice(0, 16).replace("T", " "))}` : "not screened yet"),
  ].join("");
  $("#pt-desk").innerHTML = `<b>Pitch desk</b>
    <span class="${st.enabled ? "pos" : "muted"}">weekly batch ${st.enabled ? "on" : "off (ABG_PITCH_ENABLED)"}: ${esc(st.schedule)}, ${st.max_names} names</span>
    <span class="${st.webhook ? "pos" : "muted"}">Discord ${st.webhook ? "connected" : "not set (ABG_PITCH_WEBHOOK_URL): memos stay here"}</span>
    <span>Claude write-up ${st.ai_memo ? "on" : "off"}</span>
    ${(st.errors || []).slice(-2).map((e) => `<span class="neg">${esc(e.where)}: ${esc(e.error)}</span>`).join("")}
    <span class="spacer"></span>
    <button class="btn small" id="pt-screen-run">Run screen</button>
    <button class="btn small" id="pt-score-run" title="Mark every pitch against SPY now">Update scorecard</button>`;
  $("#pt-screen-run").onclick = runScreen;
  $("#pt-score-run").onclick = async () => { busy(true); try { await post("/api/pitch/score"); await loadPitch(); toast("Scorecard updated"); } catch (e) { toast(e.message); } finally { busy(false); } };
}

function renderScreen(scr) {
  if (!scr) { $("#pt-screen").innerHTML = '<p class="muted">No screen yet. Run it once (about a minute for the full universe; cached afterwards).</p>'; return; }
  $("#pt-screen-note").textContent = `${scr.n} names ranked${scr.missing && scr.missing.length ? `, ${scr.missing.length} without data` : ""}; click a row to write its memo`;
  $("#pt-screen").innerHTML = `<table><thead><tr><th class="n">#</th><th>Ticker</th><th>Name</th><th>Sector</th><th class="n">Score</th><th class="n">12-1m mom.</th><th class="n">Earn. yield</th><th class="n">Margin</th><th class="n">EPS growth</th><th class="n">Fwd P/E</th><th></th></tr></thead><tbody>
    ${scr.rows.map((r) => `<tr><td class="n">${r.rank}</td><td><b>${esc(r.symbol)}</b></td><td>${esc((r.name || "").slice(0, 28))}</td><td class="muted">${esc((r.sector || "").slice(0, 22))}</td>
      <td class="n">${r.composite == null ? "—" : sgn(r.composite, 2)}</td><td class="n">${sg(r.raw.momentum)}</td><td class="n">${p1(r.raw.value)}</td>
      <td class="n">${p1(r.raw.quality)}</td><td class="n">${sg(r.raw.growth)}</td><td class="n">${fmt(r.forward_pe, 1)}</td>
      <td><button class="btn small" data-pmake="${esc(r.symbol)}">Memo</button></td></tr>`).join("")}</tbody></table>
    <p class="muted small">Score = weighted z-scores: momentum 30%, value 25%, quality 25%, growth 15%, low volatility 5%. Research only, not advice.</p>`;
}

function renderPitches(sc) {
  const ps = (sc && sc.pitches) || [];
  $("#pt-list").innerHTML = ps.length ? `<table><thead><tr><th>Made</th><th>Ticker</th><th>Verdict</th><th class="n">Price</th><th class="n">Fair value</th><th class="n">Since</th><th class="n">SPY</th><th class="n">vs SPY</th><th></th></tr></thead><tbody>
    ${ps.map((p) => `<tr><td>${esc((p.made_at || "").slice(0, 10))}</td><td><b>${esc(p.symbol)}</b> <span class="muted">${esc((p.name || "").slice(0, 22))}</span></td>
      <td><span class="pill ${p.verdict === "LONG" ? "ok" : p.verdict === "WATCH" ? "warn" : ""}">${esc(p.verdict)}</span></td>
      <td class="n">${fmt(p.price)}</td><td class="n">${fmt(p.fair_value)} <span class="muted">${p1(p.upside)}</span></td>
      <td class="n">${sg(p.return)}</td><td class="n">${sg(p.spy_return)}</td><td class="n">${sg(p.excess)}</td>
      <td><button class="btn small" data-pmemo="${esc(p.id)}">Read</button></td></tr>`).join("")}</tbody></table>`
    : '<p class="muted">No pitches yet. Write one from the screen, or type a ticker above.</p>';
}

async function loadPitch() {
  let r;
  try { r = await api("/api/pitch"); } catch (e) { $("#pt-desk").innerHTML = `<span class="neg">${esc(e.message)}</span>`; return; }
  PT.data = r;
  renderStatus(r.status, r.scorecard);
  renderScreen(r.screen);
  renderPitches(r.scorecard);
}

async function runScreen() {
  const b = $("#pt-screen-run"); if (b) { b.disabled = true; b.textContent = "Screening…"; }
  busy(true);
  try { await post("/api/pitch/screen"); await loadPitch(); toast("Screen done"); } catch (e) { toast(e.message); }
  finally { busy(false); }
}

async function make(sym) {
  sym = (sym || "").trim().toUpperCase();
  if (!sym) return;
  window.showView("pitch", "memo");
  $("#pt-memo").innerHTML = `<p class="muted">Writing the ${esc(sym)} memo: prices, fundamentals, peers, news…</p>`;
  busy(true);
  try {
    const r = await post("/api/pitch/make", { symbol: sym, ai: $("#pt-ai") ? $("#pt-ai").checked : undefined });
    $("#pt-memo").innerHTML = md(r.memo);
    $("#pt-memo-note").textContent = `${r.id} · ${r.verdict} · saved`;
    loadPitch();
  } catch (e) { $("#pt-memo").innerHTML = `<p class="neg">${esc(e.message)}</p>`; }
  finally { busy(false); }
}

async function readMemo(id) {
  window.showView("pitch", "memo");
  try { const r = await api(`/api/pitch/memo/${encodeURIComponent(id)}`); $("#pt-memo").innerHTML = md(r.markdown); $("#pt-memo-note").textContent = id; }
  catch (e) { $("#pt-memo").innerHTML = `<p class="neg">${esc(e.message)}</p>`; }
}

document.addEventListener("click", (e) => {
  const m = e.target.closest("[data-pmake]"); if (m) { make(m.dataset.pmake); return; }
  const r = e.target.closest("[data-pmemo]"); if (r) readMemo(r.dataset.pmemo);
});
window.addEventListener("DOMContentLoaded", () => {
  const f = $("#pt-form");
  if (f) f.addEventListener("submit", (e) => { e.preventDefault(); make($("#pt-sym").value); });
});
LOADERS.pitch = (sub) => { if (sub !== "memo" || !PT.data) loadPitch(); };
window.loadPitch = loadPitch;
})();
