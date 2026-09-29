"""Chart images for Discord messages: recent daily candles with the idea's zone, stop, targets,
fills and exits drawn on.  Needs matplotlib (``pip install matplotlib`` / the ``charts`` extra);
without it messages are sent without an image."""
from __future__ import annotations

import io
import logging

log = logging.getLogger(__name__)
CHART_KINDS = {"ingested", "entry", "scale_in", "target_hit", "stop_hit", "exit", "trailing_stop", "breakeven_stop",
               "entry_changed", "advisory"}


def charts_available() -> bool:
    try:
        import matplotlib  # noqa: F401
        return True
    except Exception:
        return False


def render(idea, ohlc, bars: int = 90) -> bytes | None:
    """PNG bytes, or None if it can't be drawn."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
        import pandas as pd
    except Exception:
        return None
    try:
        df = ohlc.dropna(subset=["close"]).tail(bars).copy()
        if len(df) < 10:
            return None
        idx = pd.to_datetime(df.index)
        x = mdates.date2num(idx.to_pydatetime())
        w = 0.6 * (x[1] - x[0]) if len(x) > 1 else 0.6
        fig, ax = plt.subplots(figsize=(8, 4.2), dpi=100)
        bg, fg, grid = "#15171c", "#d6d6d6", "#2a2d35"
        fig.patch.set_facecolor(bg)
        ax.set_facecolor(bg)
        up, dn = "#1baf7a", "#e34948"
        o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
        for xi, oi, hi, li, ci in zip(x, o, h, l, c):
            col = up if ci >= oi else dn
            ax.vlines(xi, li, hi, color=col, linewidth=0.8)
            ax.add_patch(plt.Rectangle((xi - w / 2, min(oi, ci)), w, max(abs(ci - oi), 1e-9), color=col))
        lo, hi = idea.flags.get("zone_low", idea.entry_low), idea.entry_high
        if lo is not None and hi is not None:
            ax.axhspan(lo, hi if hi > lo else lo * 1.0005, color="#3987e5", alpha=0.18, label="entry zone")
        if idea.stop is not None:
            ax.axhline(idea.stop, color=dn, linestyle="--", linewidth=1.1, label=f"stop {idea.stop:,.2f}")
        if idea.soft_stop is not None:
            ax.axhline(idea.soft_stop, color="#e67e22", linestyle=":", linewidth=1)
        added = set(idea.flags.get("added_targets") or [])
        for i, t in enumerate(idea.targets):
            ax.axhline(t, color=up, linestyle="--" if t not in added else ":", linewidth=1.1 if i not in idea.targets_hit else 0.6,
                       label=("scale-out " if t in added else "target ") + f"{t:,.2f}")
        for tr in idea.tranches or []:
            if tr.get("fill") is not None and tr.get("at"):
                ax.plot(mdates.date2num(pd.Timestamp(tr["at"], unit="s").to_pydatetime()), tr["fill"], marker="^" if idea.long
                        else "v", color="#f2b33d", markersize=9, zorder=5)
        if idea.last_price:
            ax.axhline(idea.last_price, color="#9a9892", linewidth=0.6)
        ax.set_xlim(x[0] - 1, x[-1] + 3)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        ax.tick_params(colors=fg, labelsize=8)
        for sp in ax.spines.values():
            sp.set_color(grid)
        ax.grid(color=grid, linewidth=0.5)
        cls = ((idea.meta or {}).get("class") or {}).get("pattern", "")
        ax.set_title(f"{idea.symbol} {idea.direction} #{idea.id} · {cls} · {idea.status}", color=fg, fontsize=10, loc="left")
        leg = ax.legend(loc="upper left", fontsize=7, facecolor=bg, edgecolor=grid, labelcolor=fg)
        leg.get_frame().set_alpha(0.8)
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=bg)
        plt.close(fig)
        return buf.getvalue()
    except Exception:  # a chart must never break a relay
        log.exception("chart render failed")
        return None
