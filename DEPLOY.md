# Coolify Deployment

Deploy via **Git repository** or **Dockerfile** in one Coolify resource.

## Quick start (Coolify UI)

1. **New Resource** → **Application** → connect your Git repo (or paste repo URL).
2. **Build Pack**: Dockerfile (auto-detected from repo root).
3. **Port**: `8787` (Coolify → Network → Ports Exposes).
4. **Volume**: mount persistent storage at `/data` (required for paper ledger + scan history).
5. **Environment**: copy from [`.env.coolify.example`](.env.coolify.example); set at least `DASHBOARD_PASSWORD`.
6. Deploy.

Default: **trader + dashboard** in one container (`SERVICE=both`).

## Health check

Coolify can use:

- Path: `/health`
- Port: `8787`

The Dockerfile and `docker-compose.yml` include a healthcheck on that endpoint.

## Service modes

| `SERVICE` | What runs |
|-----------|-----------|
| `both` | `papertrader run` (background) + Gunicorn dashboard (default) |
| `dashboard` | Dashboard only (read-only UI) |
| `trader` | Scan loop only (no HTTP) |

| `STRATEGY` | Accounts |
|------------|----------|
| `both` | safe + asymmetric (default) |
| `safe` | safe only |
| `asymmetric` | asymmetric only |

## Persistent data

All SQLite ledgers and `scan_history.jsonl` live under `PAPERTRADER_DATA_DIR` (default `/data`).

In Coolify: **Storages** → add volume → mount path `/data`.

Without a volume, data is lost on redeploy.

## Docker Compose (local or Coolify Compose)

```bash
cp .env.coolify.example .env
# edit .env (password, API keys)
docker compose up -d --build
open http://localhost:8787
```

## Docker only

```bash
docker build -t papertrader .
docker run -d --name papertrader \
  -p 8787:8787 \
  -v papertrader-data:/data \
  -e DASHBOARD_PASSWORD=secret \
  -e OPENWEATHER_API_KEY=your-key \
  papertrader
```

## Live mode (optional)

Only enable when the wallet is funded and you accept real orders:

```env
PAPERTRADER_MODE=live
PAPERTRADER_LIVE=1
PAPERTRADER_PRIVATE_KEY=0x...
```

Also set `PAPERTRADER_FUNDER` for proxy/Magic wallets. Live ledger uses the same `/data` volume unless you override paths in config.

## Coolify checklist

- [ ] Git repo connected, branch selected
- [ ] Dockerfile build (no custom build command needed)
- [ ] Port **8787** exposed
- [ ] Volume **`/data`** attached
- [ ] `DASHBOARD_PASSWORD` set
- [ ] `OPENWEATHER_API_KEY` set (asymmetric forecasts)
- [ ] Domain + HTTPS (Coolify proxy) for dashboard access
