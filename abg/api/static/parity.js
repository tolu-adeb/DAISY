/* Ported from the MNQ bot spin-off (c3.lab/mnq-bot) for DAISY's desk tabs (docs/20). Wrapped so its names stay local. */
(() => {
"use strict";
const api = window.deskApi, toast = window.deskToast;
// MNQ bot -> Monitor: live vs backtest for recent sessions (docs/14.12) and the bot's uptime.
const PAR = { open: new Set() };

const tradeTxt = (t) => `${t.side === 1 ? "long" : "short"} ${esc(t.setup)} ${esc(t.opened)} · ${esc(t.exit || "")} ${sgn(t.r, 2, "R")}`;
const verdictPill = (v) => {
  const m = { match: ["ok", "match"], differs: ["bad", "differs"], "older strategy": ["warn", "older strategy"], "no live data": ["", "bot off"], "no backtest": ["", "no backtest yet"] }[v] || ["", v];
  return `<span class="pill ${m[0]}">${esc(m[1])}</span>`;
};

function renderParity(r) {
  const s = r.summary || {}, rows = s.rows || [];
  $("#par-note").textContent = s.judged ? `${s.matched} of ${s.judged} sessions match · live ${(s.live_r >= 0 ? "+" : "") + fmt(s.live_r, 2)}R vs backtest ${(s.backtest_r >= 0 ? "+" : "") + fmt(s.backtest_r, 2)}R` : "";
  const ro = document.body.classList.contains("readonly");
  const btn = `<div class="btns"><button class="btn" id="par-run" ${ro || r.busy ? "disabled" : ""}>${r.busy ? esc(r.busy) + "…" : "Check the last 5 sessions"}</button>
    <span class="muted small">Runs by itself after the forward lab extends its data (16:45 ET). Every lab result assumes the backtest is what the bot does; this is where that gets checked.</span></div>`;
  if (!rows.length) { $("#par").innerHTML = `<p class="muted">No sessions checked yet.</p>${btn}`; wirePar(); return; }
  $("#par").innerHTML = `<div class="scroll"><table class="parity"><thead><tr><th>Session</th><th>Result</th><th>Live</th><th>Backtest</th><th>Why</th></tr></thead><tbody>
    ${rows.map((x) => {
      const open = PAR.open.has(x.day);
      const why = (x.causes || [])[0] || (x.verdict === "match" ? "same trades, same exits" : "");
      return `<tr data-pday="${esc(x.day)}" tabindex="0"><td><b>${esc(x.day.slice(5))}</b></td><td>${verdictPill(x.verdict)}</td>
        <td class="small">${(x.live || []).map(tradeTxt).join("<br>") || '<span class="muted">no trade</span>'}</td>
        <td class="small">${(x.backtest || []).map(tradeTxt).join("<br>") || '<span class="muted">no trade</span>'}</td>
        <td class="small">${esc(why)}${(x.causes || []).length > 1 ? ` <span class="muted">+${x.causes.length - 1} more</span>` : ""}</td></tr>
        ${open ? `<tr class="detail"><td></td><td colspan="4">
          ${(x.causes || []).length ? `<ul class="plain small">${x.causes.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}
          ${(x.steps || []).length > 1 ? `<p class="small muted">One input changed at a time: ${x.steps.map((st) => `${esc(st.name)} ${st.same ? "✓ same" : "✗ differs"}`).join(" · ")}</p>` : ""}
          ${(x.ctx_diff || []).length ? `<p class="small">Morning context: ${x.ctx_diff.map(esc).join("; ")}</p>` : ""}
          ${(x.notes || []).length ? `<p class="small muted">${x.notes.map(esc).join(" · ")}</p>` : ""}</td></tr>` : ""}`;
    }).join("")}</tbody></table></div>${btn}`;
  wirePar();
}
function wirePar() {
  const b = $("#par-run");
  if (b) b.onclick = async () => { try { await api("/api/rd/parity/run", { json: {} }); toast("Checking the last 5 sessions", "ok"); setTimeout(loadParity, 4000); } catch (e) { toast(e.message, "error"); } };
}

function renderUptime(r) {
  const days = r.uptime || [];
  const ka = r.keep_awake;
  const chips = `<p class="small chips">${r.healthcheck ? '<span class="pill ok">Missed-check-in alert on</span>' : '<span><span class="pill warn">No missed-check-in alert</span> <span class="muted">set ABG_HEALTHCHECK_URL (a free healthchecks.io check) to get a message when the bot stops</span></span>'}
    ${ka === true ? '<span class="pill ok">Keeping Windows awake</span>' : ka === false ? '<span class="muted">keep-awake: idle outside 08:55–16:35 ET</span>' : ""}
    ${r.autostart === true ? '<span class="pill ok">Starts with Windows</span>' : r.autostart === false ? '<span><span class="pill warn">Not set to start with Windows</span> <span class="muted">run deploy\\windows-autostart.ps1 once</span></span>' : ""}</p>`;
  if (!days.length) { $("#up").innerHTML = chips + '<p class="muted">Recording starts with this update.</p>'; return; }
  $("#up").innerHTML = chips + `<div class="uptime">${days.map((d) => {
    const cls = d.pct >= 98 ? "ok" : d.pct >= 60 ? "warn" : "bad";
    const t = d.pct ? `${fmt(d.pct, 0)}% of 9:25–${d.so_far ? "now" : "16:00"}${d.saw_open ? "" : `; first seen ${d.first_seen}`}${(d.gaps || []).length ? "; gaps " + d.gaps.map((g) => g.join("–")).join(", ") : ""}` : "not running";
    return `<div class="up-day ${cls}" title="${esc(d.day)}: ${esc(t)}"><span class="up-bar" style="--w:${Math.max(2, d.pct)}%"></span><span class="small">${esc(d.day.slice(5))}</span><span class="small muted">${d.pct ? fmt(d.pct, 0) + "%" : "off"}${d.pct && !d.saw_open ? " · late" : ""}${d.so_far ? " so far" : ""}</span></div>`;
  }).join("")}</div>`;
}

async function loadParity() {
  let r;
  try { r = await api("/api/rd/parity"); } catch (e) { $("#par").innerHTML = `<p class="muted">${esc(e.message)}</p>`; return; }
  PAR.data = r;
  renderParity(r); renderUptime(r);
  clearTimeout(PAR.t);
  if (r.busy) PAR.t = setTimeout(() => { if (!document.hidden && !$('[data-panel="mnq/system"]').hidden) loadParity(); }, 5000);
}

(function () {
  const prev = LOADERS.mnq;
  LOADERS.mnq = (sub) => { prev(sub); if (sub === "system") loadParity(); };
  document.addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-pday]");
    if (!tr) return;
    const d = tr.dataset.pday;
    PAR.open.has(d) ? PAR.open.delete(d) : PAR.open.add(d);
    if (PAR.data) renderParity(PAR.data);
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.matches("tr[data-pday]")) e.target.click(); });
})();
})();
