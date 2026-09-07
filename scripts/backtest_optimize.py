#!/usr/bin/env python3
"""Historical backtest + parameter search over data/backtest/bars_5m_wide.parquet.

Optimizes price-path strategies that can be simulated from trade prints.
Forecast-dependent weather models use price proxies (documented in results).
"""

from __future__ import annotations

import itertools
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import polars as pl
import yaml

ROOT = Path(__file__).resolve().parents[1]
BARS = ROOT / "data/backtest/bars_5m_wide.parquet"
OUT = ROOT / "data/backtest/optimize_results.json"
SETTINGS = ROOT / "config/settings.yaml"

STAKE = 25.0  # fixed paper stake per entry


@dataclass
class Result:
    strategy: str
    params: dict[str, Any]
    trades: int
    wins: int
    pnl: float
    roi: float
    win_rate: float


def _hold_to_res_pnl(price: float, side: str, winner: str, answer1: str, answer2: str, stake: float) -> float:
    if price <= 0.01 or price >= 0.99:
        return 0.0
    shares = stake / price
    won = (side == "a1" and winner == answer1) or (side == "a2" and winner == answer2)
    return (shares - stake) if won else (-stake)


def sim_closingsoon(df: pl.DataFrame, params: dict[str, Any]) -> Result:
    min_h, max_h = params["min_hours"], params["max_hours"]
    pmin, pmax = params["price_min"], params["price_max"]
    weather_only = params.get("weather_only", True)
    rows = df
    if weather_only:
        rows = rows.filter(pl.col("category") == "weather")
    rows = rows.filter(pl.col("p1").is_not_null())
    rows = rows.with_columns(((pl.col("end_ts") - pl.col("bar_ts")) / 3600.0).alias("hours_left"))
    hits = rows.filter(
        (pl.col("hours_left") >= min_h)
        & (pl.col("hours_left") <= max_h)
        & (pl.col("p1") >= pmin)
        & (pl.col("p1") <= pmax)
    )
    # first hit per market
    first = hits.sort("bar_ts").group_by("market_id").first()
    pnls = []
    for r in first.iter_rows(named=True):
        pnls.append(_hold_to_res_pnl(r["p1"], "a1", r["winner"], r["answer1"], r["answer2"], STAKE))
    return _pack("closingsoon", params, pnls)


def sim_momentum(df: pl.DataFrame, params: dict[str, Any]) -> Result:
    trigger = params["entry_trigger_price"]
    tp = params.get("take_profit_price")
    sl = params.get("stop_loss_price")
    rows = df.filter(pl.col("category").is_in(["weather", "crypto", "other"]) & pl.col("p1").is_not_null())
    hits = rows.filter(pl.col("p1") >= trigger).sort("bar_ts").group_by("market_id").first()
    pnls = []
    # For each entry, optional early exit using later bars of same market
    by_m = {mid: g.sort("bar_ts") for mid, g in rows.group_by("market_id")}
    for r in hits.iter_rows(named=True):
        entry_p = float(r["p1"])
        entry_ts = int(r["bar_ts"])
        g = by_m.get(r["market_id"])
        exited = False
        if g is not None and (tp or sl):
            after = g.filter(pl.col("bar_ts") > entry_ts)
            for b in after.iter_rows(named=True):
                px = b["p1"]
                if px is None:
                    continue
                if tp and px >= tp:
                    shares = STAKE / entry_p
                    pnls.append(shares * px - STAKE)
                    exited = True
                    break
                if sl and px <= sl:
                    shares = STAKE / entry_p
                    pnls.append(shares * px - STAKE)
                    exited = True
                    break
        if not exited:
            pnls.append(_hold_to_res_pnl(entry_p, "a1", r["winner"], r["answer1"], r["answer2"], STAKE))
    return _pack("momentum", params, pnls)


def sim_contrarian_proxy(df: pl.DataFrame, params: dict[str, Any]) -> Result:
    """Fade longshot YES: buy NO when YES price in [min_yes, max_yes]."""
    ymin, ymax = params["min_yes_ask"], params["max_yes_ask"]
    max_no = params["max_no_ask"]
    cats = params.get("categories", ["weather", "crypto", "other"])
    rows = df.filter(pl.col("category").is_in(cats) & pl.col("p1").is_not_null() & pl.col("p2").is_not_null())
    hits = rows.filter(
        (pl.col("p1") >= ymin) & (pl.col("p1") <= ymax) & (pl.col("p2") <= max_no) & (pl.col("p2") >= 0.05)
    ).sort("bar_ts").group_by("market_id").first()
    pnls = []
    for r in hits.iter_rows(named=True):
        pnls.append(_hold_to_res_pnl(r["p2"], "a2", r["winner"], r["answer1"], r["answer2"], STAKE))
    return _pack("contrarian", params, pnls)


