"""Pitch charts (docs/21.6, Part 9.2): static PNGs saved next to the memo.

    price_chart()    close with the 50/200-day EMAs, the entry zone, stop, T1/T2, and a 12-month strip
                     with the bear / base / bull and probability-weighted targets
    football_field() the valuation ranges against today's price: 52-week range, DCF bear-bull, peer
                     comps, street target, probability-weighted
    peer_table()     the comps the valuation used, with the target's own row and the peer median

Every number drawn comes from the pack, so a chart can never disagree with the memo. matplotlib is an
optional extra (``pip install .[charts]``): without it ``render_all`` returns ``{}`` and the memo goes
out without pictures. Colours: the dataviz reference palette (validated, light surface).
"""
from __future__ import annotations

import logging
from pathlib import Path
from statistics import median

log = logging.getLogger(__name__)

PRICE, EMA50, EMA200 = "#2a78d6", "#eb6834", "#6250d6"
INK, INK2, MUTED, GRID, SURFACE = "#1f2328", "#4b535c", "#8a929b", "#e6e8eb", "#ffffff"
ZONE = "#2a78d6"


def available() -> bool:
    try:
        import matplotlib  # noqa: F401
        return True
    except Exception:
        return False


def _plt():
    import matplotlib
    matplotlib.use("Agg", force=False)
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 9, "axes.edgecolor": GRID, "axes.labelcolor": INK2,
        "xtick.color": INK2, "ytick.color": INK2, "axes.spines.top": False, "axes.spines.right": False,
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    })
    return plt


def _usd(x: float) -> str:
    return f"\\${x:,.2f}"                                # escaped: two $ in one label would be mathtext


def _spread(items: list[tuple[float, str]], lo: float, hi: float, gap: float) -> list[tuple[float, float, str]]:
    """Label positions that don't overlap: (true y, label y, text), pushed apart by at least ``gap``."""
    items = sorted(items)
    ys = [y for y, _ in items]
    for _ in range(50):
        moved = False
        for i in range(1, len(ys)):
            if ys[i] - ys[i - 1] < gap:
                mid = (ys[i] + ys[i - 1]) / 2
                ys[i - 1], ys[i] = mid - gap / 2, mid + gap / 2
                moved = True
        if not moved:
            break
    shift = max(0.0, lo - ys[0]) if ys else 0.0
    ys = [y + shift for y in ys]
    over = max(0.0, ys[-1] - hi) if ys else 0.0
    ys = [y - over for y in ys]
    return [(t[0], y, t[1]) for t, y in zip(items, ys)]


