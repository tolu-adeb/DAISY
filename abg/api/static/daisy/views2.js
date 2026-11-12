// DAISY dashboard - the explained pages (docs/23): one stock in depth, News, Portfolio, Strategy and research.
// Every page reads top-down like an article: a headline, short paragraphs with their numbers, a picture
// where a picture is faster, then the math and the tables under Deep dive.
import { $, $$, api, post, esc, safeUrl, state, toast, usd, pct, num, move, shortDate, clock, term, rankBadge, gradeChips,
  pill, empty, section, hero, table, kv, md, spark, wireTips } from "./ui.js";
import * as V from "./views.js";

let after = null;
export function afterRender(view, box) { const f = after; after = null; if (f) f(box); }
const BOOK = { trend_pullback: "Trend pullback", mean_reversion: "Mean reversion", xs_momentum: "12-1 momentum", dual_momentum: "ETF rotation", crisis_alpha: "Crisis", analyst: "Analyst pitch" };
const link = (u, t) => (safeUrl(u) ? `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(t)}</a>` : esc(t));
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : /(ch|s|x)$/.test(w) ? "es" : "s"}`;
const isAdmin = () => state.role === "admin";
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const big = (x) => { if (x == null) return "—"; const a = Math.abs(x); for (const [d, s] of [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]]) if (a >= d) return "$" + num(x / d, a >= d * 100 ? 0 : 1) + s; return usd(x, 0); };
const pp = (x, d = 1) => (x == null ? "—" : num(x, d) + "%");                    // a value already in percent points
const stat = (k, v, sub = "", tip = "") => `<div class="stat"><span class="caps muted"${tip ? ` data-tip="${esc(tip)}" tabindex="0"` : ""}>${k}</span><span class="v num">${v}</span><span class="muted small">${sub}</span></div>`;
const onPage = (items) => `<nav class="onpage" aria-label="On this page">${items.map(([id, t]) => `<a href="#${id}" data-jump="${id}">${esc(t)}</a>`).join("")}</nav>`;
const anchor = (id) => `<span id="${id}" class="anchor"></span>`;
function wireJumps(box) {
  for (const a of $$("[data-jump]", box)) a.addEventListener("click", (e) => { e.preventDefault(); $("#" + a.dataset.jump, box)?.scrollIntoView({ behavior: "smooth", block: "start" }); });
}
function loadCharts() {
  if (window.LightweightCharts) return Promise.resolve();
  return new Promise((ok, bad) => { const sc = document.createElement("script"); sc.src = "/static/vendor/lightweight-charts.js"; sc.onload = ok; sc.onerror = () => bad(new Error("the chart library did not load")); document.head.appendChild(sc); });
}
const chartOpts = (LC) => ({ autoSize: true, layout: { background: { color: "transparent" }, textColor: css("--muted"), fontFamily: "Inter, system-ui, sans-serif", fontSize: 11 },
  grid: { vertLines: { visible: false }, horzLines: { color: css("--stroke") } }, rightPriceScale: { borderColor: css("--stroke") }, timeScale: { borderColor: css("--stroke") },
  crosshair: { mode: LC.CrosshairMode.Normal } });

// ------------------------------------------------------------------ small pictures (inline SVG: a caption always carries the numbers)
function hbars(items, { fmt = (v) => num(v, 2), label = "" } = {}) {
  const vals = items.map((i) => i.value).filter((v) => v != null);
  if (!vals.length) return empty("Not in data.");
  const lo = Math.min(0, ...vals), hi = Math.max(0, ...vals), span = hi - lo || 1, x = (v) => ((v - lo) / span) * 100;
  return `<div class="hbars" role="img" aria-label="${esc(label)}: ${esc(items.map((i) => `${i.label} ${i.value == null ? "not in data" : fmt(i.value)}`).join(", "))}">${items.map((it) => {
    const v = it.value;
    if (v == null) return `<div class="hb"><span class="l">${esc(it.label)}</span><span class="trk"></span><span class="n muted">not in data</span></div>`;
    const a = x(Math.min(0, v)), b = x(Math.max(0, v));
    return `<div class="hb"><span class="l">${esc(it.label)}</span><span class="trk"><i style="left:${a.toFixed(2)}%;width:max(2px, ${(b - a).toFixed(2)}%);background:${it.color || (v < 0 ? "var(--down)" : "var(--accent-fill)")}"></i><b style="left:${x(0).toFixed(2)}%"></b></span><span class="n num">${esc(fmt(v))}</span></div>`;
  }).join("")}</div>`;
}
function ranges(rows, price, label) {                      // a "football field": each row a low-high range, the price as a line
  const all = rows.flatMap((r) => [r.lo, r.hi, r.mark]).concat([price]).filter((v) => v != null && v > 0);
  if (all.length < 2) return empty("Valuation ranges: not in data for this name.");
  const lo = Math.min(...all) * 0.94, hi = Math.max(...all) * 1.06, W = 560, L = 170, R = 20, H = 34, x = (v) => L + ((v - lo) / (hi - lo)) * (W - L - R);
  return `<svg class="pic" viewBox="0 0 ${W} ${rows.length * H + 30}" role="img" aria-label="${esc(label)}">${rows.map((r, i) => {
    const y = i * H + 8;
    if (r.lo == null || r.hi == null) return `<text x="0" y="${y + 14}" class="t">${esc(r.label)}</text><text x="${L}" y="${y + 14}" class="t m">not in data</text>`;
    return `<text x="0" y="${y + 14}" class="t">${esc(r.label)}</text><rect x="${x(Math.min(r.lo, r.hi))}" y="${y + 2}" width="${Math.max(3, Math.abs(x(r.hi) - x(r.lo)))}" height="16" rx="4" fill="var(--accent-fill)" opacity=".75"/>
      ${r.mark != null ? `<rect x="${x(r.mark) - 1.5}" y="${y - 1}" width="3" height="22" fill="var(--text)"/>` : ""}
      <text x="${x(Math.min(r.lo, r.hi))}" y="${y + 31}" class="t m s">${esc(usd(Math.min(r.lo, r.hi), 0))}</text><text x="${x(Math.max(r.lo, r.hi))}" y="${y + 31}" class="t m s" text-anchor="end">${esc(usd(Math.max(r.lo, r.hi), 0))}</text>`;
  }).join("")}<line x1="${x(price)}" x2="${x(price)}" y1="0" y2="${rows.length * H + 8}" stroke="var(--warn)" stroke-width="2" stroke-dasharray="4 3"/>
    <text x="${x(price)}" y="${rows.length * H + 24}" class="t n" text-anchor="middle">price ${esc(usd(price))}</text></svg>`;
}
function waterfall(parts, label) {                         // how a score is built, input by input
  let run = 0; const steps = parts.map((p, i) => { const a = i ? run : 0; run = i ? run + p.points : p.points; return { ...p, a, b: run }; });
  const lo = Math.min(0, ...steps.map((s) => Math.min(s.a, s.b))), hi = Math.max(100, ...steps.map((s) => Math.max(s.a, s.b))), W = 560, L = 250, R = 60, H = 26, x = (v) => L + ((v - lo) / (hi - lo)) * (W - L - R);
  return `<svg class="pic" viewBox="0 0 ${W} ${(steps.length + 1) * H + 6}" role="img" aria-label="${esc(label)}">${steps.map((s, i) => `
    <text x="0" y="${i * H + 17}" class="t">${esc(s.input.length > 38 ? s.input.slice(0, 37) + "…" : s.input)}</text>
    <rect x="${x(Math.min(s.a, s.b))}" y="${i * H + 6}" width="${Math.max(2, Math.abs(x(s.b) - x(s.a)))}" height="${H - 12}" rx="3" fill="${i === 0 ? "var(--muted)" : s.points >= 0 ? "var(--up)" : "var(--down)"}"/>
    <text x="${x(Math.max(s.a, s.b)) + 6}" y="${i * H + 17}" class="t n">${i === 0 ? num(s.points, 0) : (s.points > 0 ? "+" : "") + num(s.points, 0)}</text>`).join("")}
    <text x="0" y="${steps.length * H + 17}" class="t"><tspan font-weight="650">Score</tspan></text><rect x="${x(0)}" y="${steps.length * H + 6}" width="${Math.max(2, x(Math.max(0, Math.min(100, run))) - x(0))}" height="${H - 12}" rx="3" fill="var(--accent-fill)"/>
    <text x="${x(Math.max(0, Math.min(100, run))) + 6}" y="${steps.length * H + 17}" class="t n">${num(Math.max(0, Math.min(100, run)), 0)}</text></svg>`;
}
function diagram(kind, label) {                            // the shape of a setup, drawn once, in words underneath
  const P = { pullback: "M10,92 L60,74 L95,82 L140,56 L175,66 L205,84 L232,74 L262,50 L300,28", bounce: "M10,60 L70,48 L120,40 L150,44 L172,82 L196,66 L232,46 L300,38",
    rank: "", switch: "M10,84 L80,60 L140,36 L170,44", shield: "M10,40 L80,34 L130,48 L170,80 L230,100 L300,92" }[kind];
  const marks = { pullback: [[205, 84, "dip", -6, 18], [232, 74, "turn: buy", 8, 16]], bounce: [[172, 82, "sold hard: buy", 8, 16], [232, 46, "bounce: sell", 4, 20]],
    switch: [[170, 44, "trend breaks", -78, 34]], shield: [[130, 48, "flags on", 6, -10]] }[kind] || [];
  let body = `<path d="${P}" fill="none" stroke="var(--accent)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>`;
  if (kind === "rank") body = [86, 74, 62, 50, 38, 26, 18].map((h, i) => `<rect x="${20 + i * 40}" y="${104 - h}" width="26" height="${h}" rx="3" fill="${i < 3 ? "var(--accent-fill)" : "var(--stroke-strong)"}"/>`).join("") + `<text x="20" y="14" class="t s">top of the ranking: owned</text>`;
  if (kind === "switch") body += `<path d="M170,44 L210,70 L250,86 L300,96" fill="none" stroke="var(--down)" stroke-width="2.5" stroke-dasharray="5 4"/><path d="M170,44 L300,40" fill="none" stroke="var(--up)" stroke-width="2.5"/><text x="218" y="32" class="t s">defensive asset</text>`;
  if (kind === "pullback") body += `<path d="M10,100 L300,52" fill="none" stroke="var(--stroke-strong)" stroke-width="1.5" stroke-dasharray="3 4"/><text x="14" y="112" class="t s m">200-day average</text>`;
  return `<svg class="pic diagram" viewBox="0 0 320 118" role="img" aria-label="${esc(label)}">${body}${marks.map(([x, y, t, dx, dy]) => `<circle cx="${x}" cy="${y}" r="4.5" fill="var(--text)"/><text x="${x + dx}" y="${y + dy}" class="t s">${esc(t)}</text>`).join("")}</svg>`;
}

