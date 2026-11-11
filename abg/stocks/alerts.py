"""The live layer (docs/22.5): watch the picks, the watchlist and every active pitch, and report *changes*.

The 16:20 pass stays the source of entries. This layer never makes one. It compares a live quote with a
plan that already exists and says, in one sentence, what just changed and what the plan says to do.

    targets     who is watched: the day's top picks, active pitches, watchlist names
    detect      one quote against one target -> the events that are new since the last look
    Gate        cooldown per name, one alert per (name, event, window), a cap per tick: the reason a
                reconnect storm or a jumpy quote cannot flood Discord
    embed       several events in one post

"Setup forming" is the one event about a name that has *not* triggered: the partial bar would trigger a
rule if the session closed here. It is labelled untested, it is never a buy, and its hit rate is kept
apart in the scorecard so the team can judge whether it deserves promotion.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

KINDS = ("zone_enter", "zone_leave", "near_stop", "stop", "t1", "t2", "gap", "rel_volume", "forming",
         "news", "sentiment", "theme", "earnings")
ALWAYS = ("stop", "t1", "t2")                       # never held back by the cooldown
TITLES = {"zone_enter": "back in the entry zone", "zone_leave": "left the entry zone", "near_stop": "close to its stop",
          "stop": "touched its stop", "t1": "reached T1", "t2": "reached T2", "gap": "gapped at the open",
          "rel_volume": "unusual volume", "forming": "setup forming (untested)", "news": "news",
          "sentiment": "sentiment swing", "theme": "theme shock", "earnings": "earnings inside the hold window"}
DEFAULTS = {"near_stop_atr": 0.5, "gap_atr": 1.0, "rel_volume": 2.0, "cooldown_min": 30, "max_per_tick": 8,
            "max_per_hour": 20, "quiet": True, "kinds": {k: True for k in KINDS}}


def alert_cfg(cfg: dict | None) -> dict:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for k, v in ((cfg or {}).get("alerts") or {}).items():
        if k == "kinds" and isinstance(v, dict):
            out["kinds"].update(v)
        else:
            out[k] = v
    return out


def _f(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _px(x) -> str:
    return "—" if x is None else f"${x:,.2f}"


def session_fraction(now: datetime) -> float:
    """How much of the 9:30-16:00 session has passed (0..1)."""
    mins = (now.hour * 60 + now.minute) - 570
    return max(0.0, min(1.0, mins / 390.0))


def in_session(now: datetime) -> bool:
    return now.weekday() < 5 and 570 <= now.hour * 60 + now.minute < 960


# --------------------------------------------------------------------------- who is watched
def targets(board: dict | None, pitches: list[dict], watch: list[dict], screen: dict | None = None,
            today: str | None = None) -> dict[str, dict]:
    """symbol -> what to compare its quote with. A pick beats a pitch beats a watchlist name."""
    sc = screen or {}
    out: dict[str, dict] = {}
    for r in watch or []:
        c = r.get("card") or {}
        st = c.get("strategy") or {}
        if r.get("status") != "watching" or not st.get("book"):
            continue
        row = sc.get(r["symbol"]) or {}
        out[r["symbol"]] = {"symbol": r["symbol"], "role": "watch", "ref": c.get("price"), "atr": row.get("atr14"),
                            "book": st["book"], "label": st.get("label"), "level": st.get("level"),
                            "level_kind": st.get("level_kind"), "steps": st.get("steps"),
                            "adv_shares": (row["adv20_usd"] / row["close"]) if row.get("adv20_usd") and row.get("close") else None}
    for p in pitches or []:
        if p.get("status") != "active":
            continue
        pl = p.get("plan") or {}
        row = sc.get(p["symbol"]) or {}
        out[p["symbol"]] = {"symbol": p["symbol"], "role": "pitch", "id": p["id"], "ref": row.get("close") or p.get("last") or p.get("price"),
                            "atr": row.get("atr14"), "stop": _f(pl.get("stop")), "t1": _f(pl.get("t1")), "t2": _f(pl.get("t2")),
                            "zone": None, "book": p["book"], "entry": p.get("entry_open"),
                            "t1_done": bool((p.get("plan_mark") or {}).get("t1_hit")),
                            "adv_shares": (row["adv20_usd"] / row["close"]) if row.get("adv20_usd") and row.get("close") else None}
    if board and (today is None or board.get("entry_date") == today):
        for p in board.get("picks") or []:
            row = sc.get(p["symbol"]) or {}
            atr = row.get("atr14") or ((p["chase_above"] - p["price"]) if p.get("chase_above") and p.get("price") else None)
            out[p["symbol"]] = {"symbol": p["symbol"], "role": "pick", "id": p["id"], "ref": p.get("price"), "atr": atr,
                                "stop": p.get("stop"), "t1": p.get("t1"), "t2": p.get("t2"), "zone": p.get("zone"),
                                "chase_above": p.get("chase_above"), "book": p["book"],
                                "adv_shares": (row["adv20_usd"] / row["close"]) if row.get("adv20_usd") and row.get("close") else None}
    return out


# --------------------------------------------------------------------------- one quote, one target
def detect(t: dict, q: dict, prev: dict | None, now: datetime, ac: dict) -> tuple[list[dict], dict]:
    """The events that are new since ``prev`` (this name's remembered state), and the new state.

    ``q``: price, open, day_high, day_low, prev_close, volume (any may be missing). Every event fires on a
    change of state, so the same quote twice gives nothing the second time.
    """
    prev = prev or {}
    st = dict(prev)
    ev: list[dict] = []
    px = _f((q or {}).get("price"))
    if not px:
        return ev, st
    op, hi, lo = _f(q.get("open")), _f(q.get("day_high")) or px, _f(q.get("day_low")) or px
    atr, stop, t1, t2, zone = _f(t.get("atr")), _f(t.get("stop")), _f(t.get("t1")), _f(t.get("t2")), t.get("zone")
    sym = t["symbol"]

    def add(kind, text, plan, **nums):
        ev.append({"kind": kind, "symbol": sym, "role": t["role"], "id": t.get("id"), "book": t.get("book"),
                   "text": text, "plan": plan, "price": px, "at": now.isoformat(timespec="seconds"),
                   "untested": kind == "forming", **nums})

    if t["role"] in ("pick", "pitch"):
        if stop and lo <= stop and not prev.get("stop"):
            st["stop"] = True
            add("stop", f"{sym} touched its stop: low {_px(lo)} against a stop of {_px(stop)}, now {_px(px)}.",
                "The stop is the exit. The pitch closes here.", stop=stop, low=lo)
        elif stop and atr and not st.get("stop"):
            near = px - stop <= ac["near_stop_atr"] * atr
            if near and not prev.get("near_stop"):
                add("near_stop", f"{sym} is {_px(px - stop)} ({(px - stop) / atr:.1f} ATR) above its stop of {_px(stop)}, now {_px(px)}.",
                    "Nothing to do yet: the plan exits only if the stop is touched.", stop=stop)
            st["near_stop"] = near
        if t1 and hi >= t1 and not prev.get("t1") and not t.get("t1_done"):
            st["t1"] = True
            add("t1", f"{sym} reached its first target {_px(t1)} (high {_px(hi)}), now {_px(px)}.",
                (f"T1 is met. The stretch target is {_px(t2)}; the stop stays where it is." if t2 else
                 "T1 is the plan's target: the pitch closes here."), t1=t1)
        if t2 and hi >= t2 and not prev.get("t2"):
            st["t2"] = True
            add("t2", f"{sym} reached its stretch target {_px(t2)} (high {_px(hi)}), now {_px(px)}.",
                "T2 is met: the pitch closes at target.", t2=t2)
    if t["role"] == "pick" and zone and not st.get("stop"):
        z = "above" if px > zone[1] else "below" if px < zone[0] else "in"
        was = prev.get("zone")
        if was and z != was:
            if z == "in":
                add("zone_enter", f"{sym} is back inside its entry zone at {_px(px)} (zone {_px(zone[0])}–{_px(zone[1])}).",
                    "The plan's entry is valid at this price. The tested entry was the open.", zone=zone)
            elif was == "in":
                add("zone_leave", f"{sym} moved {z} its entry zone: {_px(px)} against {_px(zone[0])}–{_px(zone[1])}.",
                    ("Don't chase above the zone; the plan does not add here." if z == "above" else
                     "Below the zone and above the stop: the plan still holds, the stop is the line."), zone=zone)
        st["zone"] = z
    ref = _f(q.get("prev_close")) or _f(t.get("ref"))
    if op and ref and atr and not prev.get("gap_seen"):
        st["gap_seen"] = True
        g = (op - ref) / atr
        if abs(g) >= ac["gap_atr"]:
            plan = "No plan on this name yet: a gap alone is not a signal."
            if t["role"] == "pick":
                plan = ("It opened above the don't-chase line: the untested refinement says skip." if t.get("chase_above") and op > t["chase_above"] else
                        "It opened at or below the stop: skip." if stop and op <= stop else
                        "The tested entry was this open; the plan and the stop are unchanged.")
            elif t["role"] == "pitch":
                plan = "The plan is unchanged: the stop and the targets decide."
            add("gap", f"{sym} opened {_px(op)}, {g:+.1f} ATR from yesterday's close of {_px(ref)}.", plan, gap_atr=round(g, 2))
    vol, adv = _f(q.get("volume")), _f(t.get("adv_shares"))
    frac = session_fraction(now)
    if vol and adv and frac >= 0.15 and not prev.get("relvol"):
        pace = vol / (adv * frac)
        if pace >= ac["rel_volume"]:
            st["relvol"] = True
            add("rel_volume", f"{sym} has traded {pace:.1f}× its usual volume for this time of day ({vol / 1e6:.1f}M shares), now {_px(px)}.",
                "Volume is context, not a signal: the plan is unchanged.", rel_volume=round(pace, 2))
    lvl, kind = _f(t.get("level")), t.get("level_kind")
    if t["role"] == "watch" and lvl and kind and (t.get("steps") or 9) <= 1:
        would = px > lvl if kind == "above" else px < lvl
        if would and not prev.get("forming"):
            add("forming", f"{sym} would trigger the {t.get('label') or t.get('book')} rule if the session closed here: "
                           f"{_px(px)} is {'above' if kind == 'above' else 'below'} {_px(lvl)}.",
                "Forming, untested; it confirms only at the close. Not a buy.", level=lvl)
        st["forming"] = would
    return ev, st


# --------------------------------------------------------------------------- what gets through
class Gate:
    """Decides which events are posted. State lives in the desk database so a restart changes nothing."""

    def __init__(self, db, ac: dict):
        self.db, self.ac = db, ac

    def _key(self, e: dict, now: datetime) -> str:
        win = now.strftime("%Y-%m-%d") if e["kind"] not in ("zone_enter", "zone_leave", "news") else now.strftime("%Y-%m-%dT%H")
        ref = e.get("ref") or ""
        return f"{win}|{e['symbol']}|{e['kind']}|{ref}"

    def admit(self, events: list[dict], now: datetime) -> tuple[list[dict], int]:
        """-> (events to post, how many were held back). Admitted events are recorded at once."""
        out, held = [], 0
        hour_ago = (now - timedelta(hours=1)).isoformat(timespec="seconds")
        sent_hour = self.db.q("SELECT COUNT(*) n FROM event WHERE at>=? AND kind!='held'", (hour_ago,))[0]["n"]
        cool = (now - timedelta(minutes=float(self.ac["cooldown_min"]))).isoformat(timespec="seconds")
        for e in events:
            if not self.ac["kinds"].get(e["kind"], True):
                continue
            key = self._key(e, now)
            if self.db.q("SELECT 1 FROM event WHERE key=?", (key,)):
                continue                                              # already sent in this window
            if e["kind"] not in ALWAYS:
                recent = self.db.q("SELECT 1 FROM event WHERE symbol=? AND at>=? AND kind!='held' LIMIT 1", (e["symbol"], cool))
                if recent or len(out) >= self.ac["max_per_tick"] or sent_hour + len(out) >= self.ac["max_per_hour"]:
                    held += 1
                    continue
            self.db.x("INSERT OR IGNORE INTO event(key, at, kind, symbol, data) VALUES(?,?,?,?,?)",
                      (key, now.isoformat(timespec="seconds"), e["kind"], e["symbol"], json.dumps(e, default=str)))
            out.append(e)
        return out, held

    def recent(self, since: str, limit: int = 100) -> list[dict]:
        out = []
        for r in self.db.q("SELECT at, kind, symbol, data FROM event WHERE at>=? ORDER BY at DESC LIMIT ?", (since, limit)):
            try:
                out.append(json.loads(r["data"]))
            except ValueError:
                continue
        return out


def embed(events: list[dict], now: datetime, held: int = 0, base_url: str | None = None, tag: str = "",
          degraded: str | None = None) -> dict:
    """One post for everything that changed in this tick."""
    t = now.strftime("%I:%M %p").lstrip("0")
    title = (f"{events[0]['symbol']} {TITLES.get(events[0]['kind'], events[0]['kind'])}" if len(events) == 1 else
             f"{len(events)} changes on the watched names")
    fields = []
    for e in events[:10]:
        val = e["text"] + "\n**Plan:** " + e["plan"]
        if e.get("url"):
            val += f"\n[source]({e['url']})"
        if base_url and e.get("id"):
            val += f"\n[open the pitch]({base_url.rstrip('/')}/#/pitch/{e['id']})"
        fields.append({"name": f"{e['symbol']} — {TITLES.get(e['kind'], e['kind'])}"[:256], "value": val[:1024], "inline": False})
    desc = []
    if held:
        desc.append(f"{held} more change(s) held back by the cooldown; they are on the dashboard.")
    if degraded:
        desc.append(f"⚠️ {degraded}")
    worst = any(e["kind"] == "stop" for e in events)
    good = any(e["kind"] in ("t1", "t2") for e in events)
    return {"title": ((f"[{tag}] " if tag else "") + f"{t} · {title}")[:256], "description": "\n".join(desc)[:1000],
            "fields": fields, "color": 0xB42318 if worst else 0x1A7F4B if good else 0x446266,
            "footer": {"text": "Live check on existing plans · signals come from the close · research, not advice"}}


def forming_stats(events: list[dict], pitched: set[tuple[str, str]]) -> dict:
    """The untested "setup forming" flag: how often the rule really triggered at that day's close.
    ``pitched``: {(symbol, day)} of our pitches."""
    rows = [e for e in events if e.get("kind") == "forming"]
    days = {(e["symbol"], e["at"][:10]) for e in rows}
    hit = sum(1 for k in days if k in pitched)
    return {"n": len(days), "confirmed": hit, "rate": round(hit / len(days), 4) if days else None,
            "label": "untested: a partial bar that would trigger a rule; counted as confirmed when the rule "
                     "pitched the name at that day's close"}