# ---------------------------------------------------------------------------------------------- price
def price_chart(pack: dict, bars, path: Path, sessions: int = 252) -> Path | None:
    """Close, EMA50, EMA200 for the last year, with the plan's levels and the 12-month targets."""
    if bars is None or not len(bars) or "close" not in bars:
        return None
    plt = _plt()
    from matplotlib import gridspec
    from matplotlib.transforms import blended_transform_factory
    from abg.tradebook.common.indicators import ema

    close = bars["close"].astype(float).dropna()
    e50, e200 = ema(close, 50), ema(close, 200)
    cut = close.index[-min(sessions, len(close))]
    c, e50, e200 = close[close.index >= cut], e50[e50.index >= cut], e200[e200.index >= cut]

    pl = pack.get("plan") or {}
    ent, tgt, inv = pl.get("entry") or {}, pl.get("targets") or {}, pl.get("investment") or {}
    levels: list[tuple[float, str, str]] = []                         # (y, label, style)
    if pack.get("stop"):
        levels.append((float(pack["stop"]), f"Stop {_usd(pack['stop'])}", "stop"))
    zone = ent.get("zone")
    if tgt.get("t1"):
        levels.append((float(tgt["t1"]), f"T1 {_usd(tgt['t1'])}", "target"))
    if tgt.get("t2"):
        levels.append((float(tgt["t2"]), f"T2 {_usd(tgt['t2'])}", "target"))
    ref = ent.get("reference") or pack.get("price")
    if ref:
        levels.append((float(ref), f"Entry ref {_usd(ref)}", "entry"))

    twelve: list[tuple[float, str, str]] = []
    cases = inv.get("cases") or {}
    if inv.get("available"):
        for k in ("bear", "base", "bull"):
            v = (cases.get(k) or {}).get("value")
            if v:
                twelve.append((float(v), f"{k.title()} {_usd(v)}", k))
        if inv.get("prob_weighted"):
            twelve.append((float(inv["prob_weighted"]), f"Weighted {_usd(inv['prob_weighted'])}", "pw"))
        if inv.get("street_target"):
            twelve.append((float(inv["street_target"]), f"Street {_usd(inv['street_target'])}", "street"))

    # the y range is the year's prices and the trade's levels; 12-month targets outside it are pinned
    # to the edge with an arrow, so a far-off DCF case can't squash the trade into a sliver
    pts = list(c.values) + [y for y, _, _ in levels] + (list(zone) if zone else [])
    lo, hi = min(pts), max(pts)
    pad = (hi - lo) * 0.06 or hi * 0.02
    lo, hi = lo - pad, hi + pad

    has12 = bool(twelve)
    fig = plt.figure(figsize=(10, 5.4), dpi=150)
    gs = gridspec.GridSpec(1, 2 if has12 else 1, width_ratios=[6.2, 1.5] if has12 else [1], wspace=0.4)
    ax = fig.add_subplot(gs[0])
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.plot(c.index, c.values, color=PRICE, linewidth=2, label="Close")
    ax.plot(e50.index, e50.values, color=EMA50, linewidth=1.6, label="50-day EMA")
    ax.plot(e200.index, e200.values, color=EMA200, linewidth=1.6, label="200-day EMA")
    if zone:
        ax.axhspan(zone[0], zone[1], color=ZONE, alpha=0.10, linewidth=0)
    for y, _, style in levels:
        ax.axhline(y, color=INK if style == "stop" else MUTED, linewidth=1.1 if style == "stop" else 1.0,
                   linestyle="-" if style in ("stop", "entry") else (0, (4, 3)))
    ax.set_ylim(lo, hi)
    ax.margins(x=0.01)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
    import matplotlib.dates as mdates
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    tr = blended_transform_factory(ax.transAxes, ax.transData)
    lab = [(y, t) for y, t, _ in levels]
    if zone:
        lab.append(((zone[0] + zone[1]) / 2, f"Zone {_usd(zone[0])}–{_usd(zone[1])}"))
    box = {"boxstyle": "square,pad=0.15", "fc": SURFACE, "ec": "none", "alpha": 0.85}
    for y, ly, t in _spread(lab, lo, hi, (hi - lo) * 0.055):
        moved = abs(ly - y) > 1e-9
        ax.annotate(t, xy=(1.0, y), xycoords=tr, xytext=(1.02, ly), textcoords=tr, ha="left", va="center",
                    fontsize=8, color=INK, bbox=box,
                    arrowprops={"arrowstyle": "-", "color": MUTED, "lw": 0.6} if moved else None)
    ax.legend(loc="upper left", frameon=False, fontsize=8, ncol=3, labelcolor=INK2)
    title = f"{pack.get('symbol')} — last {len(c)} sessions to {pack.get('as_of')}"
    ax.set_title(title, loc="left", fontsize=11, color=INK, pad=8)

    if has12:
        sx = fig.add_subplot(gs[1], sharey=ax)
        sx.set_xlim(0, 1)
        sx.axis("off")
        sx.set_ylim(lo, hi)
        horizon = inv.get("horizon")
        sx.set_title(f"12-month view\nto {horizon}" if horizon else "12-month view", fontsize=8.5,
                     color=INK2, pad=8)
        inside = [(y, t, k) for y, t, k in twelve if lo <= y <= hi]
        outside = [(y, t, k) for y, t, k in twelve if not lo <= y <= hi]
        bb = [y for y, _, k in twelve if k in ("bear", "bull")]
        if len(bb) == 2:
            a, b = max(min(bb), lo), min(max(bb), hi)
            sx.plot([0.14, 0.14], [a, b], color=MUTED, linewidth=2, solid_capstyle="butt")
        for y, t, k in inside:
            if k == "pw":
                sx.plot([0.14], [y], marker="o", markersize=8, color=INK, markeredgecolor=SURFACE,
                        markeredgewidth=2)
            elif k == "street":
                sx.plot([0.14], [y], marker="D", markersize=7, markerfacecolor=SURFACE, markeredgecolor=INK2,
                        markeredgewidth=1.4)
            else:
                sx.plot([0.06, 0.22], [y, y], color=INK2, linewidth=2)
        for y, t, k in outside:
            edge = lo + (hi - lo) * 0.015 if y < lo else hi - (hi - lo) * 0.015
            sx.plot([0.14], [edge], marker="v" if y < lo else "^", markersize=8, color=INK2)
        lab12 = [(y, t) for y, t, _ in inside] + \
                [((lo + (hi - lo) * 0.015) if y < lo else (hi - (hi - lo) * 0.015), t + (" ↓" if y < lo else " ↑"))
                 for y, t, _ in outside]
        for y, ly, t in _spread(lab12, lo + (hi - lo) * 0.015, hi - (hi - lo) * 0.015, (hi - lo) * 0.055):
            sx.annotate(t, xy=(0.24, y), xytext=(0.30, ly), ha="left", va="center", fontsize=8, color=INK,
                        arrowprops={"arrowstyle": "-", "color": GRID, "lw": 0.6} if abs(ly - y) > 1e-9 else None)
    fig.text(0.01, 0.01, f"Source: {pack.get('provider') or 'bar store'} daily closes; levels from the pitch "
             f"plan. Research, not advice.", fontsize=7, color=MUTED)
    fig.subplots_adjust(left=0.07, right=0.97 if has12 else 0.86, top=0.9, bottom=0.1)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