// ------------------------------------------------------------------ indicator maths for the chart (same definitions as the rule books)
const ema = (xs, n) => { const k = 2 / (n + 1); let e = null; return xs.map((x) => (e = e == null ? x : x * k + e * (1 - k))); };
function rsi(cl, n = 14) { let g = 0, l = 0; const out = [null]; for (let i = 1; i < cl.length; i++) { const d = cl[i] - cl[i - 1], up = Math.max(d, 0), dn = Math.max(-d, 0);
  if (i <= n) { g += up / n; l += dn / n; out.push(i === n ? 100 - 100 / (1 + g / (l || 1e-9)) : null); } else { g = (g * (n - 1) + up) / n; l = (l * (n - 1) + dn) / n; out.push(100 - 100 / (1 + g / (l || 1e-9))); } } return out; }
function bands(cl, n = 20, k = 2) { return cl.map((_, i) => { if (i < n - 1) return null; const w = cl.slice(i - n + 1, i + 1), m = w.reduce((a, b) => a + b, 0) / n, sd = Math.sqrt(w.reduce((a, b) => a + (b - m) ** 2, 0) / n); return [m - k * sd, m, m + k * sd]; }); }

// ====================================================================== Explore: one stock, in depth
export async function explore([sym]) {
  if (!sym) return V.explore();
  let c;
  try { c = await api(`/api/stocks/name/${encodeURIComponent(sym)}`); }
  catch (e) {
    // Outside the research screen (an ETF, a foreign listing, a brand-new name): the rules have no read on it,
    // but the analysis engine may. Show that, and say plainly which it is.
    let p = null;
    try { p = await api(`/api/stocks/profile/${encodeURIComponent(sym)}`); } catch { p = null; }
    if (p && p.available) c = { symbol: p.symbol, as_of: p.day, outside: true, row: { name: p.name, close: p.price, chg_1d: p.change_pct, sector: (p.fundamentals || {}).sector },
      verdict: `${p.symbol} is outside DAISY's rule universe (an ETF, a foreign listing, or a name without enough history in the screen), so the rules have no read on it and cannot pitch it. Everything below comes from the analysis engine.`,
      grades: p.grades, sentiment: p.sentiment || {}, sentiment_series: [], news: [], setups: [], gates: [], pitches: [] };
  }
  if (!c) {
    const e = { message: `${sym.toUpperCase()} is not in the research universe and no data provider returned prices for it. Check the ticker.` };
    return hero("Explore", `No card for ${esc(sym.toUpperCase())} yet.`, esc(e.message), { short: true, extra: `<p style="margin-top:18px"><a class="link-caps" href="#/explore">Search another name</a></p>` });
  }
  const r = c.row || {}, s = c.sentiment || {}, name = r.name && r.name !== c.symbol ? r.name : "";
  const noteName = (() => { try { return localStorage.getItem("daisy-name") || ""; } catch { return ""; } })();
  after = (box) => { wireJumps(box); drawPrice(box, c.symbol); fillProfile(box, c); wirePitch(box, c); };
  const ind = ["close", "ema50", "ema200", "sma200", "sma5", "rsi14", "min_rsi14_5", "rsi2", "mom_12_1", "mom126", "vol252", "atr14", "prev_high", "macd_hist", "macd_hist_prev", "adv20_usd", "dist_200", "mom_score", "mom_rank", "mom_pct", "trend_pct", "chg_5d", "chg_21d"];
  return hero(`Explore · as of the ${shortDate(c.as_of)} close`, `${esc(c.symbol)}${name ? ` <span class="muted">— ${esc(name)}</span>` : ""}`,
      `<span class="num" style="font-size:1.5rem">${usd(r.close)}</span> ${move(r.chg_1d)} <span class="muted">on the day</span>`, { short: true,
      extra: `<div class="chips" style="margin-top:16px;align-items:center">${c.drank ? rankBadge(c.drank) + " " + pill(c.rank_words || "") : pill("Not ranked")}
          ${isAdmin() ? `<a class="btn solid" href="#pitch-this" data-jump="pitch-this">Pitch this stock</a>` : ""}<a class="btn" href="#/explore">Search another</a></div>
        <div style="margin-top:14px">${gradeChips(c.grades)}</div>
        <div id="ex-summary" class="article" style="margin-top:22px"><p>${esc(c.verdict)}</p><p class="muted small" role="status">Loading the deeper read: key numbers, ratings, valuation and risk…</p></div>` })
    + `<div class="grid">${onPage([["ex-chart-sec", "Chart"], ["ex-key", "Key numbers"], ["ex-ratings", "Ratings"], ["ex-val", "Valuation"], ["ex-risk", "Risk and outlook"], ["ex-news", "News"], ["ex-rules", "Rules"]].concat(isAdmin() ? [["pitch-this", "Pitch this"]] : []))}</div>`
    + section("Price", `${anchor("ex-chart-sec")}<div class="toolbar" role="group" aria-label="Chart options">
        ${[["3M", 63], ["6M", 126], ["1Y", 252], ["2Y", 520]].map(([t, n]) => `<button class="pill${n === 252 ? " on" : ""}" data-range="${n}" aria-pressed="${n === 252}">${t}</button>`).join("")}
        <span class="sep" aria-hidden="true"></span>
        ${[["bb", "Bollinger bands"], ["rsi", "RSI"], ["macd", "MACD"], ["cmp", "Compare with SPY"]].map(([k, t]) => `<button class="pill" data-ind="${k}" aria-pressed="false">${t}</button>`).join("")}</div>
      <div class="chart-legend" id="ex-legend"><span class="key"><span class="swatch" style="background:var(--ema50)"></span>50-day EMA</span>
        <span class="key"><span class="swatch" style="background:var(--ema200)"></span>200-day EMA</span><span class="muted" id="ex-read">Daily candles; hover for a day's numbers.</span></div>
      <div class="chart" id="ex-chart" role="img" aria-label="Daily price chart for ${esc(c.symbol)} with its 50- and 200-day averages. The numbers are in the indicator table under Deep dive."><p class="loading">Loading the chart…</p></div>
      <div class="chart sub" id="ex-rsi" hidden role="img" aria-label="RSI-14 for ${esc(c.symbol)}"></div><div class="chart sub" id="ex-macd" hidden role="img" aria-label="MACD histogram for ${esc(c.symbol)}"></div>
      <p class="muted small" id="ex-src" style="margin-top:10px"></p>`, { wide: true })
    + section("Key numbers", `${anchor("ex-key")}<div id="ex-keynums">${kv([["Sector", esc(r.sector || "not in data")], ["Average daily trading", big(r.adv20_usd)],
        ["History", c.outside ? "not in the screen" : `${r.sessions ?? "—"} sessions${r.too_new ? " (too new for the tested rules)" : ""}`], ["Pitch eligible", r.eligible ? "yes" : `no: ${esc(r.why_not || "outside the rule universe")}`]])}</div>`, { alt: true })
    + section("Ratings", `${anchor("ex-ratings")}<div id="ex-ratings-body"><p><b>DAISY Rank${c.drank ? ` ${c.drank} · ${esc(c.rank_words || "")}` : ": not ranked"}.</b> ${esc(c.verdict)}</p>${gradeChips(c.grades)}</div>`)
    + section("Valuation", `${anchor("ex-val")}<div id="ex-val-body"><p class="muted">Loading…</p></div>`, { alt: true })
    + section("Risk and outlook", `${anchor("ex-risk")}<div id="ex-risk-body"><p class="muted">Loading…</p></div>`)
    + section("News and sentiment", `${anchor("ex-news")}<p>${esc(s.level == null ? "No scored news in the last three days." :
        `News sentiment is ${num(s.level, 2)} over ${plural(s.n, "article")} in three days${s.change != null ? `, ${s.change >= 0 ? "up" : "down"} ${num(Math.abs(s.change), 2)} against its 20-day baseline` : ""}${s.volume_ratio ? `; today's news flow is ${num(s.volume_ratio, 1)} times normal` : ""}.`)}
        <span class="muted small">Scores run from −1 (bad) to +1 (good), from a finance word list.</span></p>
        ${spark((c.sentiment_series || []).map((x) => ({ y: x.score })), { label: `Daily news sentiment for ${c.symbol}, last 30 days` })}
        ${(c.news || []).length ? `<ul class="feed">${c.news.map((n) => `<li><time datetime="${esc(n.at)}">${esc(shortDate(n.at))}</time>${link(n.url, n.title)} <span class="muted small">${esc(n.source || "")}${n.kind === "filing" ? " · filing" : ` · score ${num(n.sent, 2)}`}</span></li>`).join("")}</ul>` :
          empty("No stored headlines for this name yet. News is collected for holdings, watched names and the day's movers.")}`, { alt: true })
    + section("What DAISY's rules say", `${anchor("ex-rules")}<p>${esc(c.verdict)}</p>${(c.setups || []).length ? `<ul class="feed">${c.setups.map((x) => `<li><b>${esc(BOOK[x.book] || x.book)}</b> — ${x.steps === 1 ? "one step" : x.steps + " steps"} away. Needs ${esc(x.trigger)}.
        ${x.stop_note ? `<span class="muted">${esc(x.stop_note)}.</span>` : ""}</li>`).join("")}</ul>` : ""}
        ${(c.pitches || []).length ? table("Every pitch the desk has made on this name", ["Pitch", "Rating", "Status", "Return"],
          c.pitches.map((p) => [`<a href="#/pitch/${encodeURIComponent(p.id)}">${esc(shortDate(p.as_of))} · ${esc(BOOK[p.book] || p.book)}</a>`, esc(p.rating), esc(p.status === "closed" ? `closed: ${p.close_reason}` : p.status), move((p.track || {}).ret)]), { numeric: [3] }) : ""}
        <p style="margin-top:14px"><a class="link-caps" href="#/research">How the rules work</a></p>`)
    + (isAdmin() ? section("Pitch this stock", `${anchor("pitch-this")}<p>Have an idea on ${esc(c.symbol)}? Write it here. DAISY builds the full pitch (memo, charts, valuation, deck), fills in every number from data and checks your levels.
        It is filed as <b>an analyst pitch</b>: your idea, not a rule signal, and scored apart from the rules.</p>
        <form id="pitch-form" class="form-grid">
          <div><label for="pt-n">Your name</label><input id="pt-n" name="analyst" type="text" maxlength="40" required value="${esc(noteName)}"></div>
          <div><label for="pt-d">Direction</label><select id="pt-d" name="direction"><option value="buy">Buy</option><option value="sell">Sell / avoid</option><option value="watch">Watch</option></select></div>
          <div><label for="pt-h">Time horizon (trading days)</label><input id="pt-h" name="horizon_days" type="text" inputmode="numeric" value="20"></div>
          <div class="span"><label for="pt-t">Your thesis: why this, why now</label><textarea id="pt-t" name="thesis" rows="4" minlength="20" maxlength="1500" required></textarea></div>
          <div><label for="pt-e">Entry near ($, optional)</label><input id="pt-e" name="entry" type="text" inputmode="decimal" placeholder="${esc(num(r.close, 2))}"></div>
          <div><label for="pt-s">Stop ($)</label><input id="pt-s" name="stop" type="text" inputmode="decimal"></div>
          <div><label for="pt-1">First target ($)</label><input id="pt-1" name="t1" type="text" inputmode="decimal"></div>
          <div><label for="pt-2">Stretch target ($, optional)</label><input id="pt-2" name="t2" type="text" inputmode="decimal"></div>
          <div class="span"><label style="display:inline-flex;gap:8px;color:inherit"><input type="checkbox" name="post"> Also post it to Discord</label></div>
          <div class="span"><button class="btn solid">Build the pitch</button> <span class="muted small" id="pt-status" role="status"></span></div></form>`, { alt: true }) : "")
    + `<div class="deep">`
    + section("Deep dive · rule by rule", (c.gates || []).map((g) => table(`${BOOK[g.book] || g.book}: ${g.passes ? "every check passes" : "not every check passes"}`, ["Check", "Result"],
        g.checks.map((x) => [esc(x.check), x.pass == null ? '<span class="muted">not in data</span>' : x.pass ? '<span class="up">✓ passes</span>' : '<span class="down">✗ fails</span>']))).join("<br>"))
    + section("Deep dive · the numbers the rules read", table("From the last close", ["Indicator", "Value"], ind.map((k) => [esc(k), num(r[k], Math.abs(r[k] || 0) >= 1000 ? 0 : 4)]), { numeric: [1] })
        + `<div id="ex-deep-more"></div>`, { alt: true })
    + `</div>`;
}

