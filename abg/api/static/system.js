/* DAISY - System tab (docs/20): monitor, background jobs, recent errors, data freshness, which features and keys are set
   (yes/no only - no secret values ever reach the page). */
"use strict";
(() => {
const yes = (b, on = "on", off = "off") => `<span class="pill ${b ? "ok" : ""}">${b ? on : off}</span>`;
const age = (h) => (h == null ? "—" : h < 1 ? `${Math.round(h * 60)}m` : h < 48 ? `${fmt(h, 1)}h` : `${fmt(h / 24, 0)}d`);

async function loadSystem() {
  let r;
  try { r = await api("/api/system"); } catch (e) { $("#sy-monitor").innerHTML = `<span class="neg">${esc(e.message)}</span>`; return; }
  const m = r.monitor || {}, other = m.elsewhere;
  $("#sy-monitor").innerHTML = `<b>Monitor</b>
    ${m.running ? '<span class="pill ok">running in this window</span>' : other ? `<span class="pill warn">running elsewhere: pid ${esc(other.pid)} on ${esc(other.host)}</span>` : '<span class="pill bad">not running</span>'}
    ${r.note ? `<span class="muted">${esc(r.note)}</span>` : ""}
    <span class="muted">data: ${esc(r.data_dir)}</span>`;
  $("#sy-features").innerHTML = `<table><tbody>${Object.entries(r.features || {}).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${yes(v)}</td></tr>`).join("")}</tbody></table>
    <p class="muted small">Switch features in <code>.env</code> and restart the monitor. New desks stay off until you turn them on.</p>`;
  $("#sy-keys").innerHTML = `<table><tbody>${Object.entries(r.keys || {}).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${yes(v, "set", "not set")}</td></tr>`).join("")}</tbody></table>
    <p class="muted small">Only whether each key is set is shown here; values stay in <code>.env</code>.</p>`;
  $("#sy-files").innerHTML = `<table><thead><tr><th>File</th><th class="n">Updated</th><th class="n">Size</th></tr></thead><tbody>
    ${Object.entries(r.files || {}).map(([k, f]) => `<tr><td><code>${esc(k)}</code></td><td class="n ${f.exists ? (f.age_h > 72 ? "neg" : "") : "muted"}">${f.exists ? age(f.age_h) + " ago" : "missing"}</td><td class="n">${f.kb == null ? "—" : fmt(f.kb, 0) + " KB"}</td></tr>`).join("")}</tbody></table>`;
  const jobs = r.jobs || {};
  const row = (name, j) => {
    if (!j) return `<tr><td>${esc(name)}</td><td colspan="2" class="muted">off</td></tr>`;
    const errs = (j.errors || []).slice(-2).map((e) => esc(typeof e === "string" ? e : `${e.where || ""} ${e.error || ""}`)).join("<br>");
    let last = j.last_pass || j.last || j.last_batch;
    if (last && typeof last === "object" && !Object.keys(last).length) last = null;
    return `<tr><td>${esc(name)}</td><td>${j.enabled === false ? '<span class="muted">off</span>' : j.busy ? `<span class="pos">busy: ${esc(j.busy)}</span>` : "idle"}
      ${last ? `<span class="muted"> · last ${esc(typeof last === "string" ? last : (last.at || JSON.stringify(last)).slice(0, 60))}</span>` : ""}</td><td class="neg small">${errs}</td></tr>`;
  };
  $("#sy-jobs").innerHTML = `<table><tbody>${row("Scheduler", jobs.scheduler)}${row("R&D desk", jobs.rd)}${row("Forward lab", jobs.forward)}${row("Swing desk", jobs.swing)}${row("Pitch desk", jobs.pitch)}</tbody></table>`;
  const errs = (m.errors || []).slice(-10).reverse();
  $("#sy-errors").innerHTML = errs.length ? `<ul class="plain">${errs.map((e) => `<li><span class="muted">${e.ts ? new Date(e.ts * 1000).toLocaleString() : ""}</span> <b>${esc(e.where || "")}</b> ${esc(e.error || "")}</li>`).join("")}</ul>`
    : '<p class="muted">No recent errors.</p>';
}

LOADERS.system = () => loadSystem();
setInterval(() => { const v = $("#view-system"); if (v && !v.hidden && !document.hidden) loadSystem(); }, 30000);
})();
