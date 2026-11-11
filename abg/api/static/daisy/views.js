// DAISY dashboard - the destinations. Each opens simple; elements with class "deep" appear when the
// Deep dive switch is on, in place. Nothing here shows a portfolio: the desk pitches ideas (docs/22).
import { $, $$, api, post, esc, safeUrl, state, toast, usd, pct, num, move, shortDate, clock, term, rankBadge, gradeChips,
  statusWords, pill, empty, section, hero, table, kv, pickSentence, stateDot, md, spark, wireTips } from "./ui.js";

let after = null;                                        // the current view's event wiring
export function afterRender(view, box) { const f = after; after = null; if (f) f(box); }
const BOOK = { trend_pullback: "Trend pullback", mean_reversion: "Mean reversion", xs_momentum: "12-1 momentum", dual_momentum: "ETF rotation", crisis_alpha: "Crisis" };
const link = (u, t) => (safeUrl(u) ? `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(t)}</a>` : esc(t));
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : /(ch|s|x)$/.test(w) ? "es" : "s"}`;
const isAdmin = () => state.role === "admin";

// ====================================================================== Today
export async function today() {
  const d = await api("/api/stocks/desk");
  const st = d.state || {}, board = d.board || {}, picks = board.picks || [], cand = Object.fromEntries((d.candidates || []).map((c) => [c.id, c]));
  const wm = (d.watch || {}).meta || {}, rows = ((d.watch || {}).rows || []).filter((r) => ["watching", "triggered", "in_play"].includes(r.status));
  const mood = d.mood || {};
  const inTop = new Set(picks.map((p) => p.symbol)), notPicked = rows.filter((r) => !inTop.has(r.symbol));   // the preview shows what the top 5 does not
  const moodWord = { "risk-on": "Risk-on.", "risk-off": "Risk-off.", neutral: "A neutral tape." }[mood.label] || "The desk today.";
  const title = `${esc(moodWord)} ${picks.length ? `${plural(picks.length, "top pick")} in play.` : "No buys in play."}`;
  const lede = [mood.sentence, wm.changed_line ? `Watchlist: ${wm.changed_line}.` : null].filter(Boolean).map(esc).join(" ");
  const shocks = (d.shocks || []).slice(0, 2).map((s) => `<li><b>Theme shock · ${esc(s.label)}</b> — ${s.names.slice(0, 5).map((n) => `${esc(n.symbol)} ${move(n.chg)}`).join(", ")}
    ${s.source && safeUrl(s.source.url) ? ` · ${link(s.source.url, "source")}` : ""}</li>`).join("");
  const heat = d.heat || {}, b = heat.breadth || {};
  const heatHtml = `<div class="heat" aria-label="Sector and theme moves at the last close">
      ${(heat.sectors || []).map((s) => `<span class="cell">${esc(s.label)} ${move(s.chg_1d)}</span>`).join("")}
    </div>
    <div class="heat" style="margin-top:8px">${(heat.themes || []).map((t) => `<span class="cell">${esc(t.label)} ${move(t.chg_1d)}</span>`).join("")}
      ${b.above_200 != null ? `<span class="cell">${term("Breadth")} <b class="num">${pct(b.above_200, 0)}</b> above the 200-day</span>` : ""}
      ${b.new_highs != null ? `<span class="cell"><b class="num">${b.new_highs}</b> new highs · <b class="num">${b.new_lows}</b> new lows</span>` : ""}</div>`;
  const pickCard = (p) => {
    const c = cand[p.id] || {};
    return `<article class="card"><div class="card-top"><span class="sym">${rankBadge(c.daisy_rank)} ${esc(p.symbol)}</span>${pill(p.rating, true)}</div>
      <h3><a href="#/pitch/${encodeURIComponent(p.id)}">${esc(p.name && p.name !== p.symbol ? p.name : p.book_label)}</a></h3>
      <p class="status-line">${stateDot((p.live || {}).state)}<span>${esc(pickSentence(p))}</span></p>
      ${kv([["Book", esc(p.book_label)], p.zone ? [term("Entry zone"), `${usd(p.zone[0])}–${usd(p.zone[1])}`] : null,
        p.stop ? [term("Stop"), usd(p.stop)] : null, p.t1 ? [term("T1 / T2"), `${usd(p.t1)}${p.t2 ? " / " + usd(p.t2) : ""}`] : null,
        p.pct_of_fund != null ? ["Size", `${pct(p.pct_of_fund)} of the fund`] : null,
        p.bucket ? [term("Conviction"), `${esc(p.bucket)} (${esc(p.score)})`] : null])}
      ${gradeChips(c.grades)}<a class="link-caps" href="#/pitch/${encodeURIComponent(p.id)}">Read the pitch</a></article>`;
  };
  const feed = ((d.live || {}).feed || []).slice(0, 12);
  const cal = (d.calendar || {}).rows || [];
  const w = Object.entries(st.weights || {}).sort((a, c) => (c[1] || 0) - (a[1] || 0));
  const ld = d.last_daily || {};
  return hero(`Today · signals from the ${shortDate(st.date)} close`, title, lede, { extra: shocks ? `<ul class="feed" style="margin-top:20px;max-width:70ch">${shocks}</ul>` : "" })
    + section("Market at a glance", heatHtml + `<p class="muted small" style="margin-top:12px">Moves are from the last close. Regime: <b>${esc((st.regime || "—").replace(/_/g, " "))}</b>.</p>`)
    + section(`Top ${picks.length || 5} today`, picks.length ? `<div class="cards">${picks.map(pickCard).join("")}</div>
        ${(board.dropped || []).length ? `<p class="muted small" style="margin-top:14px">Out since this morning: ${board.dropped.map((x) => `${esc(x.symbol)} (${esc(statusWords((x.live || {}).state))})`).join(", ")}.</p>` : ""}` :
      empty("No picks yet. The list is built after the 16:20 ET pass and posted at 9:00, with check-ins at 12:00 and 15:00."), { alt: true, wide: true })
    + section("Morning watchlist", rows.length ? `<div class="cards">${(notPicked.length ? notPicked : rows).slice(0, 6).map(watchCard).join("")}</div>
        <p style="margin-top:18px"><a class="link-caps" href="#/watchlist">See all ${rows.length} names</a></p>` :
      empty("The morning watchlist is built at 8:30 ET on trading days. It will list names close to a trigger, with the plan and the dates that matter."), { wide: true })
    + section("Live updates", `<div aria-live="polite">${feed.length ? `<ul class="feed">${feed.map((e) => `<li><time datetime="${esc(e.at)}">${esc(shortDate(e.at))} ${esc(clock(e.at))}</time>
        <b>${esc(e.symbol)}</b> ${esc(e.text)} <span class="muted">Plan: ${esc(e.plan)}</span>${e.untested ? " " + pill("untested") : ""}</li>`).join("")}</ul>` :
      empty("Nothing has changed yet. Live changes on the picks, the watchlist and active pitches appear here; the first check-in is at 12:00 ET.")}</div>`, { alt: true })
    + section("Calendar", cal.length ? table("Earnings, IPOs and reviews for the names we are watching, next two weeks", ["Date", "What", "Name"],
      cal.map((r) => [esc(shortDate(r.date)), esc(r.kind === "ipo" ? "IPO" : r.kind), esc(r.text)])) :
      empty("Nothing on the calendar for the watched names in the next two weeks."))
    + `<div class="deep">` + section("Deep dive · regime", kv([["Regime", esc(st.regime)], ["Stress", num(st.stress, 2)], ["Crisis mode", st.crisis ? "on" : "off"],
        ["Flags on", esc(Object.entries(st.flags || {}).filter(([, v]) => v).map(([k]) => k).join(", ") || "none")],
        ["Volatility scale", num(st.vol_scale, 2)], ["Rule weights by book", w.map(([k, v]) => `${esc(BOOK[k] || k)} ${pct(v, 0)}`).join(" · ") || "—"]]), { alt: true })
    + section("Deep dive · every candidate", (d.candidates || []).length ? table("What each rule book wants to do at the next open (the rules decide; ratings are against our own open pitches)",
        ["Rating", "Ticker", "Book", "Close", "Stop", "Rank in book", "Conviction", "Checks passed"],
        d.candidates.map((c) => [esc(c.rating || c.action), `<a href="#/pitch/${encodeURIComponent(c.id)}">${esc(c.symbol)}</a>`, esc(BOOK[c.book] || c.book), usd(c.price), usd(c.stop),
          c.rank ? `${c.rank} of ${c.of}` : "—", c.conviction ? `${esc(c.conviction.bucket)} (${c.conviction.score})` : "—",
          `${Object.values(c.gates || {}).filter(Boolean).length} of ${Object.keys(c.gates || {}).length}`]), { numeric: [3, 4, 5, 7] }) : empty("No candidates from the last pass."), { wide: true })
    + section("Deep dive · run log", kv([["Daily pass", esc(ld.at ? `${ld.at} · ${ld.seconds}s · posted: ${ld.posted}` : "not run yet")],
        ["Screen", esc(d.screen && d.screen.day ? `${d.screen.covered} names covered, ${d.screen.eligible} eligible, ${d.screen.too_new} too new · ${d.screen.seconds}s` : "not built yet")],
        ["Peak memory", ld.memory_mb ? `${num(ld.memory_mb, 0)} MB` : "—"],
        ["Live layer", esc((d.live || {}).heartbeat ? `last look ${d.live.heartbeat.last_tick} · ${d.live.heartbeat.watched} watched · ${d.live.heartbeat.degraded || "healthy"}` : "no look yet today")]]), { alt: true })
    + `</div>`;
}

// ====================================================================== Watchlist
function watchCard(r) {
  const c = r.card || {}, s = c.strategy || {}, tl = c.timeline || {};
  const plan = s.steps === 0 ? `Buy at the open near ${usd(s.entry_ref)}${s.stop ? `, stop ${usd(s.stop)}` : ""}${s.t1 ? `, first target ${usd(s.t1)}` : ""}.` :
    s.book ? `Needs ${esc(s.trigger)}.` : esc(s.trigger || "");
  const when = [s.steps && tl.earliest ? ["Earliest trigger", shortDate(tl.earliest)] : s.steps === 0 && tl.earliest ? ["Enters", shortDate(tl.earliest)] : null,
    tl.expected_hold ? ["Expected hold", esc(tl.expected_hold)] : null, tl.review ? ["Next review", shortDate(tl.review)] : null,
    tl.earnings ? ["Earnings", shortDate(tl.earnings)] : null, tl.listing ? ["Listing", shortDate(tl.listing)] : null,
    tl.leaves ? ["Leaves the list", shortDate(tl.leaves)] : null];
  const to = c.pitch_id ? `#/pitch/${encodeURIComponent(c.pitch_id)}` : `#/explore/${encodeURIComponent(r.symbol)}`;
  return `<article class="card" data-status="${esc(r.status)}" data-rank="${esc(r.drank)}">
    <div class="card-top"><span class="sym">${rankBadge(r.drank)} ${esc(r.symbol)}</span><span class="num">${usd(c.price)} ${move(c.chg_1d)}</span></div>
    <h3><a href="${to}">${esc(c.name && c.name !== r.symbol ? c.name : (s.label || r.symbol))}</a></h3>
    <p class="status-line">${stateDot(r.status)}<span>${esc(statusWords(r.status))}${c.stale ? " (not in today's scan; still on the clock)" : ""}</span></p>
    ${gradeChips(c.grades)}
    <p><b>${esc(s.label || "")}${s.label ? ": " : ""}</b>${plan}${s.stop_note && s.steps ? ` <span class="muted">If it triggers: ${esc(s.stop_note)}.</span>` : ""}</p>
    ${kv(when)}
    <p class="muted small">${esc(c.why || "")}</p>
    ${(c.links || []).filter((l) => safeUrl(l.url)).slice(0, 2).map((l) => `<p class="small">${link(l.url, l.title || "source")}</p>`).join("")}
    <div class="deep small">${(s.passing || []).length ? `<p><b>Passing:</b> ${s.passing.map(esc).join("; ")}.</p>` : ""}
      ${(s.missing || []).length ? `<p><b>Still missing:</b> ${s.missing.map(esc).join("; ")}.</p>` : ""}
      ${s.conviction != null ? `<p><b>${term("Conviction")}:</b> ${esc(s.bucket)} (${s.conviction})${s.rr ? ` · ${term("Reward:risk")} ${num(s.rr, 2)}` : ""}</p>` : ""}
      <p><a href="#/explore/${encodeURIComponent(r.symbol)}">Indicators and every rule check</a> · added ${esc(r.added)}, updated ${esc(r.updated)}</p></div>
  </article>`;
}

