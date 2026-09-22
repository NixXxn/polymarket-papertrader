from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


@dataclass(frozen=True)
class City:
    name: str
    slug: str
    station: str
    lat: float
    lon: float
    tz: str
    country: str
    strategies: tuple[str, ...]
    position_usd: float


@dataclass(frozen=True)
class EdgeSettings:
    min_ask: float
    max_ask: float
    target_ask: float
    min_possible: float
    min_edge: float
    take_profit: float
    sell_bias: float
    stop_loss: float
    min_event_volume: float
    min_best_ask_size: float
    max_spread: float
    position_usd: float
    max_position_usd: float
    max_open_positions: int
    max_notional_at_risk: float
    min_sell_bid: float
    max_hourly_rise_f: float
    high_hour_local: int


@dataclass(frozen=True)
class ExitLadderStep:
    multiple: float
    fraction: float


@dataclass(frozen=True)
class AsymmetricSettings:
    min_ask: float
    max_ask: float
    min_model_prob: float
    min_prob_ratio: float
    min_edge: float
    take_profit_bid: float
    exit_model_prob: float
    stop_loss_bid: float
    min_sell_bid: float
    min_event_volume: float
    min_ensemble_members: int
    position_usd: float
    max_position_usd: float
    max_open_positions: int
    max_hourly_rise_f: float
    high_hour_local: int
    cities: tuple[str, ...]
    preferred_limit: float
    fallback_limit: float
    maker_tick: float
    high_conf_max_limit: float
    high_conf_min_ratio: float
    min_dual_edge: float
    strict_limit: bool
    exit_ladder: tuple[ExitLadderStep, ...] = ()
    hours_before_resolution: int = 1
    exit_model_prob_min_days_ahead: int = 0


@dataclass(frozen=True)
class OddsPapiSettings:
    enabled: bool
    min_edge: float
    primary_bookmaker: str
    fallback_bookmakers: tuple[str, ...]
    sport_ids: tuple[int, ...]
    max_daily_requests: int
    max_monthly_requests: int
    refresh_interval_hours: float
    kelly_fraction: float
    maker_edge_cents: float
    require_match: bool
    max_tournaments_per_sport: int
    base_url: str


@dataclass(frozen=True)
class ContrarianSettings:
    min_yes_ask: float
    max_yes_ask: float
    max_model_yes: float
    min_edge: float
    min_vig_edge: float
    maker_tick: float
    max_no_bets_per_event: int
    kelly_fraction: float
    max_event_fraction: float
    min_event_volume: float
    min_ensemble_members: int
    max_position_usd: float
    max_open_positions: int
    max_open_per_city: int
    take_profit_no_bid: float
    stop_loss_no_bid: float
    min_no_entry: float
    max_no_ask: float
    min_days_ahead: int
    max_days_ahead: int
    starting_balance: float | None
    exit_model_yes: float
    exit_model_prob_min_days_ahead: int
    min_sell_bid: float
    max_hourly_rise_f: float
    high_hour_local: int
    # HEART: L2 ask walk + strict BUY LIMIT (never unbounded FAK past EV).
    strict_limit: bool
    book_walk_min_ev: float
    # Bayes shadow: market prior × shrunk model LR (log only; no size change).
    bayes_shadow: bool
    bayes_lr_shrink: float
    bayes_max_lr: float
    bayes_fee_buffer: float
    cities: tuple[str, ...]


@dataclass(frozen=True)
class EsportsSettings:
    horizon_hours: float
    poll_interval_seconds: int
    min_ask: float
    max_ask: float
    take_profit_pct: float
    stop_loss_entry_pct: float
    position_usd: float
    max_position_usd: float
    max_open_positions: int
    min_event_volume: float
    search_queries: tuple[str, ...]
    event_tags: tuple[str, ...]
    search_limit: int
    tag_slug: str
    exclude_slug_patterns: tuple[str, ...]
    oddspapi: OddsPapiSettings


@dataclass(frozen=True)
class MomentumSettings:
    ws_url: str
    use_websocket: bool
    poll_interval_seconds: int
    mode: str
    specific_token_id: str
    entry_trigger_price: float
    take_profit_price: float | None
    stop_loss_price: float | None
    max_entry_price: float
    order_size_shares: float
    use_share_sizing: bool
    position_usd: float
    max_position_usd: float
    max_open_positions: int
    min_event_volume: float
    entry_price_buffer: float
    exit_slippage_buffer: float
    cities: tuple[str, ...]


@dataclass(frozen=True)
class VolumeSpikeSettings:
    min_liquidity: float
    price_min: float
    price_max: float
    volume_history_len: int
    spike_threshold: float
    min_edge: float
    min_confidence: float
    kelly_fraction: float
    max_position_usd: float
    max_open_positions: int
    position_usd: float
    stop_loss_pct: float
    take_profit_pct: float
    allow_cold_start: bool