def sim_safe_proxy(df: pl.DataFrame, params: dict[str, Any]) -> Result:
    """Near-resolution favorites on weather."""
    amin, amax = params["min_ask"], params["max_ask"]
    max_hours = params["max_hours"]
    rows = df.filter(pl.col("category") == "weather").filter(pl.col("p1").is_not_null())
    rows = rows.with_columns(((pl.col("end_ts") - pl.col("bar_ts")) / 3600.0).alias("hours_left"))
    hits = rows.filter(
        (pl.col("hours_left") > 0)
        & (pl.col("hours_left") <= max_hours)
        & (pl.col("p1") >= amin)
        & (pl.col("p1") <= amax)
    ).sort("bar_ts").group_by("market_id").first()
    pnls = []
    for r in hits.iter_rows(named=True):
        pnls.append(_hold_to_res_pnl(r["p1"], "a1", r["winner"], r["answer1"], r["answer2"], STAKE))
    return _pack("safe", params, pnls)


def sim_arbitrage(df: pl.DataFrame, params: dict[str, Any]) -> Result:
    max_pair = params["max_pair_cost"]
    prefer = params.get("prefer_crypto_weather", True)
    rows = df.filter(pl.col("p1").is_not_null() & pl.col("p2").is_not_null())
    if prefer:
        rows = rows.filter(pl.col("category").is_in(["crypto", "weather", "btc5m"]))
    rows = rows.with_columns((pl.col("p1") + pl.col("p2")).alias("pair"))
    hits = rows.filter((pl.col("pair") <= max_pair) & (pl.col("pair") >= 0.5)).sort("bar_ts").group_by("market_id").first()
    pnls = []
    for r in hits.iter_rows(named=True):
        # equal shares funded by stake on combined cost
        pair = float(r["p1"] + r["p2"])
        if pair <= 0:
            continue
        shares = STAKE / pair
        # one side always pays $1
        pnls.append(shares * 1.0 - STAKE)
    return _pack("arbitrage", params, pnls)


def sim_meanrev(df: pl.DataFrame, params: dict[str, Any]) -> Result:
    window = params["rolling_window"]
    min_z = params["min_z_score"]
    pmin, pmax = params["price_min"], params["price_max"]
    rows = df.filter(pl.col("category").is_in(["crypto", "other", "weather"]) & pl.col("p1").is_not_null())
    pnls: list[float] = []
    entered: set[str] = set()
    for mid, g in rows.sort(["market_id", "bar_ts"]).group_by("market_id"):
        market_id = mid[0] if isinstance(mid, tuple) else mid
        g = g.sort("bar_ts")
        closes = g["p1"].to_list()
        meta = g.select(["winner", "answer1", "answer2", "bar_ts"]).to_dicts()
        if len(closes) < window + 2:
            continue
        for i in range(window, len(closes)):
            window_vals = closes[i - window : i]
            mu = sum(window_vals) / window
            var = sum((x - mu) ** 2 for x in window_vals) / window
            sd = var ** 0.5
            if sd < 1e-4:
                continue
            z = (closes[i] - mu) / sd
            px = closes[i]
            if not (pmin <= px <= pmax):
                continue
            if market_id in entered:
                break
            if z <= -min_z:
                # buy yes (mean reversion up)
                pnls.append(_hold_to_res_pnl(px, "a1", meta[i]["winner"], meta[i]["answer1"], meta[i]["answer2"], STAKE))
                entered.add(market_id)
                break
            if z >= min_z:
                # fade: buy no if available
                # approximate no price as 1-px
                no_px = max(0.02, min(0.98, 1.0 - px))
                pnls.append(_hold_to_res_pnl(no_px, "a2", meta[i]["winner"], meta[i]["answer1"], meta[i]["answer2"], STAKE))
                entered.add(market_id)
                break
    return _pack("meanrev", params, pnls)


