from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from provider_odds.env import load_dotenv
from provider_odds.models import ProviderOddsEvent
from provider_odds.providers.base import (
    OddsProvider,
    ProviderConfigurationError,
    ProviderRequestError,
)

ODDSPAPI_API_KEY_ENV = "ODDSPAPI_API_KEY"
ODDSPAPI_API_KEYS_ENV = "ODDSPAPI_API_KEYS"
ODDSPAPI_FALLBACK_API_KEY_ENVS = (
    ODDSPAPI_API_KEY_ENV,
    "ODDS_PAPI_API_KEY",
    "ODDSPAPI_TOKEN",
    "ODDS_PAPI_TOKEN",
)
DEFAULT_ODDSPAPI_KEY_STATE_PATH = Path(".cache/oddspapi_key_state.json")
SPORT_ID_ALIASES = {
    "dota": "16",
    "dota2": "16",
    "dota 2": "16",
    "esport dota": "16",
    "esport dota2": "16",
    "esport dota 2": "16",
    "cs2": "17",
    "counterstrike": "17",
    "counter strike": "17",
    "esport counter strike": "17",
    "lol": "18",
    "leagueoflegends": "18",
    "league of legends": "18",
    "esport league of legends": "18",
}
BOOKMAKER_CONTAINER_KEYS = ("bookmakers", "books", "odds")
MARKET_CONTAINER_KEYS = ("markets", "odds", "bettingMarkets", "lines")
OUTCOME_CONTAINER_KEYS = ("outcomes", "selections", "runners")
MAPPING_PROFILE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER = (
    "oddspapi_stake_dota2_market161_selection_order"
)
MAPPING_PROFILE_ODDSPAPI_STAKE_CS2_MARKET171_SELECTION_ORDER = (
    "oddspapi_stake_cs2_market171_selection_order"
)
MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER = (
    "oddspapi_stake_lol_market181_selection_order"
)
SUPPORTED_MAPPING_PROFILES = {
    MAPPING_PROFILE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER,
    MAPPING_PROFILE_ODDSPAPI_STAKE_CS2_MARKET171_SELECTION_ORDER,
    MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER,
}
MAPPING_SOURCE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER = (
    "experimental:"
    f"{MAPPING_PROFILE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER}"
)
MAPPING_SOURCE_ODDSPAPI_STAKE_CS2_MARKET171_SELECTION_ORDER = (
    "experimental:"
    f"{MAPPING_PROFILE_ODDSPAPI_STAKE_CS2_MARKET171_SELECTION_ORDER}"
)
MAPPING_SOURCE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER = (
    "experimental:"
    f"{MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER}"
)
DOTA2_DRAW_OR_THREE_WAY_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:draw|tie|three[-\s]?way|3[-\s]?way|3way)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
DOTA2_EWC_RE = re.compile(
    r"\b(?:ewc|esports?\s+world\s+cup)\b",
    re.IGNORECASE,
)
DOTA2_BO2_RE = re.compile(
    r"\b(b0?2|bo[-\s]?2|best\s+of\s+(?:2|two)|best-of-(?:2|two))\b",
    re.IGNORECASE,
)
DOTA2_MAP_OR_GAME_RE = re.compile(
    r"\b(?:map|game)\s*[1-9]\b|"
    r"\b(?:first|second|third|1st|2nd|3rd)\s+(?:map|game)\b|"
    r"\b(?:map|game)\s+winner\b|"
    r"\b(?:exact|correct)\s+maps?\b|"
    r"\bmaps?\s+(?:exact|correct|score)\b",
    re.IGNORECASE,
)
DOTA2_PROP_RE = re.compile(
    r"\b("
    r"handicap|spread|total|totals|over|under|prop|props|player|"
    r"hero|kill|kills|roshan|tower|towers|barracks"
    r")\b|over/under",
    re.IGNORECASE,
)
DOTA2_OUTRIGHT_RE = re.compile(
    r"\b("
    r"outright|outrights|future|futures|tournament\s+winner|"
    r"championship\s+winner|event\s+winner|overall\s+winner|winner\s+of|"
    r"region\s+winner|regional\s+winner|champion"
    r")\b",
    re.IGNORECASE,
)


class OddsPapiAllKeysExhaustedError(ProviderRequestError):
    """Raised when one request cycle rate-limits every configured OddsPapi key."""


