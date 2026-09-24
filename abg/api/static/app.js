/* ABG Intelligence Terminal — dashboard logic (vanilla JS, no build step). */
"use strict";
const $ = (s) => document.querySelector(s);
const LOCALE = (() => { try { new Intl.NumberFormat(navigator.language); return navigator.language; } catch { return "en-US"; } })();
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const fmt = (x, d = 2, sfx = "") => (x === null || x === undefined || Number.isNaN(+x)) ? "—" :
  (+x).toLocaleString(LOCALE, {minimumFractionDigits: d, maximumFractionDigits: d}) + sfx;
const sgn = (x, d = 2, sfx = "") => (x === null || x === undefined) ? '<span class="muted">—</span>' :
  `<span class="${x > 0 ? "pos" : x < 0 ? "neg" : ""}">${x > 0 ? "+" : ""}${fmt(x, d)}${sfx}</span>`;
const big = (x) => { if (x == null) return "—"; const a = Math.abs(x);
  for (const [d, s] of [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]]) if (a >= d) return fmt(x / d, 2) + s; return fmt(x, 0); };
const safeUrl = (u) => (/^https?:\/\//i.test(String(u || "")) ? u : null);   // blocks javascript:/data: links from feeds
const pct = (x, d = 1) => x == null ? "—" : fmt(x * 100, d) + "%";

const state = { report: null, charts: [], overlays: { sma_20: true, sma_50: true, sma_200: true, bb: true, vwap_20: false } };

// ------------------------------------------------------------------ plumbing
async function api(path, opts) {
  const res = await fetch(path, opts);
  let body;
  try { body = await res.json(); } catch { body = null; }
  if (!res.ok) {
    const e = body?.error || {};
    let msg = e.message || `HTTP ${res.status}`;
    if (e.attempts?.length) msg += "\n" + e.attempts.map((a) => `• ${a.provider}: ${a.error}`).join("\n");
    throw new Error(msg);
  }
  return body;
}
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.hidden = false; clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 9000); }
function busy(on) { $("#loading").hidden = !on; }

// ------------------------------------------------------------------ init
async function init() {
  const saved = (() => { try { return localStorage.getItem("abg-theme"); } catch { return null; } })();
  if (saved) document.documentElement.dataset.theme = saved;
  const periods = ["1mo", "3mo", "6mo", "ytd", "1y", "2y", "5y", "10y", "max"];
  for (const sel of ["#period", "#cmp-period"]) $(sel).innerHTML = periods.map((p) => `<option ${p === "1y" ? "selected" : ""}>${p}</option>`).join("");
  $("#interval").innerHTML = ["1d", "1wk", "1mo", "1h", "30m", "15m", "5m"].map((i) => `<option>${i}</option>`).join("");
  try {
    const m = await api("/api/meta");
    $("#ver").textContent = "v" + m.version;
    $("#demo-banner").hidden = !m.demo;
    $("#source").innerHTML += m.providers.map((p) => `<option value="${esc(p)}">${esc(p)}</option>`).join("");
    if (!m.ai) $("#opt-ai").parentElement.title = "Set ANTHROPIC_API_KEY for Claude insight; rule-based insight is used otherwise.";
  } catch (e) { toast("Server unreachable: " + e.message); }
  $("#search").addEventListener("submit", (e) => { e.preventDefault(); analyze(); });
  $("#compare-form").addEventListener("submit", (e) => { e.preventDefault(); compare(); });
  $("#csv").addEventListener("change", uploadCSV);
  $("#theme").addEventListener("click", toggleTheme);
  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
  const q = new URLSearchParams(location.search).get("s");
  if (q) { $("#sym").value = q; analyze(); }
}

function showView(v) {
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  for (const id of ["analyze", "portfolio", "compare", "status"]) $("#view-" + id).hidden = id !== v;
  if (v === "status") loadStatus();
  if (v === "portfolio" && window.loadPortfolio) {
    window.loadPortfolio();
    if (typeof PF !== "undefined") { PF.unread = 0; updateBadge(); }
  }
}

function toggleTheme() {
  const cur = document.documentElement.dataset.theme ||
    (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("abg-theme", next); } catch {}
  if (state.report) { renderCharts(state.report); if (window.renderForecast && FC.report) drawForecast(FC.report.forecast, FC.report); }
}

