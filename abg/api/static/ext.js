/* ABG Intelligence Terminal — external signals tab (loaded after app.js / portfolio.js; reuses their helpers). */
"use strict";
const EXT = { ideas: [], sel: null, unread: 0, timer: null, status: null };
const extVisible = () => !$("#view-signals").hidden;
const md = (t) => esc(t).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
const lvl = (x) => x == null ? "?" : (+x).toLocaleString(LOCALE, { maximumFractionDigits: x >= 100 ? 2 : 4 });

function planText(i) {
  let e;
  if (i.entry_type === "market") e = "mkt";
  else if (i.entry_low === i.entry_high) e = ({ breakout_above: ">", limit_above: "≥", breakdown_below: "<", limit_below: "≤" }[i.entry_type] || "") + lvl(i.entry_low);
  else e = `${lvl(i.entry_low)}–${lvl(i.entry_high)}`;
  return `${e} · SL ${lvl(i.stop)}${i.stop_basis === "close" ? "c" : ""} · TP ${(i.targets || []).map(lvl).join(", ")}`;
}

async function loadExt(select) {
  try {
    const r = await api(`/api/ext/ideas?status=${$("#ext-filter").value}&limit=300`);
    EXT.ideas = r.ideas; EXT.status = r.status;
    renderExtKpis(r.stats); renderExtStatus(); renderIdeas(); renderSources(r.stats);
    const want = select ?? EXT.sel;
    if (want != null) showIdea(want, false);
    EXT.unread = 0; extBadge();
  } catch (e) { $("#ext-ideas").innerHTML = `<p class="muted">${esc(e.message)}</p>`; }
}
window.loadExt = loadExt;

function extBadge() { const b = $("#ext-badge"); b.textContent = EXT.unread; b.hidden = !EXT.unread; }

function renderExtKpis(st) {
  const pf = st.profit_factor;
  $("#ext-kpis").innerHTML =
    kpi("Open ideas", `${st.pending + st.active}`, `${st.pending} waiting for entry · ${st.active} in a trade`) +
    kpi("Closed trades", `${st.closed}`, `${st.never_filled} never filled (missed / invalidated / expired)`) +
    kpi("Win rate", st.win_rate == null ? "—" : fmt(st.win_rate * 100, 0) + "%", `${st.wins} wins · ${st.losses} losses`) +
    kpi("Average R", st.avg_r == null ? "—" : sgn(st.avg_r, 2, "R"), `total ${st.total_r >= 0 ? "+" : ""}${fmt(st.total_r, 2)}R · PF ${pf == null ? "—" : fmt(pf, 2)}`) +
    kpi("Paper P&L", sgnMoney(st.realized_pnl), "sized at the configured risk % per idea");
}

function renderExtStatus() {
  const m = PF.status || {}, x = m.external || {}, d = x.discord || {}, relay = (EXT.status || {}).relay || x.relay || {};
  const tracking = m.running ? '<span class="live"><i class="pulse"></i>Tracking live</span>'
    : m.elsewhere ? '<span class="live"><i class="pulse"></i>Tracking in another window</span>'
    : '<span class="live"><i class="pulse off"></i>Monitor stopped — ideas are not being tracked</span>';
  const disc = d.enabled ? `<span class="chan on" title="${esc((d.channels || []).join(", "))}">discord in · ${(d.channels || []).length} ch · ${ago(d.last_poll)}</span>`
    : '<span class="chan off" title="set ABG_DISCORD_BOT_TOKEN and ABG_EXT_DISCORD_CHANNEL_IDS">discord in</span>';
  const rel = relay.enabled ? `<span class="chan on">relay · ${esc(relay.mode)}</span>`
    : '<span class="chan off" title="set ABG_EXT_RELAY_WEBHOOK_URL (or ABG_DISCORD_WEBHOOK_URL)">relay</span>';
  const errs = (d.errors || []);
  $("#ext-status").innerHTML = `${tracking}<span class="muted">quotes ${ago(x.last_quotes_at || m.last_quote_sweep)}</span>
    <span class="muted">re-graded ${ago(x.last_review_at)}</span>
    ${errs.length ? `<span class="neg" title="${esc(errs.map((e) => e.where + ": " + e.error).join("\n"))}">${errs.length} discord errors</span>` : ""}
    <span class="spacer"></span>${disc} ${rel}
    <button class="btn small" id="ext-refresh">Refresh</button>`;
  $("#ext-refresh").onclick = () => loadExt();
}