export async function watchlist() {
  const d = await api("/api/stocks/watchlist");
  const m = d.meta || {}, all = d.rows || [], live = all.filter((r) => ["watching", "triggered", "in_play"].includes(r.status));
  const gone = all.filter((r) => ["exited", "dropped"].includes(r.status));
  const nod = m.name_of_the_day;
  after = (box) => {
    for (const b of $$("[data-filter]", box)) b.addEventListener("click", () => {
      const f = b.dataset.filter;
      for (const x of $$("[data-filter]", box)) { x.classList.toggle("on", x === b); x.setAttribute("aria-pressed", x === b ? "true" : "false"); }
      let n = 0;
      for (const c of $$("#wl-cards .card", box)) { const show = f === "all" || c.dataset.status === f || c.dataset.rank === f; c.hidden = !show; n += show; }
      $("#wl-count", box).textContent = `${plural(n, "name")} shown`;
    });
    $("#wl-scan", box)?.addEventListener("click", async () => {
      toast("Scanning. This takes a moment.");
      try { await post("/api/stocks/watchlist/scan", { post: false }); toast("Watchlist rebuilt (nothing was posted)."); location.reload(); } catch (e) { toast(e.message); }
    });
  };
  const filters = [["all", "All"], ["triggered", "Triggered"], ["watching", "Watching"], ["in_play", "In play"], ["1", "Rank 1"], ["2", "Rank 2"], ["3", "Rank 3"], ["4", "Rank 4"]];
  return hero(`Morning watchlist · ${m.day ? shortDate(m.day) : "not built yet"}`, "Names worth watching, and what each one still needs.",
    esc(m.changed_line ? `What changed: ${m.changed_line}.` : "The list is built at 8:30 ET every trading day from the whole-market screen."), { short: true })
    + (nod ? `<section class="sec band"><div class="grid"><span class="caps eyebrow" style="color:#b8b1ad">Name of the day</span><div class="body"><h2>${esc(nod.symbol)}</h2>
        <p style="margin-top:14px;font-size:1rem">${esc(nod.text)}</p><p><a class="link-caps" style="color:#e4d7d0" href="#/explore/${encodeURIComponent(nod.symbol)}">Open ${esc(nod.symbol)}</a></p></div></div></section>` : "")
    + section("The list", `<div class="toolbar" role="group" aria-label="Filter the watchlist">${filters.map(([k, t], i) =>
        `<button class="pill${i ? "" : " on"}" data-filter="${k}" aria-pressed="${i ? "false" : "true"}" style="cursor:pointer;background:none;color:inherit">${t}</button>`).join("")}
        <span id="wl-count" class="muted small" aria-live="polite">${plural(live.length, "name")} shown</span>
        ${isAdmin() ? `<button class="btn" id="wl-scan" style="margin-left:auto">Scan now (no post)</button>` : ""}</div>
      ${live.length ? `<div class="cards" id="wl-cards">${live.map(watchCard).join("")}</div>` :
        empty("No names yet. The first list appears after the next 8:30 ET scan; it needs one daily pass (16:20 ET) to have run first.")}`, { wide: true })
    + (gone.length ? section("Recently off the list", table("Names that left the list", ["Ticker", "What happened", "Added", "Left"],
        gone.slice(0, 30).map((r) => [esc(r.symbol), esc(statusWords(r.status)), esc(r.added), esc(r.updated)])), { alt: true }) : "")
    + `<div class="deep">` + section("Deep dive · does the rank mean anything?", rankScore(d.score), { alt: !gone.length }) + `</div>`;
}
const rankScore = (s) => (!s ? empty("No score yet.") : table(`Names by the rank they joined with: how often a pitch followed within 15 days, and the ${s.horizon_sessions}-session return from the day they were added`,
  ["Rank when added", "Names", "Went on to a pitch", "Avg return", "Avg vs SPY"],
  [["1–2", s.rank_1_2], ["3", s.rank_3], ["4–5", s.rank_4_5]].map(([k, v]) => [k, v.n, v.trigger_rate == null ? "—" : pct(v.trigger_rate, 0), move(v.avg_ret), move(v.avg_excess)]), { numeric: [1, 2, 3, 4] }));