class OddsPapiProvider(OddsProvider):
    name = "oddspapi"

    def __init__(
        self,
        api_key: str | None = None,
        api_keys: list[str] | tuple[str, ...] | None = None,
        base_url: str = "https://api.oddspapi.io",
        timeout_seconds: int = 20,
        max_tournament_ids_per_request: int = 5,
        key_state_path: str | Path = DEFAULT_ODDSPAPI_KEY_STATE_PATH,
    ) -> None:
        if max_tournament_ids_per_request < 1:
            raise ValueError("max_tournament_ids_per_request must be positive")
        self.api_keys = _api_key_values(
            api_keys if api_keys is not None else ([api_key] if api_key else [])
        )
        self.key_state_path = Path(key_state_path)
        self.current_key_index = 0
        self.last_failover_at = ""
        self.last_error = ""
        self.failover_count = 0
        self.all_keys_exhausted = False
        self._load_key_state()
        self.api_key = self._current_api_key()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_tournament_ids_per_request = max_tournament_ids_per_request
        self.last_note: str | None = None
        self.last_debug: dict[str, Any] = {}

    @classmethod
    def from_env(cls, env_path: str | Path = ".env") -> "OddsPapiProvider":
        load_dotenv(env_path)
        return cls(api_keys=_api_keys_from_env())

    def fetch_tournament_candidates(
        self,
        *,
        sport: str | int = "16",
        tournament_keywords: object = None,
        max_candidates: int = 30,
    ) -> list[dict[str, str]]:
        self._ensure_api_key()

        self.last_note = None
        resolved_sport_id = _resolve_sport_id(sport)
        keywords = _keyword_values(tournament_keywords)
        candidate_limit = _candidate_limit(max_candidates)
        tournaments_url = self._tournaments_url(resolved_sport_id)
        self.last_debug = {
            "request_mode": "tournament_candidates",
            "resolved_sport_id": resolved_sport_id,
            "bookmaker": "",
            "tournament_candidate_keywords": keywords,
            "tournaments_url": _sanitize_url(tournaments_url),
        }
        self._update_key_pool_debug()

        tournaments_payload = self._get_json(tournaments_url)
        tournament_records = _tournament_records(tournaments_payload)
        candidate_records = _select_tournament_records(
            tournament_records,
            keyword=keywords,
        )
        printed_records = candidate_records[:candidate_limit]
        candidates = _tournament_candidate_samples(
            printed_records,
            secret=self.api_key,
        )
        self.last_debug.update(
            {
                "tournaments_count": len(tournament_records),
                "candidate_count": len(candidate_records),
                "printed_count": len(candidates),
                "tournament_candidates": candidates,
                "last_note": "",
            }
        )
        return candidates

    def fetch_tournament_list(
        self,
        *,
        sport: str | int = "16",
    ) -> list[dict[str, str]]:
        self._ensure_api_key()

        self.last_note = None
        resolved_sport_id = _resolve_sport_id(sport)
        tournaments_url = self._tournaments_url(resolved_sport_id)
        self.last_debug = {
            "request_mode": "tournament_list",
            "resolved_sport_id": resolved_sport_id,
            "bookmaker": "",
            "tournaments_url": _sanitize_url(tournaments_url),
        }
        self._update_key_pool_debug()

        tournaments_payload = self._get_json(tournaments_url)
        tournament_records = _tournament_records(tournaments_payload)
        candidates = _tournament_candidate_samples(
            tournament_records,
            secret=self.api_key,
        )
        self.last_debug.update(
            {
                "tournaments_count": len(tournament_records),
                "candidate_count": len(candidates),
                "printed_count": len(candidates),
                "last_note": "",
            }
        )
        return candidates

    def fetch_events_for_tournaments(
        self,
        *,
        sport: str | int,
        tournament_ids: list[str],
        market_type: str = "h2h",
        bookmaker: str | None = None,
        verbosity: str | int = "3",
        mapping_profile: str | None = None,
    ) -> list[ProviderOddsEvent]:
        self._ensure_api_key()

        limited_tournament_ids = [
            str(tournament_id).strip()
            for tournament_id in tournament_ids
            if str(tournament_id).strip()
        ]
        if not limited_tournament_ids:
            return []

        self.last_note = None
        resolved_sport_id = _resolve_sport_id(sport)
        verbosity_text = str(verbosity).strip() or "3"
        mapping_profile = _validated_mapping_profile(mapping_profile)
        odds_url = self._odds_by_tournaments_url(
            limited_tournament_ids,
            bookmaker=bookmaker,
            verbosity=verbosity_text,
        )
        self.last_debug = {
            "resolved_sport_id": resolved_sport_id,
            "bookmaker": bookmaker or "",
            "mapping_profile": mapping_profile or "",
            "selection_mode": "tournament_ids",
            "request_mode": "tournament_ids",
            "selected_tournament_ids": limited_tournament_ids,
            "odds_by_tournaments_url": _sanitize_url(odds_url),
        }
        self._update_key_pool_debug()
        payload = self._get_json(odds_url)
        events = self.normalize_response(
            payload,
            market_type=market_type,
            bookmaker=bookmaker,
            sport=resolved_sport_id,
            mapping_profile=mapping_profile,
        )
        self.last_debug["last_note"] = self.last_note or ""
        return events

    def fetch_events(
        self,
        *,
        sport: str | int | None = None,
        market_type: str = "h2h",
        bookmaker: str | None = None,
        tournament_id: str | None = None,
        tournament_keyword: str | None = None,
        fixture_id: str | None = None,
        verbosity: str | int = "3",
        mapping_profile: str | None = None,
    ) -> list[ProviderOddsEvent]:
        self._ensure_api_key()

        self.last_note = None
        resolved_sport_id = _resolve_sport_id(sport) if sport is not None else ""
        verbosity_text = str(verbosity).strip() or "3"
        mapping_profile = _validated_mapping_profile(mapping_profile)
        self.last_debug = {
            "resolved_sport_id": resolved_sport_id,
            "bookmaker": bookmaker or "",
            "mapping_profile": mapping_profile or "",
            "selection_mode": "",
            "request_mode": "",
        }
        self._update_key_pool_debug()

        requested_fixture_id = str(fixture_id).strip() if fixture_id else ""
        if requested_fixture_id:
            odds_url = self._fixture_odds_url(
                requested_fixture_id,
                bookmaker=bookmaker,
                verbosity=verbosity_text,
            )
            self.last_debug.update(
                {
                    "request_mode": "fixture_id",
                    "selection_mode": "fixture_id",
                    "selected_fixture_id": requested_fixture_id,
                    "odds_url": _sanitize_url(odds_url),
                }
            )
            payload = self._get_json(odds_url)
            events = self.normalize_response(
                payload,
                market_type=market_type,
                bookmaker=bookmaker,
                sport=resolved_sport_id or None,
                mapping_profile=mapping_profile,
            )
            self.last_debug["last_note"] = self.last_note or ""
            return events

        if sport is None:
            raise ProviderConfigurationError("OddsPapi sport is required without fixture_id")

        requested_tournament_id = str(tournament_id).strip() if tournament_id else ""
        if requested_tournament_id:
            limited_tournament_ids = [requested_tournament_id]
            self.last_debug.update(
                {
                    "request_mode": "tournament_id",
                    "selection_mode": "tournament_id",
                    "selected_tournament_ids": limited_tournament_ids,
                    "selected_tournament_samples": [
                        {
                            "id": limited_tournament_ids[0],
                            "name": "",
                            "slug": "",
                            "category": "",
                        }
                    ],
                }
            )
        else:
            tournaments_url = self._tournaments_url(resolved_sport_id)
            self.last_debug["tournaments_url"] = _sanitize_url(tournaments_url)
            tournaments_payload = self._get_json(tournaments_url)
            tournament_records = _tournament_records(tournaments_payload)
            selected_records = _select_tournament_records(
                tournament_records,
                keyword=tournament_keyword,
            )
            selection_mode = (
                "tournament_keyword" if tournament_keyword else "fallback_first_n"
            )
            selected_records_with_ids = [
                record
                for record in selected_records
                if _tournament_ids_from_records([record])
            ]
            limited_records = selected_records_with_ids[
                : self.max_tournament_ids_per_request
            ]
            limited_tournament_ids = _tournament_ids_from_records(limited_records)
            self.last_debug.update(
                {
                    "request_mode": "tournaments",
                    "selection_mode": selection_mode,
                    "tournaments_count": len(tournament_records),
                    "tournament_samples": _tournament_samples(
                        tournament_records,
                        secret=self.api_key,
                    ),
                    "selected_tournament_ids": limited_tournament_ids,
                    "selected_tournament_samples": _tournament_samples(
                        limited_records,
                        secret=self.api_key,
                    ),
                }
            )
            if not limited_tournament_ids:
                self.last_note = _no_tournaments_note(
                    resolved_sport_id,
                    tournament_keyword=tournament_keyword,
                )
                self.last_debug.update(
                    {
                        "normalized_events_count": 0,
                        "last_note": self.last_note,
                    }
                )
                return []

        odds_url = self._odds_by_tournaments_url(
            limited_tournament_ids,
            bookmaker=bookmaker,
            verbosity=verbosity_text,
        )
        self.last_debug["odds_by_tournaments_url"] = _sanitize_url(odds_url)
        payload = self._get_json(odds_url)
        events = self.normalize_response(
            payload,
            market_type=market_type,
            bookmaker=bookmaker,
            sport=resolved_sport_id,
            mapping_profile=mapping_profile,
        )
        self.last_debug["last_note"] = self.last_note or ""
        return events

    def normalize_response(
        self,
        payload: object,
        *,
        market_type: str = "h2h",
        bookmaker: str | None = None,
        sport: str | None = None,
        mapping_profile: str | None = None,
    ) -> list[ProviderOddsEvent]:
        events: list[ProviderOddsEvent] = []
        mapping_profile = _validated_mapping_profile(mapping_profile)
        used_mapping_sources: list[str] = []
        profile_rejection_counts: dict[str, int] = {}
        for event in _event_records(payload):
            team_a, team_b = _event_teams(event)
            if not team_a or not team_b:
                continue

            for book, market in _bookmaker_markets(event):
                raw_book_name = _bookmaker_name(book) or _bookmaker_name(event)
                if (
                    bookmaker
                    and raw_book_name
                    and bookmaker.casefold() != raw_book_name.casefold()
                ):
                    continue
                book_name = bookmaker or raw_book_name
                if not book_name:
                    continue

                profile_rejection_reason = (
                    _profile_team_odds_rejection_reason(
                        market,
                        event,
                        bookmaker=book_name,
                        sport=sport,
                        mapping_profile=mapping_profile,
                    )
                    if mapping_profile
                    else ""
                )
                profile_odds = _profile_team_odds(
                    market,
                    event,
                    bookmaker=book_name,
                    sport=sport,
                    mapping_profile=mapping_profile,
                )
                mapping_source = ""
                if profile_odds:
                    odds_a, odds_b, mapping_source = profile_odds
                    event_market_type = "h2h"
                else:
                    if _mapping_profile_requires_exact_match(mapping_profile):
                        if profile_rejection_reason:
                            profile_rejection_counts[profile_rejection_reason] = (
                                profile_rejection_counts.get(
                                    profile_rejection_reason,
                                    0,
                                )
                                + 1
                            )
                        continue
                    if _is_match_winner_market(market, sport=sport):
                        normalized_market_type = "match_winner"
                    else:
                        normalized_market_type = _normalized_market_type(
                            _market_type_text(market) or market_type
                        )
                    if not _market_matches(market_type, normalized_market_type):
                        continue

                    odds_a, odds_b = _team_odds(market, event, team_a, team_b)
                    event_market_type = normalized_market_type
                if odds_a is None or odds_b is None:
                    continue
                if mapping_source and mapping_source not in used_mapping_sources:
                    used_mapping_sources.append(mapping_source)

                raw = {
                    "event": event,
                    "bookmaker": book,
                    "market": market,
                }
                if mapping_source:
                    raw["mapping_source"] = mapping_source

                events.append(
                    ProviderOddsEvent(
                        provider=self.name,
                        bookmaker=book_name,
                        game=_event_game(event, sport),
                        tournament=_event_tournament(event),
                        start_time=_event_start_time(event),
                        team_a=team_a,
                        team_b=team_b,
                        market_type=event_market_type,
                        odds_a=odds_a,
                        odds_b=odds_b,
                        event_url=_first_text(
                            event.get("event_url"),
                            event.get("eventUrl"),
                            event.get("url"),
                            event.get("link"),
                        )
                        or None,
                        raw=raw,
                    )
                )

        self._record_odds_payload_debug(payload, len(events), sport, bookmaker)
        self.last_debug["mapping_profile"] = mapping_profile or ""
        self.last_debug["mapping_sources"] = used_mapping_sources
        self.last_debug["mapping_profile_rejection_reasons"] = dict(
            sorted(profile_rejection_counts.items())
        )
        return events

    def _record_odds_payload_debug(
        self,
        payload: object,
        normalized_events_count: int,
        sport: str | None,
        bookmaker: str | None,
    ) -> None:
        debug = self.last_debug or {}
        debug.setdefault("resolved_sport_id", sport or "")
        debug.setdefault("bookmaker", bookmaker or "")
        debug.update(_odds_payload_debug(payload, bookmaker=bookmaker))
        debug["market_explorer"] = _market_explorer_debug(
            payload,
            bookmaker=bookmaker,
            secret=self.api_key,
        )
        debug["normalized_events_count"] = normalized_events_count
        debug["last_note"] = self.last_note or ""
        self.last_debug = debug

    def _tournaments_url(self, sport: str) -> str:
        return self._api_url(
            "/v4/tournaments",
            {
                "sportId": sport,
                "apiKey": self.api_key or "",
            },
        )

    def _odds_by_tournaments_url(
        self,
        tournament_ids: list[str],
        *,
        bookmaker: str | None = None,
        verbosity: str = "3",
    ) -> str:
        query = {
            "tournamentIds": ",".join(tournament_ids),
            "language": "en",
            "verbosity": verbosity,
            "oddsFormat": "decimal",
            "apiKey": self.api_key or "",
        }
        if bookmaker:
            query = {
                "tournamentIds": query["tournamentIds"],
                "bookmakers": bookmaker,
                "language": query["language"],
                "verbosity": query["verbosity"],
                "oddsFormat": query["oddsFormat"],
                "apiKey": query["apiKey"],
            }
        return self._api_url("/v4/odds-by-tournaments", query)

    def _fixture_odds_url(
        self,
        fixture_id: str,
        *,
        bookmaker: str | None = None,
        verbosity: str = "3",
    ) -> str:
        query = {
            "fixtureId": fixture_id,
            "language": "en",
            "verbosity": verbosity,
            "oddsFormat": "decimal",
            "apiKey": self.api_key or "",
        }
        if bookmaker:
            query = {
                "fixtureId": query["fixtureId"],
                "bookmakers": bookmaker,
                "language": query["language"],
                "verbosity": query["verbosity"],
                "oddsFormat": query["oddsFormat"],
                "apiKey": query["apiKey"],
            }
        return self._api_url("/v4/odds", query)

    def _api_url(self, path: str, query: dict[str, str]) -> str:
        return f"{self.base_url}{path}?{urlencode(query, safe=',')}"

    def _get_json(self, url: str) -> object:
        self.all_keys_exhausted = False
        self._update_key_pool_debug()
        attempts = max(len(self.api_keys), 1)
        for attempt in range(attempts):
            request_url = self._url_for_current_key(url)
            request = Request(
                request_url,
                headers={"User-Agent": "provider-odds-probe/0.1"},
            )
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self._record_key_success()
                return payload
            except HTTPError as exc:
                body_preview = _response_body_preview(
                    exc,
                    secrets=self.api_keys,
                )
                message = (
                    f"OddsPapi returned HTTP {exc.code} for "
                    f"{_sanitize_url(request_url)}: {body_preview}"
                )
                if self._has_key_pool and _is_oddspapi_rate_limit_error(
                    exc.code,
                    body_preview,
                ):
                    if attempt < attempts - 1:
                        self._advance_key_after_rate_limit(message)
                        continue
                    self._record_all_keys_exhausted(message)
                    raise OddsPapiAllKeysExhaustedError(
                        "OddsPapi all_keys_exhausted: "
                        "oddspapi_all_keys_exhausted=true "
                        f"attempts={attempts} last_error={message}"
                    ) from exc
                raise ProviderRequestError(message) from exc
            except URLError as exc:
                raise ProviderRequestError(f"OddsPapi request failed: {exc}") from exc

        raise OddsPapiAllKeysExhaustedError(
            "OddsPapi all_keys_exhausted: oddspapi_all_keys_exhausted=true"
        )

    @property
    def _has_key_pool(self) -> bool:
        return len(self.api_keys) > 1

    def _ensure_api_key(self) -> None:
        if self.api_key:
            return
        raise ProviderConfigurationError(
            f"Missing {ODDSPAPI_API_KEYS_ENV} or one of "
            f"{', '.join(ODDSPAPI_FALLBACK_API_KEY_ENVS)} "
            "in environment or .env file"
        )

    def _current_api_key(self) -> str | None:
        if not self.api_keys:
            return None
        self.current_key_index %= len(self.api_keys)
        return self.api_keys[self.current_key_index]

    def _url_for_current_key(self, url: str) -> str:
        self.api_key = self._current_api_key()
        if not self.api_key:
            return url
        return _url_with_api_key(url, self.api_key)

    def _load_key_state(self) -> None:
        if not self._has_key_pool:
            return
        try:
            if not self.key_state_path.exists():
                return
            payload = json.loads(self.key_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        self.current_key_index = _int_value(payload.get("current_key_index"))
        self.current_key_index %= len(self.api_keys)
        self.last_failover_at = _debug_text(
            payload.get("last_failover_at"),
            secret=None,
        )
        self.last_error = _redact_secrets(
            _debug_text(payload.get("last_error"), secret=None, limit=500),
            self.api_keys,
        )
        self.failover_count = max(_int_value(payload.get("failover_count")), 0)

    def _write_key_state(self) -> None:
        if not self._has_key_pool:
            return
        payload = {
            "current_key_index": self.current_key_index,
            "last_failover_at": self.last_failover_at,
            "last_error": _redact_secrets(self.last_error, self.api_keys),
            "failover_count": self.failover_count,
        }
        try:
            self.key_state_path.parent.mkdir(parents=True, exist_ok=True)
            self.key_state_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError:
            return

    def _advance_key_after_rate_limit(self, error: str) -> None:
        if not self._has_key_pool:
            return
        self.current_key_index = (self.current_key_index + 1) % len(self.api_keys)
        self.api_key = self._current_api_key()
        self.failover_count += 1
        self.last_failover_at = _utc_state_timestamp()
        self.last_error = _redact_secrets(error, self.api_keys)
        self.all_keys_exhausted = False
        self._write_key_state()
        self._update_key_pool_debug()

    def _record_key_success(self) -> None:
        self.api_key = self._current_api_key()
        self.all_keys_exhausted = False
        self._write_key_state()
        self._update_key_pool_debug()

    def _record_all_keys_exhausted(self, error: str) -> None:
        self.last_error = _redact_secrets(error, self.api_keys)
        self.all_keys_exhausted = True
        self._write_key_state()
        self._update_key_pool_debug()

    def _update_key_pool_debug(self) -> None:
        if not isinstance(self.last_debug, dict):
            self.last_debug = {}
        self.last_debug.update(
            {
                "oddspapi_key_pool_size": len(self.api_keys),
                "oddspapi_key_index": (
                    f"key_{self.current_key_index}" if self.api_keys else ""
                ),
                "oddspapi_key_failover_count": self.failover_count,
                "oddspapi_all_keys_exhausted": self.all_keys_exhausted,
            }
        )


def _sanitize_url(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.query:
        return url

    redacted_query = [
        (key, "***" if key.casefold() == "apikey" else value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(redacted_query, safe=",*"),
            parsed.fragment,
        )
    )


def _url_with_api_key(url: str, api_key: str) -> str:
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    replaced = False
    updated_query: list[tuple[str, str]] = []
    for key, value in query:
        if key.casefold() == "apikey":
            updated_query.append((key, api_key))
            replaced = True
        else:
            updated_query.append((key, value))
    if not replaced:
        updated_query.append(("apiKey", api_key))
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(updated_query, safe=","),
            parsed.fragment,
        )
    )