function renderIdeas() {
  const rows = EXT.ideas;
  if (!rows.length) { $("#ext-ideas").innerHTML = '<p class="muted">Nothing tracked yet. Paste a signal above, or connect a Discord channel (docs/11).</p>'; return; }
  $("#ext-ideas").innerHTML = `<table><thead><tr><th class="n">#</th><th>Symbol</th><th>Status</th><th>Plan</th><th class="n">Last</th>
    <th class="n">To entry / Entry</th><th>Grade</th><th class="n">R</th><th>Source</th><th class="n">Age</th></tr></thead><tbody>${rows.map((i) => `
    <tr data-id="${i.id}" class="${i.id === EXT.sel ? "sel" : ""}">
      <td class="n">${i.id}</td>
      <td><a class="sym" data-sym="${esc(i.symbol)}">${esc(i.symbol)}</a> <span class="${i.direction === "long" ? "pos" : "neg"}">${i.direction === "long" ? "▲" : "▼"}</span></td>
      <td><span class="st ${esc(i.status)}">${esc(i.status)}</span></td>
      <td class="plan">${esc(planText(i))}</td>
      <td class="n">${fmt(i.last_price)}</td>
      <td class="n">${i.distance_pct != null ? (i.distance_pct > 0 ? "+" : "") + fmt(i.distance_pct, 1, "%") : i.entry_price != null ? fmt(i.entry_price) : "—"}</td>
      <td>${i.grade ? `<span class="grade ${esc(i.grade)}" title="${esc((i.grade_reasons || []).join("\n"))}">${esc(i.grade)}</span>` : ""}</td>
      <td class="n">${i.entry_price != null ? sgn(i.total_r, 2, "R") : ""}</td>
      <td>${esc(i.author || i.channel_name || i.source || "")}</td>
      <td class="n">${ago(i.created_at).replace(" ago", "")}</td></tr>`).join("")}</tbody></table>`;
  document.querySelectorAll("#ext-ideas tr[data-id]").forEach((tr) => tr.addEventListener("click", (e) => {
    if (e.target.closest("a.sym")) return;
    showIdea(+tr.dataset.id, true);
  }));
  bindSymLinks("#ext-ideas");
}

function renderSources(st) {
  const by = Object.entries(st.by_source || {});
  $("#ext-sources").innerHTML = `<h2>Track record by source</h2>` + (by.length ? `<table><thead><tr><th>Source</th><th class="n">Ideas</th>
    <th class="n">Triggered</th><th class="n">Closed</th><th class="n">Win rate</th><th class="n">Avg R</th><th class="n">Total R</th></tr></thead><tbody>
    ${by.map(([k, b]) => `<tr><td>${esc(k)}</td><td class="n">${b.ideas}</td><td class="n">${b.triggered}</td><td class="n">${b.closed}</td>
      <td class="n">${b.win_rate == null ? "—" : fmt(b.win_rate * 100, 0) + "%"}</td><td class="n">${b.avg_r == null ? "—" : sgn(b.avg_r, 2, "R")}</td>
      <td class="n">${sgn(b.total_r, 2, "R")}</td></tr>`).join("")}</tbody></table>` : '<p class="muted">No ideas yet.</p>');
}

async function showIdea(id, scroll) {
  EXT.sel = id;
  document.querySelectorAll("#ext-ideas tr[data-id]").forEach((tr) => tr.classList.toggle("sel", +tr.dataset.id === id));
  let r;
  try { r = await api(`/api/ext/ideas/${id}`); } catch (e) { toast(e.message); return; }
  const i = r.idea, open = i.status === "pending" || i.status === "active";
  const reasons = (i.grade_reasons || []).map((x) => `<li class="${x.startsWith("+") ? "pos" : "neg"}">${esc(x)}</li>`).join("");
  $("#ext-timeline").innerHTML = `<div class="idea-head">
      <b>#${i.id} ${esc(i.symbol)} ${esc(i.direction.toUpperCase())}</b> <span class="st ${esc(i.status)}">${esc(i.status)}</span>
      ${i.grade ? `<span class="grade ${esc(i.grade)}">${esc(i.grade)}</span>` : ""}
      <div class="plan">${esc(i.levels)}</div>
      ${i.entry_price != null ? `<div>entry ${fmt(i.entry_price)} · ${fmt(i.shares, i.shares % 1 ? 4 : 0)} units · ${fmt((1 - i.remaining) * 100, 0)}% closed ·
        trade ${sgn(i.total_r, 2, "R")} · best ${fmt(i.mfe_pct, 1)}% / worst ${fmt(i.mae_pct, 1)}%</div>` : ""}
      ${i.close_reason ? `<div class="muted">${esc(i.close_reason)}</div>` : ""}
      ${reasons ? `<ul class="warn-list" style="color:inherit">${reasons}</ul>` : ""}
      <div class="raw">${esc(i.raw)}</div></div>
    ${open ? `<div class="idea-actions">
      <input id="ext-new-stop" type="number" step="any" placeholder="new stop" aria-label="New stop">
      <button class="btn small" id="ext-set-stop">Set stop</button>
      ${i.status === "active" ? `<button class="btn small" id="ext-trim">Trim ½</button><button class="btn small" id="ext-close">Close</button>` : ""}
      <button class="btn small" id="ext-cancel">Cancel</button></div>` : ""}
    ${r.events.map((e) => `<div class="ev ${esc(e.type)}"><div class="when">${esc(new Date(e.ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }))}</div>
      <div class="t">${esc(e.title)}</div><div class="body">${md(e.text)}</div>
      ${Object.keys(e.relayed || {}).length ? `<div class="relay">relayed: ${Object.entries(e.relayed).map(([k, v]) => esc(k + " " + v)).join(", ")}</div>` : ""}</div>`).join("")}`;
  const act = async (path, body) => { busy(true); try { await api(`/api/ext/ideas/${id}/${path}`, { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) }); await loadExt(id); } catch (e) { toast(e.message); } finally { busy(false); } };
  if ($("#ext-set-stop")) $("#ext-set-stop").onclick = () => { const v = parseFloat($("#ext-new-stop").value); if (v > 0) act("edit", { stop: v }); };
  if ($("#ext-trim")) $("#ext-trim").onclick = () => act("close", { fraction: 0.5 });
  if ($("#ext-close")) $("#ext-close").onclick = () => act("close", {});
  if ($("#ext-cancel")) $("#ext-cancel").onclick = () => act("cancel");
  if (scroll && window.innerWidth < 1100) $("#ext-detail").scrollIntoView({ behavior: "smooth" });
}

