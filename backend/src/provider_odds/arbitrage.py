from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any


EPSILON = 1e-12


@dataclass(frozen=True, slots=True)
class ArbitrageConfig:
    polymarket_fee_rate: float = 0.0
    cancel_risk_odds_threshold: float = 2.0

    def __post_init__(self) -> None:
        if self.polymarket_fee_rate < 0 or self.polymarket_fee_rate >= 1:
            raise ValueError("polymarket_fee_rate must be >= 0 and < 1")
        if self.cancel_risk_odds_threshold <= 1:
            raise ValueError("cancel_risk_odds_threshold must be > 1")


def calculate_arbitrage(
    matches: list[dict[str, Any]],
    *,
    config: ArbitrageConfig | None = None,
) -> dict[str, Any]:
    config = config or ArbitrageConfig()
    opportunities: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()

    for match_index, match in enumerate(matches):
        if not isinstance(match, dict):
            reason_counts["invalid_match"] += 1
            continue

        stake_event, polymarket_event = _stake_and_polymarket_events(match)
        if not stake_event:
            reason_counts["stake_event_missing"] += 1
            continue
        if not polymarket_event:
            reason_counts["polymarket_event_missing"] += 1
            continue

        orientation = _text(match.get("orientation") or "same")
        if orientation not in {"same", "swapped"}:
            reason_counts["invalid_orientation"] += 1
            continue

        (
            pm_team_a,
            pm_odds_a,
            pm_side_a,
            pm_team_b,
            pm_odds_b,
            pm_side_b,
        ) = _polymarket_canonical_legs(
            polymarket_event,
            orientation=orientation,
        )
        candidates = [
            {
                "stake_team": _text(stake_event.get("team_a")),
                "stake_odds": _decimal_odds(stake_event.get("odds_a")),
                "polymarket_team": pm_team_b,
                "polymarket_raw_odds": pm_odds_b,
                "polymarket_side_index": pm_side_b,
                "leg": "stake_team_a_polymarket_team_b",
            },
            {
                "stake_team": _text(stake_event.get("team_b")),
                "stake_odds": _decimal_odds(stake_event.get("odds_b")),
                "polymarket_team": pm_team_a,
                "polymarket_raw_odds": pm_odds_a,
                "polymarket_side_index": pm_side_a,
                "leg": "stake_team_b_polymarket_team_a",
            },
        ]

        for candidate in candidates:
            stake_odds = candidate["stake_odds"]
            if stake_odds is None:
                reason_counts["invalid_stake_odds"] += 1
                continue

            executable = _polymarket_executable_side(
                polymarket_event,
                side_index=candidate["polymarket_side_index"],
            )
            if executable["odds"] is None:
                reason_counts["non_executable_polymarket_price"] += 1
                continue

            polymarket_raw_odds = executable["odds"]
            executable_buy_price = executable["price"]
            price_source = _text(executable["source"])
            if polymarket_raw_odds is None:
                reason_counts["invalid_polymarket_odds"] += 1
                continue

            polymarket_effective_odds = polymarket_effective_decimal_odds(
                polymarket_raw_odds,
                config=config,
            )
            all_in_price = 1 / polymarket_effective_odds
            fee_per_share = (
                all_in_price - executable_buy_price
                if executable_buy_price is not None
                else None
            )
            implied_sum = (1 / stake_odds) + (1 / polymarket_effective_odds)
            if implied_sum > 1.0 + EPSILON:
                reason_counts["no_arbitrage"] += 1
                continue

            opportunities.append(
                {
                    "game": _text(stake_event.get("game")),
                    "start_time": _text(stake_event.get("start_time")),
                    "team_a": _text(stake_event.get("team_a")),
                    "team_b": _text(stake_event.get("team_b")),
                    "stake_team": candidate["stake_team"],
                    "stake_odds": stake_odds,
                    "polymarket_team": candidate["polymarket_team"],
                    "polymarket_raw_odds": polymarket_raw_odds,
                    "polymarket_effective_odds": polymarket_effective_odds,
                    "polymarket_executable_buy_price": executable_buy_price,
                    "polymarket_price_source": price_source,
                    "polymarket_reference_price": executable["reference_price"],
                    "polymarket_all_in_price": all_in_price,
                    "polymarket_fee_per_share": fee_per_share,
                    "implied_sum": implied_sum,
                    "edge_percent": max(0.0, (1.0 - implied_sum) * 100),
                    "cancel_risk": (
                        polymarket_raw_odds < config.cancel_risk_odds_threshold
                    ),
                    "stake_url": _text(stake_event.get("event_url")),
                    "polymarket_url": _text(polymarket_event.get("event_url")),
                    "source_ids": _source_ids(
                        match,
                        stake_event=stake_event,
                        polymarket_event=polymarket_event,
                        match_index=match_index,
                    ),
                    "leg": candidate["leg"],
                }
            )

    return {
        "summary": {
            "match_count": len(matches),
            "opportunity_count": len(opportunities),
        },
        "opportunities": opportunities,
        "debug": {
            "reason_counts": dict(sorted(reason_counts.items())),
        },
    }