@dataclass(frozen=True)
class ArbitrageSettings:
    """Two-leg spread capture: buy both sides when combined cost < $1."""

    max_pair_cost: float
    max_maker_ask_sum: float
    min_edge: float
    fee_buffer: float
    min_liquidity: float
    min_volume_24h: float
    min_ask: float
    max_ask: float
    min_ask_size: float
    position_usd: float
    max_position_usd: float
    max_open_pairs: int
    maker_tick: float
    paper_fak: bool
    prefer_crypto_weather: bool
    prefer_lp_rewards: bool
    scan_limit: int
    starting_balance: float | None
    # Legacy ladder fields kept for YAML compat; exits no longer use them.
    exit_ladder_prices: tuple[float, ...]
    exit_ladder_fraction: float
    lose_leg_bid_max: float
    lose_leg_bid_min: float
    lose_leg_lead_bid: float
    rebalance_enabled: bool
    rebalance_move: float
    rebalance_fraction: float
    rebalance_min_lead: float
    # Sell both legs when pair MTM ≥ cost×(1+pct) or bid_a+bid_b ≥ threshold.
    min_pair_profit_pct: float
    pair_bid_sum_exit: float


@dataclass(frozen=True)
class WeatherlockSettings:
    """Buy weather NO with enough edge that ~90% WR can still be +EV."""

    buy_min: float
    buy_max: float
    sell_limit: float
    take_profit_offset: float
    stop_bid: float | None
    min_ask_size: float
    min_event_volume: float
    min_days_ahead: int
    max_days_ahead: int
    position_usd: float
    max_position_usd: float
    max_open_positions: int
    max_open_per_event: int
    paper_fill_at_limit: bool
    include_lowest: bool
    starting_balance: float | None
    cities: tuple[str, ...]


@dataclass(frozen=True)
class EndgameSettings:
    """Sports Yes/No near-expiry locks with capped size and earlier stop."""

    min_minutes: float
    max_minutes: float
    look_ahead_minutes: float
    price_min: float
    price_max: float
    min_liquidity: float
    min_ask_size: float
    use_full_capital: bool
    position_usd: float
    max_position_usd: float
    max_open_positions: int
    sell_limit: float
    take_profit_offset: float
    stop_bid: float
    paper_fill_at_limit: bool
    poll_interval_seconds: int
    starting_balance: float | None
    yes_no_only: bool


@dataclass(frozen=True)
class CopySettings:
    username: str
    wallet: str
    wallets: tuple[str, ...] = ()
    scale: float | None = None
    poll_interval_ms: int = 2000
    recent_limit: int = 50

    @property
    def poll_interval_seconds(self) -> float:
        return max(0.05, float(self.poll_interval_ms) / 1000.0)


@dataclass(frozen=True)
class LiveSettings:
    clob_host: str
    chain_id: int
    signature_type: int
    funder: str


@dataclass(frozen=True)
class IntelSettings:
    """World-intel style overlays (macro + event risk + BTC regime)."""

    enabled: bool
    shadow_only: bool
    ttl_seconds: float
    fail_open_on_error: bool
    block_event_score: int
    caution_size_mult: float
    btc_min_fear_greed: int
    strategies: tuple[str, ...]


@dataclass(frozen=True)
class PredictionHuntSettings:
    """PredictionHunt cross-platform edge + arb signals (tier-aware)."""

    enabled: bool
    shadow_only: bool
    min_request_interval_seconds: float
    max_monthly_requests: int
    max_matched_monthly: int
    max_arb_monthly: int
    cache_ttl_hours: float
    min_cross_platform_count: int
    min_dislocation: float
    use_matching_markets: bool
    scan_arb: bool
    arb_min_roi: float
    arb_limit: int
    arb_platforms: str
    execute_polymarket_arb_legs: bool
    strategies: tuple[str, ...]


@dataclass(frozen=True)
class FadeFinderSettings:
    """Prediction Hunt fade-finder / smart-money + sports cross-platform fades."""

    poll_interval_seconds: int
    use_fade_alerts: bool
    use_smart_money_alerts: bool
    sports_fallback: bool
    sports: tuple[str, ...]
    min_dislocation: float
    min_whale_stake_usd: float
    min_no_ask: float
    max_no_ask: float
    min_yes_ask: float
    max_yes_ask: float
    position_usd: float
    max_position_usd: float
    max_open_positions: int
    take_profit_pct: float
    stop_loss_entry_pct: float
    alert_lookback_hours: float
    alert_limit: int
    sports_per_scan: int
    sports_cache_ttl_hours: float
    starting_balance: float | None


@dataclass(frozen=True)
class AdaptiveSizingSettings:
    """Vol-regime Kelly: size up when σ_current < σ_rolling (past-only bars)."""

    enabled: bool
    rolling_window: int
    recent_window: int
    min_observations: int
    regime_floor: float
    regime_cap: float
    strategies: tuple[str, ...]


