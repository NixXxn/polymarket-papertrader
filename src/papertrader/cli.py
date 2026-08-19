from __future__ import annotations

import logging
from pathlib import Path

import click

from papertrader.accounts import data_dir_from_env, make_engine, reset_all_strategies
from papertrader.config import ROOT, load_settings
from papertrader.execution import get_shared_live_client
from papertrader.live import LiveTrader, PyClobLiveClient
from papertrader.loop import run_copy_loop, run_esports_loop, run_loop, run_momentum_loop
from papertrader.mode import ModeError, load_dotenv_file, resolve_mode

log = logging.getLogger("papertrader")

_STRATEGIES = ("safe", "asymmetric", "contrarian", "both", "copy", "esports", "momentum")


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
    contrarian_engine = None
    copy_engine = None
    esports_engine = None
    if strategy in ("safe", "both"):
        safe_engine = make_engine("safe", resolved.data_dir, settings.starting_balance, reset=reset)
    if strategy in ("asymmetric", "both"):
        asymmetric_engine = make_engine(
            "asymmetric", resolved.data_dir, settings.starting_balance, reset=reset
        )
    if strategy == "contrarian":
        contrarian_engine = make_engine(
            "contrarian", resolved.data_dir, settings.starting_balance, reset=reset
        )
    if strategy in ("esports", "both"):
        esports_engine = make_engine(
            "esports", resolved.data_dir, settings.starting_balance, reset=reset
        )
    if strategy == "copy":
        copy_engine = make_engine("copy", resolved.data_dir, settings.starting_balance, reset=reset)
        if live is not None:
            live.sync_cash(copy_engine)
        run_copy_loop(
            settings=settings,
            copy_engine=copy_engine,
            dry_run=dry_run,
            once=once,
            live=live,
            data_dir=resolved.data_dir,
        )
        return
    if strategy == "esports":
        if live is not None:
            live.sync_cash(esports_engine)
        run_esports_loop(
            settings=settings,
            esports_engine=esports_engine,
            dry_run=dry_run,
            once=once,
            live=live,
            data_dir=resolved.data_dir,
        )
        return
    if strategy == "momentum":
        momentum_engine = make_engine(
            "momentum", resolved.data_dir, settings.starting_balance, reset=reset
        )
        if live is not None:
            live.sync_cash(momentum_engine)
        run_momentum_loop(
            settings=settings,
            momentum_engine=momentum_engine,
            dry_run=dry_run,
            once=once,
            live=live,
            data_dir=resolved.data_dir,
        )
        return
    run_loop(
        settings=settings,
        safe_engine=safe_engine,
        asymmetric_engine=asymmetric_engine,
        contrarian_engine=contrarian_engine,
        copy_engine=copy_engine,
        esports_engine=esports_engine,
        dry_run=dry_run,
        once=once,
        live=live,
        data_dir=resolved.data_dir,
    )


@main.command("run")
@click.option(
    "--strategy",
    type=click.Choice(_STRATEGIES),
    default="both",
    help="both = safe + asymmetric + esports; esports runs on its own poll interval.",
)
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
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("py_clob_client_v2").setLevel(logging.WARNING)
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
    live_client: LiveTrader | PyClobLiveClient | None = None
    if resolved.is_live and resolved.private_key:
        try:
            live_client = PyClobLiveClient(resolved)
        except Exception as e:
            click.echo(f"wallet: unavailable ({e})")
    wallet_bal: float | None = None
    if live_client is not None:
        wallet_bal = live_client.get_balance()
        funder = resolved.funder or "(EOA)"
        click.echo(f"wallet: {funder}")
        if wallet_bal is None:
            click.echo("  CLOB balance: unavailable")
        else:
            click.echo(f"  CLOB balance: ${wallet_bal:.2f}")
    for name in ("safe", "asymmetric", "contrarian", "copy", "esports", "momentum"):
        engine = make_engine(name, resolved.data_dir, settings.starting_balance)
        try:
            if (
                resolved.is_live
                and live_client is not None
                and wallet_bal is not None
                and name == "copy"
            ):
                LiveTrader(live_client).sync_cash(engine)
            elif name in ("safe", "asymmetric", "contrarian", "esports", "momentum"):
                acct = engine.get_account()
                if acct.cash == 0 and acct.starting_balance == 0:
                    engine.init_account(settings.starting_balance)
            acct = engine.get_account()
            positions = engine.db.get_open_positions()
            click.echo(f"=== {name} ===")
            if resolved.is_live and wallet_bal is not None and name == "copy":
                click.echo(f"  cash: ${acct.cash:.2f}  (synced from CLOB)")
            else:
                click.echo(f"  cash: ${acct.cash:.2f}  starting: ${acct.starting_balance:.2f}")
            click.echo(f"  open positions: {len(positions)}")
            for p in positions:
                click.echo(
                    f"    {p.market_slug} {p.outcome} shares={p.shares:.2f} "
                    f"avg={p.avg_entry_price:.3f} cost=${p.total_cost:.2f}"
                )
        finally:
            engine.close()


@main.command("reset-balances")
@click.option(
    "--balance",
    type=float,
    default=None,
    help="Cash per strategy (default: starting_balance from settings).",
)
@click.option("--data-dir", type=click.Path(path_type=Path), default=data_dir_from_env, show_default=True)
@click.option("--mode", "cli_mode", type=click.Choice(["paper", "test", "live"], case_sensitive=False), default=None)
def reset_balances_cmd(balance: float | None, data_dir: Path, cli_mode: str | None) -> None:
    """Wipe all strategy ledgers and set cash to the configured balance."""
    settings = load_settings()
    try:
        resolved = resolve_mode(
            settings_mode=settings.mode,
            cli_mode=cli_mode,
            confirm_live=False,
            data_dir=data_dir,
            clob_host=settings.live.clob_host,
            chain_id=settings.live.chain_id,
            signature_type=settings.live.signature_type,
            funder=settings.live.funder,
            require_credentials=False,
        )
    except ModeError as e:
        raise click.ClickException(str(e)) from e
    amount = balance if balance is not None else settings.starting_balance
    if resolved.is_live:
        click.echo(
            click.style("Warning: ", fg="yellow")
            + "resetting local ledgers only — does not move funds on Polymarket."
        )
    click.echo(f"Resetting all strategies under {resolved.data_dir} to ${amount:.2f}")
    for name, cash, starting in reset_all_strategies(resolved.data_dir, amount):
        click.echo(f"  {name}: cash=${cash:.2f} starting=${starting:.2f}")


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
