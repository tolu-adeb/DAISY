// DAISY dashboard - shell: routes, the nav, theme, the Deep dive switch, sign-in.
import { $, $$, api, post, state, toast, wireTips, esc } from "./ui.js";
import * as V from "./views.js";

const ROUTES = {
  today: V.today, watchlist: V.watchlist, pitches: V.pitches, pitch: V.pitch, scorecard: V.scorecard,
  explore: V.explore, research: V.research, settings: V.settings, glossary: V.glossary,
};
const NAV = { pitches: "pitch", pitch: "pitch", glossary: "settings" };
const TITLES = { today: "Today", watchlist: "Watchlist", pitches: "Pitches", pitch: "Pitch", scorecard: "Scorecard",
  explore: "Explore", research: "Research", settings: "Settings", glossary: "Glossary" };
let seq = 0;

async function render() {
  const [name = "today", ...rest] = (location.hash.replace(/^#\/?/, "") || "today").split("/");
  const view = ROUTES[name] ? name : "today";
  const mine = ++seq;
  for (const a of $$("[data-nav]")) {
    if (a.dataset.nav === (NAV[view] || view)) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  $("#nav-links").classList.remove("open");
  $("#nav-menu").setAttribute("aria-expanded", "false");
  document.title = `${TITLES[view]} — DAISY Stock Intelligence Terminal`;
  const box = $("#view");
  box.setAttribute("aria-busy", "true");
  try {
    const html = await ROUTES[view](rest.map(decodeURIComponent), box);
    if (mine !== seq) return;                       // a newer navigation won
    if (typeof html === "string") box.innerHTML = html;
    V.afterRender?.(view, box);
    for (const im of $$("img[data-light]", box)) im.addEventListener("error", () => { if (im.src !== im.dataset.light) im.src = im.dataset.light; }, { once: true });
    wireTips(box);
  } catch (e) {
    if (mine !== seq) return;
    box.innerHTML = `<section class="hero short"><div class="grid" style="width:100%"><div class="wide"><span class="caps eyebrow">Something went wrong</span>
      <h1>We couldn't load this page.</h1><p class="lede">${esc(e.message)}</p><p><a class="link-caps" href="#/today">Back to Today</a></p></div></div></section>`;
  } finally {
    box.removeAttribute("aria-busy");
  }
  if (!location.hash.includes("?keep")) { window.scrollTo(0, 0); $("#main").focus({ preventScroll: true }); }
}

// ------------------------------------------------------------------ theme, depth, menu
function setTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("daisy-theme", t); } catch { /* private window */ }
}
function setDeep(on) {
  if (on) document.documentElement.dataset.deep = "1"; else delete document.documentElement.dataset.deep;
  $("#deep").setAttribute("aria-checked", on ? "true" : "false");
  try { localStorage.setItem("daisy-deep", on ? "1" : "0"); } catch { /* private window */ }
}
$("#theme").addEventListener("click", () => { setTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light"); if ($("img[data-light]")) render(); });
$("#deep").addEventListener("click", () => setDeep(document.documentElement.dataset.deep !== "1"));
$("#deep").setAttribute("aria-checked", document.documentElement.dataset.deep === "1" ? "true" : "false");
$("#nav-menu").addEventListener("click", () => {
  const open = $("#nav-links").classList.toggle("open");
  $("#nav-menu").setAttribute("aria-expanded", open ? "true" : "false");
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { $("#nav-links").classList.remove("open"); $("#nav-menu").setAttribute("aria-expanded", "false"); } });

// soft parallax on the hero's drop; off under prefers-reduced-motion
const calm = window.matchMedia("(prefers-reduced-motion: reduce)");
let ticking = false;
window.addEventListener("scroll", () => {
  if (calm.matches || ticking) return;
  ticking = true;
  requestAnimationFrame(() => {
    const d = $(".drop");
    if (d) d.style.transform = `translate3d(0, ${Math.min(160, window.scrollY * 0.18)}px, 0)`;
    ticking = false;
  });
}, { passive: true });

// ------------------------------------------------------------------ sign-in (the token goes into an HttpOnly cookie; it is never kept in the page)
document.addEventListener("daisy:signin", () => { const d = $("#signin"); if (!d.open) d.showModal(); });
$("#signin-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#signin-err");
  err.hidden = true;
  try {
    const r = await fetch("/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" },
      credentials: "same-origin", body: JSON.stringify({ token: $("#signin-token").value }) });
    if (!r.ok) throw new Error("That token was not accepted.");
    $("#signin-token").value = "";
    $("#signin").close();
    await boot();
  } catch (x) { err.textContent = x.message; err.hidden = false; }
});
export async function signOut() { await post("/api/auth/logout"); toast("Signed out."); location.hash = "#/today"; location.reload(); }

async function boot() {
  try {
    const me = await api("/api/auth/me");
    state.role = me.role; state.protected = me.protected;
    if (me.role === "none") { document.dispatchEvent(new CustomEvent("daisy:signin")); return; }
    state.glossary = await api("/api/stocks/glossary");
  } catch { /* render() will say what failed */ }
  await render();
}
window.addEventListener("hashchange", render);
boot();