def polymarket_effective_decimal_odds(
    raw_odds: float,
    *,
    config: ArbitrageConfig | None = None,
) -> float:
    """Return decimal odds after a conservative fee on the profit portion.

    Decimal odds include the returned stake. A taker-style fee should reduce only
    the profit portion, so 3.00 at a 2% fee becomes 1 + (3.00 - 1) * 0.98.
    """

    config = config or ArbitrageConfig()
    return 1.0 + ((raw_odds - 1.0) * (1.0 - config.polymarket_fee_rate))


def extract_matches(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("matches", [])
    if not isinstance(payload, list):
        raise ValueError("matches payload must be a list or an object with matches")
    return [item for item in payload if isinstance(item, dict)]


def _stake_and_polymarket_events(
    match: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    left = _event(match.get("left_event"))
    right = _event(match.get("right_event"))
    events = (left, right)
    stake_event = next((event for event in events if _is_stake_event(event)), {})
    polymarket_event = next(
        (event for event in events if _is_polymarket_event(event)),
        {},
    )
    return stake_event, polymarket_event


def _polymarket_canonical_legs(
    event: dict[str, Any],
    *,
    orientation: str,
) -> tuple[str, float | None, int, str, float | None, int]:
    if orientation == "swapped":
        return (
            _text(event.get("team_b")),
            _decimal_odds(event.get("odds_b")),
            1,
            _text(event.get("team_a")),
            _decimal_odds(event.get("odds_a")),
            0,
        )
    return (
        _text(event.get("team_a")),
        _decimal_odds(event.get("odds_a")),
        0,
        _text(event.get("team_b")),
        _decimal_odds(event.get("odds_b")),
        1,
    )


def _polymarket_executable_side(
    event: dict[str, Any],
    *,
    side_index: int,
) -> dict[str, Any]:
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    price = _side_number(raw.get("executable_buy_prices"), side_index)
    source = _side_text(raw.get("executable_price_sources"), side_index)
    actionable = _side_bool(raw.get("polymarket_actionable"), side_index)
    if price is not None and actionable:
        return {
            "price": price,
            "odds": 1.0 / price,
            "source": source or "executable_buy_price",
            "reference_price": _side_number(raw.get("original_prices"), side_index),
        }
    return {
        "price": None,
        "odds": None,
        "source": source,
        "reference_price": _side_number(raw.get("original_prices"), side_index),
    }


def _source_ids(
    match: dict[str, Any],
    *,
    stake_event: dict[str, Any],
    polymarket_event: dict[str, Any],
    match_index: int,
) -> dict[str, Any]:
    source_ids: dict[str, Any] = {
        "match_index": match_index,
    }
    for source_key in ("orientation", "score", "team_score"):
        if match.get(source_key) not in (None, ""):
            source_ids[f"match_{source_key}"] = match.get(source_key)

    for prefix, event in (
        ("stake", stake_event),
        ("polymarket", polymarket_event),
    ):
        raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
        for raw_key in ("fixture_id", "event_id", "market_id", "condition_id"):
            if raw.get(raw_key) not in (None, ""):
                source_ids[f"{prefix}_{raw_key}"] = raw.get(raw_key)
    return source_ids


def _is_stake_event(event: dict[str, Any]) -> bool:
    return _text(event.get("bookmaker")).casefold() == "stake"


def _is_polymarket_event(event: dict[str, Any]) -> bool:
    return (
        _text(event.get("provider")).casefold() == "polymarket"
        or _text(event.get("bookmaker")).casefold() == "polymarket"
    )


def _event(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _decimal_odds(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        odds = float(value)
    except (TypeError, ValueError):
        return None
    if odds <= 1.0:
        return None
    return odds


def _side_number(value: object, side_index: int) -> float | None:
    item = _side_value(value, side_index)
    if item is None or item == "":
        return None
    try:
        number = float(item)
    except (TypeError, ValueError):
        return None
    if number <= 0 or number >= 1:
        return None
    return number


def _side_text(value: object, side_index: int) -> str:
    item = _side_value(value, side_index)
    return _text(item)


def _side_bool(value: object, side_index: int) -> bool:
    item = _side_value(value, side_index)
    if item is True:
        return True
    if item is False or item is None:
        return False
    return _text(item).casefold() in {"true", "1", "yes"}


def _side_value(value: object, side_index: int) -> object:
    if isinstance(value, (list, tuple)):
        if 0 <= side_index < len(value):
            return value[side_index]
        return None
    return None


def _text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())
