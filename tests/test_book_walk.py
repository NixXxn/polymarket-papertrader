from __future__ import annotations

from types import SimpleNamespace

from papertrader.quant.book_walk import max_price_for_positive_ev, walk_asks_for_buy


def test_max_price_for_positive_ev():
    assert max_price_for_positive_ev(0.85) == 0.85
    assert abs(max_price_for_positive_ev(0.85, min_ev=0.05) - 0.80) < 1e-12


def test_walk_asks_stops_past_ev_and_clips_kelly():
    """User example: levels 0.10/50, 0.13/120, 0.16/300; EV flips above 0.15."""
    book = SimpleNamespace(
        asks=[
            SimpleNamespace(price=0.10, size=50),
            SimpleNamespace(price=0.13, size=120),
            SimpleNamespace(price=0.16, size=300),
            SimpleNamespace(price=0.20, size=1000),
        ]
    )
    # p=0.15 → max pay 0.15; Kelly $15 → 50@$0.10 + ~76.9@$0.13
    walk = walk_asks_for_buy(book, p_win=0.15, budget_usd=15.0, min_ev=0.0, min_usd=1.0)
    assert not walk.skipped
    assert walk.limit_price == 0.13
    assert walk.levels_taken == 2
    assert walk.fillable_usd == 15.0
    assert abs(walk.fillable_shares - (50 + 10.0 / 0.13)) < 1e-6
    assert abs(walk.vwap - (15.0 / walk.fillable_shares)) < 1e-6


def test_walk_asks_respects_ev_even_when_kelly_is_large():
    book = SimpleNamespace(
        asks=[
            SimpleNamespace(price=0.10, size=50),
            SimpleNamespace(price=0.13, size=120),
            SimpleNamespace(price=0.16, size=300),
        ]
    )
    walk = walk_asks_for_buy(book, p_win=0.15, budget_usd=40.0, min_ev=0.0, min_usd=1.0)
    assert not walk.skipped
    assert walk.limit_price == 0.13
    # 50*0.10 + 120*0.13 = 5 + 15.6 = 20.6
    assert abs(walk.fillable_usd - 20.6) < 1e-9
    assert walk.fillable_shares == 170.0


def test_walk_asks_skips_when_all_asks_above_ev():
    book = SimpleNamespace(asks=[SimpleNamespace(price=0.40, size=100)])
    walk = walk_asks_for_buy(book, p_win=0.30, budget_usd=20.0, min_usd=1.0)
    assert walk.skipped
    assert walk.reason == "no_depth_inside_ev"