# ---------------------------------------------------------------------------------------------- valuation
def football_field(pack: dict, path: Path) -> Path | None:
    """Horizontal ranges per method against today's price; nothing drawn that the pack doesn't hold."""
    inv = (pack.get("plan") or {}).get("investment") or {}
    val = pack.get("valuation") or {}
    price = pack.get("price")
    if not inv.get("available") or not price:
        return None
    cases = inv.get("cases") or {}
    metrics = (pack.get("overview") or {}).get("metrics") or {}
    rows: list[tuple[str, float | None, float | None, float | None]] = []   # label, low, high, mark
    rng = metrics.get("52-week range")
    if rng and all(rng):
        rows.append(("52-week range", float(rng[0]), float(rng[1]), None))
    vals = [(cases.get(k) or {}).get("value") for k in ("bear", "base", "bull")]
    if vals[0] and vals[2]:
        rows.append(("Earnings DCF\nbear – base – bull", float(vals[0]), float(vals[2]),
                     float(vals[1]) if vals[1] else None))
    cm = val.get("comps") or {}
    legs = [x for x in (cm.get("value_pe"), cm.get("value_forward_pe")) if x]
    if legs:
        rows.append((f"Peer comps\n(P/E, forward P/E; n={cm.get('n_pe') or cm.get('n_forward_pe') or len(cm.get('peers') or [])})",
                     float(min(legs)), float(max(legs)), float(cm["value"]) if cm.get("value") else None))
    if inv.get("street_target"):
        rows.append(("Street mean target", None, None, float(inv["street_target"])))
    if inv.get("prob_weighted"):
        rows.append(("Probability-weighted", None, None, float(inv["prob_weighted"])))
    if not rows:
        return None
    plt = _plt()
    fig, ax = plt.subplots(figsize=(10, 0.62 * len(rows) + 1.5), dpi=150)
    ax.spines["left"].set_visible(False)
    ax.tick_params(axis="y", length=0)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    allx = [float(price)] + [x for r in rows for x in r[1:] if x is not None]
    span = max(allx) - min(allx) or float(price) * 0.1
    for i, (label, a, b, m) in enumerate(rows):
        y = len(rows) - 1 - i
        if a is not None and b is not None:
            ax.barh(y, b - a, left=a, height=0.42, color=PRICE, alpha=0.85, linewidth=0)
            ax.text(a - span * 0.01, y, _usd(a), ha="right", va="center", fontsize=8, color=INK2)
            ax.text(b + span * 0.01, y, _usd(b), ha="left", va="center", fontsize=8, color=INK2)
        if m is not None:
            if a is None:
                ax.plot([m], [y], marker="o", markersize=8, color=INK, markeredgecolor=SURFACE, markeredgewidth=2)
                ax.text(m + span * 0.015, y, _usd(m), ha="left", va="center", fontsize=8, color=INK)
            else:
                ax.plot([m, m], [y - 0.21, y + 0.21], color=SURFACE, linewidth=2.5)
                ax.text(m, y + 0.3, _usd(m), ha="center", va="bottom", fontsize=7.5, color=INK)
    ax.axvline(float(price), color=INK, linewidth=1.2)
    ax.text(float(price), len(rows) - 0.35, f"Price {_usd(price)}", ha="center", va="bottom", fontsize=8.5,
            color=INK, fontweight="bold")
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([r[0] for r in reversed(rows)], fontsize=8.5, color=INK)
    ax.set_ylim(-0.6, len(rows) - 0.1)
    ax.set_xlim(min(allx) - span * 0.12, max(allx) + span * 0.12)
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
    ax.set_title(f"{pack.get('symbol')} — what it might be worth (12-month view to {inv.get('horizon')})",
                 loc="left", fontsize=11, color=INK, pad=18)
    w = inv.get("weights") or (pack.get("plan") or {}).get("settings", {}).get("scenario_weights")
    note = "DCF discounts EPS (owner-earnings proxy). "
    if isinstance(w, dict) and w:
        note += "Weighted = " + " + ".join(f"{int(round(float(v) * 100))}% {k}" for k, v in w.items()) + ". "
    fig.text(0.01, 0.01, note + "Research, not advice.", fontsize=7, color=MUTED)
    fig.subplots_adjust(left=0.2, right=0.97, top=0.8, bottom=0.12 if len(rows) > 2 else 0.25)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