def sim_volspike(df: pl.DataFrame, params: dict[str, Any]) -> Result:
    hist = params["volume_history_len"]
    thr = params["spike_threshold"]
    pmin, pmax = params["price_min"], params["price_max"]
    rows = df.filter(pl.col("category").is_in(["crypto", "other", "weather"]) & pl.col("p1").is_not_null())
    pnls: list[float] = []
    entered: set[str] = set()
    for mid, g in rows.sort(["market_id", "bar_ts"]).group_by("market_id"):
        market_id = mid[0] if isinstance(mid, tuple) else mid
        g = g.sort("bar_ts")
        if g.height < hist + 2:
            continue
        vols = g["v1"].fill_null(0.0).to_list()
        closes = g["p1"].to_list()
        meta = g.select(["winner", "answer1", "answer2"]).to_dicts()
        for i in range(hist, len(closes)):
            base = sum(vols[i - hist : i]) / hist
            if base <= 0:
                continue
            if vols[i] < thr * base:
                continue
            px = closes[i]
            if px is None or not (pmin <= px <= pmax):
                continue
            if market_id in entered:
                break
            # follow direction vs prior bar
            prev = closes[i - 1]
            if prev is None:
                continue
            if px >= prev:
                pnls.append(_hold_to_res_pnl(px, "a1", meta[i]["winner"], meta[i]["answer1"], meta[i]["answer2"], STAKE))
            else:
                no_px = max(0.02, min(0.98, 1.0 - px))
                pnls.append(_hold_to_res_pnl(no_px, "a2", meta[i]["winner"], meta[i]["answer1"], meta[i]["answer2"], STAKE))
            entered.add(market_id)
            break
    return _pack("volspike", params, pnls)


def sim_btc5m(df: pl.DataFrame, params: dict[str, Any]) -> Result:
    min_ask, max_ask = params["min_ask"], params["max_ask"]
    confirm = params["min_confirm"]  # absolute price move from first bar
    rows = df.filter(pl.col("category") == "btc5m").filter(pl.col("p1").is_not_null())
    pnls: list[float] = []
    for mid, g in rows.sort(["market_id", "bar_ts"]).group_by("market_id"):
        g = g.sort("bar_ts")
        if g.height < 2:
            continue
        first = g.row(0, named=True)
        # use bar ~1 as confirmation (5m markets short)
        conf = g.row(min(1, g.height - 1), named=True)
        move = float(conf["p1"]) - float(first["p1"])
        # buy the leading side
        if abs(move) < confirm:
            continue
        if move > 0:
            px = float(conf["p1"])
            side = "a1"
        else:
            px = float(conf["p2"]) if conf.get("p2") is not None else max(0.02, 1 - float(conf["p1"]))
            side = "a2"
        if not (min_ask <= px <= max_ask):
            continue
        pnls.append(_hold_to_res_pnl(px, side, conf["winner"], conf["answer1"], conf["answer2"], STAKE))
    return _pack("btc5m", params, pnls)


def sim_asymmetric_proxy(df: pl.DataFrame, params: dict[str, Any]) -> Result:
    """Cheap YES lottery on weather: buy low ask, hold to res."""
    amin, amax = params["min_ask"], params["max_ask"]
    rows = df.filter(pl.col("category") == "weather").filter(pl.col("p1").is_not_null())
    hits = rows.filter((pl.col("p1") >= amin) & (pl.col("p1") <= amax)).sort("bar_ts").group_by("market_id").first()
    pnls = []
    for r in hits.iter_rows(named=True):
        pnls.append(_hold_to_res_pnl(r["p1"], "a1", r["winner"], r["answer1"], r["answer2"], STAKE))
    return _pack("asymmetric", params, pnls)


def _pack(name: str, params: dict[str, Any], pnls: list[float]) -> Result:
    trades = len(pnls)
    wins = sum(1 for x in pnls if x > 0)
    pnl = float(sum(pnls))
    stake_total = trades * STAKE
    roi = (pnl / stake_total) if stake_total else 0.0
    wr = (wins / trades) if trades else 0.0
    return Result(name, params, trades, wins, pnl, roi, wr)


def grid(params: dict[str, list]) -> list[dict[str, Any]]:
    keys = list(params)
    out = []
    for vals in itertools.product(*[params[k] for k in keys]):
        out.append(dict(zip(keys, vals)))
    return out


