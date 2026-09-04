from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from papertrader.config import load_settings
from papertrader.strategies.arbitrage import (
    _ArbMarket,
    _quote_pair,
    analyze_arbitrage,
    arbitrage_exits,
)
from helpers import FakeLevel


def test_arbitrage_settings_loaded():
    s = load_settings()
    assert s.arbitrage.max_pair_cost < 1.0
    assert s.arbitrage.position_usd > 0
    assert s.arbitrage.max_open_pairs >= 1
    assert s.arbitrage.starting_balance == 1000


def test_analyze_arbitrage_emits_paired_legs(monkeypatch, tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.db.get_open_positions.return_value = []
    engine.get_account.return_value = SimpleNamespace(cash=1000.0)

    market = _ArbMarket(
        condition_id="0xarb",
        slug="will-btc-hit-100k",
        question="Will Bitcoin hit 100k?",
        outcome_a="Yes",
        outcome_b="No",
        liquidity=5000,
        volume_24h=20000,
        lp_reward_score=2.5,
        preferred=True,
    )

    def fake_discover(*_a, **_k):
        return [market]

    def fake_quote(eng, mkt, sett):
        return SimpleNamespace(
            market=mkt,
            ask_a=0.42,
            ask_b=0.48,
            size_a=100.0,
            size_b=100.0,
            pair_cost=0.90,
            edge=0.09,
        )

    import papertrader.strategies.arbitrage as arb_mod

    monkeypatch.setattr(arb_mod, "discover_arb_markets", fake_discover)
    monkeypatch.setattr(arb_mod, "_quote_pair", fake_quote)

    sigs = analyze_arbitrage(engine, settings, paper_mode=True)
    assert len(sigs) == 2
    outcomes = {s.outcome for s in sigs}
    assert outcomes == {"yes", "no"}
    assert all(s.action == "buy" for s in sigs)
    assert all(s.order_type == "fak" for s in sigs)
    assert sum(s.limit_price or 0 for s in sigs) <= settings.arbitrage.max_pair_cost + 1e-9
    assert sum(s.amount_usd or 0 for s in sigs) <= settings.arbitrage.max_position_usd + 1e-6


def test_analyze_arbitrage_maker_paper_fill_at_limit(monkeypatch, tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.db.get_open_positions.return_value = []
    engine.get_account.return_value = SimpleNamespace(cash=1000.0)

    market = _ArbMarket(
        condition_id="0xmaker",
        slug="btc-updown-15m",
        question="Bitcoin Up or Down?",
        outcome_a="Up",
        outcome_b="Down",
        liquidity=8000,
        volume_24h=50000,
        lp_reward_score=1.0,
        preferred=True,
    )

    import papertrader.strategies.arbitrage as arb_mod

    monkeypatch.setattr(arb_mod, "discover_arb_markets", lambda *_a, **_k: [market])
    monkeypatch.setattr(
        arb_mod,
        "_quote_pair",
        lambda *_a, **_k: SimpleNamespace(
            market=market,
            ask_a=0.52,
            ask_b=0.51,
            size_a=200.0,
            size_b=200.0,
            pair_cost=1.03,
            edge=-0.03,
        ),
    )

    sigs = analyze_arbitrage(engine, settings, paper_mode=True)
    assert len(sigs) == 2
    assert {s.outcome for s in sigs} == {"up", "down"}
    assert all(s.order_type == "limit" for s in sigs)
    assert all(s.paper_fill_at_limit for s in sigs)
    pair_sum = sum(s.limit_price or 0 for s in sigs)
    assert pair_sum <= settings.arbitrage.max_pair_cost + 1e-9
    assert pair_sum < 1.0


def test_analyze_arbitrage_scales_with_budget(monkeypatch, tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.db.get_open_positions.return_value = []

    market = _ArbMarket(
        condition_id="0xscale",
        slug="eth-updown",
        question="Ethereum Up or Down?",
        outcome_a="Up",
        outcome_b="Down",
        liquidity=5000,
        volume_24h=20000,
        lp_reward_score=0.0,
        preferred=True,
    )

    import papertrader.strategies.arbitrage as arb_mod

    monkeypatch.setattr(arb_mod, "discover_arb_markets", lambda *_a, **_k: [market])
    monkeypatch.setattr(
        arb_mod,
        "_quote_pair",
        lambda *_a, **_k: SimpleNamespace(
            market=market,
            ask_a=0.40,
            ask_b=0.45,
            size_a=500.0,
            size_b=500.0,
            pair_cost=0.85,
            edge=0.15,
        ),
    )

    engine.get_account.return_value = SimpleNamespace(cash=500.0)
    small = analyze_arbitrage(engine, settings, paper_mode=True)
    engine.get_account.return_value = SimpleNamespace(cash=1000.0)
    large = analyze_arbitrage(engine, settings, paper_mode=True)
    assert sum(s.amount_usd or 0 for s in large) >= sum(s.amount_usd or 0 for s in small)


def test_quote_pair_rejects_over_cap(monkeypatch):
    settings = load_settings()
    engine = MagicMock()
    market = _ArbMarket(
        condition_id="0x1",
        slug="test-market",
        question="Test?",
        outcome_a="Yes",
        outcome_b="No",
        liquidity=1000,
        volume_24h=1000,
        lp_reward_score=0.0,
        preferred=True,
    )
    full = MagicMock()
    full.get_token_id.side_effect = lambda o: f"tok-{o.lower()}"
    engine.api.get_market.return_value = full
    engine.api.get_order_book.side_effect = lambda token: SimpleNamespace(
        asks=[FakeLevel(0.55 if "yes" in token else 0.55, 50)],
        bids=[FakeLevel(0.50, 20)],
    )
    assert _quote_pair(engine, market, settings) is None


def test_arbitrage_exits_orphan(monkeypatch, tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    orphan = SimpleNamespace(
        shares=10.0,
        is_resolved=False,
        market_condition_id="0xorphan",
        market_slug="orphan-market",
        outcome="yes",
        avg_entry_price=0.4,
        total_cost=4.0,
    )
    engine.db.get_open_positions.return_value = [orphan]
    full = MagicMock()
    full.get_token_id.return_value = "tok-yes"
    engine.api.get_market.return_value = full
    engine.api.get_order_book.return_value = SimpleNamespace(
        asks=[FakeLevel(0.6, 10)],
        bids=[FakeLevel(0.35, 10)],
    )
    sigs = arbitrage_exits(engine, settings)
    assert len(sigs) == 1
    assert sigs[0].action == "sell"
    assert sigs[0].outcome == "yes"


def test_paper_fill_at_limit_buy(tmp_path):
    from papertrader.accounts import make_engine
    from papertrader.loop import execute_signal
    from papertrader.signals import Signal

    engine = make_engine("arbitrage", tmp_path, 1000.0)
    market = MagicMock()
    market.condition_id = "0xfill"
    market.slug = "fill-market"
    market.question = "Fill?"
    engine.api.get_market = MagicMock(return_value=market)
    engine._validate_outcome = MagicMock(return_value="yes")

    sig = Signal(
        action="buy",
        slug="fill-market",
        outcome="yes",
        reason="test fill",
        amount_usd=10.0,
        order_type="limit",
        limit_price=0.40,
        paper_fill_at_limit=True,
    )
    assert execute_signal(engine, sig, dry_run=False, strategy="arbitrage") is True
    positions = engine.db.get_open_positions()
    assert len(positions) == 1
    assert abs(positions[0].avg_entry_price - 0.40) < 1e-6
    assert abs(positions[0].shares - 25.0) < 1e-6
    assert engine.get_account().cash == 990.0
    engine.close()
