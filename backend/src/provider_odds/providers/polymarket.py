from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from provider_odds.models import ProviderOddsEvent
from provider_odds.providers.base import (
    OddsProvider,
    ProviderConfigurationError,
    ProviderRequestError,
)

SPORT_ALIASES = {
    "dota2": "dota2",
    "dota 2": "dota2",
    "dota-2": "dota2",
    "cs2": "cs2",
    "counter strike": "cs2",
    "counter-strike": "cs2",
    "league of legends": "league_of_legends",
    "league-of-legends": "league_of_legends",
    "league_of_legends": "league_of_legends",
    "lol": "league_of_legends",
}
SPORT_SEARCH_TERMS = {
    "dota2": "Dota 2",
    "cs2": "Counter-Strike",
    "league_of_legends": "League of Legends",
}
SPORT_TEXT_ALIASES = {
    "dota2": ("dota 2", "dota2", "dota-2"),
    "cs2": ("cs2", "counter strike", "counter-strike"),
    "league_of_legends": (
        "league of legends",
        "league-of-legends",
        "league_of_legends",
        "lol",
    ),
}
WRAPPER_KEYS = ("data", "events", "markets", "results", "records")
MARKET_KEYS = ("markets", "eventMarkets")
EVENT_CONTEXT_KEYS = ("event", "events")
OUTCOME_KEYS = ("outcomes", "outcomeNames", "shortOutcomes")
PRICE_KEYS = ("outcomePrices", "prices")
ASK_PRICE_KEYS = ("bestAsk", "ask", "askPrice", "bestAskPrice", "best_ask")
INDEXED_ASK_PRICE_KEYS = (
    "bestAsks",
    "asks",
    "askPrices",
    "bestAskPrices",
    "best_asks",
)
TOKEN_KEYS = ("tokens", "clobTokens")
SECRET_QUERY_KEYS = {"apikey", "api_key", "token", "key", "secret"}
GENERIC_OUTCOMES = {
    "yes",
    "no",
    "over",
    "under",
    "other",
    "draw",
    "tie",
    "none",
}
MAP_OR_GAME_RE = re.compile(
    r"\b(?:map|game)\s*[1-9]\b|"
    r"\b(?:first|second|third|1st|2nd|3rd)\s+(?:map|game)\b|"
    r"\b(?:map|game)\s+winner\b",
    re.IGNORECASE,
)
PROP_RE = re.compile(
    r"\b("
    r"kill|kills|handicap|spread|total|over/under|round|rounds|score|"
    r"correct score|exact score|first blood|duration|pistol|race to|"
    r"player|roshan|baron|dragon|tower"
    r")\b",
    re.IGNORECASE,
)
OUTRIGHT_RE = re.compile(
    r"\b("
    r"outright|outrights|future|futures|tournament\s+winner|"
    r"championship\s+winner|event\s+winner|overall\s+winner|winner\s+of"
    r")\b|"
    r"\bwin\s+(?:the\s+)?(?:msi|mid[-\s]+season\s+invitational)\b",
    re.IGNORECASE,
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
DOTA2_SAFE_BEST_OF_RE = re.compile(
    r"\b(?P<short>b0?(?P<short_maps>[35])|bo[-\s]?(?P<bo_maps>[35]))\b|"
    r"\bbest[-\s]+of[-\s]+(?P<word_maps>3|5|three|five)\b",
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
    r"kill|kills|handicap|spread|total|totals|over/under|over\s+under|"
    r"round|rounds|score|correct score|exact score|first blood|duration|"
    r"player|hero|roshan|tower|towers"
    r")\b",
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
UNSUPPORTED_SPORT_RE = re.compile(
    r"\b("
    r"nba|nfl|mlb|nhl|wnba|soccer|football|basketball|baseball|tennis|"
    r"ufc|mma|boxing|golf|cricket|rugby|formula\s*1|f1"
    r")\b",
    re.IGNORECASE,
)
MATCHUP_RE = re.compile(r"\b(?:vs\.?|v\.?|versus)\b", re.IGNORECASE)
MATCH_WINNER_RE = re.compile(
    r"\b(match\s+winner|winner|moneyline|money\s+line|head[- ]?to[- ]?head|h2h)\b",
    re.IGNORECASE,
)


class PolymarketProvider(OddsProvider):
    name = "polymarket"

    def __init__(
        self,
        base_url: str = "https://gamma-api.polymarket.com",
        timeout_seconds: int = 20,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.last_note: str | None = None
        self.last_debug: dict[str, Any] = {}

    @classmethod
    def from_env(cls, env_path: object = None) -> "PolymarketProvider":
        return cls()

    def fetch_events(
        self,
        *,
        sport: str,
        market_type: str = "h2h",
        search: str | None = None,
        event_slug: str | None = None,
        market_slug: str | None = None,
        max_events: int = 20,
        bookmaker: str | None = None,
    ) -> list[ProviderOddsEvent]:
        resolved_sport = _resolve_sport(sport)
        if not resolved_sport:
            raise ProviderConfigurationError(
                f"Unsupported Polymarket sport alias: {sport}"
            )
        event_limit = _event_limit(max_events)
        clean_event_slug = _text(event_slug)
        clean_market_slug = _text(market_slug)

        if clean_market_slug:
            request_mode = "market_slug"
            url = self._market_slug_url(clean_market_slug)
            source = "gamma_query_slug"
        elif clean_event_slug:
            request_mode = "event_slug"
            url = self._event_slug_url(clean_event_slug)
            source = "gamma_query_slug"
        else:
            request_mode = "gamma_events_search"
            query = _discovery_query(resolved_sport, search)
            url = self._events_url(query=query, max_events=event_limit)
            source = "gamma_query_search"

        request_debug = {
            "request_mode": request_mode,
            "request_url": _sanitize_url(url),
            "resolved_sport": resolved_sport,
            "search": search or "",
            "event_slug": clean_event_slug,
            "market_slug": clean_market_slug,
            "max_events": event_limit,
            "bookmaker": bookmaker or "polymarket",
        }

        payload = self._get_json(url)
        events = self.normalize_response(
            payload,
            sport=resolved_sport,
            market_type=market_type,
            max_events=event_limit,
            source=source,
        )
        self.last_debug = {**request_debug, **self.last_debug}
        return events

    def normalize_response(
        self,
        payload: object,
        *,
        sport: str | None = None,
        market_type: str = "h2h",
        max_events: int = 100,
        bookmaker: str | None = None,
        source: str = "gamma_normalize",
    ) -> list[ProviderOddsEvent]:
        requested_sport = _resolve_sport(sport) if sport else None
        event_limit = _event_limit(max_events)
        pairs = list(_event_market_pairs(payload))
        events: list[ProviderOddsEvent] = []
        reject_counts: Counter[str] = Counter()
        rejected_samples: list[dict[str, str]] = []

        for event, market in pairs:
            normalized, reason = _normalize_market(
                event,
                market,
                requested_sport=requested_sport,
                requested_market_type=market_type,
                source=source,
            )
            if normalized is None:
                reject_counts[reason] += 1
                if len(rejected_samples) < 5:
                    rejected_samples.append(_rejected_sample(event, market, reason))
                continue

            if len(events) >= event_limit:
                reject_counts["max_events_exceeded"] += 1
                continue
            events.append(normalized)

        self.last_note = "" if events else "No matching Polymarket esports h2h markets"
        self.last_debug = {
            "resolved_sport": requested_sport or "",
            "bookmaker": bookmaker or "polymarket",
            "raw_root_type": type(payload).__name__,
            "fetched_count": len(pairs),
            "normalized_count": len(events),
            "normalized_events_count": len(events),
            "reject_reason_counts": dict(sorted(reject_counts.items())),
            "normalized_samples": [_normalized_sample(event) for event in events[:5]],
            "rejected_samples": rejected_samples,
            "last_note": self.last_note,
        }
        return events

    def _events_url(self, *, query: str, max_events: int) -> str:
        params = {
            "active": "true",
            "closed": "false",
            "limit": str(max_events),
            "search": query,
        }
        return f"{self.base_url}/events?{urlencode(params)}"

    def _event_slug_url(self, slug: str) -> str:
        return f"{self.base_url}/events?{urlencode({'slug': slug})}"

    def _market_slug_url(self, slug: str) -> str:
        return f"{self.base_url}/markets?{urlencode({'slug': slug})}"

    def _get_json(self, url: str) -> object:
        request = Request(url, headers={"User-Agent": "provider-odds-probe/0.1"})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body_preview = _response_body_preview(exc)
            raise ProviderRequestError(
                f"Polymarket Gamma returned HTTP {exc.code} for "
                f"{_sanitize_url(url)}: {body_preview}"
            ) from exc
        except URLError as exc:
            raise ProviderRequestError(f"Polymarket Gamma request failed: {exc}") from exc


def _normalize_market(
    event: dict[str, Any],
    market: dict[str, Any],
    *,
    requested_sport: str | None,
    requested_market_type: str,
    source: str,
) -> tuple[ProviderOddsEvent | None, str]:
    if not _market_type_matches(requested_market_type):
        return None, "unsupported_market_type"

    open_reason = _open_rejection_reason(event, market)
    if open_reason:
        return None, open_reason

    sport = _record_sport(event, market)
    if requested_sport and sport and sport != requested_sport:
        return None, "sport_mismatch"
    if not sport and _has_unsupported_sport_context(event, market):
        return None, "unsupported_sport"
    game = sport or requested_sport
    if not game:
        return None, "unsupported_sport"

    market_text = _market_classification_text(market)
    event_text = _event_classification_text(event)
    dota2_safety_reason = _dota2_safety_rejection_reason(
        game=game,
        event=event,
        market=market,
        market_text=market_text,
        event_text=event_text,
    )
    if dota2_safety_reason:
        return None, dota2_safety_reason
    if MAP_OR_GAME_RE.search(market_text):
        return None, "map_or_game_winner"
    if PROP_RE.search(market_text):
        return None, "prop_market"
    if game == "league_of_legends" and OUTRIGHT_RE.search(
        f"{market_text} {event_text}"
    ):
        return None, "outright_market"

    outcomes = _outcome_names(market)
    if len(outcomes) != 2:
        return None, "outcome_count"
    if not outcomes[0] or not outcomes[1]:
        return None, "missing_team_name"
    if any(_is_generic_outcome(outcome) for outcome in outcomes):
        return None, "generic_outcome"
    if not all(_is_team_like_outcome(outcome) for outcome in outcomes):
        return None, "non_team_outcome"

    reference_prices = _outcome_prices(market)
    if len(reference_prices) != 2:
        return None, "invalid_price"
    price_details = _selected_price_details(
        market,
        outcomes=outcomes,
        reference_prices=reference_prices,
    )
    selected_prices = [detail["price"] for detail in price_details]
    odds = [_decimal_odds(price) for price in selected_prices]
    if odds[0] is None or odds[1] is None:
        return None, "invalid_price"

    match_winner_source = _match_winner_source(
        market_text=market_text,
        event_text=event_text,
        outcomes=outcomes,
    )
    if not match_winner_source:
        return None, "not_match_winner"

    return (
        ProviderOddsEvent(
            provider="polymarket",
            bookmaker="polymarket",
            game=game,
            tournament=_tournament(event, market),
            start_time=_start_time(event, market),
            team_a=outcomes[0],
            team_b=outcomes[1],
            market_type="h2h",
            odds_a=odds[0],
            odds_b=odds[1],
            event_url=_event_url(event, market),
            raw=_raw_snapshot(
                event,
                market,
                outcomes,
                reference_prices,
                selected_prices,
                price_details,
                game,
                source,
                match_winner_source,
            ),
        ),
        "",
    )


def _resolve_sport(value: str | None) -> str:
    text = _alias_key(value)
    return SPORT_ALIASES.get(text, "")


def _alias_key(value: object) -> str:
    return re.sub(r"[\s_-]+", " ", _text(value).casefold())


def _event_limit(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 20
    return min(max(parsed, 1), 100)


def _discovery_query(sport: str, search: str | None) -> str:
    sport_term = SPORT_SEARCH_TERMS.get(sport, sport)
    search_text = _text(search)
    if search_text:
        return f"{sport_term} {search_text}"
    return sport_term


def _sanitize_url(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.query:
        return url
    query = [
        (key, "***" if key.casefold() in SECRET_QUERY_KEYS else value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query, safe=",*"),
            parsed.fragment,
        )
    )


def _response_body_preview(exc: HTTPError, limit: int = 500) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
    sanitized = re.sub(
        r"(?i)(apiKey|api_key|token|key|secret)=([^&\"'\s]+)",
        r"\1=***",
        body,
    )
    return sanitized[:limit]


def _event_market_pairs(payload: object) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for record in _records(payload):
        if _looks_like_market(record):
            pairs.append((_market_event_context(record), record))
            continue

        markets = _market_records(record)
        for market in markets:
            pairs.append((record, market))
    return pairs


def _records(value: object) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []

    if _looks_like_market(value) or _market_records(value):
        return [value]

    records: list[dict[str, Any]] = []
    for key in WRAPPER_KEYS:
        if key not in value:
            continue
        child = value[key]
        if isinstance(child, list):
            records.extend(item for item in child if isinstance(item, dict))
        elif isinstance(child, dict):
            records.extend(_records(child))
    return records


def _market_records(event: dict[str, Any]) -> list[dict[str, Any]]:
    for key in MARKET_KEYS:
        value = event.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [
                _dict_with_default(item, "market_key", market_key)
                for market_key, item in value.items()
                if isinstance(item, dict)
            ]
    return []


def _market_event_context(market: dict[str, Any]) -> dict[str, Any]:
    for key in EVENT_CONTEXT_KEYS:
        value = market.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    return item
        elif isinstance(value, dict):
            return value
    return {}


def _looks_like_market(record: dict[str, Any]) -> bool:
    return bool(
        _first_present(record, "conditionId", "question", "outcomePrices")
        and _first_present(record, *PRICE_KEYS)
    )


def _open_rejection_reason(event: dict[str, Any], market: dict[str, Any]) -> str:
    if _is_closed(event) or _is_closed(market):
        return "closed"
    if _is_inactive(event) or _is_inactive(market):
        return "inactive"
    return ""


def _is_closed(record: dict[str, Any]) -> bool:
    for key in ("closed", "resolved", "archived", "expired"):
        if _is_true_value(record.get(key)):
            return True
    return False


def _is_inactive(record: dict[str, Any]) -> bool:
    for key in ("active", "isActive", "open", "isOpen"):
        if key in record and _is_false_value(record.get(key)):
            return True
    return False


def _record_sport(event: dict[str, Any], market: dict[str, Any]) -> str:
    text = _normalized_blob(_sport_text_parts(event) + _sport_text_parts(market))
    for sport, aliases in SPORT_TEXT_ALIASES.items():
        for alias in aliases:
            if _alias_in_text(alias, text):
                return sport
    return ""


def _has_unsupported_sport_context(
    event: dict[str, Any],
    market: dict[str, Any],
) -> bool:
    text = _normalized_blob(_sport_text_parts(event) + _sport_text_parts(market))
    return bool(UNSUPPORTED_SPORT_RE.search(text))


def _sport_text_parts(record: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    for key in (
        "sport",
        "sportSlug",
        "sportName",
        "game",
        "category",
        "categorySlug",
        "series",
        "tags",
        "title",
        "question",
        "slug",
        "description",
    ):
        parts.extend(_text_values(record.get(key)))
    return parts


def _alias_in_text(alias: str, text: str) -> bool:
    normalized_alias = _normalized_blob([alias])
    if normalized_alias == "lol":
        return bool(re.search(r"\blol\b", text))
    return normalized_alias in text


def _outcome_names(market: dict[str, Any]) -> list[str]:
    values = _first_present(market, *OUTCOME_KEYS)
    names = []
    for item in _sequence(values):
        if isinstance(item, dict):
            names.append(
                _first_text(
                    item.get("name"),
                    item.get("title"),
                    item.get("label"),
                    item.get("value"),
                )
            )
        else:
            names.append(_text(item))
    return names


def _outcome_prices(market: dict[str, Any]) -> list[float | None]:
    values = _first_present(market, *PRICE_KEYS)
    prices: list[float | None] = []
    for item in _sequence(values):
        if isinstance(item, dict):
            prices.append(
                _first_number(
                    item.get("price"),
                    item.get("value"),
                    item.get("outcomePrice"),
                )
            )
        else:
            prices.append(_first_number(item))
    return prices


def _outcome_price_source(market: dict[str, Any]) -> str:
    for key in PRICE_KEYS:
        if key in market:
            return key
    return ""


def _selected_price_details(
    market: dict[str, Any],
    *,
    outcomes: list[str],
    reference_prices: list[float | None],
) -> list[dict[str, Any]]:
    reference_source = _outcome_price_source(market) or "outcomePrices"
    details: list[dict[str, Any]] = []
    for index, reference_price in enumerate(reference_prices):
        executable_price, executable_source = _executable_buy_price(
            market,
            side_index=index,
            outcome=outcomes[index] if index < len(outcomes) else "",
        )
        if executable_price is not None:
            details.append(
                {
                    "price": executable_price,
                    "price_source": executable_source,
                    "executable_buy_price": executable_price,
                    "executable_price_source": executable_source,
                    "reference_price": reference_price,
                    "reference_price_source": reference_source,
                    "actionable": True,
                    "non_actionable_reason": "",
                }
            )
            continue

        details.append(
            {
                "price": reference_price,
                "price_source": reference_source,
                "executable_buy_price": None,
                "executable_price_source": "",
                "reference_price": reference_price,
                "reference_price_source": reference_source,
                "actionable": False,
                "non_actionable_reason": "missing_executable_buy_price",
            }
        )
    return details


def _executable_buy_price(
    market: dict[str, Any],
    *,
    side_index: int,
    outcome: str,
) -> tuple[float | None, str]:
    for key in TOKEN_KEYS:
        price, source = _token_ask_price(
            market.get(key),
            side_index=side_index,
            outcome=outcome,
        )
        if price is not None:
            return price, source

    for key in INDEXED_ASK_PRICE_KEYS:
        if key not in market:
            continue
        price = _indexed_price(market.get(key), side_index=side_index, outcome=outcome)
        if price is not None:
            return price, key

    for key in ASK_PRICE_KEYS:
        if key not in market:
            continue
        price = _scalar_ask_price(market.get(key), side_index=side_index)
        if price is not None:
            return price, key

    return None, ""


def _token_ask_price(
    value: object,
    *,
    side_index: int,
    outcome: str,
) -> tuple[float | None, str]:
    tokens = [item for item in _sequence(value) if isinstance(item, dict)]
    if not tokens:
        return None, ""

    candidates: list[dict[str, Any]] = []
    if 0 <= side_index < len(tokens):
        candidates.append(tokens[side_index])
    normalized_outcome = _normalize_name(outcome)
    for token in tokens:
        if token in candidates:
            continue
        token_outcome = _first_text(
            token.get("outcome"),
            token.get("name"),
            token.get("title"),
            token.get("label"),
        )
        if normalized_outcome and _normalize_name(token_outcome) == normalized_outcome:
            candidates.append(token)

    for token in candidates:
        for key in ASK_PRICE_KEYS:
            if key not in token:
                continue
            price = _valid_buy_price(token.get(key))
            if price is not None:
                return price, key

    return None, ""


def _indexed_price(
    value: object,
    *,
    side_index: int,
    outcome: str,
) -> float | None:
    if isinstance(value, dict):
        for key in (side_index, str(side_index), outcome, _normalize_name(outcome)):
            if key in value:
                price = _valid_buy_price(value.get(key))
                if price is not None:
                    return price
        sequence = list(value.values())
    else:
        sequence = _sequence(value)

    if 0 <= side_index < len(sequence):
        return _valid_buy_price(sequence[side_index])
    return None


def _scalar_ask_price(value: object, *, side_index: int) -> float | None:
    if side_index != 0:
        return None
    return _valid_buy_price(value)


def _valid_buy_price(value: object) -> float | None:
    price = _first_number(value)
    if price is None or price <= 0 or price >= 1:
        return None
    return price


def _decimal_odds(price: float | None) -> float | None:
    if price is None or price <= 0 or price >= 1:
        return None
    return 1.0 / price


def _is_generic_outcome(value: str) -> bool:
    normalized = _normalize_name(value)
    return normalized in GENERIC_OUTCOMES


def _is_team_like_outcome(value: str) -> bool:
    normalized = _normalize_name(value)
    if not normalized or normalized in GENERIC_OUTCOMES:
        return False
    if re.fullmatch(r"(?:over|under)\s+[\d.]+", normalized):
        return False
    return True


def _match_winner_source(
    *,
    market_text: str,
    event_text: str,
    outcomes: list[str],
) -> str:
    if _team_vs_team_text_matches(market_text, outcomes):
        return "polymarket_parent_team_vs_team_market"
    if MATCH_WINNER_RE.search(market_text) and _text_contains_all_outcomes(
        market_text,
        outcomes,
    ):
        return "polymarket_match_winner_keyword_market"
    if _text_contains_all_outcomes(event_text, outcomes):
        return "polymarket_event_title_team_match"
    return ""


def _team_vs_team_text_matches(text: str, outcomes: list[str]) -> bool:
    if len(outcomes) != 2:
        return False
    normalized_text = _normalize_name(text)
    team_a = _normalize_name(outcomes[0])
    team_b = _normalize_name(outcomes[1])
    if not team_a or not team_b:
        return False
    patterns = (
        f"{team_a} vs {team_b}",
        f"{team_b} vs {team_a}",
        f"{team_a} v {team_b}",
        f"{team_b} v {team_a}",
        f"{team_a} versus {team_b}",
        f"{team_b} versus {team_a}",
    )
    return any(pattern in normalized_text for pattern in patterns)


def _text_contains_all_outcomes(text: str, outcomes: list[str]) -> bool:
    normalized_text = _normalize_name(text)
    return all(_normalize_name(outcome) in normalized_text for outcome in outcomes)


def _market_type_matches(value: str) -> bool:
    key = _normalize_name(value).replace(" ", "_")
    return key in {"h2h", "head_to_head", "match_winner", "winner"}


def _market_classification_text(market: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("title", "question", "name", "slug", "marketType"):
        parts.extend(_text_values(market.get(key)))
    return " ".join(parts)


def _event_classification_text(event: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("title", "question", "name", "slug"):
        parts.extend(_text_values(event.get(key)))
    return " ".join(parts)


def _dota2_safety_rejection_reason(
    *,
    game: str | None,
    event: dict[str, Any],
    market: dict[str, Any],
    market_text: str,
    event_text: str,
) -> str:
    if game != "dota2":
        return ""

    flags = _dota2_unsafe_flags(
        event,
        market,
        market_text=market_text,
        event_text=event_text,
    )
    if flags.get("ewc_or_draw_risk_tournament"):
        return "ewc_or_draw_risk_tournament"
    if flags.get("bo2_or_ambiguous_format"):
        return "bo2_or_ambiguous_format"
    if flags.get("draw_or_three_way"):
        return "draw_or_three_way"
    if flags.get("map_or_game_winner"):
        return "map_or_game_winner"
    if flags.get("prop_market"):
        return "prop_market"
    if flags.get("outright_market"):
        return "outright_market"
    return ""


def _dota2_unsafe_flags(
    event: dict[str, Any],
    market: dict[str, Any],
    *,
    market_text: str,
    event_text: str,
) -> dict[str, dict[str, str]]:
    parts = _dota2_safety_parts(
        event,
        market,
        market_text=market_text,
        event_text=event_text,
    )
    checks = (
        ("ewc_or_draw_risk_tournament", DOTA2_EWC_RE),
        ("bo2_or_ambiguous_format", DOTA2_BO2_RE),
        ("draw_or_three_way", DOTA2_DRAW_OR_THREE_WAY_RE),
        ("map_or_game_winner", DOTA2_MAP_OR_GAME_RE),
        ("prop_market", DOTA2_PROP_RE),
        ("outright_market", DOTA2_OUTRIGHT_RE),
    )
    flags: dict[str, dict[str, str]] = {}
    for reason, pattern in checks:
        match = _dota2_first_match(pattern, parts)
        if match:
            flags[reason] = match
    return flags


def _dota2_first_match(
    pattern: re.Pattern[str],
    parts: list[tuple[str, str]],
) -> dict[str, str]:
    for source, text in parts:
        match = pattern.search(text)
        if match:
            return {
                "source": source,
                "matched_text": match.group(0),
                "text": _truncate(text, 120),
            }
    return {}


def _dota2_detected_best_of(text: str) -> str:
    match = DOTA2_SAFE_BEST_OF_RE.search(text)
    if not match:
        return ""
    short = _text(match.group("short")).upper()
    if short:
        return short
    maps = (
        match.group("bo_maps")
        or match.group("short_maps")
        or match.group("word_maps")
        or ""
    )
    normalized = _text(maps).casefold()
    if normalized in {"3", "three"}:
        return "BO3"
    if normalized in {"5", "five"}:
        return "BO5"
    return ""


def _dota2_safety_text(
    event: dict[str, Any],
    market: dict[str, Any],
    *,
    market_text: str,
    event_text: str,
) -> str:
    parts = [
        text
        for _source, text in _dota2_safety_parts(
            event,
            market,
            market_text=market_text,
            event_text=event_text,
        )
    ]
    return " ".join(part for part in parts if part)


def _dota2_safety_parts(
    event: dict[str, Any],
    market: dict[str, Any],
    *,
    market_text: str,
    event_text: str,
) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    if market_text:
        parts.append(("market_classification", market_text))
    if event_text:
        parts.append(("event_classification", event_text))
    for record in (event, market):
        record_name = "event" if record is event else "market"
        for key in (
            "title",
            "question",
            "name",
            "slug",
            "description",
            "tournament",
            "series",
            "league",
            "competition",
            "category",
            "categoryName",
            "tags",
        ):
            for text in _text_values(record.get(key)):
                if text:
                    parts.append((f"{record_name}.{key}", text))
    return parts


def _tournament(event: dict[str, Any], market: dict[str, Any]) -> str | None:
    for record in (event, market):
        text = _first_nested_text(
            record.get("tournament"),
            record.get("series"),
            record.get("league"),
            record.get("competition"),
            record.get("title"),
            record.get("category"),
        )
        if text:
            return text
    return None


def _start_time(event: dict[str, Any], market: dict[str, Any]) -> datetime | None:
    for record in (event, market):
        for key in (
            "gameStartTime",
            "game_start_time",
            "startTime",
            "start_time",
            "startDate",
            "startDateIso",
            "start_date",
            "commence_time",
            "endDate",
            "endDateIso",
        ):
            parsed = _parse_datetime(record.get(key))
            if parsed:
                return parsed
    return None


def _event_url(event: dict[str, Any], market: dict[str, Any]) -> str | None:
    raw_url = _first_text(
        event.get("url"),
        event.get("eventUrl"),
        event.get("event_url"),
        market.get("url"),
        market.get("marketUrl"),
        market.get("market_url"),
    )
    if raw_url:
        if raw_url.startswith("http://") or raw_url.startswith("https://"):
            return raw_url
        if raw_url.startswith("/"):
            return f"https://polymarket.com{raw_url}"

    event_slug = _first_text(event.get("slug"), market.get("eventSlug"))
    if event_slug:
        return f"https://polymarket.com/event/{event_slug}"

    market_slug = _first_text(market.get("slug"))
    if market_slug:
        return f"https://polymarket.com/market/{market_slug}"
    return None


def _raw_snapshot(
    event: dict[str, Any],
    market: dict[str, Any],
    outcomes: list[str],
    reference_prices: list[float | None],
    selected_prices: list[float | None],
    price_details: list[dict[str, Any]],
    game: str,
    source: str,
    match_winner_source: str,
) -> dict[str, Any]:
    market_slug = _first_text(market.get("slug"))
    event_slug = _first_text(event.get("slug"), market.get("eventSlug"))
    detected_best_of = (
        _dota2_detected_best_of(
            _dota2_safety_text(
                event,
                market,
                market_text=_market_classification_text(market),
                event_text=_event_classification_text(event),
            )
        )
        if game == "dota2"
        else ""
    )
    return {
        "source": source,
        "event_id": _first_text(event.get("id"), market.get("eventId")),
        "market_id": _first_text(market.get("id"), market.get("conditionId")),
        "condition_id": _first_text(market.get("conditionId")),
        "slug": market_slug or event_slug,
        "event_slug": event_slug,
        "market_slug": market_slug,
        "question": _first_text(market.get("question")),
        "detected_best_of": detected_best_of,
        "match_winner_source": match_winner_source,
        "original_outcomes": outcomes,
        "original_prices": reference_prices,
        "selected_prices": selected_prices,
        "price_sources": [
            _text(detail.get("price_source")) for detail in price_details
        ],
        "executable_buy_prices": [
            detail.get("executable_buy_price") for detail in price_details
        ],
        "executable_price_sources": [
            _text(detail.get("executable_price_source")) for detail in price_details
        ],
        "polymarket_actionable": [
            bool(detail.get("actionable")) for detail in price_details
        ],
        "non_actionable_reasons": [
            _text(detail.get("non_actionable_reason")) for detail in price_details
        ],
        "outcome_price_source": _outcome_price_source(market),
        "volume": _first_raw(market.get("volume"), market.get("volumeNum")),
        "liquidity": _first_raw(market.get("liquidity"), market.get("liquidityNum")),
        "active": {
            "event": _first_raw(event.get("active"), event.get("isActive")),
            "market": _first_raw(market.get("active"), market.get("isActive")),
        },
        "closed": {
            "event": _first_raw(event.get("closed"), event.get("resolved")),
            "market": _first_raw(market.get("closed"), market.get("resolved")),
        },
        "source_fields": {
            "game": game,
            "tournament": _tournament(event, market),
            "start_time": _first_start_time_source(event, market),
            "event_url": _event_url(event, market),
        },
        "event": event,
        "market": market,
    }


def _first_start_time_source(
    event: dict[str, Any],
    market: dict[str, Any],
) -> dict[str, str]:
    for source_name, record in (("event", event), ("market", market)):
        for key in (
            "gameStartTime",
            "game_start_time",
            "startTime",
            "start_time",
            "startDate",
            "startDateIso",
            "start_date",
            "commence_time",
            "endDate",
            "endDateIso",
        ):
            if record.get(key):
                return {"source": source_name, "field": key, "value": _text(record[key])}
    return {}


def _normalized_sample(event: ProviderOddsEvent) -> dict[str, str]:
    return {
        "source": _text(event.raw.get("source")),
        "sport": event.game,
        "teams": f"{event.team_a} vs {event.team_b}",
        "odds": f"{event.odds_a:g}/{event.odds_b:g}",
        "market_id": _text(event.raw.get("market_id")),
        "event_id": _text(event.raw.get("event_id")),
        "question": _text(event.raw.get("question")),
        "detected_best_of": _text(event.raw.get("detected_best_of")),
        "outcome_count": _text(len(_sequence(event.raw.get("original_outcomes")))),
        "outcome_names": "|".join(
            _text(outcome)
            for outcome in _sequence(event.raw.get("original_outcomes"))
        ),
        "team_names": f"{event.team_a}|{event.team_b}",
        "slug": _text(event.raw.get("slug")),
        "original_prices": "/".join(
            _text(price) for price in _sequence(event.raw.get("original_prices"))
        ),
        "executable_buy_prices": "/".join(
            _text(price)
            for price in _sequence(event.raw.get("executable_buy_prices"))
        ),
        "price_source": "/".join(
            _text(source) for source in _sequence(event.raw.get("price_sources"))
        ),
        "match_winner_source": _text(event.raw.get("match_winner_source")),
        "url": event.event_url or "",
    }


def _rejected_sample(
    event: dict[str, Any],
    market: dict[str, Any],
    reason: str,
) -> dict[str, str]:
    parsed_start_time = _start_time(event, market)
    detected_best_of = _dota2_detected_best_of(
        _dota2_safety_text(
            event,
            market,
            market_text=_market_classification_text(market),
            event_text=_event_classification_text(event),
        )
    )
    unsafe_flags = _dota2_unsafe_flags(
        event,
        market,
        market_text=_market_classification_text(market),
        event_text=_event_classification_text(event),
    )
    draw_match = unsafe_flags.get("draw_or_three_way", {})
    outcome_names = _outcome_names(market)
    return {
        "reason": reason,
        "event_id": _first_text(event.get("id"), market.get("eventId")),
        "market_id": _first_text(market.get("id"), market.get("conditionId")),
        "event_title": _truncate(
            _first_text(event.get("title"), event.get("name"), event.get("question")),
            120,
        ),
        "slug": _first_text(event.get("slug"), market.get("eventSlug"), market.get("slug")),
        "url": _event_url(event, market) or "",
        "tournament": _tournament(event, market) or "",
        "category": _first_nested_text(
            event.get("category"),
            event.get("categoryName"),
            event.get("series"),
            event.get("league"),
            market.get("category"),
            market.get("categoryName"),
        ),
        "tags": _joined_text_values(event.get("tags"), market.get("tags"), limit=5),
        "active": _status_pair_text(
            event.get("active"),
            event.get("isActive"),
            market.get("active"),
            market.get("isActive"),
        ),
        "closed": _status_pair_text(
            event.get("closed"),
            event.get("resolved"),
            market.get("closed"),
            market.get("resolved"),
        ),
        "start_time": parsed_start_time.isoformat() if parsed_start_time else "",
        "market_title": _truncate(
            _first_text(market.get("question"), market.get("title"), market.get("name")),
            120,
        ),
        "detected_best_of": detected_best_of,
        "outcome_count": _text(len(outcome_names)),
        "outcome_names": "|".join(outcome_names),
        "team_names": "|".join(
            outcome for outcome in outcome_names if not _is_generic_outcome(outcome)
        ),
        "draw_or_three_way_source": _text(draw_match.get("source")),
        "draw_or_three_way_matched_text": _text(draw_match.get("matched_text")),
        "unsafe_flags": ",".join(sorted(unsafe_flags)),
        "outcomes": "|".join(outcome_names),
        "question": _truncate(
            _first_text(market.get("question"), event.get("title"), market.get("slug")),
            120,
        ),
    }


def _status_pair_text(
    event_value: object,
    event_fallback: object,
    market_value: object,
    market_fallback: object,
) -> str:
    parts: list[str] = []
    event_status = _first_raw(event_value, event_fallback)
    market_status = _first_raw(market_value, market_fallback)
    if event_status is not None:
        parts.append(f"event={_text(event_status).lower()}")
    if market_status is not None:
        parts.append(f"market={_text(market_status).lower()}")
    return ",".join(parts)


def _joined_text_values(*values: object, limit: int = 5) -> str:
    parts: list[str] = []
    for value in values:
        for text in _text_values(value):
            if text and text not in parts:
                parts.append(text)
            if len(parts) >= limit:
                return ",".join(parts)
    return ",".join(parts)


def _sequence(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return []
            return _sequence(parsed)
        return [part.strip() for part in text.split(",") if part.strip()]
    return []


def _text_values(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        values = []
        for key in ("name", "title", "label", "slug", "id"):
            values.extend(_text_values(value.get(key)))
        return values
    if isinstance(value, list):
        values: list[str] = []
        for item in value:
            values.extend(_text_values(item))
        return values
    text = _text(value)
    return [text] if text else []


def _normalized_blob(values: list[str]) -> str:
    text = " ".join(values).casefold()
    text = re.sub(r"[_-]+", " ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def _normalize_name(value: object) -> str:
    return _normalized_blob([_text(value)])


def _dict_with_default(
    item: dict[str, Any],
    key: str,
    value: object,
) -> dict[str, Any]:
    copied = dict(item)
    copied.setdefault(key, value)
    return copied


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


def _first_nested_text(*values: object) -> str:
    for value in values:
        for text in _text_values(value):
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
            value.get("label"),
            value.get("slug"),
            value.get("id"),
        )
    return str(value).strip()


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
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)

    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _is_true_value(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    return str(value).strip().casefold() in {"true", "1", "yes"}


def _is_false_value(value: object) -> bool:
    if value is False:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 0
    return str(value).strip().casefold() in {"false", "0", "no"}


def _truncate(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."
