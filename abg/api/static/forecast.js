/* ABG Intelligence Terminal — Prediction card (Monte Carlo fan chart, thesis, confidence, recommendation). */
"use strict";
const FC = { chart: null, report: null };

window.renderForecast = function (r) {
  FC.report = r;
  drawForecast(r.forecast, r);
};

function drawForecast(fc, r) {
  const el = $("#card-forecast");
  if (FC.chart) { FC.chart.remove(); FC.chart = null; }
  if (!fc || !fc.available) {
    el.innerHTML = `<h2>Prediction</h2><p class="muted">${esc(fc?.reason || "Not available for this view.")}</p>`;
    return;
  }
  const rec = fc.recommendation || {}, th = fc.thesis || {}, conf = fc.confidence || {};
  const prim = fc.horizons.find((h) => h.days === fc.primary_horizon) || fc.horizons[fc.horizons.length - 1];
  const recCls = "rec-" + String(rec.action || "Hold").replace(" ", "-");
  const lvl = conf.rating === "High" ? "Low" : conf.rating === "Medium" ? "Moderate" : "High";   // reuse meter colours (inverse)
  const list = (xs, cls) => (xs || []).length ? `<ul class="fc-list ${cls}">${xs.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "";
  const hzBtn = (d, l) => `<button data-hz="${d}" class="${fc.primary_horizon === d ? "on" : ""}">${l}</button>`;
  el.innerHTML = `
    <div class="card-head"><h2>Prediction · ${fc.paths.toLocaleString()} simulated paths</h2>
      <span class="hz-btns" role="group" aria-label="Recommendation horizon">${hzBtn(21, "1M")}${hzBtn(63, "3M")}${hzBtn(126, "6M")}${hzBtn(252, "1Y")}</span></div>
    <div class="fc-top">
      <div>
        <div class="rec-badge ${recCls}">${esc(rec.action || "—")}</div>
        <div class="sec" style="margin-top:6px;font-size:13px">model view · ${esc(rec.horizon_label || "")}</div>
        <div class="lvl lvl-${lvl}" style="margin-top:10px"><span class="badge"><span class="dot"></span>${esc(conf.rating)} confidence</span>
          <span class="num sec" style="margin-left:6px">${fmt(conf.score, 0)}/100</span>
          <div class="meter"><i style="width:${conf.score || 0}%"></i></div></div>
        <dl class="kv">
          <dt>P(finish higher)</dt><dd>${pct(prim.prob_up, 0)}</dd>
          <dt>Median / mean</dt><dd>${sgn(prim.return_pct.p50, 1, "%")} / ${sgn(prim.expected_return_pct, 1, "%")}</dd>
          <dt>80% range</dt><dd>${fmt(prim.price.p10)} – ${fmt(prim.price.p90)}</dd>
          <dt>1-in-20 loss</dt><dd>${fmt(prim.var_95_pct, 1)}%</dd>
          <dt>Vol-target size</dt><dd title="Allocation that contributes ~10% annual volatility">${fmt(rec.vol_target_allocation_pct, 0)}%</dd>
        </dl>
        ${(rec.notes || []).map((n) => `<p class="note">${esc(n)}</p>`).join("")}
      </div>
      <div>
        <p class="fc-headline">${esc(th.headline)}</p>
        <div class="sec" style="font-size:13px">${esc(th.context)}</div>
        <div class="cases" style="margin-top:6px"><div><h3 class="pos">Supporting</h3>${list(th.bull_points, "bull")}</div>
          <div><h3 class="neg">Against</h3>${list(th.bear_points, "bear")}</div></div>
        ${th.setup ? `<p style="font-size:13px;margin:8px 0 0">${esc(th.setup)}</p>` : ""}
        ${list(th.invalidation, "inv")}
      </div>
    </div>
    <div class="subhead" style="margin-top:12px">Price paths: history + simulated percentiles (next ${fc.fan.time.length} trading days)</div>
    <div class="legend" id="fc-legend"></div>
    <div id="chart-fan" class="chart fan"></div>
    <div class="scen">${fc.scenarios.map((s) => `<div><b>${esc(s.name)} · ${pct(s.probability, 0)}</b>
      <span class="v ${s.return_pct >= 0 ? "pos" : "neg"}">${s.return_pct >= 0 ? "+" : ""}${fmt(s.return_pct, 1)}%</span>
      <span class="muted"> → ${fmt(s.price)}</span><div class="muted" style="font-size:12px">range ${fmt(s.range_pct[0], 1)}% to ${fmt(s.range_pct[1], 1)}%</div></div>`).join("")}</div>
    <div class="scroll" style="margin-top:10px"><table><thead><tr><th>Horizon</th><th class="n">P5</th><th class="n">P25</th><th class="n">Median</th><th class="n">P75</th>
      <th class="n">P95</th><th class="n">Mean</th><th class="n">P(up)</th><th class="n">P(&gt;+10%)</th><th class="n">P(&lt;−10%)</th><th class="n">VaR95</th></tr></thead>
      <tbody>${fc.horizons.map((h) => `<tr class="${h.days === fc.primary_horizon ? "atm" : ""}"><td>${esc(h.label)}</td>
        <td class="n">${sgn(h.return_pct.p5, 1, "%")}</td><td class="n">${sgn(h.return_pct.p25, 1, "%")}</td><td class="n">${sgn(h.return_pct.p50, 1, "%")}</td>
        <td class="n">${sgn(h.return_pct.p75, 1, "%")}</td><td class="n">${sgn(h.return_pct.p95, 1, "%")}</td><td class="n">${sgn(h.expected_return_pct, 1, "%")}</td>
        <td class="n">${pct(h.prob_up, 0)}</td><td class="n">${pct(h.prob_up_10, 0)}</td><td class="n">${pct(h.prob_down_10, 0)}</td><td class="n">${fmt(h.var_95_pct, 1)}%</td></tr>`).join("")}</tbody></table></div>
    ${(fc.barriers || []).map((b) => `<p style="font-size:13px;margin:8px 0 0"><b>${esc(b.setup)}</b>: target ${fmt(b.target_1)} first
      <b class="pos">${pct(b.prob_target_first, 0)}</b> · stop ${fmt(b.stop)} first <b class="neg">${pct(b.prob_stop_first, 0)}</b> ·
      neither ${pct(b.prob_neither, 0)} · expected ${b.expected_r_multiple >= 0 ? "+" : ""}${fmt(b.expected_r_multiple, 2)}R</p>`).join("")}
    <details style="margin-top:10px"><summary class="sec" style="cursor:pointer;font-size:13px">How this was calculated</summary>
      <dl class="kv" style="margin-top:8px">
        <dt>Drift (annual)</dt><dd>${fmt(fc.drift.total_annual * 100, 1)}% = rf ${fmt(fc.drift.risk_free * 100, 1)} + β×ERP ${fmt(fc.drift.equity_premium * 100, 1)}
          + signal ${fmt(fc.drift.signal_tilt * 100, 1)} + news ${fmt(fc.drift.sentiment_tilt * 100, 1)}</dd>
        <dt>Volatility</dt><dd>${fmt(fc.volatility.now_annual_pct, 0)}% now → ${fmt(fc.volatility.long_run_annual_pct, 0)}% long-run (half-life ${fc.volatility.half_life_days}d)</dd>
        <dt>Calibration</dt><dd>${fc.calibration.available ? `${esc(fc.calibration.verdict)} · past 90% bands covered ${pct(fc.calibration.coverage_90, 0)} (${fc.calibration.origins} tests)` : "n/a"}</dd>
        ${Object.entries(conf.components || {}).map(([k, c]) => `<dt>${esc(k.replace(/_/g, " "))}</dt><dd>${fmt(c.score, 2)} × ${c.weight} · ${esc(c.detail)}</dd>`).join("")}
      </dl>
      <p class="note">${esc(th.method)} ${esc(th.disclaimer)}</p></details>`;
  el.querySelectorAll("[data-hz]").forEach((b) => b.addEventListener("click", () => changeHorizon(+b.dataset.hz)));
  drawFan(fc, r);
}

async function changeHorizon(days) {
  const r = FC.report;
  if (!r) return;
  busy(true);
  try {
    const src = $("#source").value;
    const d = await api(`/api/predict/${encodeURIComponent(r.symbol)}?horizon=${days}${src ? "&source=" + encodeURIComponent(src) : ""}`);
    FC.report = { ...r, forecast: d.forecast };
    drawForecast(d.forecast, FC.report);
  } catch (e) { toast("Prediction: " + e.message); } finally { busy(false); }
}

function drawFan(fc, r) {
  if (!window.LightweightCharts) return;
  const LC = window.LightweightCharts;
  const chart = LC.createChart($("#chart-fan"), chartOpts(300));
  FC.chart = chart;
  const s = r.series;
  const line = (color, width, style, title) => chart.addLineSeries({ color, lineWidth: width, lineStyle: style, priceLineVisible: false,
    lastValueVisible: false, crosshairMarkerVisible: false, title: "" });
  const hist = line(css("--text-secondary"), 2, 0);
  if (s) {
    const n = Math.min(s.time.length, 160);
    hist.setData(s.time.slice(-n).map((t, i) => ({ time: t, value: s.close[s.time.length - n + i] })));
  }
  const lastT = s ? s.time[s.time.length - 1] : null;
  const anchor = lastT ? [{ time: lastT, value: fc.as_of_price }] : [];
  const band = (key, color, width, style) => {
    const ser = line(color, width, style);
    ser.setData(anchor.concat(fc.fan.time.map((t, i) => ({ time: t, value: fc.fan[key][i] }))).filter((p, i, a) => i === 0 || p.time > a[i - 1].time));
    return ser;
  };
  const c1 = css("--series-1"), cN = css("--neutral");
  band("p95", cN, 1, 2); band("p5", cN, 1, 2);
  band("p75", c1, 1, 1); band("p25", c1, 1, 1);
  band("p50", c1, 3, 0);
  chart.timeScale().fitContent();
  $("#fc-legend").innerHTML = `<span><i class="sw" style="background:${css("--text-secondary")}"></i>History</span>
    <span><i class="sw" style="background:${c1};height:3px"></i>Median path</span>
    <span><i class="sw" style="background:${c1}"></i>25–75% (dotted)</span>
    <span><i class="sw" style="background:${cN}"></i>5–95% (dashed)</span>`;
}