async function fillProfile(box, c) {
  let p;
  try { p = await api(`/api/stocks/profile/${encodeURIComponent(c.symbol)}`); } catch { p = null; }
  if (!document.body.contains(box) || !$("#ex-summary", box)) return;
  const r = c.row || {};
  if (!p) { $("#ex-summary", box).innerHTML = `<p>${esc(c.verdict)}</p>`; for (const id of ["#ex-val-body", "#ex-risk-body"]) $(id, box).innerHTML = empty("Not in data for this name."); return; }
  $("#ex-summary", box).innerHTML = (c.outside ? `<p>${esc(c.verdict)}</p>` : "") + (p.summary.map((s) => `<p>${esc(s.text)}</p>`).join("") || (c.outside ? "" : `<p>${esc(c.verdict)}</p>`));
  const f = p.fundamentals || {}, ex = f.extra || {}, st = p.statistics || {};
  const fr = (x) => (x == null ? null : Math.abs(x) > 1.5 ? x / 100 : x);
  $("#ex-keynums", box).innerHTML = `<div class="stats">${[
    ["Market cap", big(f.market_cap), "what the whole company is worth", "Share price times shares outstanding."],
    [term("Forward P/E"), num(f.forward_pe, 1), f.pe != null ? `trailing P/E ${num(f.pe, 1)}` : "price ÷ next year's earnings", ""],
    ["Earnings per share", f.eps == null ? "—" : usd(f.eps), ex.eps_growth_ttm_yoy_pct != null ? `${pp(ex.eps_growth_ttm_yoy_pct)} vs a year ago` : "last twelve months", "Profit divided by the number of shares."],
    ["Operating margin", fr(ex.operating_margin) == null ? "—" : pct(fr(ex.operating_margin), 0), fr(f.profit_margin) != null ? `net margin ${pct(fr(f.profit_margin), 0)}` : "profit per $1 of sales", "How much of each dollar of sales is left after running costs."],
    ["Dividend yield", f.dividend_yield == null ? "—" : pct(fr(f.dividend_yield), 1), "paid to holders each year", ""],
    [term("Beta"), num(f.beta ?? st.beta, 2), "1.0 moves with the market", ""],
    ["52-week range", f.week52_low != null ? `${usd(f.week52_low, 0)}–${usd(f.week52_high, 0)}` : "—", r.close && f.week52_high ? `${pct(r.close / f.week52_high - 1, 0)} from the high` : "", ""],
    ["Average daily trading", big(r.adv20_usd), "20-day average, in dollars", "How much of the stock changes hands on a typical day."],
  ].map(([k, v, sub, tip]) => stat(k, v === "—" ? '<span class="muted" style="font-size:1rem">not in data</span>' : v, sub, tip)).join("")}</div>
    <p class="muted small" style="margin-top:14px">${f.sector ? `${esc(f.sector)}${f.industry ? " · " + esc(f.industry) : ""}. ` : ""}Fundamentals from ${esc((p.sources || {}).fundamentals || "a data provider")}; short interest is not in a free source.</p>
    ${f.description ? `<details><summary>What the company does</summary><p>${esc(f.description)}</p></details>` : ""}`;
  const GL = { momentum: "Momentum", trend: "Trend", value: "Value", quality: "Quality", sentiment: "News/Sentiment" }, cs = p.consensus, rec = (p.forecast || {}).recommendation || {};
  const sg = (x, d = 0) => (x == null ? "—" : (x > 0 ? "+" : x < 0 ? "−" : "") + num(Math.abs(x), d) + "%"), HZ = (p.forecast || {}).horizons || [];
  const recH = HZ.find((h) => h.label === rec.horizon_label && h.return_pct);
  $("#ex-ratings-body", box).innerHTML = `<p><b>DAISY Rank${c.drank ? ` ${c.drank} · ${esc(c.rank_words || "")}` : ": not ranked"}.</b> ${esc(c.verdict)}</p>
    ${table("Style grades, A (best) to F, and the reason for each", ["Grade", "", "Why"], Object.keys(GL).map((k) => [esc(GL[k]), `<b>${esc((p.grades || {})[k] || "—")}</b>`, esc((p.grade_reasons || {})[k] || "")]))}
    <div class="two" style="margin-top:22px">
      <div><h3>Technical read</h3><p>${p.signal ? `DAISY's indicator blend scores ${esc(c.symbol)} <b>${num(p.signal.score, 0)}</b> on a scale of −100 to +100: <b>${esc(p.signal.label)}</b>.${p.regime ? ` The stock is in ${esc(String(p.regime.trend).replace(/_/g, " "))} with ${esc(p.regime.volatility_regime)} volatility.` : ""}` : "Not in data."}</p>
        ${rec.action ? `<p>Our forecast model's outlook over ${esc(rec.horizon_label || "the next quarter")}: <b>${esc(rec.action)}</b>${recH ? `; the middle of its range is ${sg(recH.return_pct.p50)}` : ""}, with a ${pct(rec.prob_up, 0)} chance of finishing higher. <span class="muted small">A model estimate, not a rule signal.</span></p>` : ""}</div>
      <div><h3>Wall Street's view <span class="muted small">(third-party)</span></h3>${cs ? `<p>${cs.analysts} analysts: consensus <b>${esc(cs.label)}</b>.</p>${hbars([["Strong buy", cs.strongBuy], ["Buy", cs.buy], ["Hold", cs.hold], ["Sell", cs.sell], ["Strong sell", cs.strongSell]].map(([l, v]) => ({ label: l, value: v })), { fmt: (v) => String(v), label: "Analyst recommendations" })}
        <p class="muted small">From Finnhub for ${esc(cs.period)}. Analysts' price targets are not on the free plan.</p>` : `<p class="muted">Analyst recommendations: not in data.</p>`}</div></div>`;
  const v = p.valuation, sc = (v || {}).scenarios || {}, cp = (v || {}).comps || {};
  $("#ex-val-body", box).innerHTML = v ? `<p>${v.upside != null ? `Our cross-check puts fair value near <b>${usd(v.fair_value)}</b>, ${pct(Math.abs(v.upside), 0)} ${v.upside >= 0 ? "above" : "below"} the price of ${usd(r.close)}.` : ""}
      It blends two views: what the company's future earnings are worth today (a ${term("DCF")}), and what similar companies trade for (${term("Comps")}).${v.implied_growth != null ? ` Today's price already assumes earnings grow about ${pct(v.implied_growth, 0)} a year.` : ""}</p>
    ${ranges([{ label: "52-week range", lo: f.week52_low, hi: f.week52_high }, { label: "Earnings value: bear to bull", lo: (sc.bear || {}).value, hi: (sc.bull || {}).value, mark: (sc.base || {}).value },
        { label: "Peer multiples", lo: cp.value_pe, hi: cp.value_forward_pe, mark: cp.value }, { label: "Blended fair value", lo: v.fair_value, hi: v.fair_value, mark: v.fair_value }], r.close, `Valuation ranges for ${c.symbol} against its price`)}
    <p class="muted small">The dark tick on each bar is that method's central estimate; the dashed line is the price. Valuation is context: the rules decide trades.</p>
    <div class="deep">${table("The arithmetic", ["Case", "Growth", "Discount rate", "Value per share"], ["bear", "base", "bull"].filter((k) => sc[k]).map((k) => [k, pct(sc[k].growth, 1), pct(sc[k].rate, 1), usd(sc[k].value)]), { numeric: [1, 2, 3] })}
      ${(p.peers || []).length ? table("Peers used", ["Peer", "P/E", "Forward P/E"], p.peers.map((x) => [esc(x.symbol || "—"), num(x.pe, 1), num(x.forward_pe, 1)]), { numeric: [1, 2] }) : ""}<p class="muted small">${esc(v.method || "")}</p></div>` :
    empty("Valuation needs earnings data, which is not in data for this name.");
  const obs = st.observations || 0, spanW = obs >= 440 ? "past two years" : obs >= 220 && obs <= 290 ? "past year" : obs >= 42 ? `past ${Math.round(obs / 21)} months` : `last ${obs} sessions`;
  const hz = HZ.find((h) => h.days >= 20) || HZ[0], scn = (p.forecast || {}).scenarios || [], rk = p.risk;
  $("#ex-risk-body", box).innerHTML = (st.ann_volatility_pct != null ? `<div class="stats">${stat("Volatility", pp(st.ann_volatility_pct, 0), "how far it swings in a year", "How much the price moves around in a year. The market is usually 15–20%.")}
      ${stat("Worst drop", pp(st.max_drawdown_pct, 0), st.trough_date ? `${spanW}; bottomed ${shortDate(String(st.trough_date).slice(0, 10))}` : spanW)}
      ${stat("Return", sg(st.total_return_pct), st.benchmark_return_pct != null ? `${spanW}; market ${sg(st.benchmark_return_pct)}` : spanW)}
      ${hz && hz.var_95_pct != null ? stat("A bad stretch", sg(-Math.abs(hz.var_95_pct)), `1-in-20 loss over ${esc(hz.label || hz.days + " days")}`, "From our forecast model: in 19 of 20 simulated paths the loss is smaller than this.") : ""}
      ${rk ? stat("Risk level", esc(rk.level || "—"), `score ${num(rk.score, 0)} of 100`) : ""}</div>` : "")
    + (hz && hz.return_pct ? `<p style="margin-top:18px">Over ${esc(hz.label || hz.days + " trading days")} our forecast model's middle outcome is ${sg(hz.return_pct.p50)}, and nine in ten simulated paths land between ${sg(hz.return_pct.p5)} and ${sg(hz.return_pct.p95)}. It is a range, not a prediction: ${pct(hz.prob_up, 0)} of paths finish higher, ${pct(hz.prob_down_10, 0)} lose more than 10% and ${pct(hz.prob_up_10, 0)} gain more than 10%.</p>` : "")
    + (scn.length ? hbars(scn.map((x) => ({ label: `${x.name} (${pct(x.probability, 0)})`, value: x.return_pct })), { fmt: (v) => (v > 0 ? "+" : "") + num(v, 0) + "%", label: "Scenario returns" }) + `<p class="muted small">Scenarios from ${esc((p.forecast || {}).model || "the forecast model")}, with the share of simulated paths in each.</p>` : "")
    + (rk && (rk.drivers || []).length ? `<p><b>What drives the risk score most:</b> ${rk.drivers.slice(0, 3).map((d) => esc(String(typeof d === "string" ? d : d.factor || d.name || "").replace(/_/g, " "))).filter(Boolean).join(", ")}.</p>` : "")
    || empty("Risk statistics need a price history from a provider; not in data for this name.");
  if (p.indicators) $("#ex-deep-more", box).innerHTML = table("More indicators, from the analysis engine", ["Indicator", "Value"], Object.entries(p.indicators).map(([k, x]) => [esc(k), num(x, Math.abs(x || 0) >= 1000 ? 0 : 3)]), { numeric: [1] });
  wireTips(box);
}

