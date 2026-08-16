from __future__ import annotations

import logging
from pathlib import Path

import click

from papertrader.accounts import data_dir_from_env, make_engine
from papertrader.config import ROOT, load_settings
from papertrader.execution import get_shared_live_client
from papertrader.live import LiveTrader
from papertrader.loop import run_loop
from papertrader.mode import ModeError, load_dotenv_file, resolve_mode

log = logging.getLogger("papertrader")

_STRATEGIES = ("safe", "asymmetric", "both")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


@click.group()
def main() -> None:
    """Polymarket weather trader (paper by default; live is opt-in)."""
    load_dotenv_file(ROOT / ".env")
    _setup_logging()


def _mode_options(fn):
    fn = click.option(
        "--data-dir",
        type=click.Path(path_type=Path),
        default=data_dir_from_env,
        show_default=True,
    )(fn)
    fn = click.option(
        "--confirm-live",
        is_flag=True,
        help="Required (or PAPERTRADER_LIVE=1) before live CLOB orders.",
    )(fn)
    fn = click.option(
        "--mode",
        "cli_mode",
        type=click.Choice(["paper", "test", "live"], case_sensitive=False),
        default=None,
        help="paper/test = simulator (default). live = real CLOB orders.",
    )(fn)
    return fn


def _start(
    *,
    strategy: str,
    dry_run: bool,
    once: bool,
    reset: bool,
    data_dir: Path,
    cli_mode: str | None,
    confirm_live: bool,
) -> None:
    settings = load_settings()
    try:
        resolved = resolve_mode(
            settings_mode=settings.mode,
            cli_mode=cli_mode,
            confirm_live=confirm_live,
            data_dir=data_dir,
            clob_host=settings.live.clob_host,
            chain_id=settings.live.chain_id,
            signature_type=settings.live.signature_type,
            funder=settings.live.funder,
        )
    except ModeError as e:
        raise click.ClickException(str(e)) from e
    if resolved.is_live and reset:
        raise click.ClickException("Refusing --reset in live mode.")
    live = None
    if resolved.is_live:
        log.warning(
            "LIVE MODE: real Polymarket CLOB orders. Ledger: %s",
            resolved.data_dir,
        )
        live = LiveTrader(get_shared_live_client(resolved))
    else:
        log.info("Paper/test mode. Simulator ledger: %s", resolved.data_dir)
    safe_engine = None
    asymmetric_engine = None
    if strategy in ("safe", "both"):
        safe_engine = make_engine("safe", resolved.data_dir, settings.starting_balance, reset=reset)
    if strategy in ("asymmetric", "both"):
        asymmetric_engine = make_engine(
            "asymmetric", resolved.data_dir, settings.starting_balance, reset=reset
        )
    run_loop(
        settings=settings,
        safe_engine=safe_engine,
        asymmetric_engine=asymmetric_engine,
        dry_run=dry_run,
        once=once,
        live=live,
        data_dir=resolved.data_dir,
    )


@main.command("run")
@click.option("--strategy", type=click.Choice(_STRATEGIES), default="both")
@click.option("--dry-run", is_flag=True, help="Log would-be trades without filling.")
@click.option("--once", is_flag=True, help="Run a single scan then exit.")
@click.option("--reset", is_flag=True, help="Wipe paper accounts and start from configured balance.")
@_mode_options
def run_cmd(
    strategy: str,
    dry_run: bool,
    once: bool,
    reset: bool,
    data_dir: Path,
    cli_mode: str | None,
    confirm_live: bool,
) -> None:
    _start(
        strategy=strategy,
        dry_run=dry_run,
        once=once,
        reset=reset,
        data_dir=data_dir,
        cli_mode=cli_mode,
        confirm_live=confirm_live,
    )


@main.command("scan")
@click.option("--strategy", type=click.Choice(_STRATEGIES), default="both")
@click.option("--dry-run/--no-dry-run", default=True)
@_mode_options
def scan_cmd(
    strategy: str,
    dry_run: bool,
    data_dir: Path,
    cli_mode: str | None,
    confirm_live: bool,
) -> None:
    """One discovery pass (dry-run by default)."""
    _start(
        strategy=strategy,
        dry_run=dry_run,
        once=True,
        reset=False,
        data_dir=data_dir,
        cli_mode=cli_mode,
        confirm_live=confirm_live,
    )


@main.command("status")
@click.option("--mode", "cli_mode", type=click.Choice(["paper", "test", "live"], case_sensitive=False), default=None)
@click.option("--data-dir", type=click.Path(path_type=Path), default=None)
def status_cmd(cli_mode: str | None, data_dir: Path | None) -> None:
    """Print portfolio snapshots for `safe` and `asymmetric`."""
    settings = load_settings()
    try:
        resolved = resolve_mode(
            settings_mode=settings.mode,
            cli_mode=cli_mode,
            confirm_live=False,
            data_dir=data_dir_from_env(data_dir),
            clob_host=settings.live.clob_host,
            chain_id=settings.live.chain_id,
            signature_type=settings.live.signature_type,
            funder=settings.live.funder,
            require_credentials=False,
        )
    except ModeError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"mode: {resolved.mode}  data: {resolved.data_dir}")
    for name in ("safe", "asymmetric"):
        engine = make_engine(name, resolved.data_dir, settings.starting_balance)
        try:
            acct = engine.get_account()
            positions = engine.db.get_open_positions()
            click.echo(f"=== {name} ===")
            click.echo(f"  cash: ${acct.cash:.2f}  starting: ${acct.starting_balance:.2f}")
            click.echo(f"  open positions: {len(positions)}")
            for p in positions:
                click.echo(
                    f"    {p.market_slug} {p.outcome} shares={p.shares:.2f} "
                    f"avg={p.avg_entry_price:.3f} cost=${p.total_cost:.2f}"
                )
        finally:
            engine.close()


@main.command("dashboard")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8787, show_default=True, type=int)
def dashboard_cmd(host: str, port: int) -> None:
    """Web dashboard: P&L, ROI, positions, trade history."""
    try:
        from papertrader.dashboard.app import run_dashboard
    except ImportError as e:
        raise click.ClickException(
            "Dashboard needs Flask. Install with: pip install -e '.[dashboard]'"
        ) from e
    click.echo(f"Dashboard http://{host}:{port}")
    run_dashboard(host=host, port=port)