// ====================================================================== Pitches
export async function pitches() {
  const d = await api("/api/stocks/pitches");
  const rows = d.rows || [], open = rows.filter((r) => r.status !== "closed"), closed = rows.filter((r) => r.status === "closed");
  const c = d.counts || {};
  const row = (r) => { const t = r.track || {}; return [`<a href="#/pitch/${encodeURIComponent(r.id)}"><b>${esc(r.symbol)}</b></a>`, esc(r.rating || "BUY"), esc(BOOK[r.book] || r.book), esc(shortDate(r.as_of)),
    `${stateDot(r.status)} ${esc(r.status === "closed" ? `Closed: ${r.close_reason || ""} ${r.closed_at ? "(" + shortDate(r.closed_at) + ")" : ""}` : statusWords(r.status))}`,
    usd(r.entry_open), move(t.ret), move(t.excess_spy), t.r == null ? "—" : num(t.r, 2) + "R"]; };
  const heads = ["Ticker", "Rating", "Book", "Pitched", "Status", "Tested entry", "Return", "vs SPY", term("R")];
  return hero("Pitches", "Every idea the desk has pitched.", esc(`${c.new || 0} entering at the next open, ${c.active || 0} active, ${c.closed || 0} closed. `
      + "A pitch is an idea with a plan, not a position: returns are measured from the tested entry, the open after the signal."), { short: true })
    + section("Open pitches", open.length ? table("New and active pitches", heads, open.map(row), { numeric: [5, 6, 7, 8] }) :
      empty("No open pitches. New ones appear after the 16:20 ET pass on any day a rule triggers."), { wide: true })
    + section("Closed pitches", closed.length ? table("Closed at a target, the stop, the time limit or a rule exit", heads, closed.map(row), { numeric: [5, 6, 7, 8] }) :
      empty("Nothing has closed yet."), { wide: true, alt: true });
}

