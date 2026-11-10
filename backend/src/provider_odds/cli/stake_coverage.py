from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from provider_odds.models import ProviderOddsEvent
from provider_odds.providers import OddsPapiProvider
from provider_odds.providers.base import (
    ProviderConfigurationError,
    ProviderRequestError,
)
from provider_odds.providers.oddspapi import (
    MAPPING_PROFILE_ODDSPAPI_STAKE_CS2_MARKET171_SELECTION_ORDER,
    SUPPORTED_MAPPING_PROFILES,
)

from provider_odds.cli.scan import (
    _canonical_tournament_bucket_name,
    _is_rate_limit_error,
    _recoverable_stake_tournament_error,
    _sleep_for_rate_limit,
    _text,
    _tournament_quality_for_sport,
    _tournament_quality_keep_decision,
)


DEFAULT_MIN_TOURNAMENT_QUALITY = "b"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose Stake/OddsPapi tournament and fixture coverage."
    )
    parser.add_argument("--sport", default="cs2")
    parser.add_argument("--bookmaker", default="stake")
    parser.add_argument(
        "--stake-mapping-profile",
        nargs="?",
        const=MAPPING_PROFILE_ODDSPAPI_STAKE_CS2_MARKET171_SELECTION_ORDER,
        choices=sorted(SUPPORTED_MAPPING_PROFILES),
    )
    parser.add_argument("--stake-request-delay-seconds", type=float, default=5.0)
    parser.add_argument("--stake-rate-limit-retries", type=int, default=2)
    parser.add_argument("--max-tournaments", type=int, default=50)
    parser.add_argument("--name-contains", action="append")
    parser.add_argument("--team-contains", action="append")
    parser.add_argument("--tournament-id", action="append")
    parser.add_argument("--fetch-fixtures", action="store_true")
    parser.add_argument("--format", choices=["table", "json"], default="table")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--debug-raw-markets", action="store_true")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args(argv)

    try:
        result = run_stake_coverage(
            sport=args.sport,
            bookmaker=args.bookmaker,
            mapping_profile=args.stake_mapping_profile,
            request_delay_seconds=args.stake_request_delay_seconds,
            rate_limit_retries=args.stake_rate_limit_retries,
            max_tournaments=args.max_tournaments,
            name_contains=args.name_contains,
            team_contains=args.team_contains,
            tournament_id=args.tournament_id,
            fetch_fixtures=args.fetch_fixtures or args.debug_raw_markets,
            env_file=args.env_file,
            include_provider_debug=args.debug or args.debug_raw_markets,
            include_fixture_market_debug=args.debug_raw_markets,
        )
    except (ProviderConfigurationError, ProviderRequestError, ValueError) as exc:
        print(_safe_text(exc), file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        print(
            format_coverage_table(
                result,
                debug=args.debug,
                market_debug=args.debug_raw_markets,
            )
        )
    return 0


def run_stake_coverage(
    *,
    sport: str = "cs2",
    bookmaker: str = "stake",
    mapping_profile: str | None = None,
    request_delay_seconds: float = 5.0,
    rate_limit_retries: int = 2,
    max_tournaments: int = 50,
    name_contains: list[str] | None = None,
    team_contains: list[str] | None = None,
    tournament_id: list[str] | None = None,
    fetch_fixtures: bool = False,
    env_file: Path = Path(".env"),
    provider: object | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    include_provider_debug: bool = False,
    include_fixture_market_debug: bool = False,
) -> dict[str, Any]:
    provider = provider or _build_provider(env_file)
    limit = _coverage_limit(max_tournaments)
    delay_seconds = max(float(request_delay_seconds), 0.0)
    max_retries = max(int(rate_limit_retries), 0)
    name_filters = _filter_values(name_contains)
    team_filters = _filter_values(team_contains)
    explicit_tournament_ids = _id_values(tournament_id)
    rate_limit_count = 0
    stopped_reason = ""

    candidate_fetch = _fetch_tournament_candidates(
        provider,
        sport=sport,
        max_tournaments=limit,
        name_filters=name_filters,
        rate_limit_retries=max_retries,
        request_delay_seconds=delay_seconds,
        sleeper=sleeper,
    )
    rate_limit_count += candidate_fetch["rate_limit_count"]
    stopped_reason = _text(candidate_fetch.get("stopped_reason"))
    rows = _coverage_rows(
        candidate_fetch["candidates"],
        sport=sport,
        name_filters=name_filters,
        explicit_tournament_ids=explicit_tournament_ids,
    )

    if fetch_fixtures and not stopped_reason:
        fetch_result = _fetch_candidate_fixtures(
            provider,
            rows,
            sport=sport,
            bookmaker=bookmaker,
            mapping_profile=mapping_profile,
            request_delay_seconds=delay_seconds,
            rate_limit_retries=max_retries,
            team_filters=team_filters,
            sleeper=sleeper,
            include_fixture_market_debug=include_fixture_market_debug,
        )
        rate_limit_count += fetch_result["rate_limit_count"]
        stopped_reason = _text(fetch_result.get("stopped_reason"))
        if team_filters and not stopped_reason:
            rows = _filter_rows_by_matched_teams(rows)

    summary = _coverage_summary(
        rows,
        raw_candidate_count=int(candidate_fetch.get("raw_candidate_count") or 0),
        filtered_candidate_count=int(
            candidate_fetch.get("filtered_candidate_count") or 0
        ),
        explicit_tournament_count=len(explicit_tournament_ids),
        team_filter_count=len(team_filters),
        rate_limit_count=rate_limit_count,
        stopped_reason=stopped_reason,
    )
    result: dict[str, Any] = {
        "summary": summary,
        "candidates": rows,
    }
    provider_debug = getattr(provider, "last_debug", {}) or {}
    if include_provider_debug and isinstance(provider_debug, dict):
        result["provider_debug"] = provider_debug
    return result


def _build_provider(env_file: Path) -> OddsPapiProvider:
    return OddsPapiProvider.from_env(env_file)


def _fetch_tournament_candidates(
    provider: object,
    *,
    sport: str,
    max_tournaments: int,
    name_filters: list[str],
    rate_limit_retries: int,
    request_delay_seconds: float,
    sleeper: Callable[[float], None],
) -> dict[str, Any]:
    fetch_candidates = getattr(provider, "fetch_tournament_candidates", None)
    if not callable(fetch_candidates):
        raise ValueError("Stake coverage requires fetch_tournament_candidates")

    attempts = 0
    rate_limit_count = 0
    while True:
        try:
            fetch_kwargs: dict[str, object] = {
                "sport": sport,
                "max_candidates": max_tournaments,
            }
            if name_filters:
                fetch_kwargs["tournament_keywords"] = name_filters
            candidates = fetch_candidates(**fetch_kwargs)
            candidates = [item for item in candidates if isinstance(item, dict)]
            provider_debug = getattr(provider, "last_debug", {}) or {}
            raw_candidate_count = _raw_candidate_count(provider_debug, candidates)
            filtered_candidate_count = _filtered_candidate_count(
                provider_debug,
                candidates,
                name_filters=name_filters,
            )
            return {
                "candidates": candidates,
                "raw_candidate_count": raw_candidate_count,
                "filtered_candidate_count": filtered_candidate_count,
                "rate_limit_count": rate_limit_count,
                "stopped_reason": "",
            }
        except ProviderRequestError as exc:
            if not _is_rate_limit_error(exc):
                raise
            rate_limit_count += 1
            if attempts >= rate_limit_retries:
                return {
                    "candidates": [],
                    "raw_candidate_count": 0,
                    "filtered_candidate_count": 0,
                    "rate_limit_count": rate_limit_count,
                    "stopped_reason": "rate_limited",
                }
            attempts += 1
            _sleep_for_rate_limit(
                exc,
                fallback_delay_seconds=request_delay_seconds,
                sleeper=sleeper,
            )


def _fetch_candidate_fixtures(
    provider: object,
    rows: list[dict[str, Any]],
    *,
    sport: str,
    bookmaker: str,
    mapping_profile: str | None,
    request_delay_seconds: float,
    rate_limit_retries: int,
    team_filters: list[str],
    sleeper: Callable[[float], None],
    include_fixture_market_debug: bool,
) -> dict[str, Any]:
    rate_limit_count = 0
    stopped_reason = ""
    for index, row in enumerate(rows):
        tournament_id = _text(row.get("tournament_id"))
        if not tournament_id:
            row["fixture_status"] = "error"
            row["error"] = "missing_tournament_id"
            stopped_reason = "error"
            break

        attempts = 0
        while True:
            try:
                events = provider.fetch_events(
                    sport=sport,
                    market_type="h2h",
                    bookmaker=bookmaker,
                    tournament_id=tournament_id,
                    mapping_profile=mapping_profile,
                )
                provider_events = [
                    event for event in events if isinstance(event, ProviderOddsEvent)
                ]
                if include_fixture_market_debug:
                    row["raw_market_audit"] = _fixture_market_audit(
                        getattr(provider, "last_debug", {}) or {}
                    )
                row["fixture_count"] = len(provider_events)
                row["sample_team_pairs"] = _sample_team_pairs(provider_events)
                row["matched_team_pairs"] = _matched_team_pairs(
                    provider_events,
                    team_filters,
                )
                row["fixture_status"] = (
                    "fetched" if provider_events else "fixture_not_found"
                )
                break
            except ProviderRequestError as exc:
                skip_reason = _recoverable_stake_tournament_error(exc)
                if skip_reason:
                    row["fixture_status"] = skip_reason
                    row["error"] = ""
                    break

                if not _is_rate_limit_error(exc):
                    row["fixture_status"] = "error"
                    row["error"] = _safe_text(exc)
                    stopped_reason = "error"
                    break

                rate_limit_count += 1
                if attempts >= rate_limit_retries:
                    row["fixture_status"] = "rate_limited"
                    row["error"] = _safe_text(exc)
                    stopped_reason = "rate_limited"
                    break
                attempts += 1
                _sleep_for_rate_limit(
                    exc,
                    fallback_delay_seconds=request_delay_seconds,
                    sleeper=sleeper,
                )

        if stopped_reason:
            break
        if request_delay_seconds and index < len(rows) - 1:
            sleeper(request_delay_seconds)

    return {
        "rate_limit_count": rate_limit_count,
        "stopped_reason": stopped_reason,
    }


def _coverage_rows(
    candidates: list[dict[str, Any]],
    *,
    sport: str,
    name_filters: list[str],
    explicit_tournament_ids: list[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    candidates_by_id = {
        _candidate_id(candidate): candidate
        for candidate in candidates
        if _candidate_id(candidate)
    }

    if name_filters:
        discovered_candidates = [
            candidate
            for candidate in candidates
            if _matched_name_filter_values(candidate, name_filters)
        ]
    elif explicit_tournament_ids:
        discovered_candidates = []
    else:
        discovered_candidates = candidates

    for candidate in discovered_candidates:
        candidate_id = _candidate_id(candidate)
        if candidate_id and candidate_id in seen_ids:
            continue
        if candidate_id:
            seen_ids.add(candidate_id)
        rows.append(
            _coverage_row(
                candidate,
                sport=sport,
                source="discovered",
                matched_filter=_matched_filter_text(
                    _matched_name_filter_values(candidate, name_filters)
                ),
            )
        )

    for explicit_id in explicit_tournament_ids:
        if explicit_id in seen_ids:
            _append_matched_filter(rows, explicit_id, f"tournament_id:{explicit_id}")
            continue
        candidate = candidates_by_id.get(explicit_id, {"id": explicit_id})
        rows.append(
            _coverage_row(
                candidate,
                sport=sport,
                source="explicit_id",
                matched_filter=f"tournament_id:{explicit_id}",
            )
        )
        seen_ids.add(explicit_id)

    return rows


def _coverage_row(
    candidate: dict[str, Any],
    *,
    sport: str,
    source: str,
    matched_filter: str,
) -> dict[str, Any]:
    name = _first_candidate_text(
        candidate,
        "name",
        "tournament_name",
        "tournamentName",
        "title",
    )
    quality_text = _candidate_quality_text(candidate) or name
    bucket_name = _canonical_tournament_bucket_name(quality_text)
    quality = _tournament_quality_for_sport(bucket_name or name, sport=sport)
    keep, reason = _tournament_quality_keep_decision(
        quality,
        sport=sport,
        bucket_name=bucket_name or name,
        min_quality=DEFAULT_MIN_TOURNAMENT_QUALITY,
        allow_unknown=False,
        include_low_tier=False,
    )
    row = {
        "tournament_id": _first_candidate_text(
            candidate,
            "id",
            "tournament_id",
            "tournamentId",
        ),
        "tournament_name": name,
        "matched_filter": matched_filter,
        "source": source,
        "raw_score": _raw_score(candidate),
        "inferred_quality": quality or "unknown",
        "quality_decision": _quality_decision(keep, reason),
        "quality_reason": reason,
        "fixture_status": "not_fetched",
        "fixture_count": 0,
        "sample_team_pairs": [],
        "matched_team_pairs": [],
    }
    row["tournament_slug"] = _first_candidate_text(candidate, "slug", "key")
    row["category"] = _first_candidate_text(
        candidate,
        "category",
        "categoryName",
        "categorySlug",
    )
    return row


def _coverage_summary(
    rows: list[dict[str, Any]],
    *,
    raw_candidate_count: int,
    filtered_candidate_count: int,
    explicit_tournament_count: int,
    team_filter_count: int,
    rate_limit_count: int,
    stopped_reason: str,
) -> dict[str, Any]:
    return {
        "candidate_count": len(rows),
        "raw_candidate_count": raw_candidate_count,
        "filtered_candidate_count": filtered_candidate_count,
        "explicit_tournament_count": explicit_tournament_count,
        "team_filter_count": team_filter_count,
        "fetched_tournament_count": sum(
            1 for row in rows if row.get("fixture_status") == "fetched"
        ),
        "matched_team_candidate_count": sum(
            1 for row in rows if row.get("matched_team_pairs")
        ),
        "fixture_not_found_count": sum(
            1 for row in rows if row.get("fixture_status") == "fixture_not_found"
        ),
        "tournaments_with_fixtures_count": sum(
            1 for row in rows if int(row.get("fixture_count") or 0) > 0
        ),
        "total_fixture_count": sum(int(row.get("fixture_count") or 0) for row in rows),
        "kept_quality_count": sum(
            1 for row in rows if row.get("quality_decision") == "keep"
        ),
        "rejected_quality_count": sum(
            1 for row in rows if row.get("quality_decision") == "reject"
        ),
        "rate_limit_count": rate_limit_count,
        "stopped_reason": stopped_reason,
    }


def format_coverage_table(
    result: dict[str, Any],
    *,
    debug: bool = False,
    market_debug: bool = False,
) -> str:
    rows = result.get("candidates") if isinstance(result.get("candidates"), list) else []
    lines = ["stake coverage:"]
    if rows:
        lines.append(_format_rows_table(_dict_rows(rows), debug=debug))
    else:
        lines.append("(no candidates)")
    lines.append("")
    lines.extend(_format_summary(result.get("summary")))
    if debug:
        lines.extend(_format_debug(result.get("provider_debug")))
    if market_debug:
        lines.extend(_format_raw_market_audit(rows))
    return "\n".join(lines)


def _format_rows_table(rows: list[dict[str, Any]], *, debug: bool) -> str:
    columns = [
        "tournament_id",
        "tournament_name",
        "matched_filter",
        "source",
        "raw_score",
        "inferred_quality",
        "quality_decision",
        "fixture_status",
        "fixture_count",
        "sample_team_pairs",
        "matched_team_pairs",
    ]
    if debug:
        columns.extend(["tournament_slug", "category", "quality_reason", "error"])
    display_rows = [
        {
            column: _table_cell(row.get(column))
            for column in columns
        }
        for row in rows
    ]
    widths = {
        column: max(len(column), *(len(row[column]) for row in display_rows))
        for column in columns
    }
    header = " | ".join(column.ljust(widths[column]) for column in columns)
    separator = "-+-".join("-" * widths[column] for column in columns)
    body = [
        " | ".join(row[column].ljust(widths[column]) for column in columns)
        for row in display_rows
    ]
    return "\n".join([header, separator, *body])


def _format_summary(value: object) -> list[str]:
    summary = value if isinstance(value, dict) else {}
    keys = [
        "candidate_count",
        "raw_candidate_count",
        "filtered_candidate_count",
        "explicit_tournament_count",
        "team_filter_count",
        "fetched_tournament_count",
        "matched_team_candidate_count",
        "fixture_not_found_count",
        "tournaments_with_fixtures_count",
        "total_fixture_count",
        "kept_quality_count",
        "rejected_quality_count",
        "rate_limit_count",
        "stopped_reason",
    ]
    return [
        "summary:",
        *[f"{key}={_table_cell(summary.get(key))}" for key in keys],
    ]


def _format_debug(value: object) -> list[str]:
    if not isinstance(value, dict) or not value:
        return []
    lines = ["", "provider_debug:"]
    for key in sorted(value):
        lines.append(f"{_table_cell(key)}={_table_cell(value.get(key))}")
    return lines


def _fixture_market_audit(provider_debug: object) -> list[dict[str, Any]]:
    if not isinstance(provider_debug, dict):
        return []
    fixtures: list[dict[str, Any]] = []
    for fixture in _dict_rows(provider_debug.get("market_explorer")):
        markets: list[dict[str, Any]] = []
        for market in _dict_rows(fixture.get("markets")):
            markets.append(
                {
                    "market_path": _safe_debug_value(market.get("market_path")),
                    "market_id": _safe_debug_value(market.get("marketId")),
                    "key": _safe_debug_value(market.get("key")),
                    "bookmaker_market_id": _safe_debug_value(
                        market.get("bookmakerMarketId")
                    ),
                    "market_name": _safe_debug_value(market.get("marketName")),
                    "market_active": _safe_debug_value(market.get("marketActive")),
                    "suspended": _safe_debug_value(market.get("suspended")),
                    "outcome_count": _safe_debug_value(market.get("outcome_count")),
                    "way_type": _safe_debug_value(market.get("way_type")),
                    "looks_like_match_winner": _safe_debug_value(
                        market.get("looks_like_match_winner")
                    ),
                    "outcome_names": _safe_debug_value(market.get("outcome_names")),
                    "decimal_odds": _safe_debug_value(market.get("decimal_odds")),
                    "outcomes": _safe_debug_value(market.get("outcomes")),
                }
            )
        fixtures.append(
            {
                "fixture_id": _safe_debug_value(fixture.get("fixtureId")),
                "tournament_id": _safe_debug_value(fixture.get("tournamentId")),
                "tournament_name": _safe_debug_value(fixture.get("tournamentName")),
                "category_name": _safe_debug_value(fixture.get("categoryName")),
                "start_time": _safe_debug_value(fixture.get("startTime")),
                "true_start_time": _safe_debug_value(fixture.get("trueStartTime")),
                "participant1_id": _safe_debug_value(fixture.get("participant1Id")),
                "participant1_name": _safe_debug_value(
                    fixture.get("participant1Name")
                ),
                "participant2_id": _safe_debug_value(fixture.get("participant2Id")),
                "participant2_name": _safe_debug_value(
                    fixture.get("participant2Name")
                ),
                "bookmaker_path": _safe_debug_value(fixture.get("bookmaker_path")),
                "bookmaker_name": _safe_debug_value(fixture.get("bookmaker_name")),
                "markets": markets,
            }
        )
    return fixtures


def _format_raw_market_audit(rows: list[object]) -> list[str]:
    lines = ["", "raw_market_audit:"]
    printed = False
    for row in _dict_rows(rows):
        audits = _dict_rows(row.get("raw_market_audit"))
        if not audits:
            continue
        printed = True
        lines.append(
            "tournament: "
            f"id={_table_cell(row.get('tournament_id'))} "
            f"name={_table_cell(row.get('tournament_name'))} "
            f"fixture_status={_table_cell(row.get('fixture_status'))}"
        )
        for fixture_index, fixture in enumerate(audits, 1):
            lines.append(
                f"fixture_{fixture_index}: "
                f"fixtureId={_table_cell(fixture.get('fixture_id'))} "
                f"tournamentId={_table_cell(fixture.get('tournament_id'))} "
                f"tournamentName={_table_cell(fixture.get('tournament_name'))} "
                f"categoryName={_table_cell(fixture.get('category_name'))} "
                f"startTime={_table_cell(fixture.get('start_time'))} "
                "teams="
                f"{_table_cell(fixture.get('participant1_name'))} vs "
                f"{_table_cell(fixture.get('participant2_name'))}"
            )
            for market_index, market in enumerate(
                _dict_rows(fixture.get("markets")),
                1,
            ):
                lines.append(
                    f"fixture_{fixture_index}_market_{market_index}: "
                    f"path={_table_cell(market.get('market_path'))} "
                    f"marketId={_table_cell(market.get('market_id'))} "
                    f"key={_table_cell(market.get('key'))} "
                    f"marketName={_table_cell(market.get('market_name'))} "
                    f"active={_table_cell(market.get('market_active'))} "
                    f"suspended={_table_cell(market.get('suspended'))} "
                    f"way_type={_table_cell(market.get('way_type'))} "
                    "looks_like_match_winner="
                    f"{_table_cell(market.get('looks_like_match_winner'))} "
                    f"outcome_names={_table_cell(market.get('outcome_names'))} "
                    f"decimal_odds={_table_cell(market.get('decimal_odds'))}"
                )
    if not printed:
        lines.append("(no raw market audit)")
    return lines


def _safe_debug_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            _safe_text(key): _safe_debug_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_debug_value(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value)
    return value


def _coverage_limit(value: int) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = 50
    return min(100, max(1, limit))


def _raw_candidate_count(
    provider_debug: object,
    candidates: list[dict[str, Any]],
) -> int:
    if isinstance(provider_debug, dict):
        for key in ("tournaments_count", "raw_candidate_count", "candidate_count"):
            count = _int_or_none(provider_debug.get(key))
            if count is not None:
                return count
    return len(candidates)


def _filtered_candidate_count(
    provider_debug: object,
    candidates: list[dict[str, Any]],
    *,
    name_filters: list[str],
) -> int:
    if isinstance(provider_debug, dict):
        for key in ("candidate_count", "filtered_candidate_count", "printed_count"):
            count = _int_or_none(provider_debug.get(key))
            if count is not None:
                return count
    if not name_filters:
        return len(candidates)
    return sum(1 for candidate in candidates if _matched_name_filter_values(candidate, name_filters))


def _filter_values(values: list[str] | None) -> list[str]:
    filters: list[str] = []
    for value in values or []:
        text = _text(value)
        if text and text.casefold() not in {item.casefold() for item in filters}:
            filters.append(text)
    return filters


def _id_values(values: list[str] | None) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        for part in str(value).split(","):
            tournament_id = _text(part)
            if not tournament_id:
                continue
            key = tournament_id.casefold()
            if key in seen:
                continue
            seen.add(key)
            ids.append(tournament_id)
    return ids


def _candidate_id(candidate: dict[str, Any]) -> str:
    return _first_candidate_text(candidate, "id", "tournament_id", "tournamentId")


def _candidate_name(candidate: dict[str, Any]) -> str:
    return _first_candidate_text(
        candidate,
        "name",
        "tournament_name",
        "tournamentName",
        "title",
    )


def _matched_name_filter_values(
    candidate: dict[str, Any],
    name_filters: list[str],
) -> list[str]:
    if not name_filters:
        return []
    candidate_name = _candidate_name(candidate).casefold()
    return [
        name_filter
        for name_filter in name_filters
        if name_filter.casefold() in candidate_name
    ]


def _matched_filter_text(name_filters: list[str]) -> str:
    return ";".join(f"name_contains:{value}" for value in name_filters)


def _append_matched_filter(
    rows: list[dict[str, Any]],
    tournament_id: str,
    matched_filter: str,
) -> None:
    for row in rows:
        if _text(row.get("tournament_id")).casefold() != tournament_id.casefold():
            continue
        current = _text(row.get("matched_filter"))
        parts = [part for part in current.split(";") if part]
        if matched_filter not in parts:
            parts.append(matched_filter)
        row["matched_filter"] = ";".join(parts)
        return


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _quality_decision(keep: bool, reason: str) -> str:
    if reason == "non_cs2_not_filtered":
        return "unknown"
    return "keep" if keep else "reject"


def _sample_team_pairs(events: list[ProviderOddsEvent]) -> list[str]:
    pairs: list[str] = []
    seen: set[str] = set()
    for event in events:
        pair = " vs ".join(part for part in (event.team_a, event.team_b) if part)
        pair = _text(pair)
        if not pair or pair in seen:
            continue
        seen.add(pair)
        pairs.append(pair)
        if len(pairs) >= 3:
            break
    return pairs


def _matched_team_pairs(
    events: list[ProviderOddsEvent],
    team_filters: list[str],
) -> list[str]:
    if not team_filters:
        return []
    pairs: list[str] = []
    seen: set[str] = set()
    for event in events:
        pair = " vs ".join(part for part in (event.team_a, event.team_b) if part)
        pair = _text(pair)
        if not pair or pair in seen:
            continue
        if not _team_filter_matches_pair(pair, team_filters):
            continue
        seen.add(pair)
        pairs.append(pair)
        if len(pairs) >= 5:
            break
    return pairs


def _team_filter_matches_pair(pair: str, team_filters: list[str]) -> bool:
    haystack = _team_filter_text(pair)
    return any(_team_filter_text(team_filter) in haystack for team_filter in team_filters)


def _team_filter_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", _safe_text(value).casefold()))


def _filter_rows_by_matched_teams(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("matched_team_pairs")]


def _first_candidate_text(candidate: dict[str, Any], *keys: str) -> str:
    for key in keys:
        text = _text(candidate.get(key))
        if text:
            return text
    return ""


def _candidate_quality_text(candidate: dict[str, Any]) -> str:
    return " ".join(
        text
        for text in (
            _first_candidate_text(
                candidate,
                "name",
                "tournament_name",
                "tournamentName",
                "title",
            ),
            _first_candidate_text(candidate, "slug", "key"),
            _first_candidate_text(candidate, "category", "categoryName"),
            _first_candidate_text(candidate, "categorySlug"),
        )
        if text
    )


def _raw_score(candidate: dict[str, Any]) -> str:
    return _first_candidate_text(
        candidate,
        "raw_score",
        "rawScore",
        "score",
        "rank_score",
        "rankScore",
    )


def _dict_rows(rows: list[object]) -> list[dict[str, Any]]:
    return [row for row in rows if isinstance(row, dict)]


def _table_cell(value: object) -> str:
    if isinstance(value, list):
        return "; ".join(_safe_text(item) for item in value)
    if isinstance(value, dict):
        return ",".join(
            f"{_safe_text(key)}={_safe_text(item)}"
            for key, item in value.items()
        )
    return _safe_text(value)


def _safe_text(value: object) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"(?i)(apiKey=)[^&\s\"']+", r"\1***", text)
    text = re.sub(r"(?i)(ODDSPAPI_API_KEY=)[^&\s\"']+", r"\1***", text)
    return " ".join(text.split())


if __name__ == "__main__":
    raise SystemExit(main())
