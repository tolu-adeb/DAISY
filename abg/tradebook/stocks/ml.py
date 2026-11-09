"""Optional walk-forward ML filter for Book C (trend_pullback).

1. Every day, every stock that meets Book C's rule-based entry conditions becomes a candidate event.
2. Label (triple barrier): from the next open, 1 if price reaches +pt_atr*ATR before -sl_atr*ATR within
   `horizon` days, else 0 (time-outs count as 0).
3. Features at the signal close: RSI14, MACD histogram, EMA50/EMA200 - 1, ATR/close, 5/20/126-day returns,
   20-day volatility, Bollinger width, relative volume, distance to EMA50 in ATR, benchmark 20-day ER and
   200-day trend.
4. For each calendar year Y, a gradient-boosted classifier is fit on events whose label window ended before
   the first trading day of Y (purged), then scores Y's candidates. No future data is used.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from abg.tradebook.common import indicators as ind


class MLFilter:
    def __init__(self, panel, I, benchmark: str, params: dict, start_i: int):
        self.p = params
        self.probs: dict[tuple[str, int], float] = {}
        self._build(panel, I, benchmark, start_i)

    def prob(self, t: str, i: int):
        return self.probs.get((t, i))

    def _build(self, panel, I, benchmark, start_i):
        p = self.p
        c, h, l, o = panel.close, panel.high, panel.low, panel.open
        cond = (c > I.ema200) & (I.ema50 > I.ema200) & (I.min_rsi14_5 <= p.get("rsi_pullback", 40)) \
            & (c > I.prev_high) & (I.macd_hist > I.macd_hist_prev) & panel.eligible
        bc = c[benchmark]
        b_er = ind.efficiency_ratio(bc, 20)
        b_tr = (bc > ind.sma(bc, 200)).astype(float)
        bw = (I.bb_up - I.bb_low) / I.bb_mid
        relvol = panel.volume / panel.volume.rolling(20, min_periods=20).mean()
        vol20 = I.ret.rolling(20, min_periods=20).std()
        feats = {
            "rsi14": I.rsi14, "macd_hist": I.macd_hist / c, "ema_ratio": I.ema50 / I.ema200 - 1,
            "atr_pct": I.atr14 / c, "ret5": c.pct_change(5), "ret20": c.pct_change(20), "ret126": I.mom126,
            "vol20": vol20, "bb_width": bw, "relvol": relvol, "dist_ema50_atr": (c - I.ema50) / I.atr14,
        }
        H, pt, sl = p.get("horizon", 15), p.get("pt_atr", 2.0), p.get("sl_atr", 2.0)
        dates = c.index
        rows = []
        cnp, hnp, lnp, onp = c.to_numpy(), h.to_numpy(), l.to_numpy(), o.to_numpy()
        atr = I.atr14.to_numpy()
        cond_np = cond.to_numpy()
        tick = list(c.columns)
        n = len(dates)
        for j, t in enumerate(tick):
            for i in np.nonzero(cond_np[:, j])[0]:
                if i + 1 >= n or np.isnan(atr[i, j]):
                    continue
                entry = onp[i + 1, j]
                if np.isnan(entry):
                    continue
                up, dn = entry + pt * atr[i, j], entry - sl * atr[i, j]
                label, end = 0, min(i + H, n - 1)
                for k in range(i + 1, end + 1):
                    if lnp[k, j] <= dn:
                        label = 0
                        end = k
                        break
                    if hnp[k, j] >= up:
                        label = 1
                        end = k
                        break
                row = {"t": t, "i": int(i), "end_i": int(end), "label": label}
                for name, df in feats.items():
                    row[name] = df.iat[i, j]
                row["b_er"], row["b_trend"] = b_er.iat[i], b_tr.iat[i]
                rows.append(row)
        ev = pd.DataFrame(rows)
        if ev.empty:
            return
        fcols = [k for k in ev.columns if k not in ("t", "i", "end_i", "label")]
        ev["year"] = dates[ev["i"].to_numpy()].year
        from sklearn.ensemble import HistGradientBoostingClassifier
        years = sorted(ev["year"].unique())
        for y in years:
            first_i = int(np.argmax(dates.year == y))
            if first_i < start_i and y != dates[start_i].year:
                continue
            train = ev[ev["end_i"] < first_i].dropna(subset=fcols)
            test = ev[ev["year"] == y].dropna(subset=fcols)
            if len(train) < p.get("min_train_events", 200) or train["label"].nunique() < 2 or test.empty:
                continue
            clf = HistGradientBoostingClassifier(max_depth=p.get("max_depth", 3), learning_rate=p.get("learning_rate", 0.05),
                                                 max_iter=p.get("max_iter", 200), l2_regularization=1.0, random_state=0)
            clf.fit(train[fcols], train["label"])
            pr = clf.predict_proba(test[fcols])[:, 1]
            for (t, i), q in zip(test[["t", "i"]].itertuples(index=False), pr):
                self.probs[(t, int(i))] = float(q)