// ------------------------------------------------------------------ analyze
async function analyze(extra = {}) {
  const sym = $("#sym").value.trim().toUpperCase();
  if (!sym) return;
  showView("analyze");
  const p = new URLSearchParams({ period: $("#period").value, interval: $("#interval").value, ai: $("#opt-ai").checked,
    options: $("#opt-options").checked, news: $("#opt-news").checked, ...extra });
  if ($("#source").value) p.set("source", $("#source").value);
  busy(true);
  try {
    const r = await api(`/api/analyze/${encodeURIComponent(sym)}?${p}`);
    history.replaceState(null, "", `?s=${encodeURIComponent(sym)}`);
    render(r);
  } catch (e) { toast(`Could not analyze ${sym}:\n${e.message}`); }
  finally { busy(false); }
}

async function uploadCSV(ev) {
  const f = ev.target.files[0];
  if (!f) return;
  const symbol = (f.name.split(/[_\s.-]/)[0] || "CSV").toUpperCase().slice(0, 15).replace(/[^A-Z0-9]/g, "") || "CSV";
  busy(true);
  try {
    const text = await f.text();
    render(await api("/api/analyze-csv", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ symbol, csv: text, period: "max" }) }));
    showView("analyze");
  } catch (e) { toast("CSV analysis failed:\n" + e.message); }
  finally { busy(false); ev.target.value = ""; }
}

function render(r) {
  state.report = r;
  $("#empty").hidden = true;
  $("#report").hidden = false;
  $("#warnings").innerHTML = (r.warnings || []).map((w) => `<div class="warn">${esc(w)}</div>`).join("");
  renderQuote(r); renderSignal(r); renderRegime(r); renderRisk(r);
  renderCharts(r); renderPlays(r); renderIndicators(r); renderLevels(r);
  if (window.renderForecast) window.renderForecast(r);
  renderInsight(r); renderRiskDetail(r); renderOptions(r.options, r.symbol); renderNews(r); renderStats(r); renderMeta(r);
}

function renderQuote(r) {
  const q = r.quote || {}, f = r.fundamentals || {};
  $("#card-quote").innerHTML = `
    <div><span class="sym">${esc(r.symbol)}</span><span class="name">${esc(r.name || "")}</span></div>
    <div class="big">${fmt(q.price)} <small class="muted" style="font-size:13px">${esc(q.currency || "")}</small></div>
    <div class="num">${sgn(q.change)} &nbsp; ${sgn(q.change_pct, 2, "%")}</div>
    <dl class="kv" style="margin-top:8px">
      <dt>Mkt cap</dt><dd>${big(f.market_cap || q.market_cap)}</dd>
      <dt>P/E</dt><dd>${fmt(f.pe)}</dd>
      <dt>52w range</dt><dd>${fmt(f.week52_low)} – ${fmt(f.week52_high)}</dd>
      <dt>Sector</dt><dd style="font-family:var(--sans)">${esc(f.sector || f.industry || "—")}</dd>
    </dl>`;
}

function renderSignal(r) {
  const s = r.signal || {};
  const pos = ((s.score ?? 0) + 100) / 2;
  const comps = Object.entries(s.components || {}).sort((a, b) => Math.abs(b[1].vote * b[1].weight) - Math.abs(a[1].vote * a[1].weight)).slice(0, 3);
  $("#card-signal").innerHTML = `<h2>Composite signal</h2>
    <div class="label-big">${esc(s.label)} <span class="num sec" style="font-size:15px">${fmt(s.score, 0)}/100</span></div>
    <div class="gauge" role="img" aria-label="Signal score ${fmt(s.score, 0)} of -100 to 100"><div class="pin" style="left:${pos}%"></div></div>
    <div class="scale"><span>-100 bearish</span><span>0</span><span>bullish +100</span></div>
    <ul class="conds">${comps.map(([k, c]) => `<li class="${c.vote > 0 ? "met" : ""}" title="weight ${c.weight}">${esc(c.reason)}</li>`).join("")}</ul>`;
}

function renderRegime(r) {
  const g = r.regime || {}, st = r.statistics || {};
  $("#card-regime").innerHTML = `<h2>Regime</h2>
    <div class="label-big" style="text-transform:capitalize">${esc(g.trend)}</div>
    <dl class="kv" style="margin-top:8px">
      <dt>Trend strength</dt><dd>${esc(g.trend_strength || "—")} (ADX ${fmt(g.adx, 1)})</dd>
      <dt>Volatility</dt><dd>${esc(g.volatility_regime || "—")}</dd>
      <dt>Vol 20d</dt><dd>${fmt(st.vol_20d_pct, 1, "%")}</dd>
      <dt>Return (${esc(r.period)})</dt><dd>${sgn(st.total_return_pct, 1, "%")}</dd>
    </dl>`;
}

