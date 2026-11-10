from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from provider_odds.matcher import match_events
from provider_odds.models import ProviderOddsEvent
from provider_odds.providers import (
    OddsPapiProvider,
    PolymarketProvider,
    ProviderConfigurationError,
)

ProviderFactory = Callable[[Path], object]


def build_snapshot(
    left_events: Sequence[ProviderOddsEvent | dict[str, Any]],
    right_events: Sequence[ProviderOddsEvent | dict[str, Any]],
    *,
    max_start_time_diff_minutes: int = 180,
    min_score: float = 0.82,
) -> dict[str, Any]:
    left_event_dicts = [_event_dict(event) for event in left_events]
    right_event_dicts = [_event_dict(event) for event in right_events]
    matches = match_events(
        left_event_dicts,
        right_event_dicts,
        max_start_time_diff_minutes=max_start_time_diff_minutes,
        min_score=min_score,
    )
    matched_left = {_event_key(match.get("left_event")) for match in matches}
    matched_right = {_event_key(match.get("right_event")) for match in matches}
    unmatched_left = [
        event for event in left_event_dicts if _event_key(event) not in matched_left
    ]
    unmatched_right = [
        event for event in right_event_dicts if _event_key(event) not in matched_right
    ]

    return {
        "left_events": left_event_dicts,
        "right_events": right_event_dicts,
        "matches": matches,
        "unmatched_left": unmatched_left,
        "unmatched_right": unmatched_right,
        "summary": {
            "left_count": len(left_event_dicts),
            "right_count": len(right_event_dicts),
            "matched_count": len(matches),
            "unmatched_left_count": len(unmatched_left),
            "unmatched_right_count": len(unmatched_right),
        },
    }


def run_snapshot_match(
    *,
    left_events: Sequence[ProviderOddsEvent | dict[str, Any]] | None = None,
    right_events: Sequence[ProviderOddsEvent | dict[str, Any]] | None = None,
    left_provider: str | None = None,
    right_provider: str | None = None,
    left_sport: str | None = None,
    right_sport: str | None = None,
    left_bookmaker: str | None = None,
    left_tournament_id: str | None = None,
    left_fixture_id: str | None = None,
    left_mapping_profile: str | None = None,
    right_event_slug: str | None = None,
    right_market_slug: str | None = None,
    env_file: Path | str = ".env",
    provider_factories: dict[str, ProviderFactory] | None = None,
    max_start_time_diff_minutes: int = 180,
    min_score: float = 0.82,
) -> dict[str, Any]:
    env_path = Path(env_file)
    if left_events is None:
        left_events = fetch_provider_events(
            side="left",
            provider_name=left_provider,
            sport=left_sport,
            bookmaker=left_bookmaker,
            tournament_id=left_tournament_id,
            fixture_id=left_fixture_id,
            mapping_profile=left_mapping_profile,
            env_file=env_path,
            provider_factories=provider_factories,
        )
    if right_events is None:
        right_events = fetch_provider_events(
            side="right",
            provider_name=right_provider,
            sport=right_sport,
            event_slug=right_event_slug,
            market_slug=right_market_slug,
            env_file=env_path,
            provider_factories=provider_factories,
        )

    return build_snapshot(
        left_events,
        right_events,
        max_start_time_diff_minutes=max_start_time_diff_minutes,
        min_score=min_score,
    )


def fetch_provider_events(
    *,
    side: str,
    provider_name: str | None,
    sport: str | None,
    bookmaker: str | None = None,
    tournament_id: str | None = None,
    fixture_id: str | None = None,
    mapping_profile: str | None = None,
    event_slug: str | None = None,
    market_slug: str | None = None,
    env_file: Path = Path(".env"),
    provider_factories: dict[str, ProviderFactory] | None = None,
) -> list[ProviderOddsEvent]:
    if not provider_name:
        raise ProviderConfigurationError(
            f"Missing --{side}-provider or --{side}-file"
        )

    provider_key = _provider_key(provider_name)
    provider = _build_provider(provider_key, env_file, provider_factories)
    if provider_key == "oddspapi":
        return _fetch_oddspapi_events(
            provider,
            side=side,
            sport=sport,
            bookmaker=bookmaker,
            tournament_id=tournament_id,
            fixture_id=fixture_id,
            mapping_profile=mapping_profile,
        )
    if provider_key == "polymarket":
        return _fetch_polymarket_events(
            provider,
            side=side,
            sport=sport,
            event_slug=event_slug,
            market_slug=market_slug,
        )

    raise ProviderConfigurationError(f"Unsupported {side} provider: {provider_name}")


