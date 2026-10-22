/* ABG Intelligence Terminal — R&D tab: Strategy Lab ledger, research reports, daily/weekly/monthly findings (docs/17). */
"use strict";

const RD = { period: "daily", data: null };
const rdKpi = (label, value, sub) => `<div class="card kpi"><h2>${label}</h2><div class="v">${value}</div><div class="s">${sub || "&nbsp;"}</div></div>`;
const rdPct = (x) => (x == null ? "—" : (x * 100).toFixed(1) + "%");
const rdUsd = (x) => (x == null ? "—" : (x < 0 ? "-$" : "+$") + Math.abs(x).toLocaleString(undefined, { maximumFractionDigits: 0 }));

async function loadRD() {
  let r;
  try { r = await api("/api/rd"); } catch (e) { $("#rd-desk").innerHTML = `<span class="muted">R&amp;D unavailable: ${esc(e.message)}</span>`; return; }
  RD.data = r;
  const lab = r.lab, dk = r.desk || {};
  const last = (r.digests || []).find((d) => d.period === "weekly") || (r.digests || [])[0];
  const b = (last && last.bot) || {};
  $("#rd-kpis").innerHTML = [
    rdKpi("Bot (last digest)", b.trades ? rdPct(b.win_rate) : "—", b.trades ? `${b.trades} trades · ${Number(b.net_r).toFixed(2)}R · ${esc(last.label)}` : "no paper trades yet"),
    rdKpi("Lab trials", lab ? lab.trials_so_far : "—", lab ? `data ${esc(lab.data.first)} → ${esc(lab.data.last)}` : esc(r.hint || "")),
    rdKpi("Holdout looks", lab ? `${lab.holdout_looks}/${lab.look_budget}` : "—", lab && lab.fresh_holdout_from ? `fresh holdout once data reaches ${esc(lab.fresh_holdout_from)}` : "locked last 6 months"),
    rdKpi("Proposals", (r.proposals || []).filter((p) => p.status === "pending").length, "waiting for approval"),
    rdKpi("Settings", lab ? `T1 ${lab.current.t1_r}R` : "—", lab ? `bank ${Math.round(lab.current.scale_frac * 100)}% · trend ${lab.current.orx_trend_len}d · flat ${esc(lab.current.flat_time)}` : ""),
  ].join("");
  $("#rd-desk").innerHTML = `<b>R&amp;D desk</b>
    <span class="${dk.webhook ? "pos" : "neg"}">Discord ${dk.webhook ? "connected" : "not set (ABG_RD_WEBHOOK_URL)"}</span>
    <span>digests: ${["daily", "weekly", "monthly"].filter((k) => dk[k] !== false).join(" · ") || "off"}</span>
    <span>agent research: ${esc(dk.auto_run || "off")}${dk.api_key ? "" : ' <span class="neg">(no API key - agents idle)</span>'}</span>
    ${dk.busy ? `<span class="pos">running: ${esc(dk.busy)}</span>` : ""}
    ${dk.hint ? `<span class="muted">${esc(dk.hint)}</span>` : ""}
    ${(dk.errors || []).map((e) => `<span class="neg">${esc(e)}</span>`).join("")}`;
  $("#rd-actions").innerHTML = ["daily", "weekly", "monthly"].map((p) =>
    `<button class="btn small ${RD.period === p ? "primary" : ""}" data-rdp="${p}">${p}</button>`).join(" ")
    + ` <button class="btn small" id="rd-post" ${dk.webhook ? "" : "disabled"} title="Build this digest now and post it to the R&D channel">post to Discord</button>`;
  document.querySelectorAll("[data-rdp]").forEach((x) => x.addEventListener("click", () => { RD.period = x.dataset.rdp; showDigest(true); loadRD(); }));
  $("#rd-post").addEventListener("click", postDigest);
  showDigest(false);
  renderReports(r.reports || []);
  renderTrials(r.trials || [], lab);
  renderProposals(r.proposals || []);
}

function digestHtml(d) {
  if (!d) return '<p class="muted">No digest yet for this period.</p>';
  const b = d.bot || {};
  const head = `<p><b>${esc(d.label)}</b> <span class="muted">built ${esc((d.built || "").replace("T", " ").slice(0, 16))}${d.delivered ? " · " + esc(d.delivered) : ""}</span></p>`;
  const bot = b.trades ? `<p>${b.trades} paper trades · win ${rdPct(b.win_rate)} · ${Number(b.net_r).toFixed(2)}R · ${rdUsd(b.net_usd)}/micro · max DD $${fmt(b.max_dd_usd, 0)}</p>` : "";
  const f = (d.findings || []).map((x) => `<li>${esc(x)}</li>`).join("");
  const note = d.note ? `<p><b>Analyst note:</b> ${esc(d.note)}</p>` : "";
  return head + bot + (f ? `<ul class="rd-findings">${f}</ul>` : '<p class="muted">Nothing notable.</p>') + note;
}