const riskOf = (r) => (r.risk || []).find((x) => !x.error) || {};
function renderRisk(r) {
  const k = riskOf(r), m = k.metrics || {};
  $("#card-risk").innerHTML = `<h2>Risk · ${esc(k.model || "baseline")}</h2>
    <div class="lvl lvl-${esc(k.level)}"><span class="badge"><span class="dot"></span>${esc(k.level || "—")}</span>
      <span class="num sec" style="margin-left:6px">${fmt(k.score, 0)}/100</span>
      <div class="meter"><i style="width:${k.score || 0}%"></i></div></div>
    <dl class="kv">
      <dt>VaR 95% (1d)</dt><dd>${fmt(m.var_95_1d_pct, 2, "%")}</dd>
      <dt>CVaR 95% (1d)</dt><dd>${fmt(m.cvar_95_1d_pct, 2, "%")}</dd>
      <dt>Top driver</dt><dd style="font-family:var(--sans)">${esc(k.drivers?.[0]?.factor || "—")}</dd>
    </dl>`;
}

// ------------------------------------------------------------------ charts
function safeLocale() {
  // some Linux browsers report tags like "en-US@posix" that Intl rejects; fall back safely
  for (const l of [navigator.language, "en-US"]) { try { new Intl.NumberFormat(l); return l; } catch {} }
  return "en-US";
}
function chartOpts(height) {
  return {
    localization: { locale: safeLocale() },
    height, autoSize: true,
    layout: { background: { type: "solid", color: css("--surface-1") }, textColor: css("--text-secondary"), fontSize: 11 },
    grid: { vertLines: { color: css("--grid") }, horzLines: { color: css("--grid") } },
    rightPriceScale: { borderVisible: false }, timeScale: { borderVisible: false, timeVisible: state.report?.interval && !["1d", "1wk", "1mo"].includes(state.report.interval) },
    crosshair: { mode: 1 }, handleScale: true, handleScroll: true,
  };
}
const pts = (s, key) => s.time.map((t, i) => ({ time: t, value: s[key]?.[i] })).filter((p) => p.value !== null && p.value !== undefined);

