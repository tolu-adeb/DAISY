// DAISY dashboard - shared pieces (vanilla ES module, no build step). Every string from the API is escaped.
export const $ = (s, root = document) => root.querySelector(s);
export const $$ = (s, root = document) => [...root.querySelectorAll(s)];
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const safeUrl = (u) => (/^https?:\/\//i.test(String(u || "")) ? u : null);
export const state = { glossary: { terms: {}, status: {}, ranks: {}, grades: {} }, role: "view", protected: false };
const NA = "—";

// ------------------------------------------------------------------ numbers (always tabular, never guessed)
const nf = (d) => new Intl.NumberFormat("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
export const num = (x, d = 2) => (x == null || Number.isNaN(+x) ? NA : nf(d).format(+x));
export const usd = (x, d = 2) => (x == null || Number.isNaN(+x) ? NA : "$" + nf(d).format(+x));
export const pct = (x, d = 1) => (x == null || Number.isNaN(+x) ? NA : nf(d).format(x * 100) + "%");
// a signed move: an arrow and a sign as well as a colour, so it never depends on colour alone
export const move = (x, d = 1) => {
  if (x == null || Number.isNaN(+x)) return `<span class="muted">${NA}</span>`;
  const cls = x > 0 ? "up" : x < 0 ? "down" : "muted", arrow = x > 0 ? "▲" : x < 0 ? "▼" : "•";
  return `<span class="${cls} num"><span aria-hidden="true">${arrow}</span> ${x > 0 ? "+" : ""}${nf(d).format(x * 100)}%</span>`;
};
export const shortDate = (iso) => {
  if (!iso) return NA;
  const d = new Date(String(iso).slice(0, 10) + "T12:00:00");
  return Number.isNaN(+d) ? esc(iso) : d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
};
export const clock = (iso) => {
  const d = new Date(iso);
  return Number.isNaN(+d) ? "" : d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
};

// ------------------------------------------------------------------ API
export async function api(path, opts) {
  const res = await fetch(path, { credentials: "same-origin", ...opts });
  let body = null;
  try { body = await res.json(); } catch { /* not JSON */ }
  if (res.status === 401) { document.dispatchEvent(new CustomEvent("daisy:signin")); throw new Error("Sign in to see the desk."); }
  if (!res.ok) throw new Error(body?.error?.message || body?.detail || `The server answered ${res.status}.`);
  return body;
}
export const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

export function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => { t.hidden = true; }, 7000);
}

// ------------------------------------------------------------------ glossary: depth one hover (or one tab) away
export const term = (label, key) => {
  const k = key || label;
  return state.glossary.terms[k] ? `<span class="term" tabindex="0" data-term="${esc(k)}">${esc(label)}</span>` : esc(label);
};
export function wireTips(root = document) {
  const tip = $("#tip");
  const show = (el) => {
    const text = state.glossary.terms[el.dataset.term] || el.dataset.tip;
    if (!text) return;
    tip.textContent = text; tip.hidden = false;
    el.setAttribute("aria-describedby", "tip");
    const r = el.getBoundingClientRect(), w = Math.min(300, window.innerWidth - 24);
    tip.style.left = Math.max(12, Math.min(window.innerWidth - w - 12, r.left)) + "px";
    tip.style.top = (r.bottom + 8 + tip.offsetHeight > window.innerHeight ? r.top - tip.offsetHeight - 8 : r.bottom + 8) + "px";
  };
  const hide = (el) => { tip.hidden = true; el.removeAttribute("aria-describedby"); };
  for (const el of $$(".term, [data-tip]", root)) {
    if (el._tip) continue;
    el._tip = true;
    el.addEventListener("mouseenter", () => show(el)); el.addEventListener("mouseleave", () => hide(el));
    el.addEventListener("focus", () => show(el)); el.addEventListener("blur", () => hide(el));
    el.addEventListener("keydown", (e) => { if (e.key === "Escape") hide(el); });
  }
}

// ------------------------------------------------------------------ small components
export const rankBadge = (r) => (r == null ? "" :
  `<span class="rank" data-r="${esc(r)}" data-tip="DAISY Rank ${esc(r)}: ${esc(state.glossary.ranks[r] || "")}" tabindex="0" aria-label="DAISY Rank ${esc(r)}, ${esc(state.glossary.ranks[r] || "")}">${esc(r)}</span>`);
const GL = { momentum: "Momentum", trend: "Trend", value: "Value", quality: "Quality", sentiment: "News" };
export const gradeChips = (g) => (!g ? "" : `<div class="grades" aria-label="Style grades">${Object.keys(GL).map((k) =>
  `<span class="grade" data-tip="${GL[k]} grade, A to F. A dash means we do not have the data.">${GL[k]}<b>${esc(g[k] ?? NA)}</b></span>`).join("")}</div>`);
export const statusWords = (s) => state.glossary.status[s] || String(s || "").replace(/_/g, " ");
export const pill = (text, on = false) => `<span class="pill${on ? " on" : ""}">${esc(text)}</span>`;
export const empty = (text) => `<p class="empty">${esc(text)}</p>`;
export const section = (eyebrow, body, { alt = false, wide = false, cls = "" } = {}) =>
  `<section class="sec${alt ? " alt" : ""} ${cls}"><div class="grid">${wide ? `<div class="wide"><span class="caps muted">${esc(eyebrow)}</span>${body}</div>` :
    `<span class="caps eyebrow">${esc(eyebrow)}</span><div class="body">${body}</div>`}</div></section>`;
