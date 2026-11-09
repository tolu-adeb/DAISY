"""Stock desk: the four-book rule system from the Stocks Rule Book, run live.

``tradebook`` (vendored at ``abg.tradebook``) is a historical backtester: it reads a panel of daily
bars and simulates years of trading.  This package adds the parts a live desk needs:

* ``store``   - a SQLite store of daily bars, and the ``Panel`` the books read
* ``universe``- which symbols the desk follows (S&P 500 + the ETFs the books need)
* ``sweep``   - a rate-limited paged refresh so a wide universe stays current
* ``live``    - runs the books' own ``on_close`` on today's panel and returns candidates
* ``thesis``  - turns a candidate into a written pitch with valuation, risks and sources
* ``desk``    - schedule, positions, Discord posting, scorecard against SPY

The decision path (indicators -> books -> regime -> risk) is the vendored code, unchanged, so a live
signal and a backtest signal come from the same lines.
"""