export async function pitch([id]) {
  if (!id) return pitches();
  const d = await api(`/api/stocks/pitch/${encodeURIComponent(id)}`);
  const p = d.pack || {}, row = d.row || {}, rec = p.rec || {}, pl = p.plan || {}, tg = pl.targets || {}, ent = pl.entry || {}, risk = pl.risk || {}, conv = rec.conviction || {};
  const sym = p.symbol || row.symbol, name = p.name || row.name || sym, rating = rec.rating || row.rating || "BUY";
  const stat = row.status ? (row.status === "closed" ? `Closed: ${row.close_reason} on ${shortDate(row.closed_at)} at ${usd(row.close_price)}` : statusWords(row.status)) : "Not in the pitch book";
  const tr = row.track || {};
  const steps = [["Pitched", shortDate(p.as_of || row.as_of), "signal from that close"],
    ["Tested entry", row.entry_open ? usd(row.entry_open) : `the ${shortDate(ent.date)} open`, ent.zone ? `zone ${usd(ent.zone[0])}–${usd(ent.zone[1])}` : `ref ${usd(p.price || row.price)}`],
    (p.stop || (row.plan || {}).stop) ? ["Stop", usd(p.stop || row.plan.stop), risk.stop_pct != null ? pct(risk.stop_pct) + " from entry" : "the exit if touched"] : null,
    tg.t1 ? ["First target", usd(tg.t1), tg.t1_kind || ""] : null, tg.t2 ? ["Stretch target", usd(tg.t2), tg.t2_kind || ""] : null,
    (pl.max_hold || tg.review_date) ? [tg.review_date ? "Next review" : "Time limit", tg.review_date ? shortDate(tg.review_date) : `${pl.max_hold} sessions`, "then it closes or is reviewed"] : null,
    risk.pct_of_fund != null ? ["Suggested size", `${pct(risk.pct_of_fund)} of the fund`, risk.shares ? `${risk.shares} shares` : esc(risk.bound || "")] : null].filter(Boolean);
  const v = d.votes || {};
  const noteName = (() => { try { return localStorage.getItem("daisy-name") || ""; } catch { return ""; } })();
  after = (box) => {
    $("#note-form", box)?.addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = new FormData(e.target), nm = String(f.get("name") || "").trim();
      try {
        await post(`/api/stocks/notes/${encodeURIComponent(d.id)}`, { name: nm, vote: f.get("vote") || null, text: f.get("text") });
        try { localStorage.setItem("daisy-name", nm); } catch { /* private window */ }
        toast("Saved."); location.hash = `#/pitch/${encodeURIComponent(d.id)}?keep`; location.hash = `#/pitch/${encodeURIComponent(d.id)}`;
      } catch (x) { toast(x.message); }
    });
  };
  const val = p.valuation, peers = p.peers || [];
  return hero(`${rating} · ${p.book_label || BOOK[row.book] || ""} · pitched ${shortDate(p.as_of || row.as_of)}`, `${esc(sym)}${name !== sym ? ` <span class="muted">— ${esc(name)}</span>` : ""}`,
      esc(rec.one_liner || p.what_happened || ""), { short: true, extra: `<div class="chips" style="margin-top:18px;align-items:center">${rankBadge(rec.daisy_rank ?? row.drank)}
        ${pill(stat, row.status === "active")} ${conv.bucket ? pill(`${conv.bucket} conviction (${conv.score})`) : ""}
        ${d.pack ? `<a class="btn" href="/api/stocks/deck/${encodeURIComponent(d.id)}">Download deck (.pptx)</a>` : ""}</div>
        <div style="margin-top:14px">${gradeChips(p.grades)}</div>` })
    + section("The plan", `<ol class="timeline">${steps.map(([k, val_, sub]) => `<li><span class="caps muted">${esc(k)}</span><span class="v num">${val_}</span><span class="muted small">${esc(sub)}</span></li>`).join("")}</ol>
        ${tr.ret != null ? `<p style="margin-top:22px">Since the tested entry: ${move(tr.ret)}; SPY over the same days ${move(tr.spy)}${tr.sector != null ? `; ${esc(tr.sector_etf)} ${move(tr.sector)}` : ""}${tr.r != null ? ` · ${num(tr.r, 2)}${term("R")}` : ""}.</p>` : ""}
        ${(ent.refinements || []).length ? `<p class="muted small">Untested refinements (scored apart): ${ent.refinements.map((x) => esc(x.text)).join(" ")}</p>` : ""}`, { alt: true })
    + ((p.flags || []).length ? section("Flags", `<ul class="feed">${p.flags.map((f) => `<li>⚑ ${esc(f.text)} ${safeUrl(f.url) ? "· " + link(f.url, "source") : ""}</li>`).join("")}</ul>`) : "")
    + section("Lifecycle", (d.history || []).length ? `<ul class="feed">${d.history.map((h) => `<li><time datetime="${esc(h.at)}">${esc(shortDate(h.at))}</time><b>${esc(h.status)}</b> ${esc(h.note || "")}</li>`).join("")}</ul>` :
      empty("This pitch has no recorded history yet."))
    + section("The memo", d.memo ? `<div class="memo">${md(d.memo)}</div>` : empty("No memo was saved for this pitch (it predates the pitch format)."), { alt: true })
    + section("Team notes", `<p>${pill(`👍 ${v.up || 0}`)} ${pill(`👎 ${v.down || 0}`)} ${pill(`Hold ${v.hold || 0}`)} <span class="muted small">one vote per name; the latest counts</span></p>
        ${(d.notes || []).length ? `<ul class="feed">${d.notes.map((n) => `<li><time datetime="${esc(n.at)}">${esc(shortDate(n.at))}</time><b>${esc(n.name)}</b> ${n.vote ? pill(n.vote) : ""} ${esc(n.text || "")}</li>`).join("")}</ul>` :
          `<p class="muted">No notes yet. Leave the first one: it builds the fund's decision log.</p>`}
        <form id="note-form" class="stack" style="max-width:560px;margin-top:20px">
          <div><label for="nn">Your name</label><input id="nn" name="name" type="text" maxlength="40" required value="${esc(noteName)}" autocomplete="nickname"></div>
          <fieldset style="border:0;padding:0;margin:0"><legend class="muted" style="margin-bottom:6px">Your vote</legend>
            ${[["up", "👍 Agree"], ["down", "👎 Disagree"], ["hold", "Hold off"], ["", "No vote"]].map(([k, t]) => `<label style="display:inline-flex;gap:6px;margin-right:14px;color:inherit"><input type="radio" name="vote" value="${k}"${k === "" ? " checked" : ""}> ${t}</label>`).join("")}</fieldset>
          <div><label for="nt">Note (optional)</label><textarea id="nt" name="text" maxlength="600"></textarea></div>
          <div><button class="btn">Save note</button></div></form>`)
    + `<div class="deep">`
    + section("Deep dive · conviction", (conv.breakdown || []).length ? table("Every input to the conviction score", ["Input", "Points"], conv.breakdown.map((x) => [esc(x.input), (x.points > 0 ? "+" : "") + num(x.points, 0)]), { numeric: [1] }) : empty("No conviction breakdown."), { alt: true })
    + section("Deep dive · valuation and comps", (val ? `<pre>${esc(JSON.stringify(val, null, 1))}</pre>` : empty("Valuation: not in data for this name."))
        + (peers.length ? table("Sector peers used for the comps", ["Peer", "P/E", term("Forward P/E"), "EPS"], peers.map((x) => [esc(x.symbol || "—"), num(x.pe, 1), num(x.forward_pe, 1), num(x.eps, 2)]), { numeric: [1, 2, 3] }) : ""))
    + section("Deep dive · write-up check and pack", `<details><summary>Write-up validator log</summary><pre>${esc(JSON.stringify(d.writeup || "template text: no LLM write-up on this pitch", null, 1))}</pre></details>
        <details><summary>Pack JSON (every number behind this page, with its as-of)</summary><pre>${esc(JSON.stringify(d.pack_json || row, null, 1))}</pre></details>`, { alt: true })
    + `</div>`;
}

// ====================================================================== Scorecard
const cut = (caption, obj, label) => {
  const keys = Object.keys(obj || {});
  return keys.length ? table(caption, [label, "Pitches", "Open", "Avg return", "vs SPY", "vs sector", "Winners", "Beat SPY"],
    keys.map((k) => { const a = obj[k]; return [esc(BOOK[k] || k), a.n, a.open, move(a.avg_ret), move(a.avg_excess_spy), move(a.avg_excess_sector), a.win_rate == null ? "—" : pct(a.win_rate, 0), a.beat_spy == null ? "—" : pct(a.beat_spy, 0)]; }),
    { numeric: [1, 2, 3, 4, 5, 6, 7] }) : empty("Not enough pitches to cut this way yet.");
};
export async function scorecard() {
  const d = await api("/api/stocks/scorecard");
  const t = d.track || {}, a = t.all || {}, plan = d.plan || {}, pa = plan.all || {};
  const said = a.marked ? `${plural(a.n, "pitch")} so far: ${a.open} open and ${a.closed} closed. Measured from the tested entry, the average pitch is ${a.avg_ret >= 0 ? "up" : "down"} ${pct(Math.abs(a.avg_ret))}, `
      + `${a.avg_excess_spy == null ? "with no SPY comparison yet" : (a.avg_excess_spy >= 0 ? "ahead of" : "behind") + " SPY by " + pct(Math.abs(a.avg_excess_spy))}. `
      + `${a.beat_spy == null ? "" : pct(a.beat_spy, 0) + " of them beat SPY."}` :
    a.n ? `${plural(a.n, "pitch")} so far, none old enough to measure. A pitch is marked once the session after its signal has traded.` : "No pitches yet. The record starts with the first one.";
  const stat = (k, v, sub) => `<div class="stat"><span class="caps muted">${k}</span><span class="v num">${v}</span><span class="muted small">${sub}</span></div>`;
  const hit = (x) => (x == null ? "—" : pct(x, 0));
  const ps = (o) => [o.n, o.resolved, hit(o.t1_rate), hit(o.t2_rate), hit(o.stop_first_rate), hit(o.time_rate), o.avg_r_now == null ? "—" : num(o.avg_r_now, 2)];
  const f = d.forming || {};
  return hero("Scorecard", "How have our pitches done?", esc(said), { short: true })
    + section("The record", `<div class="stats">${stat("Pitches", a.n ?? 0, `${a.open ?? 0} open · ${a.closed ?? 0} closed`)}
        ${stat("Average return", a.avg_ret == null ? "—" : (a.avg_ret > 0 ? "+" : "") + pct(a.avg_ret), "from the tested entry")}
        ${stat("Against SPY", a.avg_excess_spy == null ? "—" : (a.avg_excess_spy > 0 ? "+" : "") + pct(a.avg_excess_spy), "same days, same entry")}
        ${stat("Against its sector", a.avg_excess_sector == null ? "—" : (a.avg_excess_sector > 0 ? "+" : "") + pct(a.avg_excess_sector), "the sector ETF")}
        ${stat("Reached T1", hit(pa.t1_rate), `of ${pa.resolved ?? 0} resolved plans`)}
        ${stat("Stopped out first", hit(pa.stop_first_rate), "before any target")}</div>
        <p class="muted small" style="margin-top:18px">There is no portfolio behind these numbers: each pitch is one idea, measured on its own from the open after its signal.</p>`, { alt: true })
    + section("By conviction", cut("High, medium and low conviction pitches", t.by_bucket, term("Conviction")))
    + section("By DAISY Rank", cut("By the rank each pitch carried when it was made", t.by_rank, term("DAISY Rank")), { alt: true })
    + section("By rule book", cut("Which rule produced the pitch", t.by_book, "Book"))
    + section("By sector", cut("By the company's sector", t.by_sector, "Sector"), { alt: true })
    + section("The watchlist", rankScore(d.watchlist))
    + `<div class="deep">`
    + section("Deep dive · plan marks", table("How the plans resolved, per book", ["Book", "Pitches", "Resolved", "T1", "T2", "Stop first", "Time", "Avg R now"],
        [["All", ...ps(pa)], ...Object.entries(plan.by_book || {}).map(([k, o]) => [esc(BOOK[k] || k), ...ps(o)])], { numeric: [1, 2, 3, 4, 5, 6, 7] }), { alt: true })
    + section("Deep dive · tested and untested", table("Untested refinements and flags are scored apart from the tested rule", ["What", "Status", "Count", "Result"],
        [["The rule as back-tested: enter at the next open", "tested", (plan.tested || {}).n ?? 0, `T1 ${hit((plan.tested || {}).t1_rate)} · stop first ${hit((plan.tested || {}).stop_first_rate)}`],
         ["Skip if it opens 1 ATR above the close or below the stop", "untested", (plan.refined || {}).n ?? 0, `T1 ${hit((plan.refined || {}).t1_rate)} · stop first ${hit((plan.refined || {}).stop_first_rate)}`],
         ["Setup forming: a partial bar that would trigger", "untested", f.n ?? 0, f.n ? `${f.confirmed} confirmed at the close (${hit(f.rate)})` : "none recorded yet"],
         ...Object.entries(t.by_sentiment || {}).map(([k, o]) => [`News sentiment was ${esc(k)} when pitched`, "untested", o.n, `avg ${o.avg_ret == null ? "—" : pct(o.avg_ret)} · vs SPY ${o.avg_excess_spy == null ? "—" : pct(o.avg_excess_spy)}`])])
        + `<p class="muted small">A sentiment rule can gate an entry only after the research study shows it helps out of sample. Until then it is tracked here.</p>`)
    + section("Deep dive · every pitch", table("The 200 most recent pitches", ["Pitch", "Status", "Entry", "Return", "vs SPY", "R", "Plan outcome"],
        (d.pitches || []).map((r) => [`<a href="#/pitch/${encodeURIComponent(r.id)}">${esc(r.id)}</a>`, esc(r.status === "closed" ? `closed: ${r.close_reason}` : r.status), usd(r.entry_open),
          move((r.track || {}).ret), move((r.track || {}).excess_spy), (r.track || {}).r == null ? "—" : num(r.track.r, 2), esc((r.plan_mark || {}).outcome || "—")]), { numeric: [2, 3, 4, 5] })
        + (d.migration && d.migration.rows_in_file ? `<p class="muted small">Carried over from the old pitches.json: ${d.migration.carried_over} of ${d.migration.rows_in_file} rows.</p>` : ""), { wide: true, alt: true })
    + `</div>`;
}

// ====================================================================== Explore
function nameCard(c) {
  const r = c.row || {}, s = c.sentiment || {};
  const facts = [["Close", `${usd(r.close)} ${move(r.chg_1d)}`], ["Sector", esc(r.sector || "not in data")],
    ["Pitch eligible", r.eligible ? "yes" : `no: ${esc(r.why_not || "")}`], ["History", `${r.sessions ?? "—"} sessions${r.too_new ? " (too new for the tested rules)" : ""}`],
    ["Themes", (c.themes || []).map((t) => esc(t.label)).join(", ") || "none"]];
  const sentLine = s.level == null ? "No scored news in the last three days." :
    `News sentiment ${num(s.level, 2)} over ${plural(s.n, "article")} in three days${s.change != null ? `, ${s.change >= 0 ? "up" : "down"} ${num(Math.abs(s.change), 2)} against its 20-day baseline` : ""}${s.volume_ratio ? `; today's news flow is ${num(s.volume_ratio, 1)}× normal` : ""}.`;
  const ind = ["close", "ema50", "ema200", "sma200", "sma5", "rsi14", "min_rsi14_5", "rsi2", "mom_12_1", "mom126", "vol252", "atr14", "prev_high", "macd_hist", "macd_hist_prev", "adv20_usd", "dist_200", "mom_score", "mom_rank", "mom_pct", "trend_pct", "chg_5d", "chg_21d"];
  return hero(`Explore · as of the ${shortDate(c.as_of)} close`, `${esc(c.symbol)}${r.name && r.name !== c.symbol ? ` <span class="muted">— ${esc(r.name)}</span>` : ""}`, esc(c.verdict), { short: true,
      extra: `<div class="chips" style="margin-top:18px;align-items:center">${c.drank ? rankBadge(c.drank) + " " + pill(c.rank_words || "") : pill("Not ranked")}</div><div style="margin-top:14px">${gradeChips(c.grades)}</div>
        <p style="margin-top:18px"><a class="link-caps" href="#/explore">Search another name</a></p>` })
    + section("At a glance", kv(facts), { alt: true })
    + section("Closest setups", (c.setups || []).length ? `<ul class="feed">${c.setups.map((x) => `<li><b>${esc(BOOK[x.book] || x.book)}</b> — ${x.steps === 1 ? "one step" : x.steps + " steps"} away. Needs ${esc(x.trigger)}.
        ${x.stop_note ? `<span class="muted">${esc(x.stop_note)}.</span>` : ""}</li>`).join("")}</ul>` : empty("No rule is close to a trigger on this name."))
    + section("News and sentiment", `<p>${esc(sentLine)}</p>${spark((c.sentiment_series || []).map((x) => ({ y: x.score })), { label: `Daily news sentiment for ${c.symbol}, last 30 days` })}
        ${(c.news || []).length ? `<ul class="feed">${c.news.map((n) => `<li><time datetime="${esc(n.at)}">${esc(shortDate(n.at))}</time>${link(n.url, n.title)} <span class="muted small">${esc(n.source || "")}${n.kind === "filing" ? " · filing" : ` · score ${num(n.sent, 2)}`}</span></li>`).join("")}</ul>` :
          empty("No stored headlines for this name yet. News is collected for watched names and the day's movers.")}`, { alt: true })
    + ((c.pitches || []).length ? section("Our pitches on it", table("Every pitch the desk has made on this name", ["Pitch", "Rating", "Status", "Return"],
        c.pitches.map((p) => [`<a href="#/pitch/${encodeURIComponent(p.id)}">${esc(shortDate(p.as_of))} · ${esc(BOOK[p.book] || p.book)}</a>`, esc(p.rating), esc(p.status === "closed" ? `closed: ${p.close_reason}` : p.status), move((p.track || {}).ret)]), { numeric: [3] })) : "")
    + `<div class="deep">`
    + section("Deep dive · rule by rule", (c.gates || []).map((g) => table(`${BOOK[g.book] || g.book}: ${g.passes ? "every check passes" : "not every check passes"}`, ["Check", "Result"],
        g.checks.map((x) => [esc(x.check), x.pass == null ? '<span class="muted">not in data</span>' : x.pass ? '<span class="up">✓ passes</span>' : '<span class="down">✗ fails</span>']))).join("<br>"))
    + section("Deep dive · indicators", table("The numbers the rules read, from the last close", ["Indicator", "Value"], ind.map((k) => [esc(k), num(r[k], Math.abs(r[k] || 0) >= 1000 ? 0 : 4)]), { numeric: [1] }), { alt: true })
    + section("Deep dive · sentiment series", (c.sentiment_series || []).length ? table("Daily news score and article count", ["Day", "Score", "Articles"], c.sentiment_series.map((x) => [esc(x.day), num(x.score, 2), x.n]), { numeric: [1, 2] }) : empty("No sentiment history yet."))
    + `</div>`;
}
const exploreRows = (rows) => (rows.length ? table("Matching names from the research universe", ["Ticker", "Company", "Close", "1 day", "Rank", "Pitch eligible", "Grades"],
  rows.map((r) => [`<a href="#/explore/${encodeURIComponent(r.symbol)}"><b>${esc(r.symbol)}</b></a>`, esc(r.name || "—"), usd(r.close), move(r.chg_1d), r.drank ? rankBadge(r.drank) : "—",
    r.eligible ? "yes" : `<span class="muted">no: ${esc(r.why_not || "")}</span>`, gradeChips(r.grades)]), { numeric: [2, 3] }) : empty("No name matches that. Try a ticker (NVDA) or part of a company name."));

export async function explore([sym]) {
  if (sym) return nameCard(await api(`/api/stocks/name/${encodeURIComponent(sym)}`));
  const d = await api("/api/stocks/explore?limit=12&only=eligible");
  const cov = d.coverage || {}, ls = ((d.listings || {}).rows || []).slice(0, 30);
  after = (box) => {
    const inp = $("#ex-q", box), out = $("#ex-out", box);
    let timer = null, n = 0;
    const run = async () => {
      const q = inp.value.trim(), mine = ++n;
      try { const r = await api(`/api/stocks/explore?limit=30&q=${encodeURIComponent(q)}${q ? "" : "&only=eligible"}`); if (mine === n) { out.innerHTML = exploreRows(r.rows || []); wireTips(out); } }
      catch (e) { if (mine === n) out.innerHTML = empty(e.message); }
    };
    inp.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(run, 220); });
    $("#ex-form", box).addEventListener("submit", (e) => { e.preventDefault(); const q = inp.value.trim().toUpperCase(); if (/^[A-Z0-9.^-]{1,12}$/.test(q)) location.hash = `#/explore/${encodeURIComponent(q)}`; });
  };
  return hero("Explore", "Search any US stock.", esc(cov.day ? `${cov.covered.toLocaleString()} names researched as of the ${shortDate(cov.day)} close; ${cov.eligible.toLocaleString()} clear the floor to be pitched, `
      + `${cov.too_new.toLocaleString()} are too new for the tested rules. Every one has a card that says why it is or isn't pitched.` : "The research universe is built by the daily pass (16:20 ET). It has not run yet."), { short: true,
      extra: `<form id="ex-form" role="search" style="margin-top:22px"><label for="ex-q" class="caps">Ticker or company</label><input id="ex-q" type="search" placeholder="NVDA, or part of a name" autocomplete="off" spellcheck="false"></form>` })
    + section("Results", `<div id="ex-out" aria-live="polite">${exploreRows(d.rows || [])}</div>`, { wide: true })
    + section("IPOs and new listings", ls.length ? table("Too new for the tested rules: watched and researched, not rule-pitched", ["Date", "Company", "Ticker", "Exchange", "Price", "Status"],
        ls.map((x) => [esc(shortDate(x.date)), esc(x.name), x.symbol ? `<a href="#/explore/${encodeURIComponent(x.symbol)}">${esc(x.symbol)}</a>` : "—", esc(x.exchange || "—"), esc(x.price ?? "—"), esc(x.status || "—")])) :
      empty("No upcoming IPOs or new listings on file. The calendar is read during the 8:30 ET scan and needs a Finnhub key."), { alt: true });
}