export const hero = (eyebrow, title, lede = "", { short = false, extra = "" } = {}) =>
  `<section class="hero${short ? " short" : ""}"><div class="drop" aria-hidden="true"></div><div class="grid" style="width:100%"><div class="wide">
    <span class="caps eyebrow">${esc(eyebrow)}</span><h1>${title}</h1>${lede ? `<p class="lede">${lede}</p>` : ""}${extra}</div></div></section>`;
export const table = (caption, heads, rows, { numeric = [] } = {}) => `<div class="table-wrap"><table><caption>${esc(caption)}</caption>
  <thead><tr>${heads.map((h, i) => `<th scope="col"${numeric.includes(i) ? ' class="n"' : ""}>${h}</th>`).join("")}</tr></thead>
  <tbody>${rows.map((r) => `<tr>${r.map((c, i) => `<td${numeric.includes(i) ? ' class="n"' : ""}>${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
export const kv = (pairs) => `<dl class="kv">${pairs.filter(Boolean).map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>`;

// One sentence for a pick, in words an analyst would use (never a state code).
export function pickSentence(p) {
  const lv = p.live;
  if (!lv || !lv.state) {
    return `Buy at the open near ${usd(p.price)}` + (p.stop ? `. Stop ${usd(p.stop)}` : "") + (p.t1 ? `, first target ${usd(p.t1)}` : "") + ".";
  }
  if (lv.state === "no_quote") return "No live quote right now; the plan is unchanged.";
  let s = lv.open != null ? `Entered at the ${usd(lv.open)} open, now ${usd(lv.price)} (${lv.from_open > 0 ? "up" : lv.from_open < 0 ? "down" : "flat"} ${pct(Math.abs(lv.from_open || 0))})` : `Now ${usd(lv.price)}`;
  if (p.t1 && lv.open && lv.price > lv.open && lv.state !== "t1") {
    const f = (lv.price - lv.open) / (p.t1 - lv.open);
    if (f > 0.15) s += f >= 0.7 ? ", most of the way to the first target" : f >= 0.35 ? ", about halfway to the first target" : ", a first step toward the target";
  }
  return `${s}. ${statusWords(lv.state)}.`;
}
export const stateDot = (s) => `<span class="dot ${["t1", "in_zone", "in_play", "active"].includes(s) ? "up" : ["stopped", "gap_stop", "exited", "dropped"].includes(s) ? "down" : ["above_zone", "below_zone", "missed"].includes(s) ? "warn" : ""}" aria-hidden="true"></span>`;

// ------------------------------------------------------------------ a small markdown renderer for memos (text is escaped first)
export function md(text) {
  const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\*(.+?)\*/g, "<i>$1</i>").replace(/`(.+?)`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (m, t, u) => `<a href="${u}" target="_blank" rel="noopener noreferrer">${t}</a>`);
  const out = [];
  let list = false, tbl = null;
  const flush = () => { if (list) { out.push("</ul>"); list = false; } if (tbl) { out.push(tbl + "</tbody></table></div>"); tbl = null; } };
  for (const line of (text || "").split("\n")) {
    if (/^\|/.test(line)) {
      const cells = line.split("|").slice(1, -1).map((c) => c.trim());
      if (cells.every((c) => /^:?-+:?$/.test(c))) continue;
      if (!tbl) { if (list) { out.push("</ul>"); list = false; } tbl = `<div class="table-wrap"><table><thead><tr>${cells.map((c) => `<th scope="col">${inline(c)}</th>`).join("")}</tr></thead><tbody>`; continue; }
      tbl += `<tr>${cells.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`; continue;
    }
    if (/^- /.test(line)) { if (tbl) { out.push(tbl + "</tbody></table></div>"); tbl = null; } if (!list) { out.push("<ul>"); list = true; } out.push(`<li>${inline(line.slice(2))}</li>`); continue; }
    flush();
    const h = line.match(/^(#{1,3}) (.*)/), img = line.trim().match(/^!\[([^\]]*)\]\(([\w.-]+\.png)\)$/);
    if (h) out.push(`<h${h[1].length + 1}>${inline(h[2])}</h${h[1].length + 1}>`);
    else if (img) {                                   // the dark theme has its own render of each chart; fall back to the light one
      const light = `/api/stocks/chart/${encodeURIComponent(img[2])}`, dark = `/api/stocks/chart/${encodeURIComponent(img[2].replace(/\.png$/, "-dark.png"))}`;
      const isDark = document.documentElement.dataset.theme !== "light";
      out.push(`<figure><img src="${isDark ? dark : light}" data-light="${light}" alt="${esc(img[1])}" loading="lazy"><figcaption>${esc(img[1])}</figcaption></figure>`);
    }
    else if (/^> /.test(line)) out.push(`<blockquote>${inline(line.slice(2))}</blockquote>`);
    else if (/^---+$/.test(line.trim())) out.push("<hr>");
    else if (line.trim()) out.push(`<p>${inline(line)}</p>`);
  }
  flush();
  return out.join("");
}

// A sparkline with a zero line; the caption carries the numbers, so the picture is never the only source.
export function spark(points, { label = "" } = {}) {
  const ys = points.map((p) => p.y).filter((y) => y != null);
  if (ys.length < 2) return `<p class="muted small">Not enough history to draw yet.</p>`;
  const lo = Math.min(-0.05, ...ys), hi = Math.max(0.05, ...ys), W = 300, H = 56;
  const x = (i) => (i / (points.length - 1)) * W, y = (v) => H - ((v - lo) / (hi - lo)) * H;
  const d = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.y ?? 0).toFixed(1)}`).join(" ");
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(label)}">
    <line x1="0" x2="${W}" y1="${y(0).toFixed(1)}" y2="${y(0).toFixed(1)}" stroke="var(--stroke-strong)" stroke-width="1"/>
    <path d="${d}" fill="none" stroke="var(--accent)" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>`;
}