function wirePitch(box, c) {
  $("#pitch-form", box)?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target).entries()), status = $("#pt-status", box);
    f.symbol = c.symbol; f.post = !!f.post;
    status.textContent = "Building the pitch: memo, charts and checks. This takes a moment…";
    try {
      const r = await post("/api/stocks/analyst-pitch", f);
      try { localStorage.setItem("daisy-name", f.analyst); } catch { /* private window */ }
      toast(`Pitch filed. ${r.passed} of ${r.of} checks passed.`);
      location.hash = `#/pitch/${encodeURIComponent(r.id)}`;
    } catch (x) { status.textContent = x.message; toast(x.message); }
  });
}

async function drawPrice(box, sym) {
  const el = $("#ex-chart", box);
  if (!el) return;
  try {
    const [d] = await Promise.all([api(`/api/stocks/bars/${encodeURIComponent(sym)}`), loadCharts()]);
    if (!document.body.contains(el)) return;
    const LC = window.LightweightCharts, up = css("--up"), down = css("--down"), B = d.bars.filter((b) => b.o != null), cl = B.map((b) => b.c), T = B.map((b) => b.t);
    el.innerHTML = "";
    const chart = LC.createChart(el, chartOpts(LC));
    const candles = chart.addCandlestickSeries({ upColor: up, downColor: down, wickUpColor: up, wickDownColor: down, borderVisible: false });
    candles.setData(B.map((b) => ({ time: b.t, open: b.o, high: b.h, low: b.l, close: b.c })));
    const vol = chart.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false, color: css("--stroke-strong") });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });
    vol.setData(B.filter((b) => b.v != null).map((b) => ({ time: b.t, value: b.v })));
    const line = (data, color, w = 2, style = 0) => { const sr = chart.addLineSeries({ color, lineWidth: w, lineStyle: style, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }); sr.setData(data); return sr; };
    line(B.filter((b) => b.e50 != null).map((b) => ({ time: b.t, value: b.e50 })), css("--ema50"));
    line(B.filter((b) => b.e200 != null).map((b) => ({ time: b.t, value: b.e200 })), css("--ema200"));
    for (const l of d.levels || []) candles.createPriceLine({ price: l.price, title: l.label, color: l.kind === "stop" ? down : l.kind === "entry" ? css("--muted") : css("--text"), lineWidth: 1, lineStyle: l.kind === "stop" ? 0 : 2, axisLabelVisible: true });
    const byDay = Object.fromEntries(B.map((b) => [b.t, b])), read = $("#ex-read", box), idle = read.textContent;
    chart.subscribeCrosshairMove((p) => {
      const t = p && p.time ? (typeof p.time === "string" ? p.time : `${p.time.year}-${String(p.time.month).padStart(2, "0")}-${String(p.time.day).padStart(2, "0")}`) : null, b = t && byDay[t];
      read.innerHTML = b ? `<b>${esc(shortDate(b.t))}</b> · open ${usd(b.o)} · high ${usd(b.h)} · low ${usd(b.l)} · close ${usd(b.c)} · 50-day ${usd(b.e50)} · 200-day ${usd(b.e200)}` : esc(idle);
    });
    let span = 252; const subs = {};
    const show = () => { if (!T.length) return; const rng = { from: T[Math.max(0, T.length - span)], to: T[T.length - 1] }; chart.timeScale().setVisibleRange(rng); for (const s of Object.values(subs)) s.chart.timeScale().setVisibleRange(rng); };
    show();
    for (const b of $$("[data-range]", box)) b.addEventListener("click", () => { for (const x of $$("[data-range]", box)) { x.classList.toggle("on", x === b); x.setAttribute("aria-pressed", x === b ? "true" : "false"); } span = +b.dataset.range; show(); });
    const extra = {};
    const sub = (id) => { const host = $(id, box); host.hidden = false; host.innerHTML = ""; const ch = LC.createChart(host, { ...chartOpts(LC), timeScale: { borderColor: css("--stroke"), visible: false } }); return ch; };
    const toggles = {
      bb: (on) => { if (on) { const bb = bands(cl); extra.bb = [0, 2].map((k) => line(T.map((t, i) => (bb[i] ? { time: t, value: bb[i][k] } : null)).filter(Boolean), css("--muted"), 1, 2)); } else { (extra.bb || []).forEach((s) => chart.removeSeries(s)); extra.bb = null; } },
      rsi: (on) => { const host = $("#ex-rsi", box); if (!on) { host.hidden = true; subs.rsi?.chart.remove(); delete subs.rsi; return; }
        const ch = sub("#ex-rsi"), rs = rsi(cl), sr = ch.addLineSeries({ color: css("--s1"), lineWidth: 2, priceLineVisible: false, title: "RSI-14" });
        sr.setData(T.map((t, i) => (rs[i] != null ? { time: t, value: rs[i] } : null)).filter(Boolean)); [30, 70].forEach((lv) => sr.createPriceLine({ price: lv, color: css("--stroke-strong"), lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: lv === 30 ? "oversold" : "overbought" })); subs.rsi = { chart: ch }; show(); },
      macd: (on) => { const host = $("#ex-macd", box); if (!on) { host.hidden = true; subs.macd?.chart.remove(); delete subs.macd; return; }
        const ch = sub("#ex-macd"), a = ema(cl, 12), b2 = ema(cl, 26), m = a.map((x, i) => x - b2[i]), sg = ema(m, 9), h = ch.addHistogramSeries({ priceLineVisible: false, title: "MACD histogram" });
        h.setData(T.map((t, i) => (i > 34 ? { time: t, value: m[i] - sg[i], color: m[i] - sg[i] >= 0 ? up : down } : null)).filter(Boolean)); subs.macd = { chart: ch }; show(); },
      cmp: async (on) => { if (!on) { if (extra.cmp) chart.removeSeries(extra.cmp); extra.cmp = null; chart.priceScale("right").applyOptions({ mode: 0 }); return; }
        const s = await api("/api/stocks/bars/SPY"); extra.cmp = line(s.bars.filter((x) => x.c != null && byDay[x.t]).map((x) => ({ time: x.t, value: x.c })), css("--s1"), 2); extra.cmp.applyOptions({ title: "SPY" });
        chart.priceScale("right").applyOptions({ mode: LC.PriceScaleMode.Percentage }); },
    };
    for (const b of $$("[data-ind]", box)) b.addEventListener("click", async () => { const on = b.getAttribute("aria-pressed") !== "true"; b.setAttribute("aria-pressed", on ? "true" : "false"); b.classList.toggle("on", on);
      try { await toggles[b.dataset.ind](on); } catch (e) { toast(e.message); } });
    $("#ex-src", box).textContent = `${B.length} daily bars to ${shortDate(d.as_of)}, from ${d.source}.` + ((d.levels || []).length ? ` Lines: ${d.levels.map((l) => `${l.label} ${usd(l.price)}`).join(", ")}.` : "");
  } catch (e) { el.innerHTML = `<p class="empty" style="margin:16px">No chart for ${esc(sym)}: ${esc(e.message)}</p>`; }
}

