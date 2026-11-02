/* Ported from the MNQ bot spin-off (c3.lab/mnq-bot) for DAISY's desk tabs (docs/20). Wrapped so its names stay local. */
(() => {
"use strict";
const api = window.deskApi, toast = window.deskToast;
/* Swing desk section: book, add idea, all ideas, track record, research (docs/19).  Uses app.js helpers. */
const SW = { data: null, record: null, research: null, filter: store.get("sw-filter", "open"), q: "", sel: null,
             history: null, preview: null, previewSeq: 0, fieldsDirty: false };
const lv = (x) => x == null ? "—" : (+x).toLocaleString(LOCALE, { maximumFractionDigits: Math.abs(x) >= 100 ? 2 : Math.abs(x) >= 1 ? 2 : 4 });
const STATE_LABEL = { FILLED: "Open", WATCHING: "Working", NEW: "New", WIN: "Target hit", LOSS: "Stopped", SCRATCH: "Scratch",
  SKIPPED: "Skipped", EXPIRED: "Expired", NOFILL_RAN: "Ran away", NOFILL_INVALID: "Invalidated", CANCELLED: "Cancelled" };
const KIND_LABEL = { GRADED: "Graded", IN_ZONE: "In zone", FILLED: "Filled", TP_HIT: "Target", STOPPED: "Stopped",
  CANCELLED: "Cancelled", EXPIRED: "Expired", SCRATCH: "Scratch" };
const FILTERS = [["open", "Open & working", (i) => ["FILLED", "WATCHING", "NEW"].includes(i.state)],
                 ["closed", "Closed", (i) => ["WIN", "LOSS", "SCRATCH"].includes(i.state)],
                 ["skipped", "Skipped by the gate", (i) => i.state === "SKIPPED"],
                 ["missed", "Never filled", (i) => ["NOFILL_RAN", "NOFILL_INVALID", "EXPIRED", "CANCELLED"].includes(i.state)],
                 ["all", "All", () => true]];
const daysSince = (d) => d ? Math.max(0, Math.round((Date.now() - Date.parse(d + "T16:00:00-04:00")) / 86400000)) : null;

// ------------------------------------------------------------------ the price ladder: stop · entry zone · take-profit · price
function ladder(i, opts) {
  opts = opts || {};
  const long = i.direction !== "short";
  const lim = i.limit ?? (i.entry_type === "zone" ? (long ? i.entry_hi : i.entry_lo) : i.entry_lo);
  const stop = i.stop_px ?? i.stop, tp = i.tp ?? (long ? i.target_lo : i.target_hi);
  const price = i.price, fill = i.result && i.result.fill_px;
  const pts = [stop, lim, tp, price, fill, i.entry_lo, i.entry_hi].filter((x) => x != null && isFinite(x));
  if (pts.length < 2 || stop == null || tp == null) return '<div class="ladder empty muted">levels incomplete</div>';
  let lo = Math.min(...pts), hi = Math.max(...pts);
  const pad = (hi - lo) * 0.06 || hi * 0.01; lo -= pad; hi += pad;
  const x = (v) => ((v - lo) / (hi - lo)) * 100;
  const seg = (a, b, cls) => a == null || b == null ? "" : `<i class="${cls}" style="left:${Math.min(x(a), x(b))}%;width:${Math.abs(x(b) - x(a))}%"></i>`;
  const tick = (v, cls, label, title) => v == null ? "" : `<b class="tk ${cls}" style="left:${x(v)}%" title="${esc(title)}"><em>${esc(label)}</em></b>`;
  const zone = i.entry_type === "zone" && i.entry_lo != null && i.entry_hi != null && i.entry_hi > i.entry_lo
    ? `<i class="zone" style="left:${x(i.entry_lo)}%;width:${x(i.entry_hi) - x(i.entry_lo)}%" title="posted entry zone ${lv(i.entry_lo)}–${lv(i.entry_hi)}"></i>` : "";
  const ref = fill || lim;
  const pr = price != null ? `<b class="px ${ref != null ? ((long ? price >= ref : price <= ref) ? "up" : "dn") : ""}" style="left:${x(price)}%"
      title="${i.quote ? "live quote" : "last close " + esc(i.close_date || "")}"><em>${lv(price)}</em></b>` : "";
  const fl = fill ? `<b class="fill" style="left:${x(fill)}%" title="filled at ${lv(fill)} on ${esc(i.result.fill_date || "")}"></b>` : "";
  return `<div class="ladder${opts.big ? " wide" : ""}" role="img" aria-label="${esc(i.symbol)}: stop ${lv(stop)}, entry ${lv(lim)}, take profit ${lv(tp)}, price ${lv(price)}">
    <div class="rail">${seg(stop, ref, "risk")}${seg(ref, tp, "reward")}${zone}</div>
    ${tick(stop, "stop", lv(stop), `stop ${lv(stop)} (${i.stop_basis === "close" ? "daily close" : "intraday"})`)}
    ${tick(lim, "lim", lv(lim), `limit ${lv(lim)}`)}${tick(tp, "tp", lv(tp), `take profit ${lv(tp)} (posted target ${lv(i.posted_target ?? i.target_lo)})`)}
    ${fl}${pr}</div>`;
}

function spark(vals, w = 120, h = 28, cls = "") {
  if (!vals || vals.length < 2) return "";
  const mn = Math.min(...vals), mx = Math.max(...vals), r = mx - mn || 1;
  const p = vals.map((v, k) => `${(k / (vals.length - 1) * w).toFixed(1)},${(h - 2 - (v - mn) / r * (h - 4)).toFixed(1)}`).join(" ");
  return `<svg class="spark ${cls}" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" aria-hidden="true"><polyline points="${p}"/></svg>`;
}

const sideTag = (i) => `<span class="side ${i.direction === "short" ? "dn" : "up"}" title="${i.direction}">${i.direction === "short" ? "Short" : "Long"}</span>`;
const stateChip = (i) => `<span class="st s-${esc(i.state)}">${esc(STATE_LABEL[i.state] || i.state)}</span>`;
const tierChip = (i) => i.tier && i.tier !== "SKIP" ? `<span class="tier t-${esc(i.tier)}" title="${i.tier === "A" ? "both gate conditions pass" : "one gate condition passes"}">${esc(i.tier)}</span>` : "";

// ------------------------------------------------------------------ book
function renderGate(g, st) {
  if (!g) { $("#sw-gate").innerHTML = '<p class="muted">No SPY prices yet, so the market gate can\'t be read.</p>'; return; }
  const q = g.quote && g.quote_at && Date.now() / 1000 - g.quote_at < 3 * 3600 ? g.quote : null;
  const n = g.spark.length, line = g.spark, sma = g.sma_spark;
  const all = line.concat(sma.filter((v) => v != null)), mn = Math.min(...all), mx = Math.max(...all), w = 220, h = 46;
  const P = (arr) => arr.map((v, k) => v == null ? null : `${(k / (n - 1) * w).toFixed(1)},${(h - 3 - (v - mn) / (mx - mn || 1) * (h - 6)).toFixed(1)}`).filter(Boolean).join(" ");
  $("#sw-gate").innerHTML = `
    <div class="gate-chart"><svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img" aria-label="SPY versus its ${g.sma_n}-day average, last 60 sessions">
      <polyline class="sma" points="${P(sma)}"/><polyline class="spy" points="${P(line)}"/></svg>
      <span class="muted small">SPY and its ${g.sma_n}-day average, 60 sessions</span></div>
    <div class="gate-text">
      <p class="gate-state ${g.market_branch ? "on" : "off"}">${g.market_branch
        ? "Market is pulling back: every idea passes the gate"
        : `Market is extended: new ideas need ATR at or under ${g.max_atr_pct}% of price`}</p>
      <p class="muted">SPY closed ${lv(g.spy_close)} on ${esc(g.date)}, ${g.gap_pct >= 0 ? "+" : ""}${fmt(g.gap_pct, 2)}% vs its ${g.sma_n}-day average ${lv(g.spy_sma)}${q ? `; live ${lv(q)}` : ""}.
      The gate takes an idea when ${g.require === "all" ? "both" : "either"} the stock's ATR is ≤ ${g.max_atr_pct}% ${g.require === "all" ? "and" : "or"} SPY is below the average.</p>
    </div>
    <div class="gate-act">
      <button class="btn" id="sw-run" ${st.busy ? "disabled" : ""}>${st.busy ? `Running ${esc(st.busy)}…` : "Refresh prices now"}</button>
      <span class="muted small">last pass ${ago(st.last_pass)}${st.next_pass ? `; next ${esc(st.next_pass.slice(11, 16))} ET` : ""}</span></div>`;
  $("#sw-run").onclick = runPass;
}

function renderStrip(sb, ideas) {
  const open = ideas.filter((i) => i.state === "FILLED");
  const openR = open.reduce((a, i) => a + (i.r || 0), 0);
  const be = SW.record && SW.record.breakeven_win_rate;
  $("#sw-strip").innerHTML = [
    stat("Open positions", sb.open, open.length ? `${sgn(openR, 2, "R")} marked to market` : "none"),
    stat("Working orders", sb.watching, "limit orders waiting for a fill"),
    stat("Win rate", sb.win_rate == null ? "—" : fmt(sb.win_rate, 1) + "%", `${sb.wins} of ${sb.closed} closed${be ? `; break-even ${fmt(be, 0)}%` : ""}`,
         sb.closed >= 10 && be && sb.win_rate < be ? "caution" : ""),
    stat("Total", sgn(sb.sum_R, 2, "R"), `${sb.taken} ideas taken`),
    stat("Skipped by the gate", sb.skipped, "tracked on paper so the gate can be re-tested"),
  ].join("");
}

function posRow(i) {
  const held = daysSince(i.result.fill_date);
  return `<div class="lrow" data-key="${esc(i.key)}" tabindex="0" role="button" aria-label="${esc(i.symbol)} details">
    <div class="who"><b class="sym">${esc(i.symbol)}</b>${sideTag(i)}${tierChip(i)}<span class="muted small">${esc(i.author || "")}, filled ${esc((i.result.fill_date || "").slice(5))}${held != null ? `, ${held}d` : ""}</span></div>
    ${ladder(i)}
    <div class="num lg ${i.r > 0 ? "pos" : i.r < 0 ? "neg" : ""}">${i.r == null ? "—" : (i.r > 0 ? "+" : "") + fmt(i.r, 2) + "R"}</div></div>`;
}
function ordRow(i) {
  const d = i.to_entry_pct;
  const near = d != null && Math.abs(d) < 1.5;
  return `<div class="lrow" data-key="${esc(i.key)}" tabindex="0" role="button" aria-label="${esc(i.symbol)} details">
    <div class="who"><b class="sym">${esc(i.symbol)}</b>${sideTag(i)}${tierChip(i)}<span class="muted small">${esc(i.author || "")}, posted ${esc(i.posted_et.slice(5, 10))}</span></div>
    ${ladder(i)}
    <div class="num ${near ? "caution" : ""}">${d == null ? "—" : `${d > 0 ? "+" : ""}${fmt(d, 1)}%`}<span class="muted small">${d == null ? "" : i.direction === "short" ? (d < 0 ? "below the limit" : "above") : (d > 0 ? "above the limit" : "below")}</span></div></div>`;
}

function renderFeed(alerts) {
  $("#sw-feed-note").textContent = alerts.length ? "newest first" : "";
  $("#sw-feed").innerHTML = alerts.length ? alerts.map((a) => {
    const [head, ...rest] = (a.text || "").split("\n");
    return `<div class="fi k-${esc(a.kind)}" data-key="${esc(a.key)}"><span class="k">${esc(KIND_LABEL[a.kind] || a.kind)}</span>
      <time title="${esc(new Date(a.at * 1000).toLocaleString())}">${ago(a.at)}</time>
      <p>${esc(head.replace(/^[^\w$]+/u, ""))}</p>${rest.length ? `<p class="muted small">${esc(rest.join(" "))}</p>` : ""}
      <span class="dl">${esc(a.delivered)}</span></div>`;
  }).join("") : '<p class="muted">Nothing yet. The desk writes here when an idea is graded, fills, hits its target or stop, or is cancelled.</p>';
}

function renderDeskBar(st) {
  const errs = (st.errors || []).map((e) => `<span class="neg" title="${esc(e.error)}">${esc(e.where)}</span>`).join(" ");
  $("#sw-desk").innerHTML = `<span>${st.webhook ? '<span class="pill ok">Alerts post to Discord</span>' : '<span class="pill">Alerts stay on this page</span> <span class="muted small">set ABG_SWING_WEBHOOK_URL to post them</span>'}</span>
    <span class="muted small">prices from ${esc(st.source)}; passes every ${st.interval_min} min in market hours and at ${esc(st.close_time)} ET${st.scan ? " with the scanner" : ""}</span>
    <span class="muted small">R&amp;D agent ${st.research === "weekly" ? "weekly" : "off"}${st.api_key ? "" : " (no API key)"}</span>
    ${errs ? `<span class="spacer"></span><span class="small">errors: ${errs}</span>` : ""}`;
}

function swingData(r) {
  SW.data = r;
  SESSION.info = r.status.session; SESSION.at = Date.now();
  const ideas = r.ideas || [];
  const pos = ideas.filter((i) => i.state === "FILLED"), ords = ideas.filter((i) => i.state === "WATCHING");
  const cnt = $("#nav-swing-count"); cnt.textContent = pos.length; cnt.hidden = !pos.length; cnt.title = `${pos.length} open positions`;
  renderGate(r.gate, r.status);
  renderStrip(r.scoreboard, ideas);
  $("#sw-pos-note").textContent = pos.length ? "stop, entry, take-profit and the latest price" : "";
  $("#sw-positions").innerHTML = pos.length ? pos.map(posRow).join("") : '<p class="muted">No open positions. Fills from working orders show up here.</p>';
  $("#sw-ord-note").textContent = ords.length ? "distance from the latest price to the limit" : "";
  $("#sw-orders").innerHTML = ords.length ? ords.map(ordRow).join("") : '<p class="muted">No working orders. <a href="#swing/add">Add an idea</a> to start one.</p>';
  renderFeed(r.alerts || []);
  renderDeskBar(r.status);
  if (!$('[data-panel="swing/ideas"]').hidden) renderIdeas();
  busyState(r.status.busy);
}
window.swingData = swingData;

function busyState(b) { if (b && !SW.wasBusy) busy(true); if (!b && SW.wasBusy) busy(false); SW.wasBusy = !!b; }

async function loadSwing() {
  try { swingData(await api("/api/swing")); }
  catch (e) { $("#sw-gate").innerHTML = `<p class="neg">${esc(e.message)}</p>`; }
}

async function runPass() {
  const b = $("#sw-run"); b.disabled = true; b.textContent = "Running…"; busy(true);
  try {
    const r = await api("/api/swing/run", { method: "POST" });
    toast(r.alerts.length ? `Pass done: ${r.alerts.length} change${r.alerts.length > 1 ? "s" : ""}` : "Pass done: nothing changed", "ok");
  } catch (e) { toast(e.message, "error"); }
  finally { busy(false); loadSwing(); }
}

// ------------------------------------------------------------------ all ideas
function renderIdeas() {
  const ideas = (SW.data && SW.data.ideas) || [];
  $("#sw-filters").innerHTML = FILTERS.map(([k, label, f]) => `<button class="chip ${SW.filter === k ? "on" : ""}" data-f="${k}" aria-pressed="${SW.filter === k}">${label} <span>${ideas.filter(f).length}</span></button>`).join("");
  $$("#sw-filters .chip").forEach((b) => (b.onclick = () => { SW.filter = b.dataset.f; store.set("sw-filter", SW.filter); renderIdeas(); }));
  const f = (FILTERS.find((x) => x[0] === SW.filter) || FILTERS[0])[2];
  const q = SW.q.trim().toLowerCase();
  const rows = ideas.filter(f).filter((i) => !q || i.symbol.toLowerCase().includes(q) || (i.author || "").toLowerCase().includes(q));
  $("#sw-ideas").innerHTML = rows.length ? `<table class="ideas"><thead><tr><th>Ticker</th><th>Posted</th><th>By</th><th>State</th><th>Tier</th>
      <th class="n">Limit</th><th class="n">Stop</th><th class="n">Take profit</th><th class="n">ATR %</th><th class="n">R</th><th></th></tr></thead><tbody>
    ${rows.map((i) => `<tr data-key="${esc(i.key)}" tabindex="0" class="${i.key === SW.sel ? "sel" : ""}">
      <td><b class="sym">${esc(i.symbol)}</b> ${sideTag(i)}</td><td>${esc(i.posted_et.slice(0, 10))}</td><td>${esc(i.author || "")}</td>
      <td>${stateChip(i)}</td><td>${tierChip(i) || '<span class="muted">—</span>'}</td>
      <td class="n">${lv(i.limit)}</td><td class="n">${lv(i.stop_px)}</td><td class="n">${lv(i.tp)}</td>
      <td class="n">${i.atr_pct == null ? "—" : fmt(i.atr_pct, 1)}</td><td class="n">${i.r == null ? "" : sgn(i.r, 2)}</td>
      <td>${spark(i.spark, 72, 20)}</td></tr>`).join("")}</tbody></table>`
    : `<p class="muted">No ideas match${q ? ` “${esc(SW.q)}”` : ""}.</p>`;
}

// ------------------------------------------------------------------ detail drawer
async function openIdea(key) {
  const i = ((SW.data && SW.data.ideas) || []).find((x) => x.key === key);
  if (!i) return;
  SW.sel = key;
  if (!SW.history) { try { SW.history = (await api("/api/swing/alerts?history=true&limit=2000")).alerts; } catch { SW.history = []; } }
  const tl = SW.history.filter((a) => a.key === key).slice().reverse();
  const eq = +store.get("sw-equity", 0);
  const size = i.shares_per_10k != null ? sizeText(i.shares_per_10k, i.risk_pct, i.r_per_share) : "—";
  const reasons = (i.reasons || []).map((r) => `<li class="${/gate failed|SKIP/.test(r) ? "neg" : ""}">${esc(r)}</li>`).join("");
  $("#drawer-body").innerHTML = `
    <h2 id="drawer-title"><b class="sym">${esc(i.symbol)}</b> ${sideTag(i)} ${stateChip(i)} ${tierChip(i)}</h2>
    <p class="muted">Posted ${esc(i.posted_et)} ET by ${esc(i.author || "?")}${i.scanner ? " (the desk's own scanner)" : ""}</p>
    ${ladder(i, { big: true })}
    <dl class="kvs">
      <dt>Plan</dt><dd>limit ${lv(i.limit)}, stop ${lv(i.stop_px)} on ${i.stop_basis === "close" ? "a daily close" : "an intraday touch"}, take profit ${lv(i.tp)} <span class="muted">(posted target ${lv(i.posted_target ?? i.target_lo)})</span></dd>
      <dt>Size</dt><dd>${i.risk_pct != null ? `risk ${fmt(i.risk_pct, 2)}% of the account: ${size}` : "—"}</dd>
      <dt>Result</dt><dd>${i.result && i.result.fill_date ? `filled ${lv(i.result.fill_px)} on ${esc(i.result.fill_date)}` : "not filled"}${i.result && i.result.exit_date ? `, closed ${esc(i.result.exit_date)}` : ""}${i.r != null ? `, ${sgn(i.r, 2, "R")}` : ""}</dd>
      <dt>Latest price</dt><dd>${lv(i.price)} <span class="muted">${i.quote ? "live quote" : "close " + esc(i.close_date || "")}</span> ${spark(i.spark, 140, 30)}</dd>
      ${reasons ? `<dt>Gate</dt><dd><ul class="plain">${reasons}</ul></dd>` : ""}
      ${i.note ? `<dt>Note</dt><dd>${esc(i.note)}</dd>` : ""}
      <dt>Posted levels</dt><dd>${i.entry_type === "cmp" ? "at market" : i.entry_type === "close_above" ? `daily close above ${lv(i.entry_lo)}` : `zone ${lv(i.entry_lo)}–${lv(i.entry_hi)}`}, target ${lv(i.target_lo)}${i.target_hi !== i.target_lo ? `–${lv(i.target_hi)}` : ""}, stop ${lv(i.stop)}</dd>
    </dl>
    <h3>Timeline</h3>
    <ol class="timeline">${tl.length ? tl.map((a) => `<li class="k-${esc(a.kind)}"><time>${esc(new Date(a.at * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }))}</time>
      <span>${esc(a.text.split("\n")[0])}</span><span class="muted small">${esc(a.delivered)}</span></li>`).join("") : '<li class="muted">No alerts recorded for this idea.</li>'}</ol>
    ${i.state === "WATCHING" ? '<div class="drawer-actions"><button class="btn danger" id="sw-cancel">Cancel this order</button></div>' : ""}`;
  $("#drawer").hidden = false;
  document.body.classList.add("drawer-open");
  $("#drawer .close").focus();
  const c = $("#sw-cancel");
  if (c) c.onclick = async () => {
    if (!confirm(`Cancel the ${i.symbol} order? The desk stops tracking it.`)) return;
    try { await api("/api/swing/ideas/cancel", { json: { key } }); toast(`Cancelled ${i.symbol}`, "ok"); closeDrawer(); SW.history = null; loadSwing(); }
    catch (e) { toast(e.message, "error"); }
  };
}
function closeDrawer() { $("#drawer").hidden = true; document.body.classList.remove("drawer-open"); SW.sel = null; }

function sizeText(per10k, riskPct, rps) {
  const eq = +store.get("sw-equity", 0);
  if (eq) {
    const n = Math.floor(eq * riskPct / 100 / rps);
    return n ? `${n} shares for $${eq.toLocaleString()}` : `under 1 share for $${eq.toLocaleString()} ($${fmt(eq * riskPct / 100, 0)} risk vs $${fmt(rps, 2)} a share)`;
  }
  return per10k ? `${per10k} shares per $10k` : `under 1 share per $10k ($${fmt(100 * riskPct, 0)} risk vs $${fmt(rps, 2)} a share)`;
}

// ------------------------------------------------------------------ add idea
const FORM_KEYS = ["symbol", "direction", "entry_type", "entry_lo", "entry_hi", "target_lo", "target_hi", "stop", "stop_basis", "note"];
function formGet() {
  const f = $("#sw-form"), o = {};
  FORM_KEYS.forEach((k) => {
    const el = f.elements[k];
    const v = el instanceof RadioNodeList ? el.value : el.value;
    o[k] = ["entry_lo", "entry_hi", "target_lo", "target_hi", "stop"].includes(k) ? (v === "" ? null : +v) : (v || null);
  });
  if (o.symbol) o.symbol = o.symbol.toUpperCase();
  return o;
}
function formSet(f) {
  const el = $("#sw-form").elements;
  FORM_KEYS.forEach((k) => { if (f[k] !== undefined && el[k]) el[k].value = f[k] == null ? "" : f[k]; });
  entryTypeUi();
}
function entryTypeUi() {
  const et = $("#sw-form").elements.entry_type.value;
  const lo = $("#sw-form").elements.entry_lo, hi = $("#sw-form").elements.entry_hi;
  lo.disabled = hi.disabled = et === "cmp";
  hi.disabled = hi.disabled || et === "close_above";
  lo.closest("label").firstChild.textContent = et === "close_above" ? "Close above" : "Entry low";
}

function renderVerdict(p) {
  const box = $("#sw-verdict"), btn = $("#sw-track");
  SW.preview = p;
  if (!p) { btn.disabled = true; return; }
  const errs = p.errors || [];
  const v = p.verdict;
  const f = p.fields || {};
  btn.disabled = !!errs.length || !v;
  if (errs.length) { box.innerHTML = `<ul class="notes bad">${errs.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>${p.last ? `<p class="muted">${esc(f.symbol)} last closed ${lv(p.last.close)} on ${esc(p.last.date)}.</p>` : ""}`; return; }
  if (!v) { box.innerHTML = ""; return; }
  const eq = +store.get("sw-equity", 0);
  const sh = eq ? Math.floor(eq * v.risk_pct / 100 / v.r_per_share) : null;
  const i = { ...f, limit: v.entry_px, stop_px: v.stop_px, tp: v.tp_px, price: p.last && p.last.close, close_date: p.last && p.last.date, result: {} };
  box.innerHTML = `<div class="vhead ${v.take ? "take" : "skip"}"><b>${v.take ? "Take it" : "Skip it"}</b>
      <span>${v.take ? `tier ${esc(v.tier)}: ${v.tier === "A" ? "both gate conditions pass" : "one gate condition passes"}` : "the gate says no"}</span></div>
    ${ladder(i, { big: true })}
    <div class="vnums">
      <div><span>Limit</span>${lv(v.entry_px)}</div><div><span>Stop</span>${lv(v.stop_px)}</div><div><span>Take profit</span>${lv(v.tp_px)}</div>
      <div><span>Risk</span>${fmt(v.risk_pct, 2)}%</div><div><span>${eq ? `Shares for $${eq.toLocaleString()}` : "Shares per $10k"}</span>${eq ? sh : v.shares_per_10k}</div>
      <div><span>ATR</span>${fmt(v.metrics.atr_pct, 2)}%</div></div>
    <ul class="plain reasons">${(v.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("")}${(v.flags || []).length ? `<li>flags: ${esc(v.flags.join(", "))}</li>` : ""}</ul>
    <p class="muted small">${esc(f.symbol)} last closed ${lv(p.last.close)} on ${esc(p.last.date)}. ${v.take ? "Tracking starts now; it fills only on a session after this one." : "You can still track it: the desk records skipped ideas so the gate can be re-tested."}</p>`;
  btn.textContent = v.take ? "Track it" : "Track it anyway";
}

let readT = null, fieldT = null;
async function readText() {
  const text = $("#sw-text").value.trim();
  if (!text) return;
  const seq = ++SW.previewSeq;
  $("#sw-read-src").textContent = "reading…";
  try {
    const p = await api("/api/swing/parse", { json: { text } });
    if (seq !== SW.previewSeq) return;
    const parsed = p.parsed || {};
    $("#sw-read-src").textContent = parsed.source ? `read with the ${parsed.source}` : "";
    $("#sw-warn").innerHTML = (parsed.warnings || []).map((w) => `<li>${esc(w)}</li>`).join("");
    if (p.fields) formSet(p.fields);
    (parsed.missing || []).forEach((k) => { const el = $("#sw-form").elements[{ entry: "entry_lo", target: "target_lo", ticker: "symbol" }[k] || k]; if (el && el.focus) el.classList.add("need"); });
    renderVerdict(p.fields ? p : null);
  } catch (e) { if (seq === SW.previewSeq) { $("#sw-read-src").textContent = ""; toast(e.message, "error"); } }
}
async function previewFields() {
  const f = formGet();
  $$("#sw-form .need").forEach((x) => x.classList.remove("need"));
  if (!f.symbol) { renderVerdict(null); return; }
  const seq = ++SW.previewSeq;
  try { const p = await api("/api/swing/parse", { json: { fields: f } }); if (seq === SW.previewSeq) renderVerdict(p); }
  catch (e) { if (seq === SW.previewSeq) toast(e.message, "error"); }
}
async function trackIt() {
  const btn = $("#sw-track"); btn.disabled = true; busy(true);
  try {
    const r = await api("/api/swing/ideas", { json: { fields: formGet(), author: $("#sw-author").value.trim() || null } });
    const i = r.idea || {};
    toast(`Tracking ${i.symbol || ""}: ${i.take ? `taken, tier ${i.tier}` : "skipped by the gate (recorded)"}`, "ok");
    $("#sw-text").value = ""; $("#sw-warn").innerHTML = ""; $("#sw-read-src").textContent = "";
    $("#sw-form").reset(); entryTypeUi();
    $("#sw-verdict").innerHTML = `<p>Added <a href="#swing/book" data-open="${esc(r.key)}">${esc(i.symbol || r.key)}</a>. ${i.take ? "It's in the book as a working order." : "It's recorded under “Skipped by the gate”."}</p>`;
    SW.history = null;
    loadSwing();
  } catch (e) { toast(e.message, "error"); btn.disabled = false; }
  finally { busy(false); }
}

// ------------------------------------------------------------------ track record
async function loadRecord() {
  let r;
  try { r = await api("/api/swing/record"); } catch (e) { $("#sw-curve").innerHTML = `<p class="muted">${esc(e.message)}</p>`; return; }
  SW.record = r;
  const sb = r.scoreboard;
  $("#sw-rec-strip").innerHTML = [
    stat("Closed", sb.closed, `${sb.wins} wins`), stat("Win rate", sb.win_rate == null ? "—" : fmt(sb.win_rate, 1) + "%", "taken ideas only"),
    stat("Total", sgn(sb.sum_R, 2, "R"), sb.closed ? `${sgn(sb.sum_R / sb.closed, 2, "R")} per trade` : ""),
    stat("Average win / loss", r.avg_win_r == null ? "—" : `${sgn(r.avg_win_r, 2)} / ${sgn(r.avg_loss_r, 2)}`, "in R"),
    stat("Break-even win rate", r.breakeven_win_rate == null ? "—" : fmt(r.breakeven_win_rate, 0) + "%", "below this the desk loses money",
         sb.win_rate != null && r.breakeven_win_rate != null && sb.win_rate < r.breakeven_win_rate ? "caution" : ""),
  ].join("");
  renderCurve(r.curve);
  const grp = (rows) => rows.length ? `<table class="grp"><thead><tr><th></th><th class="n">Closed</th><th>Win rate</th><th class="n">R</th></tr></thead><tbody>${rows.map((g) =>
    `<tr><td>${esc(g.group)}</td><td class="n">${g.n}</td><td><span class="bar" style="--w:${g.win_rate}%"><i></i><em>${fmt(g.win_rate, 0)}%</em></span></td><td class="n">${sgn(g.sum_r, 2)}</td></tr>`).join("")}</tbody></table>`
    : '<p class="muted">No closed trades yet.</p>';
  $("#sw-by-author").innerHTML = grp(r.by_author);
  $("#sw-by-tier").innerHTML = grp(r.by_tier);
  $("#sw-by-month").innerHTML = grp(r.by_month);
}

function renderCurve(pts) {
  const el = $("#sw-curve");
  if (!pts.length) { el.innerHTML = '<p class="muted">The curve starts with the first closed trade.</p>'; return; }
  $("#sw-curve-note").textContent = `${pts.length} closed trades, in exit order`;
  const W = Math.max(320, el.clientWidth || 800), H = 220, L = 44, R = 12, T = 12, B = 26;
  const ys = pts.map((p) => p.cum).concat([0]);
  let mn = Math.min(...ys), mx = Math.max(...ys); const pad = (mx - mn) * 0.08 || 1; mn -= pad; mx += pad;
  const X = (k) => L + (k / Math.max(1, pts.length - 1)) * (W - L - R), Y = (v) => T + (1 - (v - mn) / (mx - mn)) * (H - T - B);
  const raw = (mx - mn) / 5, mag = Math.pow(10, Math.floor(Math.log10(raw || 1)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((x) => x >= raw) || raw;
  const tk = []; for (let v = Math.ceil(mn / step) * step; v <= mx; v += step) tk.push(+v.toFixed(6));
  const line = pts.map((p, k) => `${X(k).toFixed(1)},${Y(p.cum).toFixed(1)}`).join(" ");
  const months = []; let lastM = "";
  pts.forEach((p, k) => { const m = (p.date || "").slice(0, 7); if (m && m !== lastM) { months.push([k, m]); lastM = m; } });
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="Cumulative R of closed trades, ending at ${fmt(pts[pts.length - 1].cum, 2)}R">
    ${tk.map((v) => `<line class="grid" x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}"/><text class="ax" x="${L - 6}" y="${Y(v) + 4}" text-anchor="end">${v > 0 ? "+" : ""}${v}R</text>`).join("")}
    <line class="zero" x1="${L}" x2="${W - R}" y1="${Y(0)}" y2="${Y(0)}"/>
    ${months.map(([k, m]) => `<text class="ax" x="${X(k)}" y="${H - 8}">${new Date(m + "-15").toLocaleString([], { month: "short" })}</text>`).join("")}
    <polyline class="cum" points="${line}"/>
    ${pts.map((p, k) => `<circle class="pt ${p.win ? "w" : "l"}" cx="${X(k)}" cy="${Y(p.cum)}" r="4"/>`).join("")}
    <line class="cross" id="sw-cross" x1="0" x2="0" y1="${T}" y2="${H - B}" visibility="hidden"/>
    <rect class="hit" x="${L}" y="0" width="${W - L - R}" height="${H}" fill="transparent"/></svg>
    <div class="tip" id="sw-tip" hidden></div>`;
  const svg = el.querySelector("svg"), tip = $("#sw-tip"), cross = $("#sw-cross");
  svg.addEventListener("mousemove", (ev) => {
    const b = svg.getBoundingClientRect(), x = (ev.clientX - b.left) * (W / b.width);
    const k = Math.max(0, Math.min(pts.length - 1, Math.round((x - L) / (W - L - R) * (pts.length - 1)))), p = pts[k];
    cross.setAttribute("x1", X(k)); cross.setAttribute("x2", X(k)); cross.setAttribute("visibility", "visible");
    tip.hidden = false; tip.innerHTML = `<b>${esc(p.symbol)}</b> ${esc(p.date || "")}<br>${p.win ? "win" : "loss"} ${sgn(p.r, 2, "R")}<br>total ${sgn(p.cum, 2, "R")}`;
    tip.style.left = `${Math.min(b.width - 150, X(k) * b.width / W + 12)}px`; tip.style.top = `${Y(p.cum) * b.height / H - 10}px`;
  });
  svg.addEventListener("mouseleave", () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); });
}

// ------------------------------------------------------------------ research
function rulesHtml(c) {
  const g = c.gate, h = c.hard_blocks, e = c.entry, x = c.exit, r = c.risk;
  return `<ol class="rules">
    <li><b>Gate.</b> Take the idea if ${g.require === "all" ? "both" : "either"}: the stock's ATR(14) is at most ${g.max_atr_pct}% of price, ${g.require === "all" ? "and" : "or"} SPY closed below its ${g.spy_below_sma}-day average when it was posted.</li>
    <li><b>Hard blocks.</b> Price under $${h.min_price} or average dollar volume under $${h.min_dollar_volume_m}M.</li>
    <li><b>Entry.</b> Limit at the top of the posted zone; cancel if price reaches the target or breaks the stop first, or after ${e.fill_window_bars} sessions.</li>
    <li><b>Exit.</b> Sell everything at +${x.take_profit_r}R, or at the posted target if that's closer. Keep the posted stop and its type.${x.time_stop_bars ? ` Time stop after ${x.time_stop_bars} sessions.` : ""}</li>
    <li><b>Size.</b> Risk ${r.risk_per_trade_pct}% of the account per trade; half size for ${r.size_down_flags.map((f) => f.replace(/_/g, " ")).join(" and ")}. At most ${r.max_open_positions} open, ${r.max_per_sector} per sector.</li>
    <li><b>Changes.</b> The R&amp;D agent may propose one only with ${c.research.min_closed_trades_for_change}+ closed trades, a gain in both halves, and at most ${c.research.max_param_changes_per_proposal} settings touched.</li></ol>`;
}
async function loadResearch() {
  if (!SW.research) $("#sw-variants").innerHTML = '<p class="muted">Replaying every idea under each variant…</p>';
  loadVariants();
  let r;
  try { r = await api("/api/swing/research"); } catch (e) { $("#sw-rules").innerHTML = `<p class="muted">${esc(e.message)}</p>`; return; }
  SW.research = r;
  $("#sw-rules-path").textContent = r.config.path;
  $("#sw-rules").innerHTML = rulesHtml(r.config.cfg) + `<details><summary>The file</summary><pre class="yaml">${esc(r.config.text)}</pre></details>`;
  const st = (SW.data && SW.data.status) || {};
  $("#sw-actions").innerHTML = `
    <div class="act"><div><b>Digest</b><p class="muted small">The scoreboard, today's alerts and the gate. Built after every close${st.webhook ? " and posted to Discord" : ""}.</p></div>
      <div class="btns"><button class="btn" data-dg="daily">Build daily</button><button class="btn" data-dg="weekly">Build weekly</button>
      <button class="btn" id="sw-dg-post" ${st.webhook ? "" : 'disabled title="set ABG_SWING_WEBHOOK_URL first"'}>Post daily to Discord</button></div></div>
    <div class="act"><div><b>R&amp;D agent</b><p class="muted small">Replays up to 8 single-setting variants, breaks results down by author and month, and writes a proposal for you. It can't change the rules. ${st.api_key ? "Uses your Anthropic key (Sonnet; a few cents per run)." : "Needs ABG_ANTHROPIC_API_KEY in .env."}</p></div>
      <div class="btns"><button class="btn" id="sw-rd" ${st.api_key ? "" : "disabled"}>Run once</button></div></div>
    <div id="sw-digest"></div>`;
  $$("[data-dg]").forEach((b) => (b.onclick = () => digest(b.dataset.dg, false)));
  $("#sw-dg-post").onclick = () => { if (confirm("Post today's swing digest to the Discord channel?")) digest("daily", true); };
  $("#sw-rd").onclick = runResearch;
  const doc = (x) => `<details><summary>${esc(x.title || x.name)} <span class="muted small">${ago(x.at)}</span>${x.changes && Object.keys(x.changes).length ? ` <code>${esc(JSON.stringify(x.changes))}</code>` : ""}</summary><pre class="report">${esc(x.text)}</pre></details>`;
  $("#sw-proposals").innerHTML = r.proposals.length ? r.proposals.map(doc).join("") : '<p class="muted">No proposals yet. The agent files one only when a change clears all three conditions.</p>';
  $("#sw-reports").innerHTML = r.reports.length ? r.reports.map(doc).join("") : `<p class="muted">No runs yet. ${st.api_key ? "Run it once above, or set ABG_SWING_RESEARCH=weekly." : ""}</p>`;
}
async function loadVariants() {
  let r;
  try { r = await api("/api/swing/variants"); } catch (e) { $("#sw-variants").innerHTML = `<p class="muted">${esc(e.message)}</p>`; return; }
  const cell = (s) => s && s.closed ? `${fmt(s.win_rate, 1)}% <span class="muted small">of ${s.closed}</span>` : '<span class="muted">—</span>';
  const v = r.variants || [];
  const base = v[0] && v[0].all;
  $("#sw-variants").innerHTML = v.length ? `<table class="variants"><thead><tr><th>Variant</th><th class="n">Taken</th><th class="n">Closed</th><th class="n">Win rate</th><th class="n">Total R</th><th class="n">Jul–Aug</th><th class="n">Sep–Oct</th></tr></thead><tbody>
    ${v.map((x, k) => `<tr class="${k === 0 ? "base" : ""}"><td>${esc(x.name)}${Object.keys(x.changes).length ? ` <code>${esc(Object.entries(x.changes).map(([a, b]) => a + "=" + b).join(", "))}</code>` : ""}</td>
      <td class="n">${x.all.taken}</td><td class="n">${x.all.closed}</td>
      <td class="n">${x.all.win_rate == null ? "—" : fmt(x.all.win_rate, 1) + "%"}${k && base && x.all.win_rate != null && base.win_rate != null ? ` <span class="small ${x.all.win_rate >= base.win_rate ? "pos" : "neg"}">${x.all.win_rate >= base.win_rate ? "+" : ""}${fmt(x.all.win_rate - base.win_rate, 1)}</span>` : ""}</td>
      <td class="n">${sgn(x.all.sum_R, 2)}</td><td class="n">${cell(x.before)}</td><td class="n">${cell(x.after)}</td></tr>`).join("")}</tbody></table>
    <p class="muted small">Same engine and data as the live book. The rules were fitted on these same ideas, so a better row here is a lead for the R&amp;D agent to test on new trades, not a reason to change the file.</p>`
    : `<p class="muted">${esc(r.error || "No variants.")}</p>`;
}
async function digest(period, post) {
  busy(true);
  try {
    const d = await api("/api/swing/digest", { json: { period, post } });
    $("#sw-digest").innerHTML = `<div class="digest"><b>${esc(d.title)}</b> <span class="muted small">${esc(d.delivered)}</span><ul class="plain">${d.lines.map((l) => `<li>${esc(l).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")}</li>`).join("")}</ul></div>`;
    if (post) toast(`Digest: ${d.delivered}`, d.delivered === "posted" ? "ok" : "error");
  } catch (e) { toast(e.message, "error"); } finally { busy(false); }
}
async function runResearch() {
  if (!confirm("Run the R&D agent now? It calls the Anthropic API (a few cents) and takes a minute or two.")) return;
  const b = $("#sw-rd"); b.disabled = true; b.textContent = "Running…"; busy(true);
  try { const r = await api("/api/swing/research/run", { json: {} }); toast(`R&D report saved: ${r.name}`, "ok"); loadResearch(); }
  catch (e) { toast(e.message, "error"); } finally { busy(false); b.disabled = false; b.textContent = "Run once"; }
}

// ------------------------------------------------------------------ live events + wiring
window.onSwingEvent = (d) => {
  if (d.type === "alert") {
    toast(`${d.symbol}: ${(d.text || "").split("\n")[0].replace(/^[^\w$]+/u, "")}`, d.kind === "STOPPED" ? "error" : "info");
    SW.history = null;
  }
  clearTimeout(SW.t); SW.t = setTimeout(loadSwing, 400);
};

LOADERS.swing = (sub) => {
  loadSwing();
  if (sub === "record") loadRecord();
  if (sub === "research") loadResearch();
  if (sub === "ideas") renderIdeas();
  if (sub === "add") setTimeout(() => $("#sw-text").focus(), 0);
  if (sub === "book" && !SW.record) api("/api/swing/record").then((r) => { SW.record = r; if (SW.data) renderStrip(SW.data.scoreboard, SW.data.ideas); }).catch(() => {});
};

function swingInit() {
  document.addEventListener("click", (e) => {
    const row = e.target.closest("[data-key]");
    if (row && row.closest("#sw-positions, #sw-orders, #sw-ideas, #sw-feed")) { openIdea(row.dataset.key); return; }
    const op = e.target.closest("[data-open]");
    if (op) { setTimeout(() => openIdea(op.dataset.open), 300); }
    if (e.target.closest("[data-close]")) closeDrawer();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#drawer").hidden) closeDrawer();
    if (e.key === "Enter" && e.target.matches("[data-key]") && e.target.closest("#sw-positions, #sw-orders, #sw-ideas")) openIdea(e.target.dataset.key);
  });
  $("#sw-search").addEventListener("input", (e) => { SW.q = e.target.value; renderIdeas(); });
  $("#sw-read").onclick = readText;
  $("#sw-text").addEventListener("paste", () => setTimeout(readText, 30));
  $("#sw-text").addEventListener("input", () => { clearTimeout(readT); readT = setTimeout(readText, 900); });
  $("#sw-form").addEventListener("input", () => { clearTimeout(fieldT); fieldT = setTimeout(previewFields, 450); });
  $("#sw-form").addEventListener("change", (e) => { if (e.target.name === "entry_type") entryTypeUi(); clearTimeout(fieldT); fieldT = setTimeout(previewFields, 150); });
  $("#sw-track").onclick = trackIt;
  const eq = $("#sw-equity"); eq.value = store.get("sw-equity", "") || "";
  eq.addEventListener("change", () => { store.set("sw-equity", eq.value || ""); if (SW.preview) renderVerdict(SW.preview); });
  entryTypeUi();
  const swingVisible = () => !!document.querySelector('[data-panel^="swing/"]:not([hidden])');
  setInterval(() => { if (!document.hidden && swingVisible()) loadSwing(); }, 60000);
  setInterval(() => { if (!document.hidden && SW.data) renderFeed(SW.data.alerts || []); }, 30000);
}
window.swingInit = swingInit;
window.lv = lv; window.loadSwing = loadSwing;
})();