function renderCharts(r) {
  state.charts.forEach((c) => c.remove());
  state.charts = [];
  const s = r.series;
  if (!s || !window.LightweightCharts) { $("#chart-main").innerHTML = '<p class="muted">Chart unavailable.</p>'; return; }
  const LC = window.LightweightCharts;
  const up = css("--up"), down = css("--down");
  const main = LC.createChart($("#chart-main"), chartOpts(420));
  const candles = main.addCandlestickSeries({ upColor: up, downColor: down, wickUpColor: up, wickDownColor: down, borderVisible: false });
  candles.setData(s.time.map((t, i) => ({ time: t, open: s.open[i], high: s.high[i], low: s.low[i], close: s.close[i] })));
  const vol = main.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
  main.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  vol.setData(s.time.map((t, i) => ({ time: t, value: s.volume[i], color: (s.close[i] >= s.open[i] ? up : down) + "55" })));

  const defs = [
    ["sma_20", "SMA 20", "--series-1", 0], ["sma_50", "SMA 50", "--series-2", 0], ["sma_200", "SMA 200", "--series-7", 0],
    ["vwap_20", "VWAP 20", "--series-5", 0], ["bb_upper", "BB upper", "--neutral", 2], ["bb_lower", "BB lower", "--neutral", 2],
  ];
  const lines = {};
  for (const [key, label, color, dash] of defs) {
    const group = key.startsWith("bb_") ? "bb" : key;
    const ser = main.addLineSeries({ color: css(color), lineWidth: key.startsWith("bb_") ? 1 : 2, lineStyle: dash,
      priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, visible: state.overlays[group] });
    ser.setData(pts(s, key));
    lines[key] = { ser, label, color: css(color), group };
  }
  // overlay toggles + legend (identity never by colour alone: legend names each line)
  $("#overlay-toggles").innerHTML = [["sma_20", "SMA20"], ["sma_50", "SMA50"], ["sma_200", "SMA200"], ["bb", "Bollinger"], ["vwap_20", "VWAP"]]
    .map(([k, l]) => `<label><input type="checkbox" data-ov="${k}" ${state.overlays[k] ? "checked" : ""}>${l}</label>`).join("");
  $("#overlay-toggles").querySelectorAll("input").forEach((cb) => cb.addEventListener("change", () => {
    state.overlays[cb.dataset.ov] = cb.checked;
    Object.values(lines).forEach((l) => l.group === cb.dataset.ov && l.ser.applyOptions({ visible: cb.checked }));
  }));
  const last = s.time.length - 1;
  const legend = (i) => {
    const o = `<span>O ${fmt(s.open[i])} H ${fmt(s.high[i])} L ${fmt(s.low[i])} C <b>${fmt(s.close[i])}</b> V ${big(s.volume[i])}</span>`;
    return o + Object.entries(lines).filter(([k, l]) => state.overlays[l.group] && !k.endsWith("lower"))
      .map(([k, l]) => `<span><i class="sw" style="background:${l.color}"></i>${l.label.replace(" upper", "")} ${fmt(s[k][i])}</span>`).join("");
  };
  $("#legend").innerHTML = legend(last);
  main.subscribeCrosshairMove((p) => {
    const i = p?.time ? s.time.indexOf(p.time) : -1;
    $("#legend").innerHTML = legend(i >= 0 ? i : last);
  });

  const rsiC = LC.createChart($("#chart-rsi"), chartOpts(130));
  const rsi = rsiC.addLineSeries({ color: css("--series-1"), lineWidth: 2, priceLineVisible: false });
  rsi.setData(pts(s, "rsi_14"));
  for (const lv of [70, 30]) rsi.createPriceLine({ price: lv, color: css("--neutral"), lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "" });

  const macdC = LC.createChart($("#chart-macd"), chartOpts(130));
  const hist = macdC.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false });
  hist.setData(s.time.map((t, i) => ({ time: t, value: s.macd_hist[i], color: (s.macd_hist[i] ?? 0) >= 0 ? up : down }))
    .filter((p) => p.value !== null));
  macdC.addLineSeries({ color: css("--series-1"), lineWidth: 2, priceLineVisible: false, lastValueVisible: false }).setData(pts(s, "macd"));
  macdC.addLineSeries({ color: css("--series-2"), lineWidth: 2, priceLineVisible: false, lastValueVisible: false }).setData(pts(s, "macd_signal"));

  state.charts = [main, rsiC, macdC];
  // keep the three panes scrolled/zoomed together
  let syncing = false;
  state.charts.forEach((c) => c.timeScale().subscribeVisibleLogicalRangeChange((rng) => {
    if (syncing || !rng) return;
    syncing = true;
    state.charts.forEach((o) => o !== c && o.timeScale().setVisibleLogicalRange(rng));
    syncing = false;
  }));
  main.timeScale().fitContent();
}

// ------------------------------------------------------------------ panels
function renderPlays(r) {
  $("#card-plays").innerHTML = `<h2>Trade setups <span class="muted" style="text-transform:none;letter-spacing:0">(rule-based)</span></h2>` +
    (r.plays || []).map((p) => {
      const L = p.levels || {};
      const lv = L.entry ? `<div class="levels-grid num">
        <div><span>Entry</span>${fmt(L.entry)}</div><div><span>Stop</span>${fmt(L.stop)}</div>
        <div><span>Target 1</span>${fmt(L.target_1)}</div><div><span>Target 2</span>${fmt(L.target_2)}</div></div>` : "";
      return `<div class="play"><div class="play-head"><b>${esc(p.name)}</b>
        <span class="dir-${esc(p.direction)}">${esc(p.direction)} · ${Math.round(p.confidence * 100)}%</span></div>
        <div class="sec" style="font-size:13px">${esc(p.thesis)}</div>
        <ul class="conds">${(p.conditions || []).map((c) => `<li class="${c.met ? "met" : ""}">${esc(c.condition)}</li>`).join("")}</ul>${lv}</div>`;
    }).join("");
}

function renderIndicators(r) {
  const i = r.indicators || {};
  const rows = [["RSI (14)", i.rsi_14, 1], ["MACD", i.macd, 3], ["MACD signal", i.macd_signal, 3], ["MACD hist", i.macd_hist, 3],
    ["ADX (14)", i.adx, 1], ["+DI / −DI", `${fmt(i.plus_di, 1)} / ${fmt(i.minus_di, 1)}`], ["Bollinger %B", i.bb_pctb, 2],
    ["ATR (14)", i.atr_14, 2], ["ATR % price", i.atr_pct, 2], ["Williams %R", i.williams_r, 1], ["CCI (20)", i.cci_20, 0],
    ["MFI (14)", i.mfi_14, 1], ["Stoch %K / %D", `${fmt(i.stoch_k, 1)} / ${fmt(i.stoch_d, 1)}`], ["VWAP (20)", i.vwap_20, 2],
    ["OBV", big(i.obv)], ["Relative volume", i.rel_volume, 2], ["ROC 20", i.roc_20, 2]];
  $("#card-indicators").innerHTML = `<h2>Indicators</h2><table>${rows.map(([l, v, d]) =>
    `<tr><td class="sec">${l}</td><td class="n">${typeof v === "string" ? esc(v) : fmt(v, d)}</td></tr>`).join("")}</table>`;
}

