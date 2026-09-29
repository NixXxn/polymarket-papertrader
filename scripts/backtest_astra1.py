#!/usr/bin/env python3
"""Strict historical *signal study* for Astra1.

This is intentionally not called an executable backtest.  The supplied trade
archive contains prints, not contemporaneous L2 books, queue position or two
leg fill acknowledgements.  It asks the narrower question: did both outcomes
print taker buys in the same second at enough size that a fee/latency-stressed
complete set would still have been below $1?

Run from the repository root (install ``.[backtest]`` first):

    python scripts/backtest_astra1.py \
      --trades data/backtest/trades_selected.parquet \
      --universe data/backtest/universe_selected.parquet
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl


def _fee_rate(category: str | None, slug: str | None) -> int:
    """Conservative documented V2 taker schedules, in bps, for the archive.

    Production Astra1 obtains the per-token schedule from CLOB for every
    quote.  The historical export does not retain it, so these are explicit
    assumptions: crypto 7%, sports 5%, and 5% elsewhere (rather than silently
    pretending we know past market-specific schedules).
    """
    text = f"{category or ''} {slug or ''}".lower()
    if any(word in text for word in ("crypto", "bitcoin", "btc", "ethereum", "eth", "solana", "xrp", "doge")):
        return 700
    return 500


def _split(timestamp: int) -> str:
    day = datetime.fromtimestamp(timestamp, UTC).date().isoformat()
    if day <= "2025-12-31":
        return "train"
    if day <= "2026-03-31":
        return "validation"
    return "test"


def run(args: argparse.Namespace) -> dict[str, Any]:
    trades = Path(args.trades)
    universe = Path(args.universe)
    if not trades.exists() or not universe.exists():
        raise SystemExit("--trades and --universe must point at existing parquet files")

    # Conservative proxy for contemporaneous executable ask capacity: both
    # outcome tokens must have a taker BUY print in the exact same second, and
    # the worst price/aggregate observed size of that second is used.
    side_seconds = (
        pl.scan_parquet(trades)
        .filter((pl.col("taker_direction") == "BUY") & pl.col("nonusdc_side").is_in(["token1", "token2"]))
        .group_by(["market_id", "timestamp", "nonusdc_side"])
        .agg(
            pl.col("price").max().alias("worst_price"),
            pl.col("token_amount").sum().alias("shares"),
        )
    )
    token1 = side_seconds.filter(pl.col("nonusdc_side") == "token1").select(
        "market_id", "timestamp", pl.col("worst_price").alias("price_1"), pl.col("shares").alias("shares_1")
    )
    token2 = side_seconds.filter(pl.col("nonusdc_side") == "token2").select(
        "market_id", "timestamp", pl.col("worst_price").alias("price_2"), pl.col("shares").alias("shares_2")
    )
    rows = (
        token1.join(token2, on=["market_id", "timestamp"], how="inner")
        .join(
            pl.scan_parquet(universe).select("market_id", "slug", "category", "end_ts"),
            on="market_id",
            how="inner",
        )
        .filter(pl.col("timestamp") < pl.col("end_ts"))
        .filter((pl.col("shares_1") >= args.target_shares) & (pl.col("shares_2") >= args.target_shares))
        .collect(engine="streaming")
    )
    candidates: list[dict[str, Any]] = []
    for row in rows.iter_rows(named=True):
        rate = _fee_rate(row.get("category"), row.get("slug"))
        price_1, price_2 = float(row["price_1"]), float(row["price_2"])
        shares = float(args.target_shares)
        fees = shares * (rate / 10_000) * (price_1 * (1 - price_1) + price_2 * (1 - price_2))
        all_in = price_1 + price_2 + fees / shares + args.latency_buffer
        if all_in > args.max_all_in_cost:
            continue
        candidates.append({**row, "fee_bps": rate, "all_in": all_in, "edge": 1 - all_in})
    # First qualifying opportunity only per market: no free re-use of capital.
    candidates.sort(key=lambda r: (int(r["timestamp"]), str(r["market_id"])))
    first: dict[str, dict[str, Any]] = {}
    for row in candidates:
        first.setdefault(str(row["market_id"]), row)

    def summarize(records: list[dict[str, Any]], *, fee_mult: float = 1.0, latency: float | None = None) -> dict[str, Any]:
        latency = args.latency_buffer if latency is None else latency
        pnl = []
        costs = []
        for r in records:
            p1, p2, shares, rate = float(r["price_1"]), float(r["price_2"]), float(args.target_shares), float(r["fee_bps"])
            fee = shares * (rate / 10_000) * (p1 * (1 - p1) + p2 * (1 - p2)) * fee_mult
            cost = shares * (p1 + p2 + latency) + fee
            pnl.append(shares - cost)
            costs.append(cost / shares)
        return {
            "markets": len(records),
            "signal_study_pnl_usd": round(sum(pnl), 2),
            "average_pnl_per_pair_usd": round(sum(pnl) / len(pnl), 4) if pnl else 0.0,
            "mean_all_in_cost": round(sum(costs) / len(costs), 6) if costs else None,
        }

    all_records = list(first.values())
    splits = {name: [row for row in all_records if _split(int(row["timestamp"])) == name] for name in ("train", "validation", "test")}
    result = {
        "kind": "Astra1 historical signal study — NOT executable performance",
        "limitations": [
            "Trade prints are a same-second proxy, not archived L2 depth or queue priority.",
            "Sequential YES/NO fills are not atomic; orphan/excess-leg costs are excluded.",
            "Historical fee schedules were not retained; documented category assumptions are stated below.",
        ],
        "assumptions": {
            "target_shares": args.target_shares,
            "latency_buffer_per_share": args.latency_buffer,
            "max_all_in_cost": args.max_all_in_cost,
            "fees": "crypto 700 bps; all other archived categories 500 bps; exact V2 p*(1-p) formula",
            "entry": "both tokens taker-BUY in same timestamp second; worst printed buy price and >= target shares each",
        },
        "splits": {name: summarize(records) for name, records in splits.items()},
        "stress_2x_fee_3c_latency": {name: summarize(records, fee_mult=2.0, latency=0.03) for name, records in splits.items()},
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trades", required=True)
    parser.add_argument("--universe", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--target-shares", type=float, default=25.0)
    parser.add_argument("--latency-buffer", type=float, default=0.01)
    parser.add_argument("--max-all-in-cost", type=float, default=0.97)
    args = parser.parse_args()
    result = run(args)
    rendered = json.dumps(result, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n")


if __name__ == "__main__":
    main()
