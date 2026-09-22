# Polymarket weather paper trader

Paper-only by default. Trades **daily high temperature** markets on Polymarket using live order books from [`polymarket-paper-trader`](https://github.com/agent-next/polymarket-paper-trader). Weather logic is ported from [`polymarket-weather-bot`](https://github.com/tobiasbischoff/polymarket-weather-bot). **No CLOB orders are placed unless you switch to live.**

Forecasts and METARs use **resolution airport stations** (e.g. Miami `KMIA`, Atlanta `KATL`, NYC `KLGA`), not city-center coordinates.

Do not treat paper P&L as a live result.

## Paper vs live

`config/settings.yaml` has `mode: paper` (aliases: `test`, `sim`). Flip the switch with any of:

- YAML: `mode: live`
- CLI: `--mode live`
- Env: `PAPERTRADER_MODE=live`

CLI wins, then env, then yaml. Default is paper.

Live still cannot send orders unless **both**:

1. `--confirm-live` **or** `PAPERTRADER_LIVE=1`
2. `PAPERTRADER_PRIVATE_KEY` in the environment

Install the CLOB client first: `pip install -e ".[live]"`.

Live uses a **separate ledger** (`~/.pm-trader-live`) so paper history is never mixed. Safe and asymmetric keep isolated sqlite cash for sizing; they submit to the **same** wallet, and a live buy is rejected if that wallet’s CLOB balance is too low. `--reset` is refused in live mode.

```bash
# still the simulator
papertrader run --strategy both --once

# real orders (wallet must already have pUSD + CLOB approvals)
export PAPERTRADER_LIVE=1
export PAPERTRADER_PRIVATE_KEY=0x...
# export PAPERTRADER_FUNDER=0x...   # Magic/proxy wallet
papertrader run --mode live --confirm-live --once
```

## Strategies

`--strategy both` (default) runs **safe + asymmetric**. Use `--strategy safe` or `--strategy asymmetric` for one account only.

### 1. Safe

Miami and Atlanta only. NOAA + Open-Meteo consensus high must land in a bucket; skip if the two sources disagree by more than 3°F. Buys YES when ask &lt; 0.60 and forecast edge clears a GFS-window threshold (10–25%). Exits if the consensus high leaves the held bucket.

### 2. Asymmetric (tail-risk arb)

Cheap YES on tail buckets ($0.02–$0.10). Every scan pulls **GFS + ECMWF** ensemble members from Open-Meteo; optional **OpenWeather** spot check via `OPENWEATHER_API_KEY` in `.env`. Enters when ensemble probability is well above the market (e.g. model 20%+ vs ask 5¢). **Hedge exit** when bid reaches ~$0.35 (forecast went mainstream) or the bucket becomes physically impossible.

## Setup

Python 3.10+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# optional: OPENWEATHER_API_KEY=... in .env
```

Paper accounts are created automatically on first run under `~/.pm-trader/safe` and `~/.pm-trader/asymmetric` ($50 each by default).

## Usage

```bash
papertrader scan --dry-run          # one pass, no fills
papertrader run --strategy both     # loop every 3 minutes (safe + asymmetric)
papertrader run --strategy safe --once
papertrader run --strategy asymmetric --dry-run
papertrader status
papertrader status --mode live      # inspect the live ledger only
```

Knobs live in `config/settings.yaml` and `config/cities.yaml`. Bet size autoscales with each account’s cash versus `starting_balance` (floor `min_position_usd`).

## Dashboard

```bash
pip install -e ".[dashboard]"
papertrader dashboard
# → http://127.0.0.1:8787
```

Shows combined **P&L**, **ROI**, win rate, per-strategy breakdown (safe / asymmetric), open positions with unrealized P&L, trade history, equity curve, and legacy **copy** account stats if `~/.pm-trader/copy` exists. Optional HTTP basic auth via `DASHBOARD_USER` / `DASHBOARD_PASSWORD` in `.env`. Refreshes every 15s.

Scan snapshots are appended to `{data_dir}/scan_history.jsonl` while `papertrader run` is active.

## Deploy (Coolify / Docker)

See **[DEPLOY.md](DEPLOY.md)** for Coolify import via Git or Dockerfile.

```bash
docker compose up -d --build   # local smoke test
```

Default container runs **trader + dashboard** on port **8787** with data in volume `/data`.

## Tests

```bash
pytest
```