function renderLevels(r) {
  const L = r.levels || {}, pv = L.pivots || {}, px = r.quote?.price;
  const row = (lab, v, cls) => `<tr><td class="${cls}">${lab}</td><td class="n">${fmt(v)}</td><td class="n muted">${px && v ? sgn((v / px - 1) * 100, 1, "%") : ""}</td></tr>`;
  $("#card-levels").innerHTML = `<h2>Key levels</h2><table>
    ${[...(L.resistance || [])].reverse().map((v) => row("Resistance", v, "neg")).join("")}
    ${row("R1 pivot", pv.r1, "sec")}${row("Pivot", pv.pivot, "")}${row("S1 pivot", pv.s1, "sec")}
    ${(L.support || []).map((v) => row("Support", v, "pos")).join("")}</table>
    <p class="note">Swing-point clusters (120 bars) and classic floor pivots.</p>`;
}

function renderInsight(r) {
  const a = r.ai_insight || {};
  const list = (xs) => (xs || []).map((x) => `<li>${esc(x)}</li>`).join("");
  $("#card-insight").innerHTML = `<h2>Insight · ${esc(a.engine || "")}${a.cached ? " (cached)" : ""}</h2>
    <p class="insight-summary">${esc(a.summary)}</p>
    <div><span class="badge">stance: ${esc(a.stance || "—")}</span> <span class="badge">confidence: ${esc(a.confidence || "—")}</span></div>
    <div class="cases" style="margin-top:10px"><div><h3 class="pos">Bull case</h3><ul>${list(a.bull_case)}</ul></div>
      <div><h3 class="neg">Bear case</h3><ul>${list(a.bear_case)}</ul></div></div>
    ${(a.risks_to_watch || []).length ? `<h3 style="font-size:12px;color:var(--text-secondary);margin:6px 0 0">RISKS TO WATCH</h3><ul style="font-size:13px">${list(a.risks_to_watch)}</ul>` : ""}
    ${a.note ? `<p class="note">${esc(a.note)}</p>` : ""}`;
}

function renderRiskDetail(r) {
  const models = r.risk || [];
  const k = riskOf(r), m = k.metrics || {};
  const drivers = (k.drivers || []).map((d) => {
    const lvl = d.sub_score >= 80 ? "High" : d.sub_score >= 65 ? "Elevated" : d.sub_score >= 45 ? "Moderate" : "Low";
    return `<span class="sec">${esc(d.factor.replace(/_/g, " "))}</span>
      <span class="track lvl-${lvl}" title="${esc(d.detail)} · weight ${d.weight}"><i class="fill" style="width:${d.sub_score}%"></i></span>
      <span class="num">${fmt(d.sub_score, 0)}</span>`;
  }).join("");
  const mrow = (l, v, s = "%") => `<dt>${l}</dt><dd>${fmt(v, 2, s)}</dd>`;
  $("#card-riskdetail").innerHTML = `<h2>Risk breakdown</h2>
    <div class="bars">${drivers}</div>
    <dl class="kv" style="margin-top:12px">
      ${mrow("Hist. VaR 99% (1d)", m.var_99_1d_pct)}${mrow("CVaR 99% (1d)", m.cvar_99_1d_pct)}
      ${mrow("Parametric VaR 95%", m.param_var_95_1d_pct)}${mrow("Cornish-Fisher VaR 95%", m.cornish_fisher_var_95_1d_pct)}
      ${mrow("VaR 95% (10d)", m.var_95_10d_pct)}<dt>Beta (252d)</dt><dd>${fmt(m.beta_252d, 2)}</dd>
    </dl>
    ${models.length > 1 ? `<p class="note">Other models: ${models.slice(1).map((x) => esc(x.model + (x.error ? " (error)" : ` ${fmt(x.score, 0)} ${x.level}`))).join(", ")}</p>` : ""}
    <p class="note">Feature schema v${esc(r.features?.schema_version)} · coverage ${pct(r.features?.coverage, 0)}</p>`;
}