# ---------------------------------------------------------------------------------------------- peers
def peer_table(pack: dict, path: Path) -> Path | None:
    """The comps the valuation used, the target's own row first and the peer median last."""
    peers = [p for p in (pack.get("peers") or []) if p.get("symbol")]
    if not peers:
        return None
    ov = pack.get("overview") or {}
    val = pack.get("valuation") or {}
    inp = val.get("inputs") or {}

    def f(x, kind):
        if x is None:
            return "—"
        return f"{x:,.1f}x" if kind == "x" else _usd(x)

    cols = [("Symbol", None), ("P/E", "x"), ("Fwd P/E", "x"), ("EPS", "$")]
    rows = [[pack.get("symbol"), f(ov.get("pe_ltm"), "x"), f(ov.get("pe_ntm"), "x"), f(inp.get("eps"), "$")]]
    for p in peers:
        rows.append([p["symbol"], f(p.get("pe"), "x"), f(p.get("forward_pe"), "x"), f(p.get("eps"), "$")])

    def med(k):
        xs = [p[k] for p in peers if p.get(k) and 0 < p[k] < 150]
        return median(xs) if xs else None
    rows.append(["Peer median", f(med("pe"), "x"), f(med("forward_pe"), "x"), "—"])
    plt = _plt()
    h = 0.32 * (len(rows) + 1) + 0.9
    fig, ax = plt.subplots(figsize=(6.4, h), dpi=150)
    ax.axis("off")
    n = len(rows) + 1
    ax.set_xlim(0, 1)
    ax.set_ylim(0, n)
    xs = [0.02, 0.42, 0.64, 0.86]
    for j, (name, _) in enumerate(cols):
        ax.text(xs[j] + (0 if j == 0 else 0.1), n - 0.5, name, ha="left" if j == 0 else "right", va="center",
                fontsize=8.5, color=INK2, fontweight="bold")
    ax.plot([0, 1], [n - 1, n - 1], color=INK2, linewidth=0.8)
    for i, r in enumerate(rows):
        y = n - 1.5 - i
        first, last = i == 0, i == len(rows) - 1
        if first:
            ax.axhspan(y - 0.5, y + 0.5, color=PRICE, alpha=0.08, linewidth=0)
        if last:
            ax.plot([0, 1], [y + 0.5, y + 0.5], color=GRID, linewidth=0.8)
        for j, v in enumerate(r):
            ax.text(xs[j] + (0 if j == 0 else 0.1), y, str(v), ha="left" if j == 0 else "right", va="center",
                    fontsize=8.5, color=INK, fontweight="bold" if first or last else "normal")
    ax.set_title(f"{pack.get('symbol')} vs same-sector peers", loc="left", fontsize=10.5, color=INK)
    fig.text(0.01, 0.01, "Multiples from the fundamentals provider; comps use positive multiples under 150x.",
             fontsize=6.5, color=MUTED)
    fig.subplots_adjust(left=0.03, right=0.97, top=0.86, bottom=0.12)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


# ---------------------------------------------------------------------------------------------- all
def render_all(pack: dict, bars, out_dir: Path) -> dict:
    """Write every chart that has data; returns {kind: filename}. Never raises: a chart is decoration."""
    if not available():
        return {}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    for kind, fn in (("price", lambda p: price_chart(pack, bars, p)),
                     ("valuation", lambda p: football_field(pack, p)),
                     ("peers", lambda p: peer_table(pack, p))):
        path = out_dir / f"{pack['id']}-{kind}.png"
        try:
            if fn(path):
                out[kind] = path.name
        except Exception as e:
            log.warning("chart %s for %s failed (%s: %s)", kind, pack.get("symbol"), type(e).__name__, e)
            try:
                import matplotlib.pyplot as plt
                plt.close("all")
            except Exception:
                pass
    return out