// ====================================================================== News
export async function news([cat]) {
  const d = await api(`/api/stocks/newsfeed${cat ? `?category=${encodeURIComponent(cat)}` : ""}`);
  const item = (n) => `<li class="news-item"><div><time datetime="${esc(n.at)}">${esc(shortDate(n.at))} ${esc(clock(n.at))}</time>${n.mine ? pill(n.mine === "holding" ? "We hold it" : "On our list", true) : ""}
      ${n.symbol ? `<a class="pill" href="#/explore/${encodeURIComponent(n.symbol)}">${esc(n.symbol)}</a>` : ""}</div>
    <div class="news-title">${link(n.url, n.title)}</div>
    <div class="muted small">${esc(n.source || "")}${n.kind === "filing" ? " · SEC filing" : n.sent ? ` · sentiment ${num(n.sent, 2)}` : ""} · ${n.categories.map((k) => esc((d.categories.find((c) => c.key === k) || {}).label || k)).join(", ")}</div></li>`;
  after = (box) => {
    const inp = $("#nw-q", box), out = $("#nw-list", box); let timer = null, n = 0;
    inp?.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(async () => { const mine = ++n;
      try { const r = await api(`/api/stocks/newsfeed?${cat ? `category=${encodeURIComponent(cat)}&` : ""}q=${encodeURIComponent(inp.value.trim())}`); if (mine === n) out.innerHTML = r.items.length ? r.items.map(item).join("") : `<li>${empty("No headline matches that.")}</li>`; } catch (e) { toast(e.message); } }, 250); });
  };
  return hero(`News${cat ? " · " + ((d.categories.find((c) => c.key === cat) || {}).label || cat) : ""}`, "What moved, and why it matters to us.", "", { short: true,
      extra: `<div class="article" style="margin-top:8px">${d.brief.map((s) => `<p>${esc(s)}</p>`).join("")}</div>` })
    + section("Headlines", `<div class="toolbar" role="group" aria-label="News categories"><a class="pill${cat ? "" : " on"}" href="#/news">All</a>
        ${d.categories.map((c) => `<a class="pill${c.key === cat ? " on" : ""}" href="#/news/${c.key}"${c.key === cat ? ' aria-current="true"' : ""}>${esc(c.label)} <span class="muted">${c.count}</span></a>`).join("")}</div>
      <form role="search" onsubmit="return false" style="margin-bottom:18px"><label for="nw-q" class="caps">Search headlines or a ticker</label><input id="nw-q" type="search" autocomplete="off" placeholder="rate cut, NVDA, merger…"></form>
      <ul class="feed" id="nw-list" aria-live="polite">${d.items.length ? d.items.map(item).join("") : `<li>${empty("No stored headlines yet. News is collected at the 8:30 ET scan, during the session and at the daily pass, and needs a Finnhub key.")}</li>`}</ul>
      <p class="muted small" style="margin-top:14px">Names the fund holds come first, then names on our pitch and watch lists, newest first. Categories are assigned by rule (the feed's own label, whose ticker it is, and keywords).</p>`, { wide: true })
    + ((d.shocks || []).length ? section("Theme shocks", `<ul class="feed">${d.shocks.map((s) => `<li><b>${esc(s.label)}</b> — ${s.names.slice(0, 6).map((n) => `<a href="#/explore/${encodeURIComponent(n.symbol)}">${esc(n.symbol)}</a> ${move(n.chg)}`).join(", ")}${s.source && safeUrl(s.source.url) ? ` · ${link(s.source.url, "source")}` : ""}</li>`).join("")}</ul>
        <p class="muted small">A theme shock is one story moving a group of related names together.</p>`, { alt: true }) : "");
}