function renderOptions(o, sym) {
  const el = $("#card-options");
  if (!o || !o.available) { el.innerHTML = `<h2>Options</h2><p class="muted">Options disabled or unavailable.</p>`; return; }
  const s = o.summary || {};
  const byK = {};
  for (const c of o.contracts || []) (byK[c.strike] ||= {})[c.kind] = c;
  const strikes = Object.keys(byK).map(Number).sort((a, b) => a - b);
  const atm = s.atm_strike;
  const cell = (c, k, d) => `<td class="n">${c ? fmt(c[k], d) : "—"}</td>`;
  const px = (c) => c ? fmt(c.mid ?? c.theo) : "—";
  el.innerHTML = `<div class="card-head"><h2>Options · ${esc(o.source)}${o.model_generated ? ' <span class="badge" style="color:var(--text-primary)">MODEL chain — no market quotes</span>' : ""}</h2>
      <label class="sec" style="font-size:13px">Expiry <select id="expiry">${(o.expirations || []).map((e) =>
        `<option value="${esc(String(e).slice(0, 10))}" ${String(e) === String(o.selected_expiry) ? "selected" : ""}>${esc(String(e).slice(0, 10))}</option>`).join("")}</select></label></div>
    <dl class="kv opt-kv">
      <dt>DTE</dt><dd>${o.days_to_expiry}</dd><dt>ATM IV</dt><dd>${pct(s.atm_iv)}</dd>
      <dt>Exp. move</dt><dd>±${fmt(s.expected_move)} (${fmt(s.expected_move_pct, 1)}%)</dd>
      <dt>25Δ skew</dt><dd>${pct(s.skew_25d)}</dd><dt>P/C OI</dt><dd>${fmt(s.put_call_oi_ratio)}</dd><dt>Max pain</dt><dd>${fmt(s.max_pain)}</dd>
    </dl>
    <div class="scroll"><table><thead><tr>
      <th class="n">Δ</th><th class="n">Γ</th><th class="n">Θ/day</th><th class="n">Vega</th><th class="n">IV</th><th class="n">OI</th><th class="n">Call</th>
      <th class="strike">Strike</th>
      <th class="n">Put</th><th class="n">OI</th><th class="n">IV</th><th class="n">Δ</th><th class="n">Γ</th><th class="n">Θ/day</th><th class="n">Vega</th>
    </tr></thead><tbody>${strikes.map((k) => { const c = byK[k].call, p = byK[k].put;
      return `<tr class="${k === atm ? "atm" : ""}">${cell(c, "delta", 2)}${cell(c, "gamma", 3)}${cell(c, "theta", 3)}${cell(c, "vega", 3)}
        <td class="n">${c ? pct(c.iv_used) : "—"}</td>${cell(c, "open_interest", 0)}<td class="n pos">${px(c)}</td>
        <td class="strike num">${fmt(k)}</td>
        <td class="n neg">${px(p)}</td>${cell(p, "open_interest", 0)}<td class="n">${p ? pct(p.iv_used) : "—"}</td>
        ${cell(p, "delta", 2)}${cell(p, "gamma", 3)}${cell(p, "theta", 3)}${cell(p, "vega", 3)}</tr>`; }).join("")}</tbody></table></div>`;
  $("#expiry").addEventListener("change", async (e) => {
    busy(true);
    try { const d = await api(`/api/options/${encodeURIComponent(sym)}?expiry=${e.target.value}`); renderOptions(d.options, sym); }
    catch (err) { toast(err.message); } finally { busy(false); }
  });
}

function renderNews(r) {
  const s = r.sentiment || {}, items = r.news || [];
  $("#card-news").innerHTML = `<h2>News · ${esc(s.label || "No coverage")} ${s.score != null ? `<span class="num">(${fmt(s.score, 2)})</span>` : ""}</h2>
    <div class="sec" style="font-size:12px;margin-bottom:6px">${s.articles || 0} articles · ${s.last_24h ?? 0} in 24h · model ${esc(s.model || "")}${s.provider_scores_used ? " + vendor scores" : ""}</div>
    ${items.slice(0, 12).map((n) => `<div class="news-item"><span class="chip ${n.sentiment > 0.15 ? "pos" : n.sentiment < -0.15 ? "neg" : ""}">${n.sentiment > 0 ? "+" : ""}${fmt(n.sentiment, 2)}</span>
      <div>${safeUrl(n.url) ? `<a href="${esc(safeUrl(n.url))}" target="_blank" rel="noopener noreferrer">${esc(n.title)}</a>` : esc(n.title)}
      <div class="src">${esc(n.publisher || n.source)} · ${esc(String(n.published_at || "").slice(0, 16).replace("T", " "))}</div></div></div>`).join("")
      || '<p class="muted">No headlines (news disabled or no provider answered).</p>'}`;
}