@dataclass(frozen=True)
class Settings:
    poll_interval_seconds: int
    horizon_days: int
    starting_balance: float
    min_position_usd: float
    min_event_volume: float
    min_best_ask_size: float
    forecast_confidence: float
    forecast_disagreement_f: float
    user_agent: str
    mode: str
    live: LiveSettings
    intel: IntelSettings
    adaptive_sizing: AdaptiveSizingSettings
    predictionhunt: PredictionHuntSettings
    fadefinder: FadeFinderSettings
    asymmetric: AsymmetricSettings
    contrarian: ContrarianSettings
    conviction: ContrarianSettings
    esports: EsportsSettings
    momentum: MomentumSettings
    volspike: VolumeSpikeSettings
    arbitrage: ArbitrageSettings
    weatherlock: WeatherlockSettings
    endgame: EndgameSettings
    edge: EdgeSettings
    copy: CopySettings
    cities: dict[str, City] = field(default_factory=dict)

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    def cities_for(self, strategy: str) -> list[City]:
        return [c for c in self.cities.values() if strategy in c.strategies]


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open() as f:
        return yaml.safe_load(f) or {}


def _parse_arb_ladder_prices(raw: Any) -> tuple[float, ...]:
    default = (0.50, 0.70, 0.85, 0.95)
    if raw is None:
        return default
    if raw == []:
        return ()
    prices: list[float] = []
    for row in raw:
        try:
            prices.append(float(row))
        except (TypeError, ValueError):
            continue
    return tuple(sorted(set(prices))) if prices else default


def _parse_exit_ladder(raw: Any) -> tuple[ExitLadderStep, ...]:
    default = (
        ExitLadderStep(2.0, 0.10),
        ExitLadderStep(5.0, 0.10),
        ExitLadderStep(10.0, 0.15),
        ExitLadderStep(20.0, 0.15),
        ExitLadderStep(50.0, 0.10),
    )
    if raw is None:
        return default
    if raw == []:
        return ()
    steps: list[ExitLadderStep] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        steps.append(
            ExitLadderStep(
                multiple=float(row["multiple"]),
                fraction=float(row["fraction"]),
            )
        )
    return tuple(steps) if steps else default


def _parse_contrarian_block(raw_cfg: dict[str, Any], *, defaults: dict[str, Any]) -> ContrarianSettings:
    merged = {**defaults, **raw_cfg}
    sb = merged.get("starting_balance")
    min_yes_ask = float(merged.get("min_yes_ask", 0.02))
    max_yes_ask = float(merged.get("max_yes_ask", 0.20))
    min_no_entry = float(merged.get("min_no_entry", 0.50))
    max_no_ask = float(merged.get("max_no_ask", 0.92))
    # Real CLOB books usually have yes_ask + no_ask ≈ 1.02–1.06. If the YES
    # ceiling sits below what the NO floor implies, every bucket is rejected.
    if max_yes_ask + min_no_entry < 0.95:
        import logging

        logging.getLogger(__name__).warning(
            "contrarian/conviction bands likely incompatible: "
            "max_yes_ask=%.3f + min_no_entry=%.3f < 0.95 (no overlapping books)",
            max_yes_ask,
            min_no_entry,
        )
    return ContrarianSettings(
        min_yes_ask=min_yes_ask,
        max_yes_ask=max_yes_ask,
        max_model_yes=float(merged.get("max_model_yes", 0.08)),
        min_edge=float(merged.get("min_edge", 0.06)),
        min_vig_edge=float(merged.get("min_vig_edge", 0.01)),
        maker_tick=float(merged.get("maker_tick", 0.01)),
        max_no_bets_per_event=int(merged.get("max_no_bets_per_event", 3)),
        kelly_fraction=float(merged.get("kelly_fraction", 0.25)),
        max_event_fraction=float(merged.get("max_event_fraction", 0.10)),
        min_event_volume=float(merged.get("min_event_volume", 200)),
        min_ensemble_members=int(merged.get("min_ensemble_members", 8)),
        max_position_usd=float(merged.get("max_position_usd", 25)),
        max_open_positions=int(merged.get("max_open_positions", 30)),
        max_open_per_city=int(merged.get("max_open_per_city", 2)),
        take_profit_no_bid=float(merged.get("take_profit_no_bid", 0.85)),
        stop_loss_no_bid=float(merged.get("stop_loss_no_bid", 0.35)),
        min_no_entry=min_no_entry,
        max_no_ask=max_no_ask,
        min_days_ahead=int(merged.get("min_days_ahead", 0)),
        max_days_ahead=int(merged.get("max_days_ahead", 2)),
        starting_balance=float(sb) if sb is not None else None,
        exit_model_yes=float(merged.get("exit_model_yes", 0.15)),
        exit_model_prob_min_days_ahead=int(
            merged.get("exit_model_prob_min_days_ahead", 0)
        ),
        min_sell_bid=float(merged.get("min_sell_bid", 0.02)),
        max_hourly_rise_f=float(merged.get("max_hourly_rise_f", 4.0)),
        high_hour_local=int(merged.get("high_hour_local", 20)),
        strict_limit=bool(merged.get("strict_limit", True)),
        book_walk_min_ev=float(merged.get("book_walk_min_ev", 0.0)),
        bayes_shadow=bool(merged.get("bayes_shadow", True)),
        bayes_lr_shrink=float(merged.get("bayes_lr_shrink", 0.5)),
        bayes_max_lr=float(merged.get("bayes_max_lr", 8.0)),
        bayes_fee_buffer=float(merged.get("bayes_fee_buffer", 0.01)),
        cities=tuple(merged.get("cities") or ()),
    )