// ====================================================================== Portfolio: what the fund holds, how it has done, what the log shows
export async function portfolio() {
  const d = await api("/api/stocks/portfolio");
  const t = d.totals || {}, open = d.open || [], closed = d.closed || [], admin = isAdmin(), pf = d.performance || {}, ps = pf.stats || {}, bd = d.breakdown || {}, cn = bd.concentration || {};
  const money = (x) => (x == null ? "—" : usd(x, 0)), signed = (x) => (x == null ? "—" : (x > 0 ? "+" : x < 0 ? "−" : "") + usd(Math.abs(x), 0)), sp = (x) => (x == null ? "—" : (x > 0 ? "+" : "") + pct(x));
  const said = !open.length && !closed.length ? "Nothing has been logged yet." + (admin ? " Add the fund's first position below." : " An admin records the fund's positions here.") :
    (pf.available ? `Since ${shortDate(pf.start)} the fund has returned ${sp(ps.total_return)} against ${esc(d.benchmark || "SPY")}'s ${sp(ps.bench_return)}: ${ps.excess >= 0 ? "ahead" : "behind"} by ${pct(Math.abs(ps.excess))}. ` : "")
    + `It holds ${plural(open.length, "position")} worth ${money(t.invested)}${t.cash ? ` plus ${money(t.cash)} in cash` : ""}.`;
  const acts = (r, isOpen) => (admin ? `<div class="row-actions">${isOpen ? `<button data-act="edit" data-id="${r.id}">Edit</button><button data-act="close" data-id="${r.id}">Sold</button>` : `<button data-act="reopen" data-id="${r.id}">Reopen</button>`}<button data-act="delete" data-id="${r.id}">Remove</button></div>` : "");
  const pl = (r) => (r.pitch_id ? `<a href="#/pitch/${encodeURIComponent(r.pitch_id)}">pitch</a>` : "");
  const openRows = open.map((r) => [`<a href="#/explore/${encodeURIComponent(r.symbol)}"><b>${esc(r.symbol)}</b></a>`, num(r.shares, r.shares % 1 ? 3 : 0), usd(r.cost), usd(r.last), money(r.value), r.weight == null ? "—" : pct(r.weight),
    `${move(r.gain_pct)}<br><span class="muted small">${signed(r.gain)}</span>`, move(r.excess), esc(shortDate(r.opened)), `${esc(r.note || "")} ${pl(r)}`, ...(admin ? [acts(r, true)] : [])]);
  const closedRows = closed.map((r) => [`<b>${esc(r.symbol)}</b>`, num(r.shares, r.shares % 1 ? 3 : 0), usd(r.cost), usd(r.exit_price), `${move(r.gain_pct)}<br><span class="muted small">${signed(r.gain)}</span>`, esc(shortDate(r.opened)), esc(shortDate(r.closed)), `${esc(r.note || "")} ${pl(r)}`, ...(admin ? [acts(r, false)] : [])]);
  const hn = d.news || {}, now = new Date(), today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  after = (box) => { wireJumps(box); drawPerf(box, pf, d.benchmark || "SPY"); wirePortfolio(box, open.concat(closed), d.flows || []); };
  return hero(`Portfolio · prices as of the ${shortDate(d.as_of)} close`, "What the fund holds, and how it has done.", esc(said), { short: true })
    + `<div class="grid">${onPage([["pf-perf", "Performance"], ["pf-pos", "Positions"], ["pf-where", "Where the money is"], ["pf-review", "What the log shows"], ["pf-news", "Holdings news"]].concat(admin ? [["pf-edit", "Log a position"]] : []))}</div>`
    + section("Performance", `${anchor("pf-perf")}${pf.available ? `<div class="stats">${stat("Fund return", sp(ps.total_return), `since ${shortDate(pf.start)}`, "Time-weighted: money moving in or out does not count as performance.")}
        ${stat(esc(d.benchmark || "SPY"), sp(ps.bench_return), "same days")}${stat("Ahead / behind", sp(ps.excess), "fund minus the market")}
        ${stat("Worst drop", sp(ps.max_drawdown), ps.current_drawdown < -0.001 ? `now ${sp(ps.current_drawdown)} from the high` : "now at a high")}
        ${stat("Volatility", ps.ann_vol == null ? "—" : pct(ps.ann_vol, 0), ps.bench_vol != null ? `market ${pct(ps.bench_vol, 0)}` : "yearly")}${stat(term("Beta"), num(ps.beta, 2), "1.0 moves with the market")}</div>
      <div class="chart-legend" style="margin-top:18px"><span class="key"><span class="swatch" style="background:var(--s1)"></span>The fund</span><span class="key"><span class="swatch" style="background:var(--ema50)"></span>${esc(d.benchmark || "SPY")}</span><span class="muted" id="pf-read">Growth of $100; hover for a day.</span></div>
      <div class="chart" id="pf-chart" role="img" aria-label="Growth of 100 dollars in the fund against the market since ${esc(pf.start)}. The fund returned ${esc(sp(ps.total_return))}, the market ${esc(sp(ps.bench_return))}."></div>
      <p class="caps muted" style="margin:18px 0 6px">Drop from the high</p><div class="chart sub" id="pf-dd" role="img" aria-label="The fund's drawdown from its running high; the worst was ${esc(sp(ps.max_drawdown))}."></div>
      ${(pf.assumptions || []).map((a) => `<p class="muted small">⚑ ${esc(a)}</p>`).join("")}` : empty(pf.reason || "Performance appears once positions are logged and prices are stored.")}
      <p class="muted small" style="margin-top:12px">Rebuilt from the position log and stored closing prices. Logged by hand by the fund's admins; DAISY places no orders.</p>`, { alt: true, wide: true })
    + section("Positions", `${anchor("pf-pos")}<div class="stats" style="margin-bottom:22px">${stat("Invested", money(t.invested), "at the last close")}${stat("Cash", money(t.cash), "as logged")}${stat("Total", money(t.total), "invested plus cash")}
        ${stat("Open gain", t.gain == null ? "—" : signed(t.gain), t.gain_pct == null ? "against cost" : `${pct(t.gain_pct)} against cost`)}${stat("Realised", t.realized == null ? "—" : signed(t.realized), `${t.closed ?? 0} closed`)}</div>
      ${open.length ? table("Open positions, largest first", ["Ticker", "Shares", "Avg cost", "Last close", "Value", "Weight", "Gain", `vs ${esc(d.benchmark || "SPY")}`, "Bought", "Note", ...(admin ? ["Change"] : [])], openRows, { numeric: [1, 2, 3, 4, 5, 6, 7] }) : empty(admin ? "No open positions logged. Use the form below to add the first one." : "No open positions have been logged yet.")}
      ${closed.length ? `<div style="margin-top:28px">${table("Positions the fund has sold", ["Ticker", "Shares", "Avg cost", "Sold at", "Result", "Bought", "Sold", "Note", ...(admin ? ["Change"] : [])], closedRows, { numeric: [1, 2, 3, 4] })}</div>` : ""}`, { wide: true })
    + (open.length ? section("Where the money is", `${anchor("pf-where")}<div class="two"><div><h3>By sector</h3>${hbars((bd.sectors || []).map((s) => ({ label: s.sector, value: s.weight })), { fmt: (v) => pct(v, 0), label: "Fund weight by sector" })}</div>
        <div><h3>What made and lost money</h3>${hbars((bd.contributions || []).slice(0, 10).map((c) => ({ label: `${c.symbol}${c.status === "closed" ? " (sold)" : ""}`, value: c.gain })), { fmt: (v) => signed(v), label: "Gain or loss by holding, in dollars" })}</div></div>
        <p style="margin-top:18px">The largest position is ${cn.top1 == null ? "—" : pct(cn.top1, 0)} of the fund and the top three are ${cn.top3 == null ? "—" : pct(cn.top3, 0)}.${(cn.over_cap || []).length ? ` ${cn.over_cap.map(esc).join(", ")} ${cn.over_cap.length === 1 ? "is" : "are"} above the ${pct(cn.cap, 0)} that a DAISY pitch would suggest at most.` : ` Nothing is above the ${pct(cn.cap, 0)} a DAISY pitch would suggest at most.`}
        <span class="muted small">SPY's own sector weights are not in data, so this shows the fund alone.</span></p>`, { alt: true }) : "")
    + ((d.review || []).length ? section("What the log shows", `${anchor("pf-review")}<p class="muted">Observations from comparing the fund's positions with what DAISY pitched. Each has its evidence and one thing to test. None is an instruction.</p>
        <div class="cards" style="margin-top:14px">${d.review.map((o) => `<article class="card"><h3>${esc(o.title)}</h3><p>${esc(o.finding)}</p><ul class="small" style="padding-left:18px;margin:0">${o.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>
          <p class="small" style="margin-top:auto"><b>To test:</b> ${esc(o.suggestion)}</p></article>`).join("")}</div>`, { wide: true }) : "")
    + section("News on our holdings", `${anchor("pf-news")}${(hn.flagged || []).length ? `<div class="card sm" style="margin-bottom:18px"><span class="caps warn">Needs a look</span><ul class="feed">${hn.flagged.map((f) => `<li>⚑ <b>${esc(f.symbol)}</b> ${esc(f.text)} ${safeUrl(f.url) ? "· " + link(f.url, "source") : ""}</li>`).join("")}</ul></div>` : ""}
      ${(hn.groups || []).length ? hn.groups.map((g) => `<div class="holding-news"><h3><a href="#/explore/${encodeURIComponent(g.symbol)}">${esc(g.symbol)}</a>${g.name ? ` <span class="muted" style="font-size:1rem">${esc(g.name)}</span>` : ""}</h3><p>${esc(String(g.line || "").replace(new RegExp("^" + g.symbol.replace(/[.^$*+?()[\]{}|\\]/g, "\\$&") + ":\\s*"), ""))}</p>
          ${g.items.length ? `<ul class="feed">${g.items.slice(0, 5).map((n) => `<li><time datetime="${esc(n.at)}">${esc(shortDate(n.at))}</time>${link(n.url, n.title)} <span class="muted small">${esc(n.source || "")}${n.kind === "filing" ? " · SEC filing" : n.sent ? ` · sentiment ${num(n.sent, 2)}` : ""}</span></li>`).join("")}</ul>` : `<p class="muted small">No stored headlines in the last ${hn.days} days.</p>`}</div>`).join("") :
        empty("News about the companies the fund holds appears here once positions are logged.")}
      <p class="muted small" style="margin-top:12px">Only companies the fund holds. <a href="#/news/holdings">All categories are on the News page.</a></p>`, { alt: true })
    + (admin ? section("Log a position", `${anchor("pf-edit")}<h3 id="pf-form-h" style="margin-bottom:14px">Add a position</h3>
        <form id="pf-form" class="form-grid"><input type="hidden" name="id">
          <div><label for="pf-s">Ticker</label><input id="pf-s" name="symbol" type="text" required maxlength="12" autocomplete="off" spellcheck="false"></div>
          <div><label for="pf-n">Shares</label><input id="pf-n" name="shares" type="text" inputmode="decimal" required></div>
          <div><label for="pf-c">Average cost ($ per share)</label><input id="pf-c" name="cost" type="text" inputmode="decimal" required></div>
          <div><label for="pf-d">Date bought</label><input id="pf-d" name="opened" type="text" value="${today}" required></div>
          <div class="span"><label for="pf-t">Note (optional)</label><input id="pf-t" name="note" type="text" maxlength="300"></div>
          <div class="span"><label for="pf-p">Pitch id it came from (optional)</label><input id="pf-p" name="pitch_id" type="text" maxlength="80"></div>
          <div class="span"><button class="btn">Save position</button></div></form>
        <form id="pf-sold" class="form-grid" hidden style="margin-top:28px"><input type="hidden" name="id"><h3 id="pf-sold-h" class="span">Sold</h3>
          <div><label for="pf-x">Exit price ($ per share)</label><input id="pf-x" name="exit_price" type="text" inputmode="decimal" required></div>
          <div><label for="pf-xd">Date sold</label><input id="pf-xd" name="closed" type="text" value="${today}" required></div><div class="span"><button class="btn">Mark as sold</button></div></form>
        <h3 style="margin:34px 0 8px">Cash, deposits and withdrawals</h3><p class="muted small">Log money put into or taken out of the fund so the return is not distorted by it. Buying and selling are not deposits.</p>
        <form id="pf-cash" class="form-grid"><div><label for="pf-k">Cash in the fund now ($)</label><input id="pf-k" name="cash" type="text" inputmode="decimal" value="${t.cash ?? 0}"></div><div style="align-self:end"><button class="btn">Save cash</button></div></form>
        <form id="pf-flow" class="form-grid" style="margin-top:16px"><div><label for="pf-fa">Amount ($; negative = withdrawal)</label><input id="pf-fa" name="amount" type="text" inputmode="decimal" required></div>
          <div><label for="pf-fd">Date</label><input id="pf-fd" name="day" type="text" value="${today}" required></div><div><label for="pf-fn">Note</label><input id="pf-fn" name="note" type="text" maxlength="200"></div>
          <div style="align-self:end"><button class="btn">Add deposit / withdrawal</button></div></form>
        ${(d.flows || []).length ? table("Deposits and withdrawals", ["Date", "Amount", "Note", "Change"], d.flows.map((f) => [esc(shortDate(f.day)), signed(f.amount), esc(f.note || ""), `<div class="row-actions"><button data-flow="${f.id}">Remove</button></div>`]), { numeric: [1] }) : ""}`) :
      section("Editing", `<p class="muted">You are signed in as a viewer: the portfolio is read-only. An admin logs and updates positions.</p>`))
    + `<div class="deep">` + section("Deep dive · change log", (d.log || []).length ? `<ul class="feed">${d.log.map((h) => `<li><time datetime="${esc(h.at)}">${esc(shortDate(h.at))} ${esc(clock(h.at))}</time>${esc(h.note || h.status)}</li>`).join("")}</ul>` : empty("No changes logged yet."), { alt: true }) + `</div>`;
}
async function drawPerf(box, pf, bench) {
  const el = $("#pf-chart", box);
  if (!el || !pf.available) return;
  try {
    await loadCharts();
    const LC = window.LightweightCharts, ch = LC.createChart(el, chartOpts(LC)), S = pf.series;
    const a = ch.addLineSeries({ color: css("--s1"), lineWidth: 2, priceLineVisible: false, title: "Fund" }), b = ch.addLineSeries({ color: css("--ema50"), lineWidth: 2, priceLineVisible: false, title: bench });
    a.setData(S.map((p) => ({ time: p.t, value: p.fund * 100 }))); b.setData(S.map((p) => ({ time: p.t, value: p.bench * 100 })));
    ch.timeScale().fitContent();
    const dd = LC.createChart($("#pf-dd", box), chartOpts(LC)), ar = dd.addAreaSeries({ lineColor: css("--down"), topColor: "transparent", bottomColor: css("--down") + "55", lineWidth: 2, priceLineVisible: false, priceFormat: { type: "percent" } });
    ar.setData(S.map((p) => ({ time: p.t, value: p.dd * 100 }))); dd.timeScale().fitContent();
    const by = Object.fromEntries(S.map((p) => [p.t, p])), read = $("#pf-read", box), idle = read.textContent;
    ch.subscribeCrosshairMove((p) => { const t = p && p.time ? (typeof p.time === "string" ? p.time : `${p.time.year}-${String(p.time.month).padStart(2, "0")}-${String(p.time.day).padStart(2, "0")}`) : null, x = t && by[t];
      read.innerHTML = x ? `<b>${esc(shortDate(x.t))}</b> · fund $${num(x.fund * 100, 1)} · ${esc(bench)} $${num(x.bench * 100, 1)} · fund value ${usd(x.value, 0)} · ${pct(x.dd)} from the high` : esc(idle); });
  } catch (e) { el.innerHTML = `<p class="empty" style="margin:16px">${esc(e.message)}</p>`; }
}
function wirePortfolio(box, rows, flows) {
  const form = $("#pf-form", box), byId = Object.fromEntries(rows.map((r) => [String(r.id), r]));
  const reload = () => { location.hash = "#/portfolio?keep"; location.hash = "#/portfolio"; };
  const send = async (path, body, ok) => { try { await post(path, body); toast(ok); reload(); } catch (e) { toast(e.message); } };
  form?.addEventListener("submit", (e) => { e.preventDefault(); const f = Object.fromEntries(new FormData(form).entries()), id = f.id; delete f.id;
    send(id ? `/api/stocks/portfolio/${id}` : "/api/stocks/portfolio", id ? { action: "update", ...f } : f, id ? "Position updated." : "Position logged."); });
  $("#pf-cash", box)?.addEventListener("submit", (e) => { e.preventDefault(); send("/api/stocks/portfolio", { cash: new FormData(e.target).get("cash") }, "Cash updated."); });
  $("#pf-flow", box)?.addEventListener("submit", (e) => { e.preventDefault(); send("/api/stocks/portfolio/flow", Object.fromEntries(new FormData(e.target).entries()), "Logged."); });
  $("#pf-sold", box)?.addEventListener("submit", (e) => { e.preventDefault(); const f = Object.fromEntries(new FormData(e.target).entries()); send(`/api/stocks/portfolio/${f.id}`, { action: "close", exit_price: f.exit_price, closed: f.closed }, "Marked as sold."); });
  for (const b of $$("[data-flow]", box)) b.addEventListener("click", () => { if (b.dataset.sure) send("/api/stocks/portfolio/flow", { delete: +b.dataset.flow }, "Entry removed."); else { b.dataset.sure = "1"; b.textContent = "Click again to remove"; } });
  for (const b of $$("[data-act]", box)) b.addEventListener("click", () => {
    const r = byId[b.dataset.id], act = b.dataset.act;
    if (act === "edit") { for (const k of ["id", "symbol", "shares", "cost", "opened", "note", "pitch_id"]) form.elements[k].value = r[k] ?? ""; $("#pf-form-h", box).textContent = `Edit ${r.symbol}`; form.scrollIntoView({ block: "center" }); form.elements.shares.focus(); }
    else if (act === "close") { const s = $("#pf-sold", box); s.hidden = false; s.elements.id.value = r.id; $("#pf-sold-h", box).textContent = `Sold ${r.symbol}: at what price?`; s.elements.exit_price.value = r.last ?? ""; s.scrollIntoView({ block: "center" }); s.elements.exit_price.focus(); }
    else if (act === "reopen") send(`/api/stocks/portfolio/${r.id}`, { action: "reopen" }, "Reopened.");
    else if (act === "delete") { if (b.dataset.sure) send(`/api/stocks/portfolio/${r.id}`, { action: "delete" }, "Entry removed."); else { b.dataset.sure = "1"; b.textContent = "Click again to remove"; } }
  });
}

// ====================================================================== Research: why these strategies, shown before told
export async function research() {
  const [s, d] = await Promise.all([api("/api/stocks/strategy"), api("/api/stocks/research")]);
  const rep = d.report, v = (rep || {}).verdict || {}, checks = Object.entries(v.checks || {}), passed = checks.filter(([, c]) => c.pass).length, cv = s.conviction || {}, rs = s.research || {};
  const regimeWords = { bull_trend: "Rising and steady", bull_chop: "Rising but choppy", risk_off: "Falling or stressed", crisis: "Crisis" };
  const bookCard = (b) => {
    const base = b.base || {}, rec = b.record;
    return `<article class="card"><div class="card-top"><span class="caps muted">${b.enabled ? "In use" : "Switched off"}${b.weight_now != null ? ` · ${pct(b.weight_now, 0)} of the rules' weight now` : ""}</span></div>
      <h3>${esc(b.label)}</h3><p>${esc(b.edge.charAt(0).toUpperCase() + b.edge.slice(1))}.</p>
      ${diagram(b.diagram, `${b.label}: ${b.steps.join("; ")}`)}
      <ol class="small steps">${b.steps.map((x) => `<li>${esc(x)}</li>`).join("")}</ol>
      <p class="small"><b>Works best in</b> ${esc(b.works)}. <b>Struggles in</b> ${esc(b.struggles)}.</p>
      <p class="small"><b>Trades when the market is</b> ${Object.entries(b.regimes).filter(([, m]) => m > 0).map(([k, m]) => `${esc((regimeWords[k] || k).toLowerCase())}${m < 1 ? ` (at ${m}× size)` : ""}`).join("; ") || "in crisis mode only, whatever the regime"}.</p>
      ${base.n ? `<p class="small"><b>In the replay</b> (${base.n} trades${base.period ? `, ${esc(base.period[0])} to ${esc(base.period[1])}` : ""}): it won ${pct(base.win_rate, 0)} of the time; an average win made ${num(base.avg_win_R, 2)}${term("R")} and an average loss cost ${num(Math.abs(base.avg_loss_R), 2)}R${base.expectancy_R != null ? `, so each trade was worth ${num(base.expectancy_R, 2)}R on average` : ""}.${base.small_sample ? " A small sample: treat with care." : ""}</p>` :
        `<p class="small muted">${esc(base.note && !/not in data/i.test(base.note) ? base.note : "The replay keeps no per-trade record for this rule.")}</p>`}
      ${rec && rec.marked ? `<p class="small"><b>Our pitches so far</b> (${rec.n}): average ${rec.avg_ret >= 0 ? "+" : ""}${pct(rec.avg_ret)} from the tested entry${rec.avg_excess_spy != null ? `, ${rec.avg_excess_spy >= 0 ? "+" : ""}${pct(rec.avg_excess_spy)} against SPY` : ""}.</p>` : `<p class="small muted">Our own pitches from this book: too few to measure yet.</p>`}
      <div class="deep small">${kv(Object.entries(b.params).map(([k, x]) => [esc(k), esc(String(x))]))}</div></article>`;
  };
  const win = s.books.filter((b) => (b.base || {}).win_rate != null);
  after = (box) => wireJumps(box);
  return hero("Strategy and research", "Why DAISY trades the way it does.", "Five written rules, each built for one kind of market. Every entry comes from a rule on the daily close and is taken at the next open, so every one can be tested before it is trusted. "
      + (rep ? `The latest study (${String(rep.at || "").slice(0, 10)}) says: ${v.overall || "no verdict"}; ${passed} of ${checks.length} pass bars were met.` : "No full study has been run on this server yet."), { short: true })
    + `<div class="grid">${onPage([["rs-books", "The five rules"], ["rs-compare", "How they compare"], ["rs-regime", "The market filter"], ["rs-conv", "Conviction and rank"], ["rs-study", "Does it hold up?"], ["rs-time", "How we got here"]])}</div>`
    + section("The five rules", `${anchor("rs-books")}<div class="cards roomy">${s.books.map(bookCard).join("")}</div>`, { wide: true })
    + section("How they compare", `${anchor("rs-compare")}${win.length ? `<div class="two"><div><h3>How often each wins</h3>${hbars(win.map((b) => ({ label: b.label, value: b.base.win_rate })), { fmt: (x) => pct(x, 0), label: "Win rate by rule book, from the replay" })}</div>
        <div><h3>What a trade is worth</h3>${hbars(win.map((b) => ({ label: b.label, value: b.base.expectancy_R })), { fmt: (x) => num(x, 2) + "R", label: "Average result per trade in R, by rule book" })}</div></div>
        <p style="margin-top:16px">${win.filter((b) => b.base.avg_win_R && b.base.avg_loss_R).map((b) => `${esc(b.label)} won ${pct(b.base.win_rate, 0)} of its trades, and its average win was ${num(Math.abs(b.base.avg_win_R / b.base.avg_loss_R), 1)} times its average loss`).join("; ")}.
          A rule does not need to win most of the time: what counts is how often it wins together with how large the wins are against the losses. We run several because they are built for different markets, so one rule's bad stretch need not be every rule's.</p>` :
        empty("The comparison appears after the first daily pass: it needs the replay's trades.")}
      <p class="muted small">R is one unit of risk: the distance from entry to stop. From the historical replay, which is a research tool, not a portfolio.</p>`, { alt: true })
    + section("The market filter", `${anchor("rs-regime")}<p>Right now the rules read the market as <b>${esc(String((s.regime || {}).now || "—").replace(/_/g, " "))}</b>${(s.regime || {}).crisis ? ", with crisis mode on" : ""}.</p><p>${esc((s.regime || {}).explain || "")}</p>
      ${table("Which rule may trade in which market, and at what size", ["Rule", ...Object.values(regimeWords)], s.books.map((b) => [esc(b.label) + (Object.values(b.regimes).every((m) => m == null) ? ' <span class="muted small">(switched by crisis mode, not by this table)</span>' : ""), ...Object.keys(regimeWords).map((k) => { const m = b.regimes[k]; return m == null ? "—" : m > 0 ? `<span class="up">✓ ${m < 1 ? m + "×" : "full"}</span>` : '<span class="muted">stands down</span>'; })]))}
      <p class="muted small">This is why buy signals are few in a falling market: most rules stand down on purpose.</p>`)
    + section("Conviction and rank", `${anchor("rs-conv")}<p>${term("Conviction")} is a score from 0 to 100 that starts at ${num((cv.weights || {}).start, 0)} and moves up or down for named reasons. It never creates a trade: it only scales the suggested size (${Object.entries(cv.size_scale || {}).map(([k, x]) => `${esc(k)} ${x}×`).join(", ")}).</p>
      ${cv.example ? `<h3 style="margin:18px 0 8px">How ${esc(cv.example.symbol)}'s score of ${cv.example.score} was built</h3>${waterfall(cv.example.breakdown, `Conviction for ${cv.example.symbol}: ${cv.example.breakdown.map((p) => `${p.input} ${p.points}`).join(", ")}`)}` : ""}
      <p style="margin-top:16px">${term("DAISY Rank")} then reads straight off the rule's state and that score:</p>${kv(Object.entries(s.rank || {}).map(([k, x]) => [rankBadge(k), esc(x)]))}
      <div class="deep">${table("Every conviction input and its points", ["Input", "Points"], Object.entries(cv.weights || {}).filter(([, x]) => typeof x === "number").map(([k, x]) => [esc(k.replace(/_/g, " ")), (x > 0 ? "+" : "") + x]), { numeric: [1] })}</div>`, { alt: true })
    + section("Does it hold up?", `${anchor("rs-study")}<p>${esc(rep ? `The weekly study replays the rules over ten years, removes one piece at a time to see what each adds, and tests them only on data that came after the data they were tuned on.` : "The weekly study has not run on this server yet. It replays the rules over ten years, removes one piece at a time, and tests them walk-forward.")}</p>
      ${(rs.ablations || []).length ? `<h3 style="margin:16px 0 8px">What each piece adds (risk-adjusted return)</h3>${hbars(rs.ablations.map((a) => ({ label: a.variant.replace(/_/g, " "), value: a.sharpe })), { fmt: (x) => num(x, 2), label: "Sharpe ratio of the system with each variation" })}
        <p class="muted small">"baseline" is the full system. A bar shorter than baseline means that variation did worse, so the piece it removed was helping.</p>` : ""}
      ${(rs.findings || []).length ? `<ul class="feed">${rs.findings.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : ""}
      ${checks.length ? table("The bars the rules are held to", ["Bar", "Result"], checks.map(([k, c]) => [esc(k), c.pass == null ? '<span class="muted">not in data</span>' : c.pass ? '<span class="up">✓ met</span>' : '<span class="down">✗ not met</span>'])) : ""}
      <h3 style="margin:22px 0 8px">Known limits</h3><ul class="feed"><li><b>Survivorship.</b> Outside the S&amp;P 500 core, the market list is today's listed names: delisted companies are missing, which flatters a backtest on them.</li>
        <li><b>News sentiment is untested as a gate.</b> It informs conviction and raises flags; the Scorecard tracks pitches by sentiment so we can see whether it adds edge.</li>
        <li><b>History depth.</b> Names outside the core have about two years of bars (the free data plan).</li></ul>`)
    + section("How we got here", `${anchor("rs-time")}<ol class="timeline tall">${(s.timeline || []).map((x) => `<li><span class="caps muted">${esc(x.when)}</span><span class="v">${esc(x.title)}</span><span class="muted small">${esc(x.why)}</span></li>`).join("")}</ol>`, { alt: true })
    + `<div class="deep">` + section("Deep dive · the full study", d.markdown ? `<div class="memo">${md(d.markdown)}</div>` : empty("No report yet."), { wide: true }) + `</div>`;
}