function renderStats(r) {
  const s = r.statistics || {};
  const rows = [["Total return", sgn(s.total_return_pct, 2, "%")], ["CAGR", fmt(s.cagr_pct, 2, "%")],
    ["Annualised volatility", fmt(s.ann_volatility_pct, 2, "%")], ["Vol 20d / 60d / 252d", `${fmt(s.vol_20d_pct, 1)} / ${fmt(s.vol_60d_pct, 1)} / ${fmt(s.vol_252d_pct, 1)}%`],
    ["Sharpe", fmt(s.sharpe)], ["Sortino", fmt(s.sortino)], ["Calmar", fmt(s.calmar)],
    ["Max drawdown", fmt(s.max_drawdown_pct, 2, "%")], ["Current drawdown", fmt(s.current_drawdown_pct, 2, "%")],
    ["Beta / correlation", `${fmt(s.beta)} / ${fmt(s.correlation)}`], ["vs benchmark", sgn(s.relative_return_pct, 2, "%")],
    ["Best / worst day", `${fmt(s.best_day_pct, 2)}% / ${fmt(s.worst_day_pct, 2)}%`], ["Up days", fmt(s.pct_up_days, 1, "%")],
    ["Skew / excess kurtosis", `${fmt(s.skew)} / ${fmt(s.excess_kurtosis)}`]];
  $("#card-stats").innerHTML = `<h2>Statistics · ${esc(r.period)}</h2><table>${rows.map(([l, v]) =>
    `<tr><td class="sec">${l}</td><td class="n">${v}</td></tr>`).join("")}</table>`;
}

function renderMeta(r) {
  const prov = (r.provenance || []).map((p) => `<code>${esc(p.capability)}</code>←<b>${esc(p.provider)}</b> ${
    p.cache && p.cache !== "miss" ? `(${esc(p.cache)})` : `(${fmt(p.latency_ms, 0)}ms)`}${
    p.attempts?.length ? ` <span title="${esc(p.attempts.map((a) => a.provider + ": " + a.error).join("\n"))}">[${p.attempts.length} failover]</span>` : ""}`).join(" · ");
  const t = Object.entries(r.timings_ms || {}).map(([k, v]) => `${k} ${fmt(v, 0)}ms`).join(" · ");
  const dq = r.data_quality || {};
  $("#card-meta").innerHTML = `<div><b>Sources:</b> ${prov}</div><div><b>Timings:</b> ${t}</div>
    <div><b>Data:</b> ${dq.bars_in_view} bars shown (${dq.bars_total} fetched incl. warm-up) · last bar ${esc(String(dq.last_bar).slice(0, 10))}${dq.csv_format ? " · CSV format " + esc(dq.csv_format) : ""}</div>
    <div style="margin-top:6px">${esc(r.disclaimer)}</div>`;
}