async function showDigest(fresh) {
  const saved = ((RD.data && RD.data.digests) || []).find((d) => d.period === RD.period);
  if (saved && !fresh) { $("#rd-digest").innerHTML = digestHtml(saved); return; }
  $("#rd-digest").innerHTML = '<p class="muted">Building the digest… (the first one loads the lab dataset)</p>';
  try { $("#rd-digest").innerHTML = digestHtml(await api(`/api/rd/digest?period=${RD.period}`)); }
  catch (e) { $("#rd-digest").innerHTML = `<p class="muted">${esc(e.message)}</p>`; }
}

async function postDigest() {
  $("#rd-post").disabled = true;
  try {
    const d = await api(`/api/rd/digest?period=${RD.period}`, { method: "POST" });
    $("#rd-digest").innerHTML = digestHtml(d);
    toast(`R&D ${RD.period} digest: ${d.delivered}`);
  } catch (e) { toast(e.message); }
  setTimeout(() => { const b = $("#rd-post"); if (b) b.disabled = false; }, 30000);
}

function renderReports(reps) {
  $("#rd-reports").innerHTML = reps.length ? reps.map((x) => `<div class="cal-row"><span>${esc(new Date(x.at * 1000).toLocaleString())}</span>
      <span><a href="#" data-rep="${esc(x.name)}">${esc(x.name)}</a> <span class="muted">${esc((x.result || "").replace(/\*\*/g, ""))}</span></span></div>`).join("")
      + '<pre id="rd-report" class="rd-report" hidden></pre>'
    : '<p class="muted">No research runs yet. With <code>ABG_ANTHROPIC_API_KEY</code> set the agents run every Saturday, or run <code>abg lab run</code>.</p>';
  document.querySelectorAll("[data-rep]").forEach((a) => a.addEventListener("click", async (e) => {
    e.preventDefault();
    try { const r = await api(`/api/rd/report/${a.dataset.rep}`); const p = $("#rd-report"); p.textContent = r.text; p.hidden = false; }
    catch (err) { toast(err.message); }
  }));
}

function renderTrials(tr, lab) {
  $("#rd-ledger-note").textContent = lab ? `${lab.trials_so_far} trials · search window ${lab.data.search[0]} → ${lab.data.search[1]} · holdout ${lab.data.holdout ? lab.data.holdout[0] + " → " + lab.data.holdout[1] : "none"}` : "";
  $("#rd-trials").innerHTML = tr.length ? `<div class="rd-scroll"><table><thead><tr><th>#</th><th>Hypothesis</th><th>Changes</th><th class="n">Trades</th><th class="n">Win</th><th class="n">Net/DD</th><th>Verdict</th><th>Failed checks</th></tr></thead><tbody>
    ${tr.map((t) => `<tr><td>${t.id}</td><td>${esc(t.name)}<br><span class="muted">${esc(t.source || "")}</span></td><td><code>${esc(JSON.stringify(t.changes))}</code></td>
      <td class="n">${t.search.trades ?? ""}</td><td class="n">${rdPct(t.search.win_rate)}</td><td class="n">${t.search.ratio == null ? "—" : Number(t.search.ratio).toFixed(2)}</td>
      <td class="${t.verdict === "confirmed" ? "pos" : t.verdict === "candidate" ? "" : "neg"}">${esc(t.verdict)}</td><td class="muted">${esc((t.failed || []).join(", "))}</td></tr>`).join("")}
    </tbody></table></div>` : '<p class="muted">No trials yet - <code>abg lab test</code> or <code>abg lab run</code>.</p>';
}

function renderProposals(ps) {
  $("#rd-proposals").innerHTML = ps.length ? ps.map((p) => {
    const ev = p.evidence || {}, h = ev.holdout || {}, hb = ev.holdout_baseline || {};
    return `<p><b>#${p.id} ${esc(p.name)}</b> <span class="${p.status === "pending" ? "" : "muted"}">[${esc(p.status)}]</span> <code>${esc(JSON.stringify(p.changes))}</code><br>
      <span class="muted">holdout: win ${rdPct(h.win_rate)} vs ${rdPct(hb.win_rate)} · net/DD ${fmt(h.ratio)} vs ${fmt(hb.ratio)}</span></p>`;
  }).join("") : '<p class="muted">No proposals. A change appears here only after it passes the search window AND the locked holdout.</p>';
}
window.loadRD = loadRD;