// ====================================================================== Research
export async function research() {
  const d = await api("/api/stocks/research");
  const rep = d.report, v = (rep || {}).verdict || {}, checks = Object.entries(v.checks || {});
  const passed = checks.filter(([, c]) => c.pass).length;
  const said = !rep ? "No study has been run yet. The weekly study replays the rule books over ten years, removes one rule at a time, and tests them walk-forward."
    : `The latest study (${String(rep.at || "").slice(0, 10)}) says: ${v.overall || "no verdict"}. ${checks.length ? `${passed} of ${checks.length} pass bars were met.` : ""} Findings are proposals: nothing here changes a rule on its own.`;
  return hero("Research", "Do the rules still earn their place?", esc(said), { short: true })
    + section("Pass bars", checks.length ? table("Each bar the rules are held to", ["Bar", "Result"], checks.map(([k, c]) => [esc(k), c.pass == null ? '<span class="muted">not in data</span>' : c.pass ? '<span class="up">✓ met</span>' : '<span class="down">✗ not met</span>'])) :
      empty("The pass bars appear after the first study."), { alt: true })
    + section("Known limits", `<ul class="feed"><li><b>Survivorship.</b> Outside the S&amp;P 500 core, the market list is today's listed names: companies that were delisted are missing, which flatters any backtest on them.</li>
        <li><b>News sentiment is untested as a gate.</b> It informs conviction and raises flags. Pitches are tracked by sentiment on the Scorecard so the team can see whether it adds edge before anyone proposes a rule.</li>
        <li><b>History depth.</b> Names outside the core have about two years of bars (the free data plan), so the live replay for them is two years.</li></ul>`)
    + `<div class="deep">` + section("Deep dive · the full report", d.markdown ? `<div class="memo">${md(d.markdown)}</div>` : empty("No report yet."), { wide: true, alt: true }) + `</div>`;
}