// ------------------------------------------------------------------ compare
async function compare() {
  const syms = $("#cmp-syms").value.toUpperCase().replace(/\s+/g, "");
  busy(true);
  try {
    const r = await api(`/api/compare?symbols=${encodeURIComponent(syms)}&period=${$("#cmp-period").value}`);
    const rows = r.rows.map((x) => x.error ? `<tr><td><b>${esc(x.symbol)}</b></td><td colspan="11" class="neg">${esc(x.error)}</td></tr>` :
      `<tr><td><b>${esc(x.symbol)}</b></td><td class="n">${fmt(x.price)}</td><td class="n">${sgn(x.change_pct, 2, "%")}</td>
       <td class="n">${fmt(x.signal, 0)}</td><td>${esc(x.label)}</td><td>${esc(x.trend)}</td><td class="n">${fmt(x.rsi, 1)}</td>
       <td class="n">${sgn(x.return_pct, 1, "%")}</td><td class="n">${fmt(x.vol_pct, 1)}%</td><td class="n">${fmt(x.sharpe)}</td>
       <td class="n">${fmt(x.max_dd_pct, 1)}%</td><td class="lvl lvl-${esc(x.risk_level)}"><span class="badge"><span class="dot"></span>${fmt(x.risk_score, 0)} ${esc(x.risk_level)}</span></td>
       <td>${esc(x.top_play)}</td></tr>`).join("");
    const p = r.portfolio || {};
    let heat = "";
    if (p.available) {
      const S = p.symbols;
      const mix = (v) => { // diverging blue(+)/red(−) with a neutral grey midpoint
        const a = Math.min(1, Math.abs(v)); const pole = v >= 0 ? "--div-pos" : "--div-neg";
        return `background:color-mix(in srgb, var(${pole}) ${Math.round(a * 85)}%, var(--div-mid));color:${a > 0.55 ? "#fff" : "var(--text-primary)"}`; };
      heat = `<div class="row two"><div class="card"><h2>Correlation (daily returns)</h2><div class="scroll"><table class="heat"><tr><th></th>${S.map((s) => `<th class="n">${esc(s)}</th>`).join("")}</tr>
        ${S.map((a) => `<tr><th>${esc(a)}</th>${S.map((b) => `<td class="n" style="${mix(p.correlation[a][b])}" title="${esc(a)} vs ${esc(b)}: ${fmt(p.correlation[a][b], 3)}">${fmt(p.correlation[a][b], 2)}</td>`).join("")}</tr>`).join("")}
        </table></div></div>
        <div class="card"><h2>Equal-weight portfolio risk</h2><dl class="kv">
          <dt>Annualised vol</dt><dd>${fmt(p.ann_volatility_pct, 2)}%</dd><dt>VaR 95% (1d)</dt><dd>${fmt(p.var_95_1d_pct, 2)}%</dd>
          <dt>CVaR 95% (1d)</dt><dd>${fmt(p.cvar_95_1d_pct, 2)}%</dd><dt>Diversification ratio</dt><dd>${fmt(p.diversification_ratio, 2)}</dd></dl>
          <h2 style="margin-top:12px">Risk contribution</h2><div class="bars">${Object.entries(p.risk_contribution_pct).map(([k, v]) =>
            `<span>${esc(k)}</span><span class="track"><i class="fill" style="width:${Math.max(0, Math.min(100, v))}%"></i></span><span class="num">${fmt(v, 1)}%</span>`).join("")}</div></div></div>`;
    }
    $("#compare-out").innerHTML = `<div class="card scroll"><h2>Comparison · ${esc(r.period)}</h2><table><thead><tr><th>Symbol</th><th class="n">Price</th><th class="n">Chg</th>
      <th class="n">Signal</th><th>Label</th><th>Trend</th><th class="n">RSI</th><th class="n">Return</th><th class="n">Vol</th><th class="n">Sharpe</th>
      <th class="n">Max DD</th><th>Risk</th><th>Top setup</th></tr></thead><tbody>${rows}</tbody></table></div>${heat}`;
  } catch (e) { toast("Compare failed:\n" + e.message); }
  finally { busy(false); }
}

// ------------------------------------------------------------------ provider status
async function loadStatus() {
  try {
    const s = await api("/api/status");
    $("#status-out").innerHTML = `<h2>Data providers</h2><div class="scroll"><table><thead><tr><th>#</th><th>Provider</th><th>Ready</th><th>Capabilities</th>
      <th>Circuit</th><th class="n">Success</th><th class="n">Latency</th><th>Notes</th></tr></thead><tbody>
      ${s.providers.map((p) => `<tr><td>${p.rank != null ? p.rank + 1 : "–"}</td><td><b>${esc(p.label)}</b></td>
        <td>${p.configured ? (p.rank != null ? '<span class="pos">✓ ready</span>' : '<span class="muted">ready · not in ABG_PROVIDER_ORDER</span>') : `<span class="muted">${p.key_env ? "set " + esc(p.key_env) : "not installed / disabled"}</span>`}</td>
        <td>${esc(p.capabilities.join(", "))}</td><td class="${p.breaker.state === "closed" ? "pos" : "neg"}">${esc(p.breaker.state)}</td>
        <td class="n">${p.health.calls ? pct(p.health.success_rate, 0) + ` (${p.health.calls})` : "—"}</td>
        <td class="n">${p.health.ewma_latency_ms ? fmt(p.health.ewma_latency_ms, 0) + "ms" : "—"}</td><td class="sec">${esc(p.notes)}</td></tr>`).join("")}
      </tbody></table></div>
      <p class="note">Cache: ${s.cache.memory_entries} in memory, ${s.cache.disk?.entries ?? 0} on disk · risk models: ${s.risk_models.map((m) => esc(m.name + " " + m.version)).join(", ")} ·
      AI: ${s.ai.configured ? "Claude " + esc(s.ai.model) : "rule-based (set ANTHROPIC_API_KEY)"}</p>`;
  } catch (e) { toast(e.message); }
}

init();