def optimize_one(
    name: str,
    df: pl.DataFrame,
    sim: Callable[[pl.DataFrame, dict[str, Any]], Result],
    space: dict[str, list],
    min_trades: int = 30,
) -> Result:
    best: Result | None = None
    tested = 0
    for params in grid(space):
        tested += 1
        r = sim(df, params)
        if r.trades < min_trades:
            continue
        # primary: pnl, then roi, then win_rate
        if best is None or (r.pnl, r.roi, r.win_rate) > (best.pnl, best.roi, best.win_rate):
            best = r
    if best is None:
        # fallback: allow fewer trades
        for params in grid(space):
            r = sim(df, params)
            if best is None or (r.pnl, r.roi) > (best.pnl, best.roi):
                best = r
    assert best is not None
    print(
        f"{name}: best pnl=${best.pnl:.0f} roi={best.roi:.1%} wr={best.win_rate:.1%} "
        f"n={best.trades} params={best.params} (tested {tested})"
    )
    return best


def apply_to_settings(best: dict[str, Result]) -> None:
    raw = yaml.safe_load(SETTINGS.read_text()) or {}

    def set_block(key: str, mapping: dict[str, Any]) -> None:
        block = dict(raw.get(key) or {})
        block.update(mapping)
        raw[key] = block

    if "closingsoon" in best:
        p = best["closingsoon"].params
        set_block(
            "closingsoon",
            {
                "weather_only": bool(p.get("weather_only", True)),
                "min_hours": float(p["min_hours"]),
                "max_hours": float(p["max_hours"]),
                "price_min": float(p["price_min"]),
                "price_max": float(p["price_max"]),
                "min_edge": 0.02,
                "position_usd": 35,
                "max_position_usd": 55,
            },
        )
    if "momentum" in best:
        p = best["momentum"].params
        set_block(
            "momentum",
            {
                "entry_trigger_price": float(p["entry_trigger_price"]),
                "take_profit_price": float(p["take_profit_price"]) if p.get("take_profit_price") else None,
                "stop_loss_price": float(p["stop_loss_price"]) if p.get("stop_loss_price") else None,
                "position_usd": 50,
                "max_position_usd": 95,
            },
        )
    if "contrarian" in best:
        p = best["contrarian"].params
        set_block(
            "contrarian",
            {
                "min_yes_ask": float(p["min_yes_ask"]),
                "max_yes_ask": float(p["max_yes_ask"]),
                "max_no_ask": float(p["max_no_ask"]),
                "min_edge": 0.04,
                "max_position_usd": 80,
            },
        )
        # conviction mirrors tighter
        set_block(
            "conviction",
            {
                "min_yes_ask": float(p["min_yes_ask"]),
                "max_yes_ask": min(float(p["max_yes_ask"]), 0.20),
                "max_no_ask": min(float(p["max_no_ask"]), 0.90),
                "min_edge": 0.05,
            },
        )
    if "safe" in best:
        p = best["safe"].params
        set_block(
            "safe",
            {
                "min_ask": float(p["min_ask"]),
                "max_ask": float(p["max_ask"]),
            },
        )
    if "arbitrage" in best:
        p = best["arbitrage"].params
        set_block(
            "arbitrage",
            {
                "max_pair_cost": float(p["max_pair_cost"]),
                "prefer_crypto_weather": bool(p.get("prefer_crypto_weather", True)),
                "position_usd": 45,
                "max_position_usd": 110,
            },
        )
    if "meanrev" in best:
        p = best["meanrev"].params
        set_block(
            "meanrev",
            {
                "rolling_window": int(p["rolling_window"]),
                "min_z_score": float(p["min_z_score"]),
                "price_min": float(p["price_min"]),
                "price_max": float(p["price_max"]),
                "position_usd": 28,
                "max_position_usd": 45,
            },
        )
    if "volspike" in best:
        p = best["volspike"].params
        set_block(
            "volspike",
            {
                "volume_history_len": int(p["volume_history_len"]),
                "spike_threshold": float(p["spike_threshold"]),
                "price_min": float(p["price_min"]),
                "price_max": float(p["price_max"]),
                "position_usd": 28,
                "max_position_usd": 50,
            },
        )
    if "btc5m" in best:
        p = best["btc5m"].params
        set_block(
            "btc5m",
            {
                "min_ask": float(p["min_ask"]),
                "max_ask": float(p["max_ask"]),
                "min_confirm_bps": float(p["min_confirm"]) * 10000,  # move -> bps approx on ~0.5
                "position_usd": 45,
                "max_position_usd": 80,
            },
        )
    if "asymmetric" in best:
        p = best["asymmetric"].params
        set_block(
            "asymmetric",
            {
                "min_ask": float(p["min_ask"]),
                "max_ask": float(p["max_ask"]),
                "position_usd": 8,
                "max_position_usd": 12,
            },
        )

    SETTINGS.write_text(yaml.safe_dump(raw, sort_keys=False, default_flow_style=False))
    print("updated", SETTINGS)