def load_event_file(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("events", [])
    if not isinstance(payload, list):
        raise ValueError(f"{path} must contain a list or an object with an events list")
    return [event for event in payload if isinstance(event, dict)]


def _fetch_oddspapi_events(
    provider: object,
    *,
    side: str,
    sport: str | None,
    bookmaker: str | None,
    tournament_id: str | None,
    fixture_id: str | None,
    mapping_profile: str | None,
) -> list[ProviderOddsEvent]:
    tournament_ids = _split_values(tournament_id, max_items=5)
    if not sport and not fixture_id:
        raise ProviderConfigurationError(
            f"Missing --{side}-sport or --{side}-fixture-id for OddsPapi"
        )
    if len(tournament_ids) > 1:
        events: list[ProviderOddsEvent] = []
        for item in tournament_ids:
            events.extend(
                provider.fetch_events(
                    sport=sport,
                    market_type="h2h",
                    bookmaker=bookmaker,
                    tournament_id=item,
                    mapping_profile=mapping_profile,
                )
            )
        return events

    return provider.fetch_events(
        sport=sport,
        market_type="h2h",
        bookmaker=bookmaker,
        tournament_id=tournament_ids[0] if tournament_ids else None,
        fixture_id=fixture_id,
        mapping_profile=mapping_profile,
    )


def _fetch_polymarket_events(
    provider: object,
    *,
    side: str,
    sport: str | None,
    event_slug: str | None,
    market_slug: str | None,
) -> list[ProviderOddsEvent]:
    if not sport:
        raise ProviderConfigurationError(f"Missing --{side}-sport for Polymarket")
    event_slugs = _split_values(event_slug, max_items=20)
    market_slugs = _split_values(market_slug, max_items=20)
    if not event_slugs and not market_slugs:
        raise ProviderConfigurationError(
            f"Missing --{side}-event-slug or --{side}-market-slug for Polymarket"
        )

    events: list[ProviderOddsEvent] = []
    for slug in market_slugs:
        events.extend(
            provider.fetch_events(
                sport=sport,
                market_type="h2h",
                market_slug=slug,
            )
        )
    for slug in event_slugs:
        events.extend(
            provider.fetch_events(
                sport=sport,
                market_type="h2h",
                event_slug=slug,
            )
        )
    return events


def _build_provider(
    provider_name: str,
    env_file: Path,
    provider_factories: dict[str, ProviderFactory] | None,
) -> object:
    if provider_factories and provider_name in provider_factories:
        return provider_factories[provider_name](env_file)
    if provider_name == "oddspapi":
        return OddsPapiProvider.from_env(env_file)
    if provider_name == "polymarket":
        return PolymarketProvider.from_env(env_file)
    raise ProviderConfigurationError(f"Unsupported provider: {provider_name}")


def _split_values(value: str | None, *, max_items: int) -> list[str]:
    if not value:
        return []
    parts = [part.strip() for part in str(value).split(",") if part.strip()]
    if len(parts) > max_items:
        raise ProviderConfigurationError(
            f"Too many values: got {len(parts)}, max {max_items}"
        )
    return parts


def _provider_key(value: str) -> str:
    key = value.strip().casefold().replace("-", "_")
    if key == "the_odds_api":
        return key
    return key


def _event_key(value: object) -> str:
    event = value if isinstance(value, dict) else {}
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    parts = [
        event.get("provider"),
        event.get("bookmaker"),
        event.get("game"),
        event.get("market_type"),
        event.get("start_time"),
        event.get("team_a"),
        event.get("team_b"),
        event.get("event_url"),
        raw.get("event_id"),
        raw.get("market_id"),
        raw.get("fixture_id"),
    ]
    return "|".join("" if part is None else str(part) for part in parts)


def _event_dict(event: ProviderOddsEvent | dict[str, Any]) -> dict[str, Any]:
    if isinstance(event, ProviderOddsEvent):
        return event.to_dict()
    data = dict(event)
    start_time = data.get("start_time")
    if isinstance(start_time, datetime):
        data["start_time"] = start_time.isoformat()
    data.setdefault("raw", {})
    return data
