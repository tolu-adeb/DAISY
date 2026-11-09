"""The day's top picks (docs/21.7): board selection, live check-ins, swaps, schedule and the posts."""
from __future__ import annotations

import asyncio
import json
import warnings
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from abg.stocks import board as B

from test_stocks import _desk

NY = ZoneInfo("America/New_York")


def pick(sym, price=100.0, stop=96.0, t1=104.0, zone=(99.0, 101.0), chase=102.0, score=60):
    return {"id": f"2026-11-06-trend_pullback-{sym}", "symbol": sym, "name": sym, "book": "trend_pullback",
            "book_label": "Trend pullback", "rating": "BUY", "score": score, "bucket": "Medium", "price": price,
            "stop": stop, "t1": t1, "t2": None, "zone": list(zone), "chase_above": chase, "entry_date": "2026-11-09"}


@pytest.mark.parametrize("q,state", [
    ({"price": 100.5, "open": 100.2, "day_high": 101, "day_low": 99.5}, "in_zone"),
    ({"price": 101.8, "open": 100.2, "day_high": 102, "day_low": 99.5}, "above_zone"),
    ({"price": 98.0, "open": 100.2, "day_high": 101, "day_low": 97.5}, "below_zone"),
    ({"price": 97.0, "open": 100.2, "day_high": 101, "day_low": 95.9}, "stopped"),
    ({"price": 103.5, "open": 100.2, "day_high": 104.2, "day_low": 99.5}, "t1"),
    ({"price": 103.0, "open": 102.5, "day_high": 103.2, "day_low": 102.1}, "missed"),
    ({"price": 95.0, "open": 95.5, "day_high": 96.0, "day_low": 94.0}, "gap_stop"),
    (None, "no_quote"),
])
def test_live_status(q, state):
    assert B.live_status(pick("AAA"), q)["state"] == state


def test_check_in_swaps_a_stopped_pick_for_a_reserve_in_play():
    board = {"picks": [pick("AAA"), pick("BBB")], "reserves": [pick("CCC", score=40), pick("DDD", score=35)],
             "entry_date": "2026-11-09"}
    quotes = {"AAA": {"price": 100.5, "open": 100.0, "day_low": 99.6, "day_high": 100.9},
              "BBB": {"price": 95.0, "open": 100.0, "day_low": 95.0, "day_high": 100.1},      # stopped
              "CCC": {"price": 101.9, "open": 100.0, "day_low": 99.8, "day_high": 102.0},     # above zone
              "DDD": {"price": 100.1, "open": 100.0, "day_low": 99.8, "day_high": 100.4}}     # in zone: preferred
    new, swapped = B.check_in(board, quotes)
    assert [p["symbol"] for p in new["picks"]] == ["AAA", "DDD"] and swapped == [pick("DDD")["id"]]
    assert new["dropped"][0]["symbol"] == "BBB" and [r["symbol"] for r in new["reserves"]] == ["CCC"]
    e = B.board_embed(new, "check", datetime(2026, 11, 9, 12, 2, tzinfo=NY))
    assert "12:02 PM check-in" in e["title"] and "BBB (stopped)" in e["description"]
    assert e["fields"][1]["name"].endswith("· new") and all(len(f["value"]) <= 1024 for f in e["fields"])


def test_due_slot_window_and_once():
    slots = ["09:00", "12:00", "15:00"]
    at = lambda h, m: datetime(2026, 11, 9, h, m, tzinfo=NY)        # noqa: E731
    assert B.due_slot(at(9, 3), slots, [], True) == "09:00"
    assert B.due_slot(at(9, 50), slots, [], True) is None              # missed by more than the grace
    assert B.due_slot(at(12, 10), slots, ["09:00"], True) == "12:00"
    assert B.due_slot(at(12, 10), slots, ["12:00"], True) is None
    assert B.due_slot(at(15, 0), slots, [], False) is None             # weekend / holiday
    assert B.is_fresh({"entry_date": "2026-11-09", "picks": []}, date(2026, 11, 9))
    assert not B.is_fresh({"entry_date": "2026-11-06", "picks": []}, date(2026, 11, 9))


def test_daily_board_then_open_post_and_check_in(tmp_path):
    d = _desk(tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        asyncio.run(d.daily(post=False))
    board = d.board()
    assert board and 0 < len(board["picks"]) <= 5
    scores = [p["score"] for p in board["picks"]]
    assert scores == sorted(scores, reverse=True)
    assert all(p["book"] in B.SINGLE_STOCK_BOOKS and p["rating"] in ("BUY", "ADD") for p in board["picks"])

    posts = []

    async def fake_post(url, embeds, content=None, username="Stock Desk", files=None):
        posts.append({"embeds": embeds, "content": content, "files": [f.name for f in files or []]})
        return "posted"
    d._post_to = fake_post
    d.s.stocks_webhook_url = "https://discord.example/api/webhooks/1/x"
    d.s.stocks_post_decks = True
    today = datetime.fromisoformat(board["entry_date"] + "T09:01:00").replace(tzinfo=NY)
    assert d._board_slot(today) in ("09:00", None)                   # None only if entry day is a holiday
    assert asyncio.run(d.board_post("09:00", today)) == "posted"
    assert posts[0]["embeds"][0]["title"].startswith("[PAPER] Today's top")
    from abg.stocks import deck as DK
    if DK.available():
        assert posts[1]["files"] and all(f.endswith(".pptx") for f in posts[1]["files"])

    async def quotes(symbols):                                        # every pick stopped: reserves come in
        return {s: {"price": 1.0, "open": 1.0, "day_low": 0.5, "day_high": 1.0} for s in symbols}
    d._quotes = quotes
    posts.clear()
    noon = today.replace(hour=12, minute=1)
    asyncio.run(d.board_post("12:00", noon))
    b2 = d.board()
    assert set(b2["posts"]) == {"09:00", "12:00"} and b2["posts"]["12:00"]["result"] == "posted"
    assert "check-in" in posts[0]["embeds"][0]["title"]
    assert d._board_slot(noon) is None                                # posted once
    d.store.close()
