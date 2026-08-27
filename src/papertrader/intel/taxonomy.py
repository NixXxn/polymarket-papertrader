"""Slug/question risk taxonomy (world-intel style event classification, offline)."""

from __future__ import annotations

# High-risk narrative markets that paper historically lost on / stay open forever.
_GEOPOLITICS = (
    "nato",
    "russia",
    "ukraine",
    "taiwan",
    "china-invade",
    "iran",
    "israel",
    "hamas",
    "nuclear",
    "war-",
    "-war",
    "military-clash",
    "invasion",
    "missile",
    "hormuz",
    "strait-of-",
)
_MACRO_LONGSHOT = (
    "fed-",
    "interest-rate",
    "fomc",
    "clarity-act",
    "recession",
    "debt-ceiling",
    "government-shutdown",
)
_CRYPTO_NARRATIVE = (
    "bitcoin-reach",
    "ethereum-above",
    "btc-above",
    "eth-above",
    "solana-above",
)
_ELECTION = (
    "election",
    "by-election",
    "presidential",
    "prime-minister",
    "parliament",
)
# Match/sports noise — meanrev already lost paper on soccer fades (e.g. lal-*).
_SPORTS = (
    "lal-",
    "epl-",
    "efl-",
    "mlb-",
    "nba-",
    "nfl-",
    "nhl-",
    "ucl-",
    "uel-",
    "serie-a",
    "bundesliga",
    "ligue-1",
    "mls-",
    "afc-",
    "us-open",
    "wimbledon",
    "french-open",
    "australian-open",
    "alcaraz",
    "djokovic",
    "sinner",
    "moneyline",
    "spread-",
    " vs ",
    " vs. ",
)
_CELEBRITY_NOISE = (
    "elon-musk",
    "of-tweets",
    "tweet-count",
    "how-many-tweets",
)


def classify_event_text(slug: str, question: str = "") -> tuple[str, int, tuple[str, ...]]:
    """Return (category, risk_score_0_100, matched_tags)."""
    text = f"{slug} {question}".lower()
    tags: list[str] = []
    score = 0
    category = "general"

    def _hit(name: str, needles: tuple[str, ...], weight: int) -> None:
        nonlocal score, category
        matched = [n for n in needles if n in text]
        if matched:
            tags.extend(matched)
            score = max(score, weight)
            if weight >= score:
                category = name

    _hit("geopolitics", _GEOPOLITICS, 85)
    _hit("macro_longshot", _MACRO_LONGSHOT, 75)
    _hit("election", _ELECTION, 70)
    _hit("sports", _SPORTS, 65)
    _hit("celebrity", _CELEBRITY_NOISE, 65)
    _hit("crypto_narrative", _CRYPTO_NARRATIVE, 55)

    # Cap and uniquify tags (order-preserving).
    seen: set[str] = set()
    uniq: list[str] = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return category, min(100, score), tuple(uniq)