def _api_key_values(values: object) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raw_values = values.split(",")
    elif isinstance(values, (list, tuple)):
        raw_values = [str(value) for value in values]
    else:
        raw_values = [str(values)]
    keys: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        key = value.strip()
        if not key or key in seen:
            continue
        keys.append(key)
        seen.add(key)
    return keys


def _api_keys_from_env() -> list[str]:
    pooled = _api_key_values(os.environ.get(ODDSPAPI_API_KEYS_ENV))
    if pooled:
        return pooled
    for env_name in ODDSPAPI_FALLBACK_API_KEY_ENVS:
        keys = _api_key_values(os.environ.get(env_name))
        if keys:
            return keys[:1]
    return []


def _is_oddspapi_rate_limit_error(status_code: int, body_preview: str) -> bool:
    text = body_preview.casefold()
    return (
        status_code == 429
        or "quota exceeded" in text
        or "rate_limited" in text
        or "rate limited" in text
        or "rate-limit" in text
        or "rate limit" in text
    )


def _redact_secrets(text: str, secrets: list[str] | tuple[str, ...]) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "***")
    return re.sub(r"(?i)(apiKey=)[^&\s\"']+", r"\1***", redacted)


def _utc_state_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _int_value(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def resolve_sport_id(sport: str | int) -> str:
    if isinstance(sport, int) and not isinstance(sport, bool):
        return str(sport)

    text = str(sport).strip()
    if re.fullmatch(r"\d+", text):
        return str(int(text))

    alias_key = _alias_key(text)
    if alias_key in SPORT_ID_ALIASES:
        return SPORT_ID_ALIASES[alias_key]

    raise ValueError(
        "Unknown OddsPapi sport alias: "
        f"{sport!r}. Use a numeric sportId or one of: "
        "dota2, dota, esport-dota, cs2, counter-strike, "
        "esport-counter-strike, lol, league-of-legends, "
        "esport-league-of-legends."
    )


def _resolve_sport_id(sport: str | int) -> str:
    return resolve_sport_id(sport)


def _alias_key(value: str) -> str:
    return re.sub(r"[\s_-]+", " ", value.strip().casefold())


def _odds_payload_debug(
    payload: object,
    *,
    bookmaker: str | None,
) -> dict[str, object]:
    if isinstance(payload, list):
        return {
            "raw_odds_root_type": "list",
            "raw_odds_top_level_count": len(payload),
            "raw_odds_first_item_keys": _first_item_keys(payload),
            "raw_odds_top_level_fixture_keys": _top_level_fixture_keys(payload),
            "raw_odds_zero_sample": _raw_odds_zero_sample(
                payload,
                bookmaker=bookmaker,
            ),
        }
    if isinstance(payload, dict):
        return {
            "raw_odds_root_type": "dict",
            "raw_odds_top_level_keys": list(payload.keys()),
            "raw_odds_first_item_keys": list(payload.keys()),
            "raw_odds_top_level_fixture_keys": _top_level_fixture_keys(payload),
            "raw_odds_zero_sample": _raw_odds_zero_sample(
                payload,
                bookmaker=bookmaker,
            ),
        }
    return {"raw_odds_root_type": type(payload).__name__}


def _market_explorer_debug(
    payload: object,
    *,
    bookmaker: str | None,
    secret: str | None,
) -> list[dict[str, object]]:
    fixtures: list[dict[str, object]] = []
    for event in _event_records(payload)[:3]:
        book, bookmaker_path = _find_bookmaker_for_debug(event, bookmaker)
        fixture = {
            "fixtureId": _debug_text(
                _first_raw(event.get("fixtureId"), event.get("id")),
                secret=secret,
            ),
            "participant1Id": _debug_text(event.get("participant1Id"), secret=secret),
            "participant1Name": _debug_text(event.get("participant1Name"), secret=secret),
            "participant1ShortName": _debug_text(
                event.get("participant1ShortName"),
                secret=secret,
            ),
            "participant1Abbr": _debug_text(event.get("participant1Abbr"), secret=secret),
            "participant2Id": _debug_text(event.get("participant2Id"), secret=secret),
            "participant2Name": _debug_text(event.get("participant2Name"), secret=secret),
            "participant2ShortName": _debug_text(
                event.get("participant2ShortName"),
                secret=secret,
            ),
            "participant2Abbr": _debug_text(event.get("participant2Abbr"), secret=secret),
            "tournamentId": _debug_text(event.get("tournamentId"), secret=secret),
            "tournamentName": _debug_text(event.get("tournamentName"), secret=secret),
            "categoryName": _debug_text(event.get("categoryName"), secret=secret),
            "startTime": _debug_text(event.get("startTime"), secret=secret),
            "trueStartTime": _debug_text(event.get("trueStartTime"), secret=secret),
            "bookmaker_path": _debug_text(bookmaker_path, secret=secret),
            "bookmaker_name": _debug_text(_bookmaker_name(book), secret=secret),
            "markets": [],
        }
        if book:
            fixture["markets"] = _market_explorer_markets(
                book,
                secret=secret,
                sport=_profile_sport_id(event, None),
            )
        fixtures.append(fixture)
    return fixtures


def _market_explorer_markets(
    book: dict[str, Any],
    *,
    secret: str | None,
    sport: str | None,
) -> list[dict[str, object]]:
    markets: list[dict[str, object]] = []
    for market, market_path in _market_records_from_book_with_paths(book)[:30]:
        raw_outcomes = _raw_outcome_records(market)
        outcomes = [
            _market_explorer_outcome(outcome, secret=secret)
            for outcome in raw_outcomes[:4]
        ]
        markets.append(
            {
                "market_path": _debug_text(market_path, secret=secret),
                "marketId": _debug_text(
                    _first_raw(
                        market.get("marketId"),
                        market.get("market_id"),
                        market.get("id"),
                    ),
                    secret=secret,
                ),
                "key": _debug_text(market.get("key"), secret=secret),
                "marketName": _debug_text(_market_type_text(market), secret=secret),
                "bookmakerMarketId": _debug_text(
                    market.get("bookmakerMarketId"),
                    secret=secret,
                ),
                "marketActive": _debug_text(market.get("marketActive"), secret=secret),
                "suspended": _debug_text(market.get("suspended"), secret=secret),
                "outcome_count": len(raw_outcomes),
                "way_count": len(raw_outcomes),
                "way_type": _market_way_type(len(raw_outcomes)),
                "looks_like_match_winner": _is_match_winner_market(
                    market,
                    sport=sport,
                ),
                "outcome_names": [
                    _debug_text(
                        outcome.get("name") or outcome.get("playerName"),
                        secret=secret,
                    )
                    for outcome in outcomes
                ],
                "decimal_odds": [
                    _debug_text(outcome.get("price"), secret=secret)
                    for outcome in outcomes
                ],
                "outcomes": outcomes,
            }
        )
    return markets


def _market_way_type(outcome_count: int) -> str:
    if outcome_count in {2, 3}:
        return f"{outcome_count}-way"
    if outcome_count > 0:
        return f"{outcome_count}-outcome"
    return "unknown"


def _market_explorer_outcome(
    outcome: dict[str, Any],
    *,
    secret: str | None,
) -> dict[str, str]:
    player = _first_player_for_debug(outcome.get("players"))
    return {
        "outcome_key": _debug_text(outcome.get("outcome_key"), secret=secret),
        "selectionId": _debug_text(outcome.get("selectionId"), secret=secret),
        "name": _debug_text(
            _first_text(
                outcome.get("name"),
                outcome.get("label"),
                outcome.get("playerName"),
                outcome.get("participantName"),
            ),
            secret=secret,
        ),
        "players_type": _debug_text(_container_type(outcome.get("players")), secret=secret),
        "player_key": _debug_text(player.get("player_key"), secret=secret),
        "playerName": _debug_text(player.get("playerName"), secret=secret),
        "participantId": _debug_text(player.get("participantId"), secret=secret),
        "player_selectionId": _debug_text(player.get("selectionId"), secret=secret),
        "bookmakerOutcomeId": _debug_text(
            player.get("bookmakerOutcomeId"),
            secret=secret,
        ),
        "price": _debug_text(
            _first_raw(
                player.get("price"),
                player.get("decimalOdds"),
                player.get("odds"),
                player.get("value"),
                outcome.get("price"),
                outcome.get("decimalOdds"),
                outcome.get("odds"),
                outcome.get("value"),
            ),
            secret=secret,
        ),
        "exchangeMeta": _debug_text(
            _exchange_meta_debug(player.get("exchangeMeta")),
            secret=secret,
        ),
    }


def _first_player_for_debug(value: object) -> dict[str, Any]:
    if isinstance(value, list):
        players = _dicts(value)
        if players:
            return players[0]
        return {}
    if not isinstance(value, dict):
        return {}
    if _looks_like_player_record(value):
        return value

    for key, item in value.items():
        if isinstance(item, dict):
            player = dict(item)
        else:
            player = {"value": item}
        player["player_key"] = str(key)
        return player
    return {}


def _first_item_keys(value: list[object]) -> list[str]:
    for item in value:
        if isinstance(item, dict):
            return list(item.keys())
    return []


def _top_level_fixture_keys(payload: object) -> list[str]:
    first_item = _first_debug_dict(payload)
    if not first_item:
        return []
    if _is_direct_fixture(first_item):
        return list(first_item.keys())
    first_nested = _first_nested_event_dict(first_item)
    if first_nested:
        return list(first_nested.keys())
    return list(first_item.keys())


def _raw_odds_zero_sample(
    payload: object,
    *,
    bookmaker: str | None,
) -> dict[str, object]:
    first_item = _first_debug_dict(payload)
    sample: dict[str, object] = {
        "first_item_keys": list(first_item.keys()) if first_item else [],
    }
    if not first_item:
        return sample

    nested_records: list[dict[str, Any]] = []
    for key in ("data", "events", "fixtures", "matches"):
        value = first_item.get(key)
        if isinstance(value, list):
            sample[f"nested_{key}_count"] = len(value)
            nested_records.extend(_dicts(value))

    first_nested = _first_nested_event_dict(first_item) or (
        nested_records[0] if nested_records else first_item
    )
    sample["first_nested_event_keys"] = list(first_nested.keys()) if first_nested else []
    if first_nested and _is_direct_fixture(first_nested):
        sample.update(_fixture_debug_values(first_nested))

    bookmaker_odds = first_nested.get("bookmakerOdds") if first_nested else None
    if bookmaker_odds is not None:
        sample["bookmaker_odds_type"] = _container_type(bookmaker_odds)
        if isinstance(bookmaker_odds, dict):
            sample["bookmaker_odds_keys"] = list(bookmaker_odds.keys())
        elif isinstance(bookmaker_odds, list):
            sample["bookmaker_odds_list_length"] = len(bookmaker_odds)
            sample["bookmaker_odds_first_item_keys"] = _first_item_keys(bookmaker_odds)

    book, bookmaker_path = _find_bookmaker_for_debug(first_nested, bookmaker)
    if book:
        sample["bookmaker_keys"] = list(book.keys())
        sample["matched_bookmaker_path"] = bookmaker_path
        sample["matched_bookmaker_name"] = _bookmaker_name(book)
        market_container = _first_present(book, *MARKET_CONTAINER_KEYS)
        sample["market_container_type"] = _container_type(market_container)
        market = _first_market_dict(first_nested, book)
        if market:
            sample["market_keys"] = list(market.keys())
            sample.update(_market_debug_values(market))
            raw_outcomes = _raw_outcome_records(market)
            if raw_outcomes:
                first_outcome = raw_outcomes[0]
                sample["first_outcome_keys"] = list(first_outcome.keys())
                players = first_outcome.get("players")
                sample["first_outcome_players_type"] = _container_type(players)
                sample.update(_players_debug(players))

            candidates = _outcome_records(market)
            sample["parsed_outcome_candidate_count"] = len(candidates)
            sample["parsed_outcome_candidate_reasons"] = (
                _outcome_candidate_debug_reasons(candidates, first_nested)
            )
            sample["parsed_outcome_candidate_samples"] = (
                _outcome_candidate_debug_samples(candidates, first_nested)
            )

    if first_nested and _is_direct_fixture(first_nested):
        sample["direct_fixture_rejection_reason"] = _direct_fixture_rejection_reason(
            first_nested,
            bookmaker=bookmaker,
        )

    return sample


def _first_debug_dict(payload: object) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                return item
    return {}


def _first_nested_event_dict(item: dict[str, Any]) -> dict[str, Any]:
    if (
        _first_bookmaker_dict(item)
        or item.get("teams")
        or item.get("participants")
        or _is_direct_fixture(item)
    ):
        return item

    for key in ("data", "events", "fixtures", "matches"):
        value = item.get(key)
        for nested in _dicts(value):
            found = _first_nested_event_dict(nested)
            if found:
                return found
    return {}


def _is_direct_fixture(event: dict[str, Any]) -> bool:
    return any(
        key in event
        for key in (
            "fixtureId",
            "participant1Id",
            "participant2Id",
            "participant1Name",
            "participant2Name",
        )
    )


def _container_type(value: object) -> str:
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    if value is None:
        return ""
    return type(value).__name__


def _fixture_debug_values(event: dict[str, Any]) -> dict[str, str]:
    return {
        "fixture_participant1_id": _debug_text(event.get("participant1Id"), secret=None),
        "fixture_participant1_name": _debug_text(
            event.get("participant1Name"),
            secret=None,
        ),
        "fixture_participant1_short": _debug_text(
            event.get("participant1ShortName"),
            secret=None,
        ),
        "fixture_participant1_abbr": _debug_text(
            event.get("participant1Abbr"),
            secret=None,
        ),
        "fixture_participant2_id": _debug_text(event.get("participant2Id"), secret=None),
        "fixture_participant2_name": _debug_text(
            event.get("participant2Name"),
            secret=None,
        ),
        "fixture_participant2_short": _debug_text(
            event.get("participant2ShortName"),
            secret=None,
        ),
        "fixture_participant2_abbr": _debug_text(
            event.get("participant2Abbr"),
            secret=None,
        ),
    }


def _market_debug_values(market: dict[str, Any]) -> dict[str, str]:
    return {
        "market_id": _debug_text(
            _first_raw(market.get("marketId"), market.get("market_id"), market.get("id")),
            secret=None,
        ),
        "market_key": _debug_text(market.get("key"), secret=None),
        "market_bookmaker_market_id": _debug_text(
            market.get("bookmakerMarketId"),
            secret=None,
        ),
        "market_active": _debug_text(market.get("marketActive"), secret=None),
    }


def _find_bookmaker_for_debug(
    event: dict[str, Any],
    bookmaker: str | None,
) -> tuple[dict[str, Any], str]:
    candidates: list[tuple[dict[str, Any], str]] = []
    for key in BOOKMAKER_CONTAINER_KEYS:
        for index, book in enumerate(_bookmaker_entries(event.get(key))):
            candidates.append((book, f"{key}[{index}]"))
    for path, book in _bookmaker_odds_entries_with_paths(event.get("bookmakerOdds")):
        candidates.append((book, f"bookmakerOdds{path}"))

    if not candidates:
        return {}, ""

    if bookmaker:
        requested = bookmaker.casefold()
        for book, path in candidates:
            book_name = _bookmaker_name(book)
            if book_name and book_name.casefold() == requested:
                return book, path

    return candidates[0]


def _direct_fixture_rejection_reason(
    event: dict[str, Any],
    *,
    bookmaker: str | None,
) -> str:
    team_a, team_b = _event_teams(event)
    if not team_a or not team_b:
        return "missing_team_names"

    book, _path = _find_bookmaker_for_debug(event, bookmaker)
    if not book:
        return "bookmaker_odds_not_found"
    if bookmaker:
        book_name = _bookmaker_name(book)
        if book_name and book_name.casefold() != bookmaker.casefold():
            return "requested_bookmaker_not_found"

    markets = _market_records_from_book(book)
    if not markets:
        return "market_container_not_found"

    winner_markets = [market for market in markets if _is_match_winner_market(market)]
    if not winner_markets:
        return "match_winner_market_not_found"

    for market in winner_markets:
        odds_a, odds_b = _team_odds(market, event, team_a, team_b)
        if odds_a is not None and odds_b is not None:
            return "unknown_zero_normalized_events"

    return "team_outcomes_not_found"


def _first_bookmaker_dict(event: dict[str, Any]) -> dict[str, Any]:
    bookmaker_entries = []
    for key in BOOKMAKER_CONTAINER_KEYS:
        bookmaker_entries.extend(_bookmaker_entries(event.get(key)))
    bookmaker_entries.extend(_bookmaker_odds_entries(event.get("bookmakerOdds")))
    if bookmaker_entries:
        return bookmaker_entries[0]
    return {}


def _first_market_dict(
    event: dict[str, Any],
    bookmaker: dict[str, Any],
) -> dict[str, Any]:
    markets = _market_records_from_book(bookmaker)
    if markets:
        return markets[0]
    event_markets = _market_records_from_container(
        _first_present(event, *MARKET_CONTAINER_KEYS)
    )
    if event_markets:
        return event_markets[0]
    return {}


def _response_body_preview(
    exc: HTTPError,
    *,
    secret: str | None = None,
    secrets: list[str] | tuple[str, ...] | None = None,
) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    finally:
        exc.close()
    body = _redact_secrets(body, [*(secrets or []), *([secret] if secret else [])])
    return body[:500]


def _event_records(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        records: list[dict[str, Any]] = []
        for item in _dicts(payload):
            records.extend(_event_records(item))
        return records
    if not isinstance(payload, dict):
        raise ValueError("OddsPapi response must be a JSON object or list")

    records = []
    context = _event_context(payload)
    for key in ("data", "events", "fixtures", "matches", "games", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            for item in _dicts(value):
                records.extend(_event_records({**context, **item}))

    if records:
        return records

    return [payload]


def _tournament_ids(payload: object) -> list[str]:
    return _tournament_ids_from_records(_tournament_records(payload))


def _tournament_ids_from_records(records: list[dict[str, Any]]) -> list[str]:
    tournament_ids: list[str] = []
    seen: set[str] = set()
    for tournament in records:
        value = _first_raw(
            tournament.get("tournamentId"),
            tournament.get("tournament_id"),
            tournament.get("id"),
        )
        nested_tournament = tournament.get("tournament")
        if value is None and isinstance(nested_tournament, dict):
            value = _first_raw(
                nested_tournament.get("tournamentId"),
                nested_tournament.get("tournament_id"),
                nested_tournament.get("id"),
            )

        tournament_id = str(value).strip() if value is not None else ""
        if tournament_id and tournament_id not in seen:
            tournament_ids.append(tournament_id)
            seen.add(tournament_id)

    return tournament_ids


def _select_tournament_records(
    records: list[dict[str, Any]],
    *,
    keyword: object,
) -> list[dict[str, Any]]:
    keywords = _keyword_values(keyword)
    if not keywords:
        return records

    return [
        tournament
        for tournament in records
        if _tournament_matches_keywords(tournament, keywords)
    ]


def _tournament_matches_keywords(
    tournament: dict[str, Any],
    keywords: list[str],
) -> bool:
    haystack = _tournament_search_text(tournament).casefold()
    return any(keyword.casefold() in haystack for keyword in keywords)


def _keyword_values(value: object) -> list[str]:
    if value is None:
        return []

    raw_values: list[object]
    if isinstance(value, str):
        raw_values = [value]
    elif isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        raw_values = [value]

    keywords: list[str] = []
    for raw_value in raw_values:
        for part in str(raw_value).split(","):
            keyword = " ".join(part.strip().split())
            if keyword and keyword not in keywords:
                keywords.append(keyword)
    return keywords


def _candidate_limit(value: int) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = 30
    return min(100, max(1, limit))


def _tournament_search_text(tournament: dict[str, Any]) -> str:
    nested_tournament = tournament.get("tournament")
    nested = nested_tournament if isinstance(nested_tournament, dict) else {}
    category = tournament.get("category")
    nested_category = category if isinstance(category, dict) else {}
    return " ".join(
        value
        for value in (
            _first_text(
                tournament.get("name"),
                tournament.get("title"),
                tournament.get("tournamentName"),
                tournament.get("tournament_name"),
                nested.get("name"),
                nested.get("title"),
                nested.get("tournamentName"),
            ),
            _first_text(
                tournament.get("slug"),
                tournament.get("key"),
                nested.get("slug"),
                nested.get("key"),
            ),
            _first_text(
                tournament.get("category"),
                tournament.get("categoryName"),
                tournament.get("categorySlug"),
                tournament.get("sport"),
                tournament.get("sportId"),
                nested.get("category"),
                nested.get("categoryName"),
                nested.get("categorySlug"),
                nested_category.get("name"),
                nested_category.get("slug"),
                nested_category.get("categoryName"),
                nested_category.get("categorySlug"),
            ),
        )
        if value
    )


def _no_tournaments_note(
    sport_id: str,
    *,
    tournament_keyword: object,
) -> str:
    keywords = _keyword_values(tournament_keyword)
    if keywords:
        return (
            "No OddsPapi tournaments matched "
            f"tournament_keyword={','.join(keywords)} for sportId={sport_id}"
        )
    return f"No OddsPapi tournaments found for sportId={sport_id}"


def _tournament_samples(
    records: list[dict[str, Any]],
    *,
    secret: str | None,
) -> list[dict[str, str]]:
    samples: list[dict[str, str]] = []
    for tournament in records[:10]:
        nested_tournament = tournament.get("tournament")
        nested = nested_tournament if isinstance(nested_tournament, dict) else {}
        samples.append(
            {
                "id": _debug_text(
                    _first_raw(
                        tournament.get("tournamentId"),
                        tournament.get("tournament_id"),
                        tournament.get("id"),
                        nested.get("tournamentId"),
                        nested.get("tournament_id"),
                        nested.get("id"),
                    ),
                    secret=secret,
                ),
                "name": _debug_text(
                    _first_text(
                        tournament.get("name"),
                        tournament.get("title"),
                        tournament.get("tournamentName"),
                        tournament.get("tournament_name"),
                        nested.get("name"),
                        nested.get("title"),
                        nested.get("tournamentName"),
                    ),
                    secret=secret,
                ),
                "slug": _debug_text(
                    _first_text(
                        tournament.get("slug"),
                        tournament.get("key"),
                        nested.get("slug"),
                        nested.get("key"),
                    ),
                    secret=secret,
                ),
                "category": _debug_text(
                    _first_text(
                        tournament.get("category"),
                        tournament.get("categoryName"),
                        tournament.get("sport"),
                        tournament.get("sportId"),
                        nested.get("category"),
                        nested.get("categoryName"),
                    ),
                    secret=secret,
                ),
            }
        )
    return samples


def _tournament_candidate_samples(
    records: list[dict[str, Any]],
    *,
    secret: str | None,
) -> list[dict[str, str]]:
    samples: list[dict[str, str]] = []
    for tournament in records:
        nested_tournament = tournament.get("tournament")
        nested = nested_tournament if isinstance(nested_tournament, dict) else {}
        category = tournament.get("category")
        nested_category = category if isinstance(category, dict) else {}
        samples.append(
            {
                "id": _debug_text(
                    _first_raw(
                        tournament.get("tournamentId"),
                        tournament.get("tournament_id"),
                        tournament.get("id"),
                        nested.get("tournamentId"),
                        nested.get("tournament_id"),
                        nested.get("id"),
                    ),
                    secret=secret,
                ),
                "name": _debug_text(
                    _first_text(
                        tournament.get("name"),
                        tournament.get("title"),
                        tournament.get("tournamentName"),
                        tournament.get("tournament_name"),
                        nested.get("name"),
                        nested.get("title"),
                        nested.get("tournamentName"),
                    ),
                    secret=secret,
                ),
                "slug": _debug_text(
                    _first_text(
                        tournament.get("slug"),
                        tournament.get("key"),
                        nested.get("slug"),
                        nested.get("key"),
                    ),
                    secret=secret,
                ),
                "category": _debug_text(
                    _first_text(
                        tournament.get("category"),
                        tournament.get("categoryName"),
                        nested.get("category"),
                        nested.get("categoryName"),
                        nested_category.get("name"),
                    ),
                    secret=secret,
                ),
                "categoryName": _debug_text(
                    _first_text(
                        tournament.get("categoryName"),
                        nested.get("categoryName"),
                        nested_category.get("categoryName"),
                        nested_category.get("name"),
                    ),
                    secret=secret,
                ),
                "categorySlug": _debug_text(
                    _first_text(
                        tournament.get("categorySlug"),
                        nested.get("categorySlug"),
                        nested_category.get("categorySlug"),
                        nested_category.get("slug"),
                    ),
                    secret=secret,
                ),
            }
        )
    return samples


def _debug_text(value: object, *, secret: str | None, limit: int = 120) -> str:
    text = "" if value is None else str(value).strip()
    if secret:
        text = text.replace(secret, "***")
    text = re.sub(r"(?i)(apiKey=)[^&\s\"']+", r"\1***", text)
    text = " ".join(text.split())
    return text[:limit]


def _tournament_records(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return _dicts(payload)
    if not isinstance(payload, dict):
        raise ValueError("OddsPapi tournaments response must be a JSON object or list")

    for key in ("data", "tournaments", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return _dicts(value)

    return [payload]


def _event_context(record: dict[str, Any]) -> dict[str, Any]:
    context: dict[str, Any] = {}
    for key in (
        "sport",
        "sportId",
        "sport_id",
        "sportKey",
        "sport_key",
        "game",
        "tournament",
        "tournamentName",
        "tournamentSlug",
        "categoryName",
        "category",
        "league",
        "competition",
        "series",
    ):
        if key in record:
            context[key] = record[key]

    if "tournament" not in context:
        tournament_name = _first_text(
            record.get("tournamentName"),
            record.get("name"),
            record.get("title"),
        )
        if tournament_name:
            context["tournament"] = tournament_name

    return context


def _event_teams(event: dict[str, Any]) -> tuple[str, str]:
    team_a = _first_text(
        event.get("team_a"),
        event.get("home_team"),
        event.get("homeTeam"),
        event.get("team1"),
        event.get("participant1Name"),
        event.get("participant1ShortName"),
        event.get("home"),
    )
    team_b = _first_text(
        event.get("team_b"),
        event.get("away_team"),
        event.get("awayTeam"),
        event.get("team2"),
        event.get("participant2Name"),
        event.get("participant2ShortName"),
        event.get("away"),
    )

    participants = _first_list(
        event.get("teams"),
        event.get("participants"),
        event.get("competitors"),
    )
    if (not team_a or not team_b) and len(participants) >= 2:
        team_a = team_a or _first_text(participants[0])
        team_b = team_b or _first_text(participants[1])

    return team_a, team_b


def _bookmaker_markets(
    event: dict[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []

    bookmaker_entries: list[dict[str, Any]] = []
    for key in BOOKMAKER_CONTAINER_KEYS:
        bookmaker_entries.extend(_bookmaker_entries(event.get(key)))
    bookmaker_entries.extend(_bookmaker_odds_entries(event.get("bookmakerOdds")))

    for book in bookmaker_entries:
        for market in _market_records_from_book(book):
            pairs.append((book, market))

    event_markets = _market_records_from_container(
        _first_present(event, *MARKET_CONTAINER_KEYS)
    )
    for market in event_markets:
        pairs.append((event, market))

    if not pairs:
        pairs.append((event, event))

    return pairs


def _bookmaker_entries(value: object) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return _dicts(value)
    if not isinstance(value, dict):
        return []

    entries: list[dict[str, Any]] = []
    for key, item in value.items():
        if isinstance(item, dict):
            entries.append({"key": key, **item})
        elif isinstance(item, list):
            entries.append({"key": key, "markets": item})
    return entries


def _bookmaker_odds_entries(value: object) -> list[dict[str, Any]]:
    return [book for _path, book in _bookmaker_odds_entries_with_paths(value)]


def _bookmaker_odds_entries_with_paths(
    value: object,
) -> list[tuple[str, dict[str, Any]]]:
    if isinstance(value, list):
        return [(f"[{index}]", item) for index, item in enumerate(_dicts(value))]
    if not isinstance(value, dict):
        return []

    if _has_market_container(value):
        return [("", value)]
    if _looks_like_market_record(value):
        return [("", {"markets": [value]})]
    if _looks_like_market_dict(value):
        return [("", {"markets": value})]

    entries: list[tuple[str, dict[str, Any]]] = []
    for key, item in value.items():
        path = f".{key}"
        if isinstance(item, dict):
            if _looks_like_market_dict(item) and not _has_market_container(item):
                entries.append((path, {"key": key, "markets": item}))
            else:
                entries.append((path, {"key": key, **item}))
        elif isinstance(item, list):
            entries.append((path, {"key": key, "markets": item}))
    return entries


def _market_records_from_book(book: dict[str, Any]) -> list[dict[str, Any]]:
    markets = _market_records_from_container(
        _first_present(book, *MARKET_CONTAINER_KEYS)
    )
    if markets:
        return markets
    if _looks_like_market_record(book):
        return [book]
    return []


def _market_records_from_book_with_paths(
    book: dict[str, Any],
) -> list[tuple[dict[str, Any], str]]:
    for key in MARKET_CONTAINER_KEYS:
        if key in book:
            markets = _market_records_from_container_with_paths(book[key], key)
            if markets:
                return markets
    if _looks_like_market_record(book):
        return [(book, "")]
    return []


def _market_records_from_container(value: object) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return _dicts(value)
    if not isinstance(value, dict):
        return []
    if _looks_like_market_record(value):
        return [value]

    records: list[dict[str, Any]] = []
    for key, item in value.items():
        if isinstance(item, dict):
            record = dict(item)
            record.setdefault("key", str(key))
            record.setdefault("marketId", str(key))
            records.append(record)
        elif isinstance(item, list):
            for market in _dicts(item):
                record = dict(market)
                record.setdefault("key", str(key))
                record.setdefault("marketId", str(key))
            records.append(record)
    return records


def _market_records_from_container_with_paths(
    value: object,
    base_path: str,
) -> list[tuple[dict[str, Any], str]]:
    if isinstance(value, list):
        return [
            (item, f"{base_path}[{index}]")
            for index, item in enumerate(_dicts(value))
        ]
    if not isinstance(value, dict):
        return []
    if _looks_like_market_record(value):
        return [(value, base_path)]

    records: list[tuple[dict[str, Any], str]] = []
    for key, item in value.items():
        path = f"{base_path}.{key}"
        if isinstance(item, dict):
            record = dict(item)
            record.setdefault("key", str(key))
            record.setdefault("marketId", str(key))
            records.append((record, path))
        elif isinstance(item, list):
            for index, market in enumerate(_dicts(item)):
                record = dict(market)
                record.setdefault("key", str(key))
                record.setdefault("marketId", str(key))
                records.append((record, f"{path}[{index}]"))
    return records


def _has_market_container(value: dict[str, Any]) -> bool:
    return any(isinstance(value.get(key), (dict, list)) for key in MARKET_CONTAINER_KEYS)


def _looks_like_market_dict(value: dict[str, Any]) -> bool:
    found_market = False
    for key, item in value.items():
        if isinstance(item, dict) and (
            _looks_like_market_key(key) or _looks_like_market_record(item)
        ):
            found_market = True
        elif isinstance(item, list) and _looks_like_market_key(key):
            found_market = True
        elif key in {"key", "name", "title", "bookmaker", "bookmakerName"}:
            return False
    return found_market


def _looks_like_market_key(value: object) -> bool:
    text = str(value).strip().casefold()
    if not text:
        return False
    if text.isdigit():
        return True
    return any(
        marker in text
        for marker in (
            "h2h",
            "match",
            "winner",
            "moneyline",
            "market",
        )
    )


def _looks_like_market_record(value: dict[str, Any]) -> bool:
    return bool(
        _first_present(value, *OUTCOME_CONTAINER_KEYS)
        or _first_present(
            value,
            "marketId",
            "market_id",
            "marketType",
            "market_type",
            "marketName",
            "market_name",
        )
    )


def _bookmaker_name(value: dict[str, Any]) -> str:
    return _first_text(
        value.get("bookmaker"),
        value.get("bookmakerKey"),
        value.get("bookmaker_name"),
        value.get("bookmakerName"),
        value.get("bookmakerSlug"),
        value.get("name"),
        value.get("title"),
        value.get("key"),
    )


def _event_game(event: dict[str, Any], fallback: str | None = None) -> str:
    return _first_text(
        event.get("game"),
        event.get("sportId"),
        event.get("sport_id"),
        event.get("sportKey"),
        event.get("sport_key"),
        fallback,
        event.get("sport_title"),
        event.get("sport_name"),
        event.get("sport"),
        event.get("esport"),
        event.get("category"),
    )


def _event_tournament(event: dict[str, Any]) -> str | None:
    return (
        _first_text(
            event.get("tournament"),
            event.get("tournamentName"),
            event.get("tournamentSlug"),
            event.get("categoryName"),
            event.get("league"),
            event.get("leagueName"),
            event.get("competition"),
            event.get("series"),
        )
        or None
    )


def _event_start_time(event: dict[str, Any]) -> datetime | None:
    return _parse_datetime(
        _first_raw(
            event.get("trueStartTime"),
            event.get("start_time"),
            event.get("startTime"),
            event.get("commence_time"),
            event.get("commenceTime"),
            event.get("starts_at"),
            event.get("start_at"),
            event.get("startDate"),
            event.get("startTimestamp"),
            event.get("date"),
        )
    )


def _market_type_text(market: dict[str, Any]) -> str:
    return _first_text(
        market.get("marketType"),
        market.get("market_type"),
        market.get("marketName"),
        market.get("market_name"),
        market.get("market"),
        market.get("type"),
        market.get("key"),
        market.get("name"),
        market.get("id"),
        market.get("marketId"),
        market.get("market_id"),
    )


def _is_match_winner_market(market: dict[str, Any], *, sport: str | None = None) -> bool:
    if _is_blocked_market(market, sport=sport):
        return False
    if _has_market_id(market, "161"):
        return True

    text = _market_label_text(market)
    compact = re.sub(r"[\s_-]+", "", text.casefold())
    markers = (
        "h2h",
        "headtohead",
        "matchwinner",
        "winner",
        "moneyline",
    )
    if _is_lol_sport(sport):
        markers = (*markers, "serieswinner")
    return any(marker in compact for marker in markers)


def _is_blocked_market(market: dict[str, Any], *, sport: str | None = None) -> bool:
    text = _market_label_text(market)
    if not text:
        return False
    if re.search(
        r"\b(map|maps|handicap|total|totals|over|under|prop|props|player|"
        r"kill|kills|round|rounds|spread)\b",
        text,
        flags=re.IGNORECASE,
    ):
        return True
    if _is_dota2_sport(sport) and _dota2_unsafe_market_reason(market):
        return True
    if _is_lol_sport(sport) and re.search(
        r"\b(draw|tie|threeway|three-way|3-way|3way)\b",
        text,
        flags=re.IGNORECASE,
    ):
        return True
    if _is_lol_sport(sport) and re.search(
        r"\b(outright|outrights|future|futures|tournament\s+winner|"
        r"championship\s+winner|event\s+winner|overall\s+winner|winner\s+of)\b|"
        r"\bwin\s+(?:the\s+)?(?:msi|mid[-\s]+season\s+invitational)\b",
        text,
        flags=re.IGNORECASE,
    ):
        return True
    return False


def _dota2_unsafe_market_reason(
    market: dict[str, Any],
    event: dict[str, Any] | None = None,
) -> str:
    text = _dota2_safety_text(market, event)
    if DOTA2_EWC_RE.search(text):
        return "dota2_ewc_or_draw_risk_tournament"
    if DOTA2_DRAW_OR_THREE_WAY_RE.search(text):
        return "dota2_draw_or_three_way"
    if DOTA2_BO2_RE.search(text):
        return "dota2_bo2_or_ambiguous_format"
    if DOTA2_MAP_OR_GAME_RE.search(text):
        return "dota2_map_or_game_winner"
    if DOTA2_PROP_RE.search(text):
        return "dota2_prop_market"
    if DOTA2_OUTRIGHT_RE.search(text):
        return "dota2_outright_market"
    return ""


def _dota2_safety_text(
    market: dict[str, Any],
    event: dict[str, Any] | None = None,
) -> str:
    parts = [_market_label_text(market)]
    if event:
        for key in (
            "name",
            "title",
            "slug",
            "tournament",
            "tournamentName",
            "tournamentSlug",
            "categoryName",
            "category",
            "league",
            "competition",
            "series",
        ):
            parts.append(_first_text(event.get(key)))
    return " ".join(part for part in parts if part)


def _is_dota2_sport(sport: str | None) -> bool:
    if sport is None:
        return False
    try:
        return resolve_sport_id(sport) == "16"
    except ValueError:
        return False


def _is_lol_sport(sport: str | None) -> bool:
    if sport is None:
        return False
    try:
        return resolve_sport_id(sport) == "18"
    except ValueError:
        return False


def _has_market_id(market: dict[str, Any], market_id: str) -> bool:
    return any(
        str(value).strip() == market_id
        for value in (
            market.get("id"),
            market.get("marketId"),
            market.get("market_id"),
            market.get("key"),
        )
        if value is not None
    )


def _market_label_text(market: dict[str, Any]) -> str:
    return " ".join(
        value
        for value in (
            _first_text(
                market.get("marketType"),
                market.get("market_type"),
                market.get("marketName"),
                market.get("market_name"),
                market.get("market"),
                market.get("type"),
                market.get("name"),
                market.get("key"),
            ),
        )
        if value
    )


def _validated_mapping_profile(mapping_profile: str | None) -> str | None:
    if not mapping_profile:
        return None

    profile = mapping_profile.strip()
    if profile not in SUPPORTED_MAPPING_PROFILES:
        raise ProviderConfigurationError(
            f"Unsupported OddsPapi mapping profile: {mapping_profile}"
        )
    return profile


def _profile_team_odds(
    market: dict[str, Any],
    event: dict[str, Any],
    *,
    bookmaker: str,
    sport: str | None,
    mapping_profile: str | None,
) -> tuple[float, float, str] | None:
    if bookmaker.casefold() != "stake":
        return None

    config = _profile_selection_order_config(mapping_profile)
    if not config:
        return None
    return _profile_selection_order_team_odds(
        market,
        event,
        sport=sport,
        expected_sport_id=str(config["expected_sport_id"]),
        market_id=str(config["market_id"]),
        participant1_selection_id=str(config["participant1_selection_id"]),
        participant2_selection_id=str(config["participant2_selection_id"]),
        mapping_source=str(config["mapping_source"]),
        require_market_active=bool(config.get("require_market_active")),
    )


def _profile_team_odds_rejection_reason(
    market: dict[str, Any],
    event: dict[str, Any],
    *,
    bookmaker: str,
    sport: str | None,
    mapping_profile: str | None,
) -> str:
    if bookmaker.casefold() != "stake":
        return "profile_bookmaker_mismatch"
    config = _profile_selection_order_config(mapping_profile)
    if not config:
        return ""
    return _profile_selection_order_rejection_reason(
        market,
        event,
        sport=sport,
        expected_sport_id=str(config["expected_sport_id"]),
        market_id=str(config["market_id"]),
        participant1_selection_id=str(config["participant1_selection_id"]),
        participant2_selection_id=str(config["participant2_selection_id"]),
        require_market_active=bool(config.get("require_market_active")),
        rejected_market_reasons=config.get("rejected_market_reasons"),
    )


def _mapping_profile_requires_exact_match(mapping_profile: str | None) -> bool:
    return mapping_profile in {
        MAPPING_PROFILE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER,
        MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER,
    }


def _profile_selection_order_config(
    mapping_profile: str | None,
) -> dict[str, object]:
    if mapping_profile == MAPPING_PROFILE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER:
        return {
            "expected_sport_id": "16",
            "market_id": "161",
            "participant1_selection_id": "161",
            "participant2_selection_id": "162",
            "mapping_source": MAPPING_SOURCE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER,
        }
    if mapping_profile == MAPPING_PROFILE_ODDSPAPI_STAKE_CS2_MARKET171_SELECTION_ORDER:
        return {
            "expected_sport_id": "17",
            "market_id": "171",
            "participant1_selection_id": "171",
            "participant2_selection_id": "172",
            "mapping_source": MAPPING_SOURCE_ODDSPAPI_STAKE_CS2_MARKET171_SELECTION_ORDER,
        }
    if mapping_profile == MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER:
        # MSI raw audits showed market 187 is a separate anonymous two-way
        # market. Stake's LoL match-winner UI prices matched market 181 with
        # selections 181/182 in fixture participant order.
        return {
            "expected_sport_id": "18",
            "market_id": "181",
            "participant1_selection_id": "181",
            "participant2_selection_id": "182",
            "mapping_source": MAPPING_SOURCE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER,
            "require_market_active": True,
            "rejected_market_reasons": {
                "187": "profile_lol_market187_not_match_winner"
            },
        }
    return {}


def _profile_selection_order_team_odds(
    market: dict[str, Any],
    event: dict[str, Any],
    *,
    sport: str | None,
    expected_sport_id: str,
    market_id: str,
    participant1_selection_id: str,
    participant2_selection_id: str,
    mapping_source: str,
    require_market_active: bool = False,
) -> tuple[float, float, str] | None:
    if _profile_sport_id(event, sport) != expected_sport_id:
        return None
    if not _market_id_or_key_is(market, market_id):
        return None
    if require_market_active and not _is_true_value(market.get("marketActive")):
        return None
    if not require_market_active and _is_false_value(market.get("marketActive")):
        return None
    if _is_true_value(market.get("suspended")):
        return None
    if expected_sport_id == "16" and _dota2_unsafe_market_reason(market, event):
        return None

    participant1_name = _first_text(event.get("participant1Name"))
    participant2_name = _first_text(event.get("participant2Name"))
    if not participant1_name or not participant2_name:
        return None

    prices = _profile_selection_prices(
        market,
        selection_ids={participant1_selection_id, participant2_selection_id},
    )
    if prices is None:
        return None

    return (
        prices[participant1_selection_id],
        prices[participant2_selection_id],
        mapping_source,
    )


def _profile_selection_order_rejection_reason(
    market: dict[str, Any],
    event: dict[str, Any],
    *,
    sport: str | None,
    expected_sport_id: str,
    market_id: str,
    participant1_selection_id: str,
    participant2_selection_id: str,
    require_market_active: bool = False,
    rejected_market_reasons: object = None,
) -> str:
    if _profile_sport_id(event, sport) != expected_sport_id:
        return "profile_sport_mismatch"
    if not _market_id_or_key_is(market, market_id):
        if isinstance(rejected_market_reasons, dict):
            for rejected_market_id, reason in rejected_market_reasons.items():
                if _market_id_or_key_is(market, str(rejected_market_id)):
                    return str(reason)
        return "profile_market_id_mismatch"
    if require_market_active and not _is_true_value(market.get("marketActive")):
        return "profile_market_inactive"
    if not require_market_active and _is_false_value(market.get("marketActive")):
        return "profile_market_inactive"
    if _is_true_value(market.get("suspended")):
        return "profile_market_suspended"
    if expected_sport_id == "16":
        reason = _dota2_unsafe_market_reason(market, event)
        if reason:
            return f"profile_{reason}"
    if not _first_text(event.get("participant1Name")) or not _first_text(
        event.get("participant2Name")
    ):
        return "profile_missing_participants"

    raw_outcomes = _raw_outcome_records(market)
    if len(raw_outcomes) != 2:
        return "profile_outcome_count_not_2"

    selection_ids = {participant1_selection_id, participant2_selection_id}
    seen_selection_ids: set[str] = set()
    for outcome in raw_outcomes:
        selection_id = _profile_selection_id(outcome, selection_ids=selection_ids)
        if selection_id not in selection_ids or selection_id in seen_selection_ids:
            return "profile_outcome_mapping_ambiguous"
        if _profile_outcome_price(outcome) is None:
            return "profile_outcome_price_missing"
        seen_selection_ids.add(selection_id)

    if seen_selection_ids != selection_ids:
        return "profile_outcome_mapping_ambiguous"
    return ""


def _profile_sport_id(event: dict[str, Any], sport: str | None) -> str:
    if sport:
        return resolve_sport_id(sport)
    raw_sport = (
        _first_raw(
            event.get("sportId"),
            event.get("sport_id"),
            event.get("sportKey"),
            event.get("sport_key"),
            event.get("sport"),
        )
        or ""
    )
    try:
        return resolve_sport_id(raw_sport)
    except ValueError:
        return ""


def _market_id_or_key_is(market: dict[str, Any], expected: str) -> bool:
    return any(
        _normalized_id(value) == expected
        for value in (
            market.get("marketId"),
            market.get("market_id"),
            market.get("key"),
        )
    )


def _profile_market_161_selection_prices(
    market: dict[str, Any],
) -> dict[str, float] | None:
    return _profile_selection_prices(market, selection_ids={"161", "162"})


def _profile_selection_prices(
    market: dict[str, Any],
    *,
    selection_ids: set[str],
) -> dict[str, float] | None:
    raw_outcomes = _raw_outcome_records(market)
    if len(raw_outcomes) != 2:
        return None

    prices: dict[str, float] = {}
    for outcome in raw_outcomes:
        selection_id = _profile_selection_id(outcome, selection_ids=selection_ids)
        if selection_id not in selection_ids or selection_id in prices:
            return None

        price = _profile_outcome_price(outcome)
        if price is None:
            return None
        prices[selection_id] = price

    if set(prices) != selection_ids:
        return None
    return prices


def _profile_selection_id(
    outcome: dict[str, Any],
    *,
    selection_ids: set[str],
) -> str:
    matches = {
        _normalized_id(value)
        for value in (
            outcome.get("outcome_key"),
            outcome.get("selectionId"),
            outcome.get("selection_id"),
        )
        if _normalized_id(value) in selection_ids
    }
    if len(matches) != 1:
        return ""
    return next(iter(matches))


def _profile_outcome_price(outcome: dict[str, Any]) -> float | None:
    price = _outcome_price(outcome)
    if price is not None:
        return price

    prices = [
        player_price
        for player in _player_records(outcome.get("players"))
        if (player_price := _outcome_price(player)) is not None
    ]
    if len(prices) != 1:
        return None
    return prices[0]


def _is_false_value(value: object) -> bool:
    if value is False:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 0
    return str(value).strip().casefold() in {"false", "0", "no"}


def _is_true_value(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    return str(value).strip().casefold() in {"true", "1", "yes"}


def _team_odds(
    market: dict[str, Any],
    event: dict[str, Any],
    team_a: str,
    team_b: str,
) -> tuple[float | None, float | None]:
    outcomes = _outcome_records(market)
    if outcomes:
        if len(outcomes) != 2:
            return None, None

        return _outcome_team_prices(outcomes, event, team_a, team_b)

    odds_a = _first_number(
        market.get("team_a_odds"),
        market.get("odds_a"),
        market.get("home_odds"),
        market.get("team1_odds"),
        market.get("home_price"),
    )
    odds_b = _first_number(
        market.get("team_b_odds"),
        market.get("odds_b"),
        market.get("away_odds"),
        market.get("team2_odds"),
        market.get("away_price"),
    )

    return odds_a, odds_b


def _raw_outcome_records(market: dict[str, Any]) -> list[dict[str, Any]]:
    value = _first_present(market, *OUTCOME_CONTAINER_KEYS)
    if isinstance(value, list):
        return _dicts(value)
    if not isinstance(value, dict):
        return []

    outcomes: list[dict[str, Any]] = []
    for key, item in value.items():
        if isinstance(item, dict):
            outcome = dict(item)
        else:
            outcome = {"value": item}
        outcome["outcome_key"] = str(key)
        outcome.setdefault("selectionId", str(key))
        outcomes.append(outcome)
    return outcomes


def _outcome_records(market: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for outcome in _raw_outcome_records(market):
        players = _player_records(outcome.get("players"))
        if players:
            for player in players:
                candidates.append(_outcome_candidate_from_player(player, outcome))
        else:
            candidates.append(outcome)
    return candidates


def _player_records(value: object) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return _dicts(value)
    if not isinstance(value, dict):
        return []

    if _looks_like_player_record(value):
        return [value]

    players: list[dict[str, Any]] = []
    for key, item in value.items():
        if isinstance(item, dict):
            player = dict(item)
        else:
            player = {"value": item}
        player["player_key"] = str(key)
        players.append(player)
    return players


def _looks_like_player_record(value: dict[str, Any]) -> bool:
    return bool(
        _first_present(
            value,
            "participantId",
            "participant_id",
            "playerId",
            "player_id",
            "selectionId",
            "selection_id",
            "id",
            "participantName",
            "playerName",
            "name",
            "label",
            "decimalOdds",
            "decimal_odds",
            "odds",
            "price",
            "value",
        )
    )


def _outcome_candidate_from_player(
    player: dict[str, Any],
    outcome: dict[str, Any],
) -> dict[str, Any]:
    candidate = dict(player)
    if "outcome_key" in outcome:
        candidate.setdefault("outcome_key", outcome["outcome_key"])
    for key in (
        "outcome_key",
        "outcome_path",
        "selectionId",
        "selection_id",
        "participantId",
        "participant_id",
        "playerId",
        "player_id",
        "id",
        "bookmakerOutcomeId",
        "bookmaker_outcome_id",
    ):
        if key in outcome:
            candidate[f"outer_{key}"] = outcome[key]

    for key in ("name", "label", "playerName", "participantName"):
        if key in outcome:
            candidate[f"outcome_{key}"] = outcome[key]

    outer_id = _first_text(*_outcome_id_values(outcome))
    candidate_id = _candidate_id(candidate)
    if outer_id and not candidate_id:
        candidate["selectionId"] = outer_id

    if _outcome_price(candidate) is None:
        outer_price = _first_raw(
            outcome.get("decimalOdds"),
            outcome.get("decimal_odds"),
            outcome.get("odds"),
            outcome.get("price"),
            outcome.get("value"),
        )
        if outer_price is not None:
            candidate["decimalOdds"] = outer_price

    return candidate


def _players_debug(value: object) -> dict[str, object]:
    debug: dict[str, object] = {}
    if isinstance(value, list):
        debug["first_outcome_players_count"] = len(value)
        debug["first_nested_player_keys"] = _first_item_keys(value)
        return debug

    if isinstance(value, dict):
        debug["first_outcome_players_keys"] = list(value.keys())
        players = _player_records(value)
        debug["first_outcome_players_count"] = len(players)
        if players:
            debug["first_nested_player_keys"] = list(players[0].keys())
        return debug

    return debug


def _outcome_candidate_debug_reasons(
    candidates: list[dict[str, Any]],
    event: dict[str, Any],
) -> list[str]:
    team_a, team_b = _event_teams(event)
    reasons: list[str] = []
    for candidate in candidates[:6]:
        if _outcome_price(candidate) is None:
            reasons.append("missing_price")
            continue

        team_key = _outcome_team_key(candidate, event, team_a, team_b)
        reasons.append(_candidate_mapping_reason(team_key))
    return reasons


def _outcome_candidate_debug_samples(
    candidates: list[dict[str, Any]],
    event: dict[str, Any],
) -> list[dict[str, str]]:
    team_a, team_b = _event_teams(event)
    samples: list[dict[str, str]] = []
    for candidate in candidates[:4]:
        team_key = _outcome_team_key(candidate, event, team_a, team_b)
        samples.append(
            {
                "name": _debug_text(_candidate_name(candidate), secret=None),
                "ids": _debug_text(",".join(_candidate_id_values(candidate)), secret=None),
                "outcome_key": _debug_text(candidate.get("outcome_key"), secret=None),
                "player_key": _debug_text(candidate.get("player_key"), secret=None),
                "bookmakerOutcomeId": _debug_text(
                    candidate.get("bookmakerOutcomeId"),
                    secret=None,
                ),
                "participantId": _debug_text(candidate.get("participantId"), secret=None),
                "selectionId": _debug_text(candidate.get("selectionId"), secret=None),
                "price": _debug_text(_outcome_price(candidate), secret=None),
                "mapped_team": _debug_text(team_key or "", secret=None),
                "reason": _candidate_mapping_reason(team_key),
                "exchangeMeta": _debug_text(
                    _exchange_meta_debug(candidate.get("exchangeMeta")),
                    secret=None,
                ),
            }
        )
    return samples


def _candidate_mapping_reason(team_key: str | None) -> str:
    if team_key == "a":
        return "team_a"
    if team_key == "b":
        return "team_b"
    if team_key == "ambiguous":
        return "ambiguous"
    return "team_unmatched"


def _outcome_team_prices(
    outcomes: list[dict[str, Any]],
    event: dict[str, Any],
    team_a: str,
    team_b: str,
) -> tuple[float | None, float | None]:
    odds_a: float | None = None
    odds_b: float | None = None
    matched_count = 0
    priced_count = 0

    for outcome in outcomes:
        price = _outcome_price(outcome)
        if price is None:
            continue
        priced_count += 1

        team_key = _outcome_team_key(outcome, event, team_a, team_b)
        if team_key == "a":
            odds_a = price
            matched_count += 1
        elif team_key == "b":
            odds_b = price
            matched_count += 1

    if priced_count != 2 or matched_count != 2 or odds_a is None or odds_b is None:
        return None, None
    return odds_a, odds_b


def _outcome_team_key(
    outcome: dict[str, Any],
    event: dict[str, Any],
    team_a: str,
    team_b: str,
) -> str | None:
    id_match = _outcome_team_key_by_id(outcome, event)
    if id_match:
        return id_match

    name_match = _outcome_team_key_by_name(outcome, event, team_a, team_b)
    if name_match:
        return name_match

    return None


def _outcome_team_key_by_id(
    outcome: dict[str, Any],
    event: dict[str, Any],
) -> str | None:
    candidate_ids = set(_candidate_id_values(outcome))
    participant_a, participant_b = _event_participant_ids(event)
    matches: set[str] = set()
    if candidate_ids and participant_a and participant_a in candidate_ids:
        matches.add("a")
    if candidate_ids and participant_b and participant_b in candidate_ids:
        matches.add("b")
    return _single_match(matches)


def _outcome_team_key_by_name(
    outcome: dict[str, Any],
    event: dict[str, Any],
    team_a: str,
    team_b: str,
) -> str | None:
    candidate_names = [_normalize_team_name(name) for name in _candidate_name_values(outcome)]
    candidate_names = [name for name in candidate_names if name]
    if not candidate_names:
        return None

    team_a_names = _fixture_team_names(event, team_a, side="a")
    team_b_names = _fixture_team_names(event, team_b, side="b")

    exact_matches = _candidate_name_matches(candidate_names, team_a_names, exact=True)
    exact_matches.update(_candidate_name_matches(candidate_names, team_b_names, exact=True))
    exact_match = _single_match(exact_matches)
    if exact_match:
        return exact_match

    contains_matches = _candidate_name_matches(candidate_names, team_a_names, exact=False)
    contains_matches.update(
        _candidate_name_matches(candidate_names, team_b_names, exact=False)
    )
    return _single_match(contains_matches)


def _candidate_name_matches(
    candidate_names: list[str],
    team_names: dict[str, list[str]],
    *,
    exact: bool,
) -> set[str]:
    matches: set[str] = set()
    for team_key, names in team_names.items():
        for candidate_name in candidate_names:
            for team_name in names:
                if exact and candidate_name == team_name:
                    matches.add(team_key)
                elif not exact and _safe_contains_match(candidate_name, team_name):
                    matches.add(team_key)
    return matches


def _safe_contains_match(left: str, right: str) -> bool:
    if not left or not right or left == right:
        return False
    shorter = min(len(left), len(right))
    return shorter >= 3 and (left in right or right in left)


def _single_match(matches: set[str]) -> str | None:
    if len(matches) == 1:
        return next(iter(matches))
    if len(matches) > 1:
        return "ambiguous"
    return None


def _candidate_id(candidate: dict[str, Any]) -> str:
    return _first_text(*_candidate_id_values(candidate))


def _candidate_id_values(candidate: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for value in (
        candidate.get("participantId"),
        candidate.get("participant_id"),
        candidate.get("playerId"),
        candidate.get("player_id"),
        candidate.get("selectionId"),
        candidate.get("selection_id"),
        candidate.get("id"),
        candidate.get("bookmakerOutcomeId"),
        candidate.get("bookmaker_outcome_id"),
        candidate.get("outer_selectionId"),
        candidate.get("outer_selection_id"),
        candidate.get("outer_participantId"),
        candidate.get("outer_participant_id"),
        candidate.get("outer_playerId"),
        candidate.get("outer_player_id"),
        candidate.get("outer_id"),
        candidate.get("outer_bookmakerOutcomeId"),
        candidate.get("outer_bookmaker_outcome_id"),
        candidate.get("outcome_key"),
        candidate.get("outer_outcome_key"),
        candidate.get("player_key"),
    ):
        text = _normalized_id(value)
        if text and text not in ids:
            ids.append(text)
    return ids


def _candidate_name(candidate: dict[str, Any]) -> str:
    return _first_text(*_candidate_name_values(candidate))


def _candidate_name_values(candidate: dict[str, Any]) -> list[object]:
    values = [
        candidate.get("playerName"),
        candidate.get("participantName"),
        candidate.get("name"),
        candidate.get("label"),
        candidate.get("outcome_playerName"),
        candidate.get("outcome_participantName"),
        candidate.get("outcome_name"),
        candidate.get("outcome_label"),
        candidate.get("team"),
        candidate.get("participant"),
    ]
    values.extend(_exchange_meta_string_values(candidate.get("exchangeMeta")))
    return values


def _outcome_id_values(outcome: dict[str, Any]) -> list[object]:
    return [
        outcome.get("selectionId"),
        outcome.get("selection_id"),
        outcome.get("participantId"),
        outcome.get("participant_id"),
        outcome.get("playerId"),
        outcome.get("player_id"),
        outcome.get("id"),
        outcome.get("bookmakerOutcomeId"),
        outcome.get("bookmaker_outcome_id"),
        outcome.get("outcome_key"),
    ]


def _exchange_meta_string_values(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []

    values: list[str] = []
    for item in value.values():
        if isinstance(item, (str, int, float, bool)):
            text = _debug_text(item, secret=None)
            if text:
                values.append(text)
    return values[:8]


def _exchange_meta_debug(value: object) -> str:
    if not isinstance(value, dict):
        return ""

    parts: list[str] = []
    for key, item in value.items():
        if isinstance(item, (str, int, float, bool)):
            text = _debug_text(item, secret=None, limit=40)
            if text:
                parts.append(f"{key}={text}")
    return ";".join(parts[:6])


def _event_participant_ids(event: dict[str, Any]) -> tuple[str, str]:
    return (
        _normalized_id(
            _first_raw(
                event.get("participant1Id"),
                event.get("participant1_id"),
                event.get("team1Id"),
                event.get("team1_id"),
                event.get("homeTeamId"),
                event.get("home_team_id"),
            )
        ),
        _normalized_id(
            _first_raw(
                event.get("participant2Id"),
                event.get("participant2_id"),
                event.get("team2Id"),
                event.get("team2_id"),
                event.get("awayTeamId"),
                event.get("away_team_id"),
            )
        ),
    )


def _fixture_team_names(
    event: dict[str, Any],
    fallback: str,
    *,
    side: str,
) -> dict[str, list[str]]:
    if side == "a":
        values = [
            event.get("participant1Name"),
            event.get("participant1ShortName"),
            event.get("participant1Abbr"),
            fallback,
        ]
    else:
        values = [
            event.get("participant2Name"),
            event.get("participant2ShortName"),
            event.get("participant2Abbr"),
            fallback,
        ]

    names: list[str] = []
    for value in values:
        text = _normalize_team_name(_text(value))
        if text and text not in names:
            names.append(text)
    return {side: names}


def _normalized_id(value: object) -> str:
    text = _text(value)
    if not text:
        return ""
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".", maxsplit=1)[0]
    if text.isdigit():
        return str(int(text))
    return text.casefold()


def _normalize_team_name(value: object) -> str:
    text = _text(value).strip().casefold()
    if not text:
        return ""
    text = re.sub(r"[-_.']", " ", text)
    text = re.sub(r"[^\w\s]", "", text)
    return " ".join(text.split())


def _outcome_price(outcome: dict[str, Any]) -> float | None:
    return _first_number(
        outcome.get("decimalOdds"),
        outcome.get("decimal_odds"),
        outcome.get("odds"),
        outcome.get("price"),
        outcome.get("value"),
    )


def _normalized_market_type(value: str) -> str:
    key = value.strip().casefold().replace("-", "_").replace(" ", "_")
    if key in {
        "match_winner",
        "matchwinner",
        "winner",
        "moneyline",
        "money_line",
    }:
        return "match_winner"
    if key in {"head_to_head", "headtohead", "head2head"}:
        return "h2h"
    return key or "h2h"


def _market_matches(requested: str, actual: str) -> bool:
    requested_key = _normalized_market_type(requested)
    if requested_key == actual:
        return True

    equivalent_winner_markets = {"h2h", "match_winner"}
    return requested_key in equivalent_winner_markets and actual in equivalent_winner_markets


def _dicts(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _first_present(data: dict[str, Any], *keys: str) -> object:
    for key in keys:
        if key in data:
            return data[key]
    return None


def _first_raw(*values: object) -> object:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _first_text(*values: object) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return _first_text(
            value.get("name"),
            value.get("title"),
            value.get("key"),
            value.get("slug"),
            value.get("id"),
        )
    return str(value).strip()


def _first_list(*values: object) -> list[object]:
    for value in values:
        if isinstance(value, list):
            return value
    return []


def _first_number(*values: object) -> float | None:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)

    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    return datetime.fromisoformat(text)