function renderParsed(res) {
  const p = res.parsed || res;
  const cell = (k, v) => `<div><span>${k}</span>${v == null || v === "" ? "—" : esc(v)}</div>`;
  let html;
  if (p.kind === "update") {
    html = `<div class="parsed">${cell("type", "update")}${cell("action", (p.action || "").replace("_", " "))}${cell("symbol", p.symbol)}
      ${cell("new stop", p.new_stop)}${cell("target", p.target_index != null ? "TP" + (p.target_index + 1) : null)}</div>`;
  } else if (p.kind === "idea") {
    html = `<div class="parsed">${cell("symbol", p.symbol)}${cell("direction", p.direction)}
      ${cell("entry", (p.entry_type || "").replace("_", " ") + " " + (p.entry_low === p.entry_high ? lvl(p.entry_low) : lvl(p.entry_low) + "–" + lvl(p.entry_high)))}
      ${cell("stop", p.stop == null ? "auto (2×ATR)" : lvl(p.stop) + (p.stop_basis === "close" ? " on close" : ""))}
      ${cell("targets", p.targets?.length ? p.targets.map(lvl).join(" / ") : "auto (2R / 3R)")}
      ${cell("timeframe", p.timeframe)}${cell("parse confidence", fmt(p.confidence * 100, 0) + "%")}</div>`;
  } else html = '<p class="muted">Not recognised as a trade idea or an update.</p>';
  const w = (p.warnings || []).map((x) => `<li>${esc(x)}</li>`).join("");
  const outcome = res.outcome ? `<p><b class="${res.outcome === "tracking" || res.outcome === "updated" ? "pos" : res.outcome === "rejected" ? "neg" : ""}">${esc(res.outcome.toUpperCase())}</b> — ${esc(res.message)}</p>` : "";
  $("#ext-parsed").innerHTML = outcome + html + (w ? `<ul class="warn-list">${w}</ul>` : "");
}

(function initExt() {
  $("#ext-preview").onclick = async () => {
    const text = $("#ext-text").value.trim(); if (!text) return;
    try { renderParsed(await api("/api/ext/parse", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) })); }
    catch (e) { toast(e.message); }
  };
  $("#ext-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const text = $("#ext-text").value.trim(); if (!text) return;
    busy(true);
    try {
      const r = await api("/api/ext/ingest", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, author: $("#ext-author").value.trim() || null }) });
      renderParsed(r);
      if (r.outcome === "tracking" || r.outcome === "updated") $("#ext-text").value = "";
      await loadExt(r.idea?.id);
    } catch (e) { toast(e.message); } finally { busy(false); }
  });
  $("#ext-filter").addEventListener("change", () => loadExt());
  const hook = () => {
    if (!("EventSource" in window)) return;
    if (!PF.es) return setTimeout(hook, 500);
    PF.es.addEventListener("ext", (e) => {
      const d = JSON.parse(e.data);
      if (!extVisible() || document.hidden) { EXT.unread++; extBadge(); }
      clearTimeout(EXT.timer);
      EXT.timer = setTimeout(() => { if (extVisible()) loadExt(EXT.sel ?? d.idea?.id); }, 400);
    });
    PF.es.addEventListener("status", () => { if (extVisible()) renderExtStatus(); });
  };
  hook();
  setInterval(() => { if (extVisible() && !PF.status?.running) loadExt(); }, 60000);
})();
