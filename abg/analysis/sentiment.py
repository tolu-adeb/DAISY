"""News sentiment.

Default model: a fast finance-specific lexicon scorer (inspired by the Loughran-McDonald
approach: words like "liability" or "volatile" are not negative in finance-neutral
contexts the way general-purpose lexicons assume).  It handles negation windows,
intensifiers and multi-word event phrases ("beats estimates", "cuts guidance").

The model is pluggable: anything implementing ``SentimentModel.score(texts)`` can
replace it - e.g. the AI Investment Fund's FinBERT/NLP layer:

    from abg.analysis.sentiment import set_sentiment_model
    set_sentiment_model(MyFinBertModel())

When a provider supplies its own per-ticker score (Alpha Vantage, Polygon), it is
blended 50/50 with the model score.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..models import NewsItem

PHRASES = {
    "beats estimates": 2.0, "beat estimates": 2.0, "tops estimates": 2.0, "better than expected": 1.8,
    "raises guidance": 2.2, "raised guidance": 2.2, "boosts outlook": 2.0, "record revenue": 1.8,
    "record high": 1.2, "all-time high": 1.2, "price target raised": 1.5, "raises price target": 1.5,
    "share buyback": 1.2, "stock buyback": 1.2, "dividend increase": 1.3, "strong demand": 1.4,
    "misses estimates": -2.0, "missed estimates": -2.0, "worse than expected": -1.8, "cuts guidance": -2.2,
    "lowers guidance": -2.2, "cut guidance": -2.2, "profit warning": -2.2, "price target cut": -1.5,
    "lowers price target": -1.5, "going concern": -2.5, "chapter 11": -2.5, "class action": -1.5,
    "sec investigation": -1.8, "data breach": -1.6, "layoffs": -1.0, "job cuts": -1.0, "recall": -1.2,
    "short seller": -1.2, "downgraded to sell": -2.0, "upgraded to buy": 2.0,
}
POSITIVE = {
    "beat", "beats", "surge", "surges", "soar", "soars", "jump", "jumps", "rally", "rallies", "gain", "gains",
    "rise", "rises", "climb", "climbs", "upgrade", "upgrades", "upgraded", "outperform", "bullish", "growth",
    "profit", "profitable", "strong", "stronger", "record", "expand", "expands", "expansion", "win", "wins",
    "approval", "approved", "breakthrough", "partnership", "accelerate", "accelerates", "boost", "boosts",
    "exceed", "exceeds", "exceeded", "robust", "momentum", "optimistic", "upbeat", "rebound", "rebounds",
    "recovery", "innovative", "launch", "launches", "buy", "overweight", "positive", "raise", "raises",
}
NEGATIVE = {
    "miss", "misses", "missed", "plunge", "plunges", "tumble", "tumbles", "slump", "slumps", "drop", "drops",
    "fall", "falls", "sink", "sinks", "decline", "declines", "downgrade", "downgrades", "downgraded", "bearish",
    "loss", "losses", "weak", "weaker", "lawsuit", "sued", "probe", "investigation", "fraud", "halt", "halted",
    "bankruptcy", "default", "delay", "delayed", "warning", "warns", "cut", "cuts", "slowdown", "underperform",
    "concern", "concerns", "risk", "risks", "fine", "fined", "penalty", "crash", "selloff", "sell-off",
    "negative", "pessimistic", "underweight", "sell", "disappoint", "disappoints", "disappointing", "shortfall",
    "volatile", "uncertainty", "headwinds", "tariff", "tariffs", "resigns", "exits", "scandal",
}
NEGATORS = {"not", "no", "never", "without", "fails", "failed", "despite", "isn't", "wasn't", "didn't", "won't"}
INTENSIFIERS = {"sharply": 1.5, "significantly": 1.4, "massive": 1.5, "huge": 1.4, "record": 1.3,
                "strongly": 1.4, "slightly": 0.6, "modestly": 0.7}
_TOKEN = re.compile(r"[a-z][a-z\-']*")


@runtime_checkable
class SentimentModel(Protocol):
    name: str

    def score(self, texts: list[str]) -> list[float]:
        """Return one score in [-1, 1] per text."""
        ...


class LexiconSentiment:
    name = "lexicon-v2"

    def score_one(self, text: str) -> float:
        t = (text or "").lower()
        total = 0.0
        for ph, w in PHRASES.items():
            if ph in t:
                total += w
                t = t.replace(ph, " ")
        toks = _TOKEN.findall(t)
        for i, tok in enumerate(toks):
            base = 1.0 if tok in POSITIVE else -1.0 if tok in NEGATIVE else 0.0
            if not base:
                continue
            window = toks[max(0, i - 3):i]
            if any(w in NEGATORS for w in window):
                base *= -0.7
            for w in window:
                base *= INTENSIFIERS.get(w, 1.0)
            total += base
        return math.tanh(total / 2.5)

    def score(self, texts: list[str]) -> list[float]:
        return [self.score_one(x) for x in texts]


_model: SentimentModel = LexiconSentiment()


def set_sentiment_model(model: SentimentModel) -> None:
    global _model
    if not isinstance(model, SentimentModel):
        raise TypeError("model must implement name + score(texts) -> list[float]")
    _model = model


def get_sentiment_model() -> SentimentModel:
    return _model


def analyze_news(items: list[NewsItem], half_life_hours: float = 72.0, now: datetime | None = None) -> dict:
    """Score each article and aggregate with exponential recency weighting."""
    if not items:
        return {"score": None, "label": "No coverage", "articles": 0, "model": _model.name}
    now = now or datetime.now(timezone.utc)
    try:
        raw = _model.score([f"{n.title}. {n.summary or ''}"[:600] for n in items])
    except Exception:  # a plug-in model failing must not kill the report
        raw = LexiconSentiment().score([n.title for n in items])
    wsum = ssum = 0.0
    pos = neg = neu = recent = 0
    for n, s in zip(items, raw):
        if n.provider_sentiment is not None:
            s = 0.5 * s + 0.5 * max(-1.0, min(1.0, n.provider_sentiment))
        n.sentiment = round(float(s), 3)
        age_h = 24.0
        if n.published_at:
            pa = n.published_at if n.published_at.tzinfo else n.published_at.replace(tzinfo=timezone.utc)
            age_h = max(0.0, (now - pa).total_seconds() / 3600)
        if age_h <= 24:
            recent += 1
        w = 0.5 ** (age_h / half_life_hours)
        wsum += w
        ssum += w * s
        pos += s > 0.15
        neg += s < -0.15
        neu += -0.15 <= s <= 0.15
    score = ssum / wsum if wsum else 0.0
    label = ("Very Positive" if score > 0.45 else "Positive" if score > 0.15 else "Neutral" if score >= -0.15
             else "Negative" if score >= -0.45 else "Very Negative")
    return {"score": round(score, 3), "label": label, "articles": len(items), "positive": int(pos),
            "negative": int(neg), "neutral": int(neu), "last_24h": recent, "model": _model.name,
            "provider_scores_used": any(n.provider_sentiment is not None for n in items)}
