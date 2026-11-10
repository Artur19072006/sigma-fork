from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from provider_odds.env import load_dotenv
from provider_odds.models import ProviderOddsEvent
from provider_odds.providers.base import (
    OddsProvider,
    ProviderConfigurationError,
    ProviderRequestError,
)

THE_ODDS_API_KEY_ENV = "THE_ODDS_API_KEY"


class TheOddsApiProvider(OddsProvider):
    name = "the_odds_api"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.the-odds-api.com/v4",
        timeout_seconds: int = 20,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls, env_path: str | Path = ".env") -> "TheOddsApiProvider":
        load_dotenv(env_path)
        return cls(api_key=os.environ.get(THE_ODDS_API_KEY_ENV))

    def fetch_events(
        self,
        *,
        sport: str,
        regions: str = "us",
        market_type: str = "h2h",
        bookmaker: str | None = None,
    ) -> list[ProviderOddsEvent]:
        if not self.api_key:
            raise ProviderConfigurationError(
                f"Missing {THE_ODDS_API_KEY_ENV} in environment or .env file"
            )

        query: dict[str, str] = {
            "apiKey": self.api_key,
            "regions": regions,
            "markets": market_type,
            "oddsFormat": "decimal",
        }
        if bookmaker:
            query["bookmakers"] = bookmaker

        url = f"{self.base_url}/sports/{sport}/odds?{urlencode(query)}"
        payload = self._get_json(url)
        return self.normalize_response(
            payload,
            market_type=market_type,
            bookmaker=bookmaker,
        )

    def normalize_response(
        self,
        payload: object,
        *,
        market_type: str = "h2h",
        bookmaker: str | None = None,
    ) -> list[ProviderOddsEvent]:
        if not isinstance(payload, list):
            raise ValueError("The Odds API odds response must be a list of events")

        events: list[ProviderOddsEvent] = []
        for event in payload:
            if not isinstance(event, dict):
                continue

            team_a = _clean_text(event.get("home_team"))
            team_b = _clean_text(event.get("away_team"))
            if not team_a or not team_b:
                continue

            for book in _dicts(event.get("bookmakers")):
                book_key = _clean_text(book.get("key"))
                book_title = _clean_text(book.get("title")) or book_key
                if bookmaker and bookmaker.casefold() not in {
                    book_key.casefold(),
                    book_title.casefold(),
                }:
                    continue

                for market in _dicts(book.get("markets")):
                    if market.get("key") != market_type:
                        continue

                    odds_a = _outcome_price(market.get("outcomes"), team_a)
                    odds_b = _outcome_price(market.get("outcomes"), team_b)
                    if odds_a is None or odds_b is None:
                        continue

                    events.append(
                        ProviderOddsEvent(
                            provider=self.name,
                            bookmaker=book_title,
                            game=_clean_text(event.get("sport_title"))
                            or _clean_text(event.get("sport_key")),
                            tournament=_optional_text(event.get("tournament")),
                            start_time=_parse_iso_datetime(event.get("commence_time")),
                            team_a=team_a,
                            team_b=team_b,
                            market_type=_clean_text(market.get("key")) or market_type,
                            odds_a=odds_a,
                            odds_b=odds_b,
                            event_url=_optional_text(event.get("event_url")),
                            raw={
                                "event": event,
                                "bookmaker": book,
                                "market": market,
                            },
                        )
                    )

        return events

    def _get_json(self, url: str) -> object:
        request = Request(url, headers={"User-Agent": "provider-odds-probe/0.1"})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise ProviderRequestError(
                f"The Odds API returned HTTP {exc.code}"
            ) from exc
        except URLError as exc:
            raise ProviderRequestError(f"The Odds API request failed: {exc}") from exc


def _dicts(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _optional_text(value: object) -> str | None:
    text = _clean_text(value)
    return text or None


def _outcome_price(outcomes: object, team_name: str) -> float | None:
    for outcome in _dicts(outcomes):
        if _clean_text(outcome.get("name")).casefold() != team_name.casefold():
            continue

        try:
            return float(outcome["price"])
        except (KeyError, TypeError, ValueError):
            return None

    return None


def _parse_iso_datetime(value: object) -> datetime | None:
    text = _clean_text(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    return datetime.fromisoformat(text)
