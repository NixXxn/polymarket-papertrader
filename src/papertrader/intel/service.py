"""Cached intel snapshots from free public APIs (world-intel-mcp domain subset)."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from papertrader.intel.taxonomy import classify_event_text
from papertrader.paths import root_data_dir

log = logging.getLogger("papertrader.intel")

_CACHE_FILE = "intel_snapshot.json"
_DEFAULT_TTL_SECONDS = 900  # 15 minutes


@dataclass(frozen=True)
class EventRisk:
    category: str
    score: int
    tags: tuple[str, ...]

    @property
    def is_elevated(self) -> bool:
        return self.score >= 50


@dataclass(frozen=True)
class IntelSnapshot:
    fetched_at: str
    fear_greed: int | None
    macro_verdict: str  # RISK_ON | NEUTRAL | CAUTION | RISK_OFF
    btc_sma50: float | None
    btc_sma200: float | None
    btc_death_cross: bool | None
    source_errors: tuple[str, ...] = ()

    def age_seconds(self) -> float:
        try:
            fetched = datetime.fromisoformat(self.fetched_at.replace("Z", "+00:00"))
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=timezone.utc)
        except ValueError:
            return float("inf")
        return (datetime.now(timezone.utc) - fetched).total_seconds()


def event_risk(slug: str, question: str = "") -> EventRisk:
    category, score, tags = classify_event_text(slug, question)
    return EventRisk(category=category, score=score, tags=tags)


class IntelService:
    """Refresh macro/BTC intel on a TTL; classify market event risk offline."""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        user_agent: str = "polymarket-papertrader/intel",
        timeout: float = 8.0,
    ) -> None:
        self._path = root_data_dir(Path(data_dir)) / _CACHE_FILE
        self._ttl = float(ttl_seconds)
        self._ua = user_agent
        self._timeout = timeout
        self._snap: IntelSnapshot | None = self._load()

    def _load(self) -> IntelSnapshot | None:
        if not self._path.is_file():
            return None
        try:
            raw = json.loads(self._path.read_text())
            if not isinstance(raw, dict):
                return None
            return IntelSnapshot(
                fetched_at=str(raw.get("fetched_at") or ""),
                fear_greed=raw.get("fear_greed"),
                macro_verdict=str(raw.get("macro_verdict") or "NEUTRAL"),
                btc_sma50=raw.get("btc_sma50"),
                btc_sma200=raw.get("btc_sma200"),
                btc_death_cross=raw.get("btc_death_cross"),
                source_errors=tuple(raw.get("source_errors") or ()),
            )
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            return None

    def _save(self, snap: IntelSnapshot) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(asdict(snap), indent=2))

    def snapshot(self, *, force: bool = False) -> IntelSnapshot:
        if (
            not force
            and self._snap is not None
            and self._snap.age_seconds() < self._ttl
        ):
            return self._snap
        snap = self._fetch()
        self._snap = snap
        try:
            self._save(snap)
        except OSError as e:
            log.warning("intel: cache write failed: %s", e)
        return snap

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self._timeout,
            headers={"User-Agent": self._ua},
            follow_redirects=True,
        )

    def _fetch(self) -> IntelSnapshot:
        errors: list[str] = []
        fear: int | None = None
        sma50: float | None = None
        sma200: float | None = None
        death: bool | None = None

        with self._client() as client:
            try:
                fear = self._fetch_fear_greed(client)
            except Exception as e:
                errors.append(f"fear_greed:{e}")
                log.info("intel: fear_greed failed: %s", e)
            try:
                sma50, sma200, death = self._fetch_btc_smas(client)
            except Exception as e:
                errors.append(f"btc:{e}")
                log.info("intel: btc technicals failed: %s", e)

        verdict = _macro_verdict(fear, death)
        return IntelSnapshot(
            fetched_at=datetime.now(timezone.utc).isoformat(),
            fear_greed=fear,
            macro_verdict=verdict,
            btc_sma50=sma50,
            btc_sma200=sma200,
            btc_death_cross=death,
            source_errors=tuple(errors),
        )

    @staticmethod
    def _fetch_fear_greed(client: httpx.Client) -> int:
        # Same source family as world-intel-mcp markets tools (Alternative.me).
        r = client.get("https://api.alternative.me/fng/", params={"limit": 1})
        r.raise_for_status()
        data = r.json()
        row = (data.get("data") or [None])[0]
        if not row:
            raise ValueError("empty fng payload")
        return int(row["value"])

    @staticmethod
    def _fetch_btc_smas(client: httpx.Client) -> tuple[float, float, bool]:
        # CoinGecko market chart — free, no key (world-intel crypto domain).
        r = client.get(
            "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
            params={"vs_currency": "usd", "days": "200", "interval": "daily"},
        )
        r.raise_for_status()
        prices = r.json().get("prices") or []
        closes = [float(p[1]) for p in prices if isinstance(p, (list, tuple)) and len(p) >= 2]
        if len(closes) < 200:
            raise ValueError(f"need 200 daily closes, got {len(closes)}")
        sma50 = sum(closes[-50:]) / 50.0
        sma200 = sum(closes[-200:]) / 200.0
        death = sma50 < sma200
        return sma50, sma200, death


def _macro_verdict(fear_greed: int | None, death_cross: bool | None) -> str:
    """Map free signals → coarse regime (aligned with world-intel macro composite idea)."""
    if fear_greed is None and death_cross is None:
        return "NEUTRAL"
    score = 50
    if fear_greed is not None:
        # 0 extreme fear → RISK_OFF; 100 greed → RISK_ON
        score = fear_greed
    if death_cross is True:
        score = min(score, 35)
    elif death_cross is False and fear_greed is not None and fear_greed >= 55:
        score = max(score, 60)

    if score >= 65:
        return "RISK_ON"
    if score >= 45:
        return "NEUTRAL"
    if score >= 30:
        return "CAUTION"
    return "RISK_OFF"


_services: dict[str, IntelService] = {}


def get_intel_service(data_dir: Path | str, *, ttl_seconds: float = _DEFAULT_TTL_SECONDS) -> IntelService:
    key = str(root_data_dir(Path(data_dir)))
    svc = _services.get(key)
    if svc is None or abs(svc._ttl - ttl_seconds) > 1e-6:
        svc = IntelService(data_dir, ttl_seconds=ttl_seconds)
        _services[key] = svc
    return svc