def main() -> None:
    print("loading bars…")
    df = pl.read_parquet(BARS)
    # downsample huge "other" to keep runtime sane: keep top-volume markets only for other
    meta = df.select(["market_id", "category", "volume"]).unique(subset=["market_id"])
    other_ids = (
        meta.filter(pl.col("category") == "other")
        .sort("volume", descending=True)
        .head(800)["market_id"]
        .to_list()
    )
    keep = df.filter(
        (pl.col("category") != "other") | (pl.col("market_id").is_in(other_ids))
    )
    print("bars", keep.height, "markets", keep["market_id"].n_unique())

    best: dict[str, Result] = {}
    best["closingsoon"] = optimize_one(
        "closingsoon",
        keep,
        sim_closingsoon,
        {
            "min_hours": [1, 2, 4, 6],
            "max_hours": [12, 24, 36, 48],
            "price_min": [0.70, 0.78, 0.85],
            "price_max": [0.92, 0.95, 0.97],
            "weather_only": [True, False],
        },
        min_trades=40,
    )
    best["safe"] = optimize_one(
        "safe",
        keep,
        sim_safe_proxy,
        {
            "min_ask": [0.58, 0.62, 0.68, 0.75],
            "max_ask": [0.88, 0.92, 0.95],
            "max_hours": [12, 24, 48],
        },
        min_trades=40,
    )
    best["asymmetric"] = optimize_one(
        "asymmetric",
        keep,
        sim_asymmetric_proxy,
        {
            "min_ask": [0.01, 0.02, 0.03, 0.05],
            "max_ask": [0.08, 0.12, 0.18, 0.25],
        },
        min_trades=40,
    )
    best["contrarian"] = optimize_one(
        "contrarian",
        keep,
        sim_contrarian_proxy,
        {
            "min_yes_ask": [0.02, 0.05, 0.08],
            "max_yes_ask": [0.15, 0.22, 0.30],
            "max_no_ask": [0.80, 0.87, 0.92],
            "categories": [["weather"], ["weather", "crypto"], ["weather", "crypto", "other"]],
        },
        min_trades=40,
    )
    best["momentum"] = optimize_one(
        "momentum",
        keep,
        sim_momentum,
        {
            "entry_trigger_price": [0.70, 0.80, 0.88, 0.92],
            "take_profit_price": [0.95, 0.97, None],
            "stop_loss_price": [0.55, 0.65, None],
        },
        min_trades=40,
    )
    best["arbitrage"] = optimize_one(
        "arbitrage",
        keep,
        sim_arbitrage,
        {
            "max_pair_cost": [0.94, 0.96, 0.97, 0.98, 0.99],
            "prefer_crypto_weather": [True, False],
        },
        min_trades=20,
    )
    best["meanrev"] = optimize_one(
        "meanrev",
        keep,
        sim_meanrev,
        {
            "rolling_window": [6, 12, 24],
            "min_z_score": [1.5, 2.0, 2.5],
            "price_min": [0.15, 0.25],
            "price_max": [0.75, 0.85],
        },
        min_trades=30,
    )
    best["volspike"] = optimize_one(
        "volspike",
        keep,
        sim_volspike,
        {
            "volume_history_len": [6, 12],
            "spike_threshold": [2.0, 3.0, 4.0],
            "price_min": [0.20, 0.30],
            "price_max": [0.80, 0.90],
        },
        min_trades=30,
    )
    best["btc5m"] = optimize_one(
        "btc5m",
        keep,
        sim_btc5m,
        {
            "min_ask": [0.52, 0.58, 0.62],
            "max_ask": [0.78, 0.85, 0.90],
            "min_confirm": [0.02, 0.04, 0.06],
        },
        min_trades=30,
    )

    payload = {
        name: {
            **asdict(r),
            "note": (
                "price-proxy backtest on historic trades; "
                "weather forecast strategies approximated without meteorological inputs"
            ),
        }
        for name, r in best.items()
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print("wrote", OUT)
    apply_to_settings(best)
    # ranking summary
    ranked = sorted(best.values(), key=lambda r: r.pnl, reverse=True)
    print("\n=== RANKING BY PnL ===")
    for r in ranked:
        print(f"{r.strategy:12} pnl=${r.pnl:9.0f} roi={r.roi:7.1%} wr={r.win_rate:5.1%} n={r.trades}")


if __name__ == "__main__":
    main()
