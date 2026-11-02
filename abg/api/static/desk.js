/* DAISY desk tabs (docs/20): sub-tab routing (#swing/book, #rd/forward, ...), helpers shared by the tabs ported from the
   MNQ bot spin-off (swing.js, wide.js, forward.js, parity.js), and live updates for the bot / swing / pitch desks. */
"use strict";

window.$$ = (s) => Array.from(document.querySelectorAll(s));
window.store = { get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : v; } catch { return d; } },
                 set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage blocked */ } } };
window.usd = (x) => (x == null ? "—" : (x < 0 ? "-$" : "+$") + Math.abs(x).toLocaleString(undefined, { maximumFractionDigits: 0 }));
window.stat = (label, value, sub, cls) => `<div class="stat ${cls || ""}"><span class="l">${label}</span><span class="v">${value}</span><span class="s">${sub || "&nbsp;"}</span></div>`;
window.SESSION = { info: null, at: 0 };
window.LOADERS = { mnq() {}, swing() {}, rd() {}, pitch() {}, system() {} };
// the spin-off's api() took {json: body}; DAISY's takes fetch options
window.deskApi = (path, opts) => {
  opts = Object.assign({}, opts || {});
  if (opts.json !== undefined) {
    opts.method = opts.method || "POST";
    opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers);
    opts.body = JSON.stringify(opts.json);
    delete opts.json;
  }
  return api(path, opts);
};
window.deskToast = (msg) => toast(msg);

const DESK_DEFAULT = { mnq: "live", swing: "book", rd: "findings", pitch: "screen", system: "overview" };

function deskRoute(sec, sub) {
  const view = document.getElementById("view-" + sec);
  // every sub-panel outside the visible one is hidden, so "is this panel showing?" checks in the ported tabs stay right
  $$("[data-panel]").forEach((p) => { if (!view || !view.contains(p)) p.hidden = true; });
  if (!view || !DESK_DEFAULT[sec]) return;
  const panels = Array.from(view.querySelectorAll("[data-panel]"));
  if (!panels.length) { if (LOADERS[sec]) LOADERS[sec](sub); return; }
  if (!sub || !panels.some((p) => p.dataset.panel === `${sec}/${sub}`)) sub = store.get("desk-" + sec, DESK_DEFAULT[sec]);
  if (!panels.some((p) => p.dataset.panel === `${sec}/${sub}`)) sub = DESK_DEFAULT[sec];
  panels.forEach((p) => (p.hidden = p.dataset.panel !== `${sec}/${sub}`));
  view.querySelectorAll(".subnav a").forEach((a) => a.classList.toggle("active", a.dataset.sub === sub));
  store.set("desk-" + sec, sub);
  if (location.hash !== `#${sec}/${sub}`) history.replaceState(null, "", `#${sec}/${sub}`);
  if (LOADERS[sec]) LOADERS[sec](sub);
}

(function () {
  const base = window.showView;
  window.showView = function (v, sub) {
    base(v);
    if (DESK_DEFAULT[v]) deskRoute(v, sub);
    else { $$("[data-panel]").forEach((p) => (p.hidden = true)); if (location.hash) history.replaceState(null, "", location.pathname + location.search); }
  };
  function fromHash() {
    const [sec, sub] = (location.hash || "").replace(/^#/, "").split("/");
    if (DESK_DEFAULT[sec]) window.showView(sec, sub);
  }
  window.addEventListener("hashchange", fromHash);
  window.addEventListener("DOMContentLoaded", () => {
    $$("[data-panel]").forEach((p) => (p.hidden = true));
    if (window.swingInit) { try { swingInit(); } catch (e) { console.warn("swing desk", e); } }
    fromHash();
    if (!window.EventSource) return;
    const es = new EventSource("/api/signals/stream");
    const visible = (sec) => { const v = document.getElementById("view-" + sec); return v && !v.hidden; };
    es.addEventListener("alpha", (e) => {
      let d; try { d = JSON.parse(e.data); } catch { return; }
      if (visible("mnq") && window.mnqRefresh) mnqRefresh(d);
      if (["signal", "t1", "final", "stopped", "breakeven", "closed", "failed"].includes(d.type)) toast(`MNQ: ${d.type}`);
    });
    es.addEventListener("swing", (e) => { let d; try { d = JSON.parse(e.data); } catch { return; } if (window.onSwingEvent) onSwingEvent(d); });
    es.addEventListener("pitch", (e) => { let d; try { d = JSON.parse(e.data); } catch { return; }
      toast(`Pitch: ${d.symbol} ${d.verdict}`); if (visible("pitch") && window.loadPitch) loadPitch(); });
  });
})();
