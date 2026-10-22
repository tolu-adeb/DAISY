"""The validation protocol (docs/16 section 5, made executable).

A hypothesis is only a *candidate* when every search-window check passes, and only *confirmed* when it also
holds on the locked holdout and over the whole sample.  The headline test is the owner's rule: **a higher win
rate AND a better profit-to-drawdown ratio than the current settings** - without giving up most of the trades
or most of the profit to get there.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .runner import metrics


@dataclass
class Gate:
    min_trades: int = 250             # search-window sample size
    min_trade_frac: float = 0.80      # keep at least 80% of the current trade count (frequency matters)
    min_profit_frac: float = 0.90     # keep at least 90% of the current net profit
    min_win_delta: float = 0.010      # win rate at least +1.0 point
    min_ratio_gain: float = 0.05      # net/max-drawdown at least 5% better
    min_block_wins: int = 3           # win rate >= current in at least 3 of the 4 sub-periods
    rw_max_abs_r: float = 0.15        # no edge on random-walk bars
    peek_budget: int = 6              # holdout looks allowed per holdout window

    def to_dict(self) -> dict:
        return asdict(self)


def _chk(name: str, ok: bool, detail: str, **nums) -> dict:
    return {"check": name, "pass": bool(ok), "detail": detail, **nums}


def _ratio(m: dict) -> float:
    r = m.get("ratio") or 0.0
    return 1e6 if r == float("inf") else r


def compare(cand: dict, base: dict, ds, gate: Gate) -> dict:
    """Search-window comparison of two runs (each {'trades', 'days'})."""
    c, b = metrics(cand["trades"], [str(d) for d in ds.search]), metrics(base["trades"], [str(d) for d in ds.search])
    checks = [
        _chk("sample", c["trades"] >= gate.min_trades, f"{c['trades']} trades (need {gate.min_trades})"),
        _chk("frequency", c["trades"] >= gate.min_trade_frac * b["trades"],
             f"{c['trades']} vs {b['trades']} trades ({c['trades'] / max(1, b['trades']):.0%}; need {gate.min_trade_frac:.0%})"),
        _chk("profit", c["net_usd"] >= gate.min_profit_frac * b["net_usd"],
             f"${c['net_usd']:,.0f} vs ${b['net_usd']:,.0f} (need {gate.min_profit_frac:.0%})"),
        _chk("win_rate", c["win_rate"] >= b["win_rate"] + gate.min_win_delta,
             f"{c['win_rate']:.1%} vs {b['win_rate']:.1%} (need +{gate.min_win_delta:.1%})"),
        _chk("risk_to_profit", _ratio(c) >= _ratio(b) * (1 + gate.min_ratio_gain),
             f"net/max-DD {c['ratio']:.2f} vs {b['ratio']:.2f} (need +{gate.min_ratio_gain:.0%}); "
             f"max DD ${c['max_dd_usd']:,.0f} vs ${b['max_dd_usd']:,.0f}"),
    ]
    rows, wins, money_ok = [], 0, True
    for blk in ds.blocks:
        days = [str(d) for d in blk]
        cm, bm = metrics(cand["trades"], days), metrics(base["trades"], days)
        rows.append({"from": days[0], "to": days[-1], "cand_wr": cm["win_rate"], "base_wr": bm["win_rate"],
                     "cand_net": cm["net_usd"], "base_net": bm["net_usd"]})
        wins += cm["win_rate"] >= bm["win_rate"]
        if bm["net_usd"] > 0 and cm["net_usd"] <= 0:
            money_ok = False
    checks.append(_chk("consistency", wins >= gate.min_block_wins and money_ok,
                       f"win rate >= current in {wins}/{len(ds.blocks)} sub-periods; "
                       f"{'no' if money_ok else 'a'} sub-period turned from profit to loss", blocks=rows))
    return {"search": c, "baseline": b, "checks": checks, "pass": all(x["pass"] for x in checks)}


def headline_pass(cmp_: dict) -> bool:
    """The two numbers the owner cares about, used for the neighbour (stability) check."""
    return all(x["pass"] for x in cmp_["checks"] if x["check"] in ("win_rate", "risk_to_profit"))


def confirm(cand: dict, base: dict, ds, gate: Gate) -> dict:
    """Holdout + whole-sample verdict (only called after the search checks pass; counted as a peek)."""
    hold = [str(d) for d in ds.holdout]
    full = [str(d) for d in ds.sessions]
    ch, bh = metrics(cand["trades"], hold), metrics(base["trades"], hold)
    cf, bf = metrics(cand["trades"], full), metrics(base["trades"], full)
    checks = [
        _chk("holdout_win_rate", ch["win_rate"] >= bh["win_rate"],
             f"last {len(hold)} sessions: {ch['win_rate']:.1%} vs {bh['win_rate']:.1%} (must not be worse)"),
        _chk("holdout_risk_to_profit", _ratio(ch) >= _ratio(bh) and ch["net_usd"] > 0,
             f"last {len(hold)} sessions: net/max-DD {ch['ratio']:.2f} vs {bh['ratio']:.2f}, net ${ch['net_usd']:,.0f} "
             f"(must not be worse, must be profitable)"),
        _chk("full_win_rate", cf["win_rate"] > bf["win_rate"], f"all sessions: {cf['win_rate']:.1%} vs {bf['win_rate']:.1%}"),
        _chk("full_risk_to_profit", _ratio(cf) > _ratio(bf),
             f"all sessions: net/max-DD {cf['ratio']:.2f} vs {bf['ratio']:.2f} (${cf['net_usd']:,.0f} / ${cf['max_dd_usd']:,.0f} "
             f"vs ${bf['net_usd']:,.0f} / ${bf['max_dd_usd']:,.0f})"),
    ]
    return {"holdout": ch, "holdout_baseline": bh, "full": cf, "full_baseline": bf, "checks": checks,
            "pass": all(x["pass"] for x in checks)}