def load_settings(
    settings_path: Path | None = None,
    cities_path: Path | None = None,
) -> Settings:
    raw = _load_yaml(settings_path or CONFIG_DIR / "settings.yaml")
    cities_raw = _load_yaml(cities_path or CONFIG_DIR / "cities.yaml")
    cities: dict[str, City] = {}
    for slug, row in (cities_raw.get("cities") or {}).items():
        cities[slug] = City(
            name=row["name"],
            slug=row.get("slug", slug),
            station=row["station"],
            lat=float(row["lat"]),
            lon=float(row["lon"]),
            tz=row["tz"],
            country=row["country"],
            strategies=tuple(row.get("strategies") or []),
            position_usd=float(row.get("position_usd", 10)),
        )
    from papertrader.mode import PAPER, normalize_mode

    asymmetric_raw = raw["asymmetric"]
    edge_raw = raw.get("edge") or {}
    contrarian_raw = raw.get("contrarian") or {}
    conviction_raw = raw.get("conviction") or {}
    esports_raw = raw.get("esports") or {}
    fadefinder_raw = raw.get("fadefinder") or {}
    oddspapi_raw = esports_raw.get("oddspapi") or {}
    momentum_raw = raw.get("momentum") or {}
    copy_raw = raw.get("copy") or {}
    volspike_raw = raw.get("volspike") or {}
    arbitrage_raw = raw.get("arbitrage") or {}
    weatherlock_raw = raw.get("weatherlock") or {}
    endgame_raw = raw.get("endgame") or {}
    live_raw = raw.get("live") or {}
    intel_raw = raw.get("intel") or {}
    adaptive_raw = raw.get("adaptive_sizing") or {}
    predictionhunt_raw = raw.get("predictionhunt") or {}
    mode = normalize_mode(str(raw.get("mode") or PAPER))
    return Settings(
        poll_interval_seconds=int(raw["poll_interval_seconds"]),
        horizon_days=int(raw["horizon_days"]),
        starting_balance=float(raw["starting_balance"]),
        min_position_usd=float(raw.get("min_position_usd", 1.0)),
        min_event_volume=float(raw["min_event_volume"]),
        min_best_ask_size=float(raw["min_best_ask_size"]),
        forecast_confidence=float(raw["forecast_confidence"]),
        forecast_disagreement_f=float(raw["forecast_disagreement_f"]),
        user_agent=str(raw["user_agent"]),
        mode=mode,
        live=LiveSettings(
            clob_host=str(live_raw.get("clob_host") or "https://clob.polymarket.com"),
            chain_id=int(live_raw.get("chain_id") or 137),
            signature_type=int(live_raw.get("signature_type") or 1),
            funder=str(live_raw.get("funder") or ""),
        ),
        intel=IntelSettings(
            enabled=bool(intel_raw.get("enabled", True)),
            # Start in shadow: log vetoes without blocking, then flip false after review.
            shadow_only=bool(intel_raw.get("shadow_only", False)),
            ttl_seconds=float(intel_raw.get("ttl_seconds", 900)),
            fail_open_on_error=bool(intel_raw.get("fail_open_on_error", True)),
            block_event_score=int(intel_raw.get("block_event_score", 65)),
            caution_size_mult=float(intel_raw.get("caution_size_mult", 0.40)),
            btc_min_fear_greed=int(intel_raw.get("btc_min_fear_greed", 45)),
            strategies=tuple(intel_raw.get("strategies") or ("volspike",)),
        ),
        adaptive_sizing=AdaptiveSizingSettings(
            enabled=bool(adaptive_raw.get("enabled", True)),
            rolling_window=int(adaptive_raw.get("rolling_window", 36)),
            recent_window=int(adaptive_raw.get("recent_window", 9)),
            min_observations=int(adaptive_raw.get("min_observations", 8)),
            regime_floor=float(adaptive_raw.get("regime_floor", 0.0)),
            regime_cap=float(adaptive_raw.get("regime_cap", 1.0)),
            strategies=tuple(
                adaptive_raw.get("strategies") or ("asymmetric", "contrarian", "conviction")
            ),
        ),
        predictionhunt=PredictionHuntSettings(
            enabled=bool(predictionhunt_raw.get("enabled", True)),
            shadow_only=bool(predictionhunt_raw.get("shadow_only", True)),
            min_request_interval_seconds=float(
                predictionhunt_raw.get("min_request_interval_seconds", 1.1)
            ),
            max_monthly_requests=int(predictionhunt_raw.get("max_monthly_requests", 950)),
            max_matched_monthly=int(predictionhunt_raw.get("max_matched_monthly", 8)),
            max_arb_monthly=int(predictionhunt_raw.get("max_arb_monthly", 450)),
            cache_ttl_hours=float(predictionhunt_raw.get("cache_ttl_hours", 12)),
            min_cross_platform_count=int(
                predictionhunt_raw.get("min_cross_platform_count", 2)
            ),
            min_dislocation=float(predictionhunt_raw.get("min_dislocation", 0.02)),
            use_matching_markets=bool(
                predictionhunt_raw.get("use_matching_markets", False)
            ),
            scan_arb=bool(predictionhunt_raw.get("scan_arb", True)),
            arb_min_roi=float(predictionhunt_raw.get("arb_min_roi", 0.5)),
            arb_limit=int(predictionhunt_raw.get("arb_limit", 20)),
            arb_platforms=str(
                predictionhunt_raw.get("arb_platforms") or "polymarket,kalshi"
            ),
            execute_polymarket_arb_legs=bool(
                predictionhunt_raw.get("execute_polymarket_arb_legs", True)
            ),
            strategies=tuple(
                predictionhunt_raw.get("strategies")
                or ("contrarian", "conviction", "fadefinder", "arbitrage")
            ),
        ),
        asymmetric=AsymmetricSettings(
            min_ask=float(asymmetric_raw["min_ask"]),
            max_ask=float(asymmetric_raw["max_ask"]),
            min_model_prob=float(asymmetric_raw["min_model_prob"]),
            min_prob_ratio=float(asymmetric_raw["min_prob_ratio"]),
            min_edge=float(asymmetric_raw["min_edge"]),
            take_profit_bid=float(asymmetric_raw["take_profit_bid"]),
            exit_model_prob=float(asymmetric_raw["exit_model_prob"]),
            stop_loss_bid=float(asymmetric_raw["stop_loss_bid"]),
            min_sell_bid=float(asymmetric_raw["min_sell_bid"]),
            min_event_volume=float(
                asymmetric_raw.get("min_event_volume", raw["min_event_volume"])
            ),
            min_ensemble_members=int(asymmetric_raw.get("min_ensemble_members", 8)),
            position_usd=float(asymmetric_raw["position_usd"]),
            max_position_usd=float(asymmetric_raw.get("max_position_usd", 2)),
            max_open_positions=int(asymmetric_raw["max_open_positions"]),
            max_hourly_rise_f=float(asymmetric_raw.get("max_hourly_rise_f", 4.0)),
            high_hour_local=int(asymmetric_raw.get("high_hour_local", 20)),
            cities=tuple(asymmetric_raw.get("cities") or ()),
            exit_ladder=_parse_exit_ladder(asymmetric_raw.get("exit_ladder")),
            hours_before_resolution=int(
                asymmetric_raw.get("hours_before_resolution", 1)
            ),
            exit_model_prob_min_days_ahead=int(
                asymmetric_raw.get("exit_model_prob_min_days_ahead", 0)
            ),
            preferred_limit=float(asymmetric_raw.get("preferred_limit", 0.01)),
            fallback_limit=float(asymmetric_raw.get("fallback_limit", 0.02)),
            maker_tick=float(asymmetric_raw.get("maker_tick", 0.01)),
            high_conf_max_limit=float(asymmetric_raw.get("high_conf_max_limit", 0.10)),
            high_conf_min_ratio=float(asymmetric_raw.get("high_conf_min_ratio", 2.5)),
            min_dual_edge=float(asymmetric_raw.get("min_dual_edge", 0.012)),
            strict_limit=bool(asymmetric_raw.get("strict_limit", True)),
        ),
        contrarian=_parse_contrarian_block(
            contrarian_raw,
            defaults={
                "min_event_volume": raw.get("min_event_volume", 200),
                "min_days_ahead": 0,
                "max_days_ahead": 2,
            },
        ),
        conviction=_parse_contrarian_block(
            conviction_raw or contrarian_raw,
            defaults={
                "min_event_volume": raw.get("min_event_volume", 200),
                "min_days_ahead": 0,
                "max_days_ahead": 0,
                "max_model_yes": 0.04,
                "min_edge": 0.048,
                "max_no_ask": 0.80,
                "min_ensemble_members": 10,
                "max_open_per_city": 1,
            },
        ),
        esports=EsportsSettings(
            horizon_hours=float(esports_raw.get("horizon_hours", 6)),
            poll_interval_seconds=int(esports_raw.get("poll_interval_seconds", 60)),
            min_ask=float(esports_raw.get("min_ask", 0.02)),
            max_ask=float(esports_raw.get("max_ask", 0.45)),
            take_profit_pct=float(esports_raw.get("take_profit_pct", 0.20)),
            stop_loss_entry_pct=float(esports_raw.get("stop_loss_entry_pct", 0.80)),
            position_usd=float(esports_raw.get("position_usd", 5)),
            max_position_usd=float(esports_raw.get("max_position_usd", 25)),
            max_open_positions=int(esports_raw.get("max_open_positions", 20)),
            min_event_volume=float(
                esports_raw.get("min_event_volume", raw.get("min_event_volume", 200))
            ),
            search_queries=tuple(
                esports_raw.get("search_queries")
                or ("lck", "lpl", "lec", "vct", "cs2", "dota2", "lol")
            ),
            event_tags=tuple(
                esports_raw.get("event_tags")
                or ("league-of-legends", "valorant", "dota-2", "esports")
            ),
            search_limit=int(esports_raw.get("search_limit", 40)),
            tag_slug=str(esports_raw.get("tag_slug") or "esports"),
            exclude_slug_patterns=tuple(esports_raw.get("exclude_slug_patterns") or ()),
            oddspapi=OddsPapiSettings(
                enabled=bool(oddspapi_raw.get("enabled", True)),
                min_edge=float(oddspapi_raw.get("min_edge", 0.06)),
                primary_bookmaker=str(oddspapi_raw.get("primary_bookmaker") or "pinnacle"),
                fallback_bookmakers=tuple(
                    oddspapi_raw.get("fallback_bookmakers") or ("ggbet", "bet365")
                ),
                sport_ids=tuple(
                    int(x) for x in (oddspapi_raw.get("sport_ids") or (17, 18, 16, 61))
                ),
                max_daily_requests=int(oddspapi_raw.get("max_daily_requests", 8)),
                max_monthly_requests=int(oddspapi_raw.get("max_monthly_requests", 245)),
                refresh_interval_hours=float(
                    oddspapi_raw.get("refresh_interval_hours", 3)
                ),
                kelly_fraction=float(oddspapi_raw.get("kelly_fraction", 0.25)),
                maker_edge_cents=float(oddspapi_raw.get("maker_edge_cents", 0.02)),
                require_match=bool(oddspapi_raw.get("require_match", False)),
                max_tournaments_per_sport=int(
                    oddspapi_raw.get("max_tournaments_per_sport", 8)
                ),
                base_url=str(
                    oddspapi_raw.get("base_url") or "https://api.oddspapi.io/v4"
                ),
            ),
        ),
        fadefinder=FadeFinderSettings(
            poll_interval_seconds=int(fadefinder_raw.get("poll_interval_seconds", 120)),
            use_fade_alerts=bool(fadefinder_raw.get("use_fade_alerts", True)),
            use_smart_money_alerts=bool(
                fadefinder_raw.get("use_smart_money_alerts", False)
            ),
            sports_fallback=bool(fadefinder_raw.get("sports_fallback", True)),
            sports=tuple(
                fadefinder_raw.get("sports")
                or ("nfl", "nba", "mlb", "nhl", "lol", "cs2")
            ),
            min_dislocation=float(
                fadefinder_raw.get("min_dislocation")
                or predictionhunt_raw.get("min_dislocation", 0.04)
            ),
            min_whale_stake_usd=float(fadefinder_raw.get("min_whale_stake_usd", 500)),
            min_no_ask=float(fadefinder_raw.get("min_no_ask", 0.12)),
            max_no_ask=float(fadefinder_raw.get("max_no_ask", 0.92)),
            min_yes_ask=float(fadefinder_raw.get("min_yes_ask", 0.08)),
            max_yes_ask=float(fadefinder_raw.get("max_yes_ask", 0.85)),
            position_usd=float(fadefinder_raw.get("position_usd", 5)),
            max_position_usd=float(fadefinder_raw.get("max_position_usd", 15)),
            max_open_positions=int(fadefinder_raw.get("max_open_positions", 8)),
            take_profit_pct=float(fadefinder_raw.get("take_profit_pct", 0.15)),
            stop_loss_entry_pct=float(fadefinder_raw.get("stop_loss_entry_pct", 0.70)),
            alert_lookback_hours=float(fadefinder_raw.get("alert_lookback_hours", 24)),
            alert_limit=int(fadefinder_raw.get("alert_limit", 50)),
            sports_per_scan=int(fadefinder_raw.get("sports_per_scan", 1)),
            sports_cache_ttl_hours=float(
                fadefinder_raw.get("sports_cache_ttl_hours", 24)
            ),
            starting_balance=(
                float(fadefinder_raw["starting_balance"])
                if fadefinder_raw.get("starting_balance") is not None
                else None
            ),
        ),
        momentum=MomentumSettings(
            ws_url=str(
                momentum_raw.get("ws_url")
                or "wss://ws-subscriptions-clob.polymarket.com/ws/market"
            ),
            use_websocket=bool(momentum_raw.get("use_websocket", True)),
            poll_interval_seconds=int(momentum_raw.get("poll_interval_seconds", 5)),
            mode=str(momentum_raw.get("mode") or "ANY_BUCKET").upper(),
            specific_token_id=str(momentum_raw.get("specific_token_id") or ""),
            entry_trigger_price=float(momentum_raw.get("entry_trigger_price", 0.85)),
            take_profit_price=(
                float(momentum_raw["take_profit_price"])
                if momentum_raw.get("take_profit_price") is not None
                else 0.98
            ),
            stop_loss_price=(
                float(momentum_raw["stop_loss_price"])
                if momentum_raw.get("stop_loss_price") is not None
                else None
            ),
            max_entry_price=float(momentum_raw.get("max_entry_price", 0.97)),
            order_size_shares=float(momentum_raw.get("order_size_shares", 50.0)),
            use_share_sizing=bool(momentum_raw.get("use_share_sizing", True)),
            position_usd=float(momentum_raw.get("position_usd", 50)),
            max_position_usd=float(momentum_raw.get("max_position_usd", 100)),
            max_open_positions=int(momentum_raw.get("max_open_positions", 1)),
            min_event_volume=float(
                momentum_raw.get("min_event_volume", raw.get("min_event_volume", 200))
            ),
            entry_price_buffer=float(momentum_raw.get("entry_price_buffer", 0.01)),
            exit_slippage_buffer=float(momentum_raw.get("exit_slippage_buffer", 0.01)),
            cities=tuple(momentum_raw.get("cities") or ("nyc", "miami", "atlanta")),
        ),
        volspike=VolumeSpikeSettings(
            min_liquidity=float(volspike_raw.get("min_liquidity", 5000)),
            price_min=float(volspike_raw.get("price_min", 0.05)),
            price_max=float(volspike_raw.get("price_max", 0.95)),
            volume_history_len=int(volspike_raw.get("volume_history_len", 48)),
            spike_threshold=float(volspike_raw.get("spike_threshold", 3.0)),
            min_edge=float(volspike_raw.get("min_edge", 0.05)),
            min_confidence=float(volspike_raw.get("min_confidence", 0.55)),
            kelly_fraction=float(volspike_raw.get("kelly_fraction", 0.25)),
            max_position_usd=float(volspike_raw.get("max_position_usd", 25)),
            max_open_positions=int(volspike_raw.get("max_open_positions", 10)),
            position_usd=float(volspike_raw.get("position_usd", 10)),
            stop_loss_pct=float(volspike_raw.get("stop_loss_pct", 0.20)),
            take_profit_pct=float(volspike_raw.get("take_profit_pct", 0.15)),
            allow_cold_start=bool(volspike_raw.get("allow_cold_start", False)),
        ),
        arbitrage=ArbitrageSettings(
            max_pair_cost=float(arbitrage_raw.get("max_pair_cost", 0.97)),
            max_maker_ask_sum=float(arbitrage_raw.get("max_maker_ask_sum", 1.04)),
            min_edge=float(arbitrage_raw.get("min_edge", 0.025)),
            fee_buffer=float(arbitrage_raw.get("fee_buffer", 0.01)),
            min_liquidity=float(arbitrage_raw.get("min_liquidity", 400.0)),
            min_volume_24h=float(arbitrage_raw.get("min_volume_24h", 150.0)),
            min_ask=float(arbitrage_raw.get("min_ask", 0.02)),
            max_ask=float(arbitrage_raw.get("max_ask", 0.95)),
            min_ask_size=float(arbitrage_raw.get("min_ask_size", 3.0)),
            position_usd=float(arbitrage_raw.get("position_usd", 40.0)),
            max_position_usd=float(arbitrage_raw.get("max_position_usd", 100.0)),
            max_open_pairs=int(arbitrage_raw.get("max_open_pairs", 8)),
            maker_tick=float(arbitrage_raw.get("maker_tick", 0.01)),
            paper_fak=bool(arbitrage_raw.get("paper_fak", True)),
            prefer_crypto_weather=bool(arbitrage_raw.get("prefer_crypto_weather", True)),
            prefer_lp_rewards=bool(arbitrage_raw.get("prefer_lp_rewards", True)),
            scan_limit=int(arbitrage_raw.get("scan_limit", 250)),
            starting_balance=(
                float(arbitrage_raw["starting_balance"])
                if arbitrage_raw.get("starting_balance") is not None
                else None
            ),
            exit_ladder_prices=_parse_arb_ladder_prices(
                arbitrage_raw.get("exit_ladder_prices")
            ),
            exit_ladder_fraction=float(arbitrage_raw.get("exit_ladder_fraction", 0.0)),
            lose_leg_bid_max=float(arbitrage_raw.get("lose_leg_bid_max", 0.35)),
            lose_leg_bid_min=float(arbitrage_raw.get("lose_leg_bid_min", 0.05)),
            lose_leg_lead_bid=float(arbitrage_raw.get("lose_leg_lead_bid", 0.55)),
            rebalance_enabled=bool(arbitrage_raw.get("rebalance_enabled", False)),
            rebalance_move=float(arbitrage_raw.get("rebalance_move", 0.04)),
            rebalance_fraction=float(arbitrage_raw.get("rebalance_fraction", 0.10)),
            rebalance_min_lead=float(arbitrage_raw.get("rebalance_min_lead", 0.55)),
            min_pair_profit_pct=float(arbitrage_raw.get("min_pair_profit_pct", 0.005)),
            pair_bid_sum_exit=float(arbitrage_raw.get("pair_bid_sum_exit", 0.99)),
        ),
        weatherlock=WeatherlockSettings(
            buy_min=float(weatherlock_raw.get("buy_min", 0.88)),
            buy_max=float(weatherlock_raw.get("buy_max", 0.92)),
            sell_limit=float(weatherlock_raw.get("sell_limit", 0.99)),
            take_profit_offset=float(weatherlock_raw.get("take_profit_offset", 0.06)),
            stop_bid=(
                float(weatherlock_raw["stop_bid"])
                if weatherlock_raw.get("stop_bid") is not None
                else None
            ),
            min_ask_size=float(weatherlock_raw.get("min_ask_size", 1.0)),
            min_event_volume=float(
                weatherlock_raw.get(
                    "min_event_volume", raw.get("min_event_volume", 100)
                )
            ),
            min_days_ahead=int(weatherlock_raw.get("min_days_ahead", 0)),
            max_days_ahead=int(weatherlock_raw.get("max_days_ahead", 2)),
            position_usd=float(weatherlock_raw.get("position_usd", 25.0)),
            max_position_usd=float(weatherlock_raw.get("max_position_usd", 100.0)),
            max_open_positions=int(weatherlock_raw.get("max_open_positions", 20)),
            max_open_per_event=int(weatherlock_raw.get("max_open_per_event", 4)),
            paper_fill_at_limit=bool(weatherlock_raw.get("paper_fill_at_limit", True)),
            include_lowest=bool(weatherlock_raw.get("include_lowest", True)),
            starting_balance=(
                float(weatherlock_raw["starting_balance"])
                if weatherlock_raw.get("starting_balance") is not None
                else None
            ),
            cities=tuple(weatherlock_raw.get("cities") or ()),
        ),
        endgame=EndgameSettings(
            min_minutes=float(endgame_raw.get("min_minutes", 0.0)),
            max_minutes=float(endgame_raw.get("max_minutes", 6.0)),
            look_ahead_minutes=float(endgame_raw.get("look_ahead_minutes", 360.0)),
            price_min=float(endgame_raw.get("price_min", 0.90)),
            price_max=float(endgame_raw.get("price_max", 0.95)),
            min_liquidity=float(endgame_raw.get("min_liquidity", 250.0)),
            min_ask_size=float(endgame_raw.get("min_ask_size", 8.0)),
            use_full_capital=bool(endgame_raw.get("use_full_capital", False)),
            position_usd=float(endgame_raw.get("position_usd", 150.0)),
            max_position_usd=float(endgame_raw.get("max_position_usd", 250.0)),
            max_open_positions=int(endgame_raw.get("max_open_positions", 2)),
            sell_limit=float(endgame_raw.get("sell_limit", 0.98)),
            take_profit_offset=float(endgame_raw.get("take_profit_offset", 0.05)),
            stop_bid=float(endgame_raw.get("stop_bid", 0.78)),
            paper_fill_at_limit=bool(endgame_raw.get("paper_fill_at_limit", True)),
            poll_interval_seconds=int(endgame_raw.get("poll_interval_seconds", 20)),
            starting_balance=(
                float(endgame_raw["starting_balance"])
                if endgame_raw.get("starting_balance") is not None
                else None
            ),
            yes_no_only=bool(endgame_raw.get("yes_no_only", True)),
        ),
        edge=EdgeSettings(
            min_ask=float(edge_raw.get("min_ask", 0.45)),
            max_ask=float(edge_raw.get("max_ask", 0.52)),
            target_ask=float(edge_raw.get("target_ask", 0.48)),
            min_possible=float(edge_raw.get("min_possible", 0.40)),
            min_edge=float(edge_raw.get("min_edge", 0.02)),
            take_profit=float(edge_raw.get("take_profit", 0.10)),
            sell_bias=float(edge_raw.get("sell_bias", 0.44)),
            stop_loss=float(edge_raw.get("stop_loss", 0.04)),
            min_event_volume=float(edge_raw.get("min_event_volume", raw["min_event_volume"])),
            min_best_ask_size=float(edge_raw.get("min_best_ask_size", raw["min_best_ask_size"])),
            max_spread=float(edge_raw.get("max_spread", 0.04)),
            position_usd=float(edge_raw.get("position_usd", 2)),
            max_position_usd=float(edge_raw.get("max_position_usd", 5)),
            max_open_positions=int(edge_raw.get("max_open_positions", 10)),
            max_notional_at_risk=float(edge_raw.get("max_notional_at_risk", 40)),
            min_sell_bid=float(edge_raw.get("min_sell_bid", 0.01)),
            max_hourly_rise_f=float(edge_raw.get("max_hourly_rise_f", 4.0)),
            high_hour_local=int(edge_raw.get("high_hour_local", 20)),
        ),
        copy=CopySettings(
            username=str(copy_raw.get("username") or "").lstrip("@"),
            wallet=str(copy_raw.get("wallet") or "").lower(),
            wallets=tuple(
                str(w).lower()
                for w in (copy_raw.get("wallets") or [])
                if str(w).strip()
            ),
            scale=float(copy_raw["scale"]) if copy_raw.get("scale") is not None else None,
            poll_interval_ms=int(copy_raw.get("poll_interval_ms") or 2000),
            recent_limit=int(copy_raw.get("recent_limit") or 50),
        ),
        cities=cities,
    )
