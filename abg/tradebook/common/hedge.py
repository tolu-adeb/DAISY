"""Online allocation across strategies ("experts").

Hedge / exponentially weighted average forecaster (Cesa-Bianchi & Lugosi,
Prediction, Learning, and Games, ch. 2): weights w_i are multiplied by
exp(-eta * loss_i) after every round, then normalized. With losses in [0, 1],
regret against the best single expert is at most ln(N)/eta + n*eta/8.

A weight floor keeps every expert alive so a recovering strategy can come back.
"""
from __future__ import annotations

import math

import numpy as np


class Hedge:
    def __init__(self, names: list[str], eta: float | None = None, horizon: int = 250,
                 floor: float = 0.05, loss_clip: float = 2.0):
        """
        names: expert names.
        eta: learning rate. If None, uses sqrt(8 ln N / horizon) (the textbook tuning).
        floor: minimum weight per expert after normalization (0 disables).
        loss_clip: per-round performance x is clipped to [-loss_clip, +loss_clip]
                   and mapped to loss = (loss_clip - x) / (2 * loss_clip) in [0, 1].
        """
        self.names = list(names)
        n = len(self.names)
        self.eta = eta if eta is not None else math.sqrt(8.0 * math.log(max(n, 2)) / max(horizon, 1))
        self.floor = floor
        self.loss_clip = loss_clip
        self.logw = np.zeros(n)
        self.history: list[dict] = []

    def weights(self) -> dict[str, float]:
        w = np.exp(self.logw - self.logw.max())
        w = w / w.sum()
        if self.floor > 0 and len(w) > 1:
            f = min(self.floor, 1.0 / len(w))
            w = f + (1.0 - f * len(w)) * w
        return dict(zip(self.names, w.tolist()))

    def update(self, performance: dict[str, float], tag=None) -> None:
        """performance: per-expert result for the round (e.g. net R or % return).
        Experts missing from the dict are treated as flat (0)."""
        c = self.loss_clip
        for i, name in enumerate(self.names):
            x = float(performance.get(name, 0.0))
            x = max(-c, min(c, x))
            loss = (c - x) / (2.0 * c)
            self.logw[i] -= self.eta * loss
        rec = {"tag": tag, **{f"w_{k}": v for k, v in self.weights().items()}}
        self.history.append(rec)
