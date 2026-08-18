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
class SafeSettings:
    cities: tuple[str, ...]
    max_ask: float
    max_open_positions: int
    min_sell_bid: float
    min_edge: float
    min_edge_high: float
    min_edge_low: float
    position_usd: dict[str, float]


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
    exit_ladder: tuple[ExitLadderStep, ...] = ()
    hours_before_resolution: int = 1
    exit_model_prob_min_days_ahead: int = 0


@dataclass(frozen=True)
class EsportsSettings:
    horizon_hours: float
    poll_interval_seconds: int
    min_ask: float
    max_ask: float
    take_profit_multiple: float
    position_usd: float
    max_position_usd: float
    max_open_positions: int
    min_event_volume: float
    search_queries: tuple[str, ...]
    event_tags: tuple[str, ...]
    search_limit: int
    tag_slug: str
    exclude_slug_patterns: tuple[str, ...]


@dataclass(frozen=True)
class MomentumSettings:
    ws_url: str
    use_websocket: bool
    poll_interval_seconds: int
    mode: str
    specific_token_id: str
    entry_trigger_price: float
    take_profit_price: float | None
    stop_loss_price: float
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
class CopySettings:
    username: str
    wallet: str
    scale: float | None = None
    poll_interval_ms: int = 250
    recent_limit: int = 50


@dataclass(frozen=True)
class LiveSettings:
    clob_host: str
    chain_id: int
    signature_type: int
    funder: str


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
    safe: SafeSettings
    asymmetric: AsymmetricSettings
    esports: EsportsSettings
    momentum: MomentumSettings
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


def _parse_exit_ladder(raw: Any) -> tuple[ExitLadderStep, ...]:
    default = (
        ExitLadderStep(2.0, 0.10),
        ExitLadderStep(5.0, 0.10),
        ExitLadderStep(10.0, 0.15),
        ExitLadderStep(20.0, 0.15),
        ExitLadderStep(50.0, 0.10),
    )
    if not raw:
        return default
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

    safe_raw = raw["safe"]
    asymmetric_raw = raw["asymmetric"]
    edge_raw = raw.get("edge") or {}
    esports_raw = raw.get("esports") or {}
    momentum_raw = raw.get("momentum") or {}
    copy_raw = raw.get("copy") or {}
    live_raw = raw.get("live") or {}
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
        safe=SafeSettings(
            cities=tuple(safe_raw["cities"]),
            max_ask=float(safe_raw["max_ask"]),
            max_open_positions=int(safe_raw["max_open_positions"]),
            min_sell_bid=float(safe_raw["min_sell_bid"]),
            min_edge=float(safe_raw["min_edge"]),
            min_edge_high=float(safe_raw["min_edge_high"]),
            min_edge_low=float(safe_raw["min_edge_low"]),
            position_usd={k: float(v) for k, v in safe_raw["position_usd"].items()},
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
        ),
        esports=EsportsSettings(
            horizon_hours=float(esports_raw.get("horizon_hours", 6)),
            poll_interval_seconds=int(esports_raw.get("poll_interval_seconds", 60)),
            min_ask=float(esports_raw.get("min_ask", 0.02)),
            max_ask=float(esports_raw.get("max_ask", 0.45)),
            take_profit_multiple=float(esports_raw.get("take_profit_multiple", 2.0)),
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
            stop_loss_price=float(momentum_raw.get("stop_loss_price", 0.75)),
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
            username=str(copy_raw.get("username") or "0x.aljjj").lstrip("@"),
            wallet=str(copy_raw.get("wallet") or "").lower(),
            scale=float(copy_raw["scale"]) if copy_raw.get("scale") is not None else None,
            poll_interval_ms=int(copy_raw.get("poll_interval_ms") or 250),
            recent_limit=int(copy_raw.get("recent_limit") or 50),
        ),
        cities=cities,
    )
