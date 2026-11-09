"""Column-cached DataFrames for the replay loop.

The books read one scalar at a time - ``I.close[t].iat[i]`` - hundreds of thousands of times per
replay.  Under pandas 2.x/3.x copy-on-write, ``df[t]`` builds a brand-new Series on every call (the old
item cache is gone), and that construction was about 80% of a replay's run time: roughly a minute per
simulated year at S&P 500 scale, which made a ten-year daily replay take over ten minutes.

``ColumnCached`` is a DataFrame that hands back the same Series object for the same column name.  It is
read-only by contract - the indicator and price frames are never mutated after they are built - so the
cache can never go stale.  Anything other than a single column name (a list, a boolean mask, a slice)
goes straight to pandas, and arithmetic returns an ordinary DataFrame, so code that does whole-frame
maths (the ML filter, the stress flags) behaves exactly as before.
"""
from __future__ import annotations

from dataclasses import fields, is_dataclass, replace

import pandas as pd


class ColumnCached(pd.DataFrame):
    @property
    def _constructor(self):
        # derived frames (c > I.ema200, df.shift(), ...) are plain DataFrames: they are new data, and a
        # cache on them would only waste memory
        return pd.DataFrame

    def __getitem__(self, key):
        if type(key) is str:
            try:
                cache = object.__getattribute__(self, "_colcache")
            except AttributeError:
                cache = {}
                object.__setattr__(self, "_colcache", cache)
            s = cache.get(key)
            if s is None:
                s = pd.DataFrame.__getitem__(self, key)
                cache[key] = s
            return s
        return pd.DataFrame.__getitem__(self, key)

    def __setitem__(self, key, value):
        # a write would leave a cached column stale, so drop the cache rather than serve old data
        try:
            object.__getattribute__(self, "_colcache").clear()
        except AttributeError:
            pass
        pd.DataFrame.__setitem__(self, key, value)


def cached(df):
    """Wrap ``df`` (no copy: copy-on-write shares the data). ``None`` and non-frames pass through."""
    if df is None or isinstance(df, ColumnCached) or not isinstance(df, pd.DataFrame):
        return df
    return ColumnCached(df)


def cache_attrs(obj) -> None:
    """Wrap every DataFrame attribute of ``obj`` in place."""
    for k, v in list(vars(obj).items()):
        if isinstance(v, pd.DataFrame) and not isinstance(v, ColumnCached):
            setattr(obj, k, ColumnCached(v))


def cache_dataclass(dc):
    """A copy of a dataclass (e.g. ``Panel``) with every DataFrame field wrapped."""
    if not is_dataclass(dc):
        return dc
    changes = {f.name: cached(getattr(dc, f.name)) for f in fields(dc)
               if isinstance(getattr(dc, f.name), pd.DataFrame)}
    return replace(dc, **changes) if changes else dc