// ====================================================================== Settings (read-only for viewers)
export async function settings() {
  const [s, h] = await Promise.all([api("/api/stocks/settings"), api("/api/stocks/holdings")]);
  const yes = (b) => (b ? '<span class="up">✓ on</span>' : '<span class="muted">off</span>'), has = (b) => (b ? '<span class="up">✓ present</span>' : '<span class="muted">not set</span>');
  const f = s.features || {}, k = s.keys || {}, sc = s.schedule || {}, scr = s.screen || {};
  after = (box) => {
    const run = (id, path, body, ok) => $(id, box)?.addEventListener("click", async () => { toast("Working…"); try { await post(path, body); toast(ok); } catch (e) { toast(e.message); } });
    run("#st-scan", "/api/stocks/watchlist/scan", { post: false }, "Watchlist rebuilt. Nothing was posted.");
    run("#st-tick", "/api/stocks/live/tick", {}, "Live check done. Nothing was posted.");
    run("#st-score", "/api/stocks/score", {}, "Pitches marked.");
    $("#st-out", box)?.addEventListener("click", async () => { await post("/api/auth/logout"); location.reload(); });
    $("#st-in", box)?.addEventListener("click", () => document.dispatchEvent(new CustomEvent("daisy:signin")));
    $("#hold-form", box)?.addEventListener("submit", async (e) => {
      e.preventDefault();
      const rows = $("#hold", box).value.split("\n").map((l) => l.trim().split(/[\s,]+/)).filter((p) => p[0]).map(([symbol, weight]) => ({ symbol, weight: parseFloat(weight) }));
      try { const r = await post("/api/stocks/holdings", { holdings: rows }); toast(`Saved ${r.holdings.length} holdings. They are used from the next daily pass.`); } catch (x) { toast(x.message); }
    });
  };
  return hero("Settings", "What is switched on, and when things run.", esc(`You are signed in as ${state.role === "admin" ? "an admin" : "a viewer (read-only)"}. Switches live in the server's .env file; this page shows them and never shows a key.`), { short: true })
    + section("Switches", kv([["Stock desk schedule", yes(s.enabled)], ["Discord webhook", has(s.webhook)], ["Morning watchlist (dashboard)", yes(f.watch)],
        ["Morning watchlist post", yes(f.watch_post)], ["Live watch (dashboard)", yes(f.live)], ["Live alerts post", yes(f.live_alerts)], ["News and filings", yes(f.news)], ["Weekly recap post", yes(f.weekly_recap)],
        ["Post tag", esc(s.tag || "none")]]), { alt: true })
    + section("Schedule (ET)", kv([["Daily pass", esc(sc.daily)], ["Top picks and check-ins", esc(sc.board)], ["Morning scan / post", `${esc(sc.watch_scan)} / ${esc(sc.watch_post)}`],
        ["Live look every", `${esc(sc.live_seconds)} seconds`], ["News re-read every", `${esc(sc.news_minutes)} minutes`], ["Weekly recap", `Fridays ${esc(sc.weekly_recap)}`]]))
    + section("Keys present", kv([["Polygon (whole-market bars)", has(k.polygon)], ["Finnhub (quotes, news, calendars)", has(k.finnhub)], ["Anthropic (optional write-ups)", has(k.anthropic)]]), { alt: true })
    + section("Research universe", kv([["Screen as of", esc(scr.day || "not built yet")], ["Names researched", num(scr.covered, 0)], ["Pitch eligible", num(scr.eligible, 0)],
        ["Too new for the tested rules", num(scr.too_new, 0)], ["In the rule panel", num(scr.in_rule_panel, 0)], ["Screen time", scr.seconds != null ? `${scr.seconds}s` : "—"],
        ["Fund size for share counts", s.fund_equity_set ? "set" : "not set: sizes are a % of the fund"]]))
    + section("Fund holdings", `<p>Used only for the "Why this fits our fund" section of each pitch. ${h.provided ? `${h.holdings.length} holdings on file.` : "None provided: pitches say “holdings not provided”."} The list stays on the server and is never written to the public repo.</p>`
        + (isAdmin() ? `<form id="hold-form" class="stack" style="max-width:420px"><label for="hold">One per line: ticker, then its weight in percent</label>
            <textarea id="hold" rows="8" spellcheck="false" placeholder="NVDA 8&#10;MSFT 6">${esc(h.holdings.map((x) => `${x.symbol} ${+(x.weight * 100).toFixed(2)}`).join("\n"))}</textarea><div><button class="btn">Save holdings</button></div></form>` :
          (h.provided ? table("The fund's holdings", ["Ticker", "Weight"], h.holdings.map((x) => [esc(x.symbol), pct(x.weight)]), { numeric: [1] }) : "")), { alt: true })
    + section("Session", `<div class="chips">${state.protected ? (state.role === "none" ? `<button class="btn" id="st-in">Sign in</button>` : `<button class="btn" id="st-out">Sign out</button>`) : `<span class="muted">This dashboard has no tokens set: anyone who can reach it can use it.</span>`}
        <a class="btn" href="/classic">Classic terminal</a><a class="btn" href="#/glossary">Glossary</a></div>`)
    + (isAdmin() ? section("Run now", `<div class="chips"><button class="btn" id="st-scan">Morning scan (no post)</button><button class="btn" id="st-tick">Live check (no post)</button><button class="btn" id="st-score">Mark pitches</button></div>
        <p class="muted small" style="margin-top:12px">Buttons here never post to Discord.</p>`, { alt: true }) : "")
    + `<div class="deep">` + section("Deep dive · status", `<pre>${esc(JSON.stringify({ last_daily: s.last_daily, live: s.live, news: s.news, market: s.market, universe: s.universe, pitch_book: s.pitch_book, errors: s.errors }, null, 1))}</pre>`) + `</div>`;
}

export async function glossary() {
  const g = state.glossary || {};
  return hero("Glossary", "Every term, in plain words.", "The same definitions appear when you hover or tab to a dotted word anywhere in DAISY, and at the bottom of every memo.", { short: true })
    + section("Terms", kv(Object.entries(g.terms || {}).map(([k, v]) => [`<b>${esc(k)}</b>`, esc(v)])), { alt: true })
    + section("DAISY Rank", kv(Object.entries(g.ranks || {}).map(([k, v]) => [rankBadge(k), esc(v)])));
}
