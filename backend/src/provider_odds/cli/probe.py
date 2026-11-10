from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from provider_odds.models import ProviderOddsEvent
from provider_odds.providers import (
    OddsPapiProvider,
    PolymarketProvider,
    ProviderConfigurationError,
    ProviderRequestError,
    TheOddsApiProvider,
)
from provider_odds.providers.oddspapi import SUPPORTED_MAPPING_PROFILES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print normalized bookmaker odds events from a provider response."
    )
    parser.add_argument(
        "--provider",
        choices=["oddspapi", "polymarket", "the-odds-api"],
        default="oddspapi",
    )
    parser.add_argument(
        "--sport",
        help="Provider sport key. Required without --fixture or OddsPapi --fixture-id.",
    )
    parser.add_argument("--regions", default="us")
    parser.add_argument("--market-type", default="h2h")
    parser.add_argument("--search")
    parser.add_argument("--event-slug")
    parser.add_argument("--market-slug")
    parser.add_argument("--max-events", type=int, default=20)
    parser.add_argument("--bookmaker")
    parser.add_argument("--tournament-id")
    parser.add_argument("--tournament-keyword", action="append")
    parser.add_argument("--fixture-id")
    parser.add_argument("--verbosity", default="3")
    parser.add_argument(
        "--show-tournament-candidates",
        action="store_true",
        help="Print compact OddsPapi Dota2 tournament candidates without odds calls.",
    )
    parser.add_argument("--max-candidates", type=int, default=30)
    parser.add_argument(
        "--mapping-profile",
        choices=sorted(SUPPORTED_MAPPING_PROFILES),
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--fixture",
        type=Path,
        help="Normalize a local provider JSON response instead of calling the API.",
    )
    parser.add_argument("--format", choices=["table", "json"], default="table")
    parser.add_argument(
        "--debug-provider",
        action="store_true",
        help="Print sanitized provider request/normalization diagnostics.",
    )
    parser.add_argument(
        "--debug-markets",
        action="store_true",
        help="Print compact sanitized OddsPapi fixture/market diagnostics.",
    )
    args = parser.parse_args(argv)

    provider = _build_provider(args.provider, args.env_file)

    try:
        if args.show_tournament_candidates:
            if args.provider != "oddspapi":
                parser.error("--show-tournament-candidates is only supported by OddsPapi")
            provider.fetch_tournament_candidates(
                sport=args.sport or "dota2",
                tournament_keywords=args.tournament_keyword,
                max_candidates=args.max_candidates,
            )
            print(format_tournament_candidates(provider))
            if args.debug_provider:
                print(format_provider_debug(args, provider))
            return 0

        if args.fixture:
            payload = json.loads(args.fixture.read_text(encoding="utf-8"))
            normalize_kwargs = {
                "market_type": args.market_type,
                "bookmaker": args.bookmaker,
            }
            if args.provider == "oddspapi":
                normalize_kwargs["mapping_profile"] = args.mapping_profile
            elif args.provider == "polymarket":
                normalize_kwargs["sport"] = args.sport
                normalize_kwargs["max_events"] = args.max_events
                if args.event_slug or args.market_slug:
                    normalize_kwargs["source"] = "gamma_query_slug"
            events = provider.normalize_response(
                payload,
                **normalize_kwargs,
            )
        else:
            if args.provider == "the-odds-api":
                if not args.sport:
                    parser.error("--sport is required unless --fixture is used")
                events = provider.fetch_events(
                    sport=args.sport,
                    regions=args.regions,
                    market_type=args.market_type,
                    bookmaker=args.bookmaker,
                )
            elif args.provider == "polymarket":
                if not args.sport:
                    parser.error("--sport is required unless --fixture is used")
                events = provider.fetch_events(
                    sport=args.sport,
                    market_type=args.market_type,
                    search=args.search,
                    event_slug=args.event_slug,
                    market_slug=args.market_slug,
                    max_events=args.max_events,
                )
            else:
                if not args.sport and not args.fixture_id:
                    parser.error(
                        "--sport is required unless --fixture or --fixture-id is used"
                    )
                events = provider.fetch_events(
                    sport=args.sport,
                    market_type=args.market_type,
                    bookmaker=args.bookmaker,
                    tournament_id=args.tournament_id,
                    tournament_keyword=args.tournament_keyword,
                    fixture_id=args.fixture_id,
                    verbosity=args.verbosity,
                    mapping_profile=args.mapping_profile,
                )
    except (ProviderConfigurationError, ProviderRequestError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps([event.to_dict() for event in events], indent=2))
    else:
        print(format_table(events))

    if args.debug_provider:
        print(format_provider_debug(args, provider))
    if args.debug_markets:
        print(format_market_debug(provider))

    return 0


def _build_provider(
    provider_name: str,
    env_file: Path,
) -> OddsPapiProvider | PolymarketProvider | TheOddsApiProvider:
    if provider_name == "oddspapi":
        return OddsPapiProvider.from_env(env_file)
    if provider_name == "polymarket":
        return PolymarketProvider.from_env(env_file)
    return TheOddsApiProvider.from_env(env_file)


def format_table(events: list[ProviderOddsEvent]) -> str:
    if not events:
        return "(no events)"

    columns = [
        "provider",
        "bookmaker",
        "game",
        "tournament",
        "start_time",
        "team_a",
        "odds_a",
        "team_b",
        "odds_b",
        "market_type",
        "event_url",
    ]
    rows = [_table_row(event) for event in events]
    widths = {
        column: max(len(column), *(len(row[column]) for row in rows))
        for column in columns
    }

    header = " | ".join(column.ljust(widths[column]) for column in columns)
    separator = "-+-".join("-" * widths[column] for column in columns)
    body = [
        " | ".join(row[column].ljust(widths[column]) for column in columns)
        for row in rows
    ]
    return "\n".join([header, separator, *body])


def format_tournament_candidates(provider: OddsPapiProvider) -> str:
    debug = getattr(provider, "last_debug", {}) or {}
    candidates = _debug_samples(debug.get("tournament_candidates"))
    lines = [
        "tournament_candidates:",
        f"request_mode={_debug_value(debug.get('request_mode'))}",
        f"sportId={_debug_value(debug.get('resolved_sport_id'))}",
        f"keywords={_debug_csv(debug.get('tournament_candidate_keywords'))}",
        f"candidate_count={_debug_value(debug.get('candidate_count'))}",
        f"printed_count={_debug_value(debug.get('printed_count'))}",
    ]
    if not candidates:
        lines.append("(no candidates)")
        return "\n".join(lines)

    for index, candidate in enumerate(candidates, 1):
        lines.append(
            f"candidate_{index}: "
            f"id={_debug_value(candidate.get('id'))} "
            f"name={_debug_value(candidate.get('name'))} "
            f"slug={_debug_value(candidate.get('slug'))} "
            f"category={_debug_value(candidate.get('category'))} "
            f"categoryName={_debug_value(candidate.get('categoryName'))} "
            f"categorySlug={_debug_value(candidate.get('categorySlug'))}"
        )
    return "\n".join(lines)


def format_provider_debug(
    args: argparse.Namespace,
    provider: OddsPapiProvider | PolymarketProvider | TheOddsApiProvider,
) -> str:
    if getattr(provider, "name", "") == "polymarket":
        return format_polymarket_provider_debug(args, provider)

    debug = getattr(provider, "last_debug", {}) or {}
    lines = [
        "provider_debug:",
        f"requested_provider={args.provider}",
        f"requested_sport_input={args.sport or ''}",
        f"resolved_sportId={_debug_value(debug.get('resolved_sport_id') or args.sport)}",
        f"bookmaker={_debug_value(debug.get('bookmaker') or args.bookmaker)}",
        f"mapping_profile={_debug_value(debug.get('mapping_profile'))}",
        f"mapping_sources={_debug_csv(debug.get('mapping_sources'))}",
        f"request_mode={_debug_value(debug.get('request_mode'))}",
        f"selection_mode={_debug_value(debug.get('selection_mode'))}",
        f"oddspapi_key_pool_size={_debug_value(debug.get('oddspapi_key_pool_size'))}",
        f"oddspapi_key_index={_debug_value(debug.get('oddspapi_key_index'))}",
        "oddspapi_key_failover_count="
        f"{_debug_value(debug.get('oddspapi_key_failover_count'))}",
        "oddspapi_all_keys_exhausted="
        f"{_debug_value(debug.get('oddspapi_all_keys_exhausted'))}",
        f"tournament_candidate_keywords={_debug_csv(debug.get('tournament_candidate_keywords'))}",
        f"candidate_count={_debug_value(debug.get('candidate_count'))}",
        f"printed_count={_debug_value(debug.get('printed_count'))}",
        f"tournaments_url={_debug_value(debug.get('tournaments_url'))}",
        f"tournaments_count={_debug_value(debug.get('tournaments_count'))}",
    ]

    for index, sample in enumerate(_debug_samples(debug.get("tournament_samples")), 1):
        lines.append(
            "tournament_sample_"
            f"{index}: id={_debug_value(sample.get('id'))} "
            f"name={_debug_value(sample.get('name'))} "
            f"slug={_debug_value(sample.get('slug'))} "
            f"category={_debug_value(sample.get('category'))}"
        )

    for index, sample in enumerate(
        _debug_samples(debug.get("selected_tournament_samples")),
        1,
    ):
        lines.append(
            "selected_tournament_sample_"
            f"{index}: id={_debug_value(sample.get('id'))} "
            f"name={_debug_value(sample.get('name'))} "
            f"slug={_debug_value(sample.get('slug'))} "
            f"category={_debug_value(sample.get('category'))}"
        )

    lines.extend(
        [
            "selected_tournament_ids="
            f"{_debug_csv(debug.get('selected_tournament_ids'))}",
            "odds_by_tournaments_url="
            f"{_debug_value(debug.get('odds_by_tournaments_url'))}",
            f"odds_url={_debug_value(debug.get('odds_url'))}",
            f"raw_odds_root_type={_debug_value(debug.get('raw_odds_root_type'))}",
            "raw_odds_first_item_keys="
            f"{_debug_csv(debug.get('raw_odds_first_item_keys'))}",
            "raw_odds_top_level_fixture_keys="
            f"{_debug_csv(debug.get('raw_odds_top_level_fixture_keys'))}",
        ]
    )

    if debug.get("raw_odds_root_type") == "list":
        lines.append(
            "raw_odds_top_level_count="
            f"{_debug_value(debug.get('raw_odds_top_level_count'))}"
        )
    elif debug.get("raw_odds_root_type") == "dict":
        lines.append(
            "raw_odds_top_level_keys="
            f"{_debug_csv(debug.get('raw_odds_top_level_keys'))}"
        )

    lines.append(
        "normalized_events_count="
        f"{_debug_value(debug.get('normalized_events_count'))}"
    )

    if debug.get("normalized_events_count") == 0:
        lines.extend(_format_raw_odds_zero_sample(debug.get("raw_odds_zero_sample")))

    last_note = debug.get("last_note") or getattr(provider, "last_note", None)
    if last_note:
        lines.append(f"provider_last_note={_debug_value(last_note)}")

    return "\n".join(lines)


def format_polymarket_provider_debug(
    args: argparse.Namespace,
    provider: PolymarketProvider,
) -> str:
    debug = getattr(provider, "last_debug", {}) or {}
    lines = [
        "provider_debug:",
        f"requested_provider={getattr(args, 'provider', 'polymarket')}",
        f"requested_sport_input={getattr(args, 'sport', '') or ''}",
        f"resolved_sport={_debug_value(debug.get('resolved_sport'))}",
        f"market_type={getattr(args, 'market_type', '') or ''}",
        f"search={getattr(args, 'search', '') or ''}",
        f"max_events={_debug_value(debug.get('max_events') or getattr(args, 'max_events', ''))}",
        f"request_mode={_debug_value(debug.get('request_mode'))}",
        f"request_url={_debug_value(debug.get('request_url'))}",
        f"fetched_count={_debug_value(debug.get('fetched_count'))}",
        f"normalized_count={_debug_value(debug.get('normalized_count'))}",
        "reject_reason_counts="
        f"{_debug_counts(debug.get('reject_reason_counts'))}",
    ]

    for index, sample in enumerate(_debug_samples(debug.get("normalized_samples")), 1):
        lines.append(
            "normalized_sample_"
            f"{index}: sport={_debug_value(sample.get('sport'))} "
            f"teams={_debug_value(sample.get('teams'))} "
            f"odds={_debug_value(sample.get('odds'))} "
            f"detected_best_of={_debug_value(sample.get('detected_best_of'))} "
            f"event_id={_debug_value(sample.get('event_id'))} "
            f"market_id={_debug_value(sample.get('market_id'))} "
            f"url={_debug_value(sample.get('url'))}"
        )

    for index, sample in enumerate(_debug_samples(debug.get("rejected_samples")), 1):
        lines.append(
            "rejected_sample_"
            f"{index}: reason={_debug_value(sample.get('reason'))} "
            f"event_id={_debug_value(sample.get('event_id'))} "
            f"market_id={_debug_value(sample.get('market_id'))} "
            f"detected_best_of={_debug_value(sample.get('detected_best_of'))} "
            f"draw_source={_debug_value(sample.get('draw_or_three_way_source'))} "
            f"draw_match={_debug_value(sample.get('draw_or_three_way_matched_text'))} "
            f"unsafe_flags={_debug_value(sample.get('unsafe_flags'))} "
            f"question={_debug_value(sample.get('question'))}"
        )

    last_note = debug.get("last_note") or getattr(provider, "last_note", None)
    if last_note:
        lines.append(f"provider_last_note={_debug_value(last_note)}")

    return "\n".join(lines)


def format_market_debug(
    provider: OddsPapiProvider | PolymarketProvider | TheOddsApiProvider,
) -> str:
    debug = getattr(provider, "last_debug", {}) or {}
    fixtures = _debug_samples(debug.get("market_explorer"))
    lines = ["market_debug:"]
    if not fixtures:
        lines.append("(no market debug)")
        return "\n".join(lines)

    for fixture_index, fixture in enumerate(fixtures[:3], 1):
        lines.append(
            "fixture_"
            f"{fixture_index}: fixtureId={_debug_value(fixture.get('fixtureId'))} "
            f"tournamentId={_debug_value(fixture.get('tournamentId'))} "
            f"tournamentName={_debug_value(fixture.get('tournamentName'))} "
            f"categoryName={_debug_value(fixture.get('categoryName'))} "
            f"startTime={_debug_value(fixture.get('startTime'))} "
            f"trueStartTime={_debug_value(fixture.get('trueStartTime'))}"
        )
        lines.append(
            f"fixture_{fixture_index}_team1: "
            f"id={_debug_value(fixture.get('participant1Id'))} "
            f"name={_debug_value(fixture.get('participant1Name'))} "
            f"short={_debug_value(fixture.get('participant1ShortName'))} "
            f"abbr={_debug_value(fixture.get('participant1Abbr'))}"
        )
        lines.append(
            f"fixture_{fixture_index}_team2: "
            f"id={_debug_value(fixture.get('participant2Id'))} "
            f"name={_debug_value(fixture.get('participant2Name'))} "
            f"short={_debug_value(fixture.get('participant2ShortName'))} "
            f"abbr={_debug_value(fixture.get('participant2Abbr'))}"
        )
        lines.append(
            f"fixture_{fixture_index}_bookmaker: "
            f"path={_debug_value(fixture.get('bookmaker_path'))} "
            f"name={_debug_value(fixture.get('bookmaker_name'))}"
        )

        for market_index, market in enumerate(
            _debug_samples(fixture.get("markets"))[:30],
            1,
        ):
            prefix = f"fixture_{fixture_index}_market_{market_index}"
            lines.append(
                f"{prefix}: path={_debug_value(market.get('market_path'))} "
                f"marketId={_debug_value(market.get('marketId'))} "
                f"key={_debug_value(market.get('key'))} "
                f"bookmakerMarketId={_debug_value(market.get('bookmakerMarketId'))} "
                f"marketActive={_debug_value(market.get('marketActive'))} "
                f"suspended={_debug_value(market.get('suspended'))} "
                f"outcome_count={_debug_value(market.get('outcome_count'))} "
                f"marketName={_debug_value(market.get('marketName'))} "
                f"way_type={_debug_value(market.get('way_type'))} "
                "looks_like_match_winner="
                f"{_debug_value(market.get('looks_like_match_winner'))} "
                f"outcome_names={_debug_csv(market.get('outcome_names'))} "
                f"decimal_odds={_debug_csv(market.get('decimal_odds'))}"
            )
            for outcome_index, outcome in enumerate(
                _debug_samples(market.get("outcomes"))[:4],
                1,
            ):
                lines.append(
                    f"{prefix}_outcome_{outcome_index}: "
                    f"outcome_key={_debug_value(outcome.get('outcome_key'))} "
                    f"selectionId={_debug_value(outcome.get('selectionId'))} "
                    f"name={_debug_value(outcome.get('name'))} "
                    f"players_type={_debug_value(outcome.get('players_type'))} "
                    f"player_key={_debug_value(outcome.get('player_key'))} "
                    f"playerName={_debug_value(outcome.get('playerName'))} "
                    f"participantId={_debug_value(outcome.get('participantId'))} "
                    f"playerSelectionId={_debug_value(outcome.get('player_selectionId'))} "
                    f"bookmakerOutcomeId={_debug_value(outcome.get('bookmakerOutcomeId'))} "
                    f"price={_debug_value(outcome.get('price'))} "
                    f"exchangeMeta={_debug_value(outcome.get('exchangeMeta'))}"
                )
    return "\n".join(lines)


def _debug_samples(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _debug_csv(value: object) -> str:
    if not isinstance(value, list):
        return _debug_value(value)
    return ",".join(_debug_value(item) for item in value)


def _debug_counts(value: object) -> str:
    if not isinstance(value, dict):
        return _debug_value(value)
    return ",".join(
        f"{_debug_value(key)}:{_debug_value(count)}"
        for key, count in value.items()
    )


def _format_raw_odds_zero_sample(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []

    lines = [
        "raw_odds_sample_first_item_keys="
        f"{_debug_csv(value.get('first_item_keys'))}",
    ]
    for key in ("data", "events", "fixtures", "matches"):
        count_key = f"nested_{key}_count"
        if count_key in value:
            lines.append(
                f"raw_odds_sample_nested_{key}_count="
                f"{_debug_value(value.get(count_key))}"
            )
    lines.extend(
        [
            "raw_odds_sample_first_nested_event_keys="
            f"{_debug_csv(value.get('first_nested_event_keys'))}",
            "raw_odds_sample_fixture_participant1="
            f"id={_debug_value(value.get('fixture_participant1_id'))} "
            f"name={_debug_value(value.get('fixture_participant1_name'))} "
            f"short={_debug_value(value.get('fixture_participant1_short'))} "
            f"abbr={_debug_value(value.get('fixture_participant1_abbr'))}",
            "raw_odds_sample_fixture_participant2="
            f"id={_debug_value(value.get('fixture_participant2_id'))} "
            f"name={_debug_value(value.get('fixture_participant2_name'))} "
            f"short={_debug_value(value.get('fixture_participant2_short'))} "
            f"abbr={_debug_value(value.get('fixture_participant2_abbr'))}",
            "raw_odds_sample_bookmaker_odds_type="
            f"{_debug_value(value.get('bookmaker_odds_type'))}",
            "raw_odds_sample_bookmaker_odds_keys="
            f"{_debug_csv(value.get('bookmaker_odds_keys'))}",
            "raw_odds_sample_bookmaker_odds_list_length="
            f"{_debug_value(value.get('bookmaker_odds_list_length'))}",
            "raw_odds_sample_bookmaker_odds_first_item_keys="
            f"{_debug_csv(value.get('bookmaker_odds_first_item_keys'))}",
            "raw_odds_sample_matched_bookmaker_path="
            f"{_debug_value(value.get('matched_bookmaker_path'))}",
            "raw_odds_sample_matched_bookmaker_name="
            f"{_debug_value(value.get('matched_bookmaker_name'))}",
            "raw_odds_sample_bookmaker_keys="
            f"{_debug_csv(value.get('bookmaker_keys'))}",
            "raw_odds_sample_market_container_type="
            f"{_debug_value(value.get('market_container_type'))}",
            "raw_odds_sample_market_keys="
            f"{_debug_csv(value.get('market_keys'))}",
            "raw_odds_sample_market_values="
            f"id={_debug_value(value.get('market_id'))} "
            f"key={_debug_value(value.get('market_key'))} "
            f"bookmakerMarketId={_debug_value(value.get('market_bookmaker_market_id'))} "
            f"marketActive={_debug_value(value.get('market_active'))}",
            "raw_odds_sample_first_outcome_keys="
            f"{_debug_csv(value.get('first_outcome_keys'))}",
            "raw_odds_sample_first_outcome_players_type="
            f"{_debug_value(value.get('first_outcome_players_type'))}",
            "raw_odds_sample_first_outcome_players_count="
            f"{_debug_value(value.get('first_outcome_players_count'))}",
            "raw_odds_sample_first_outcome_players_keys="
            f"{_debug_csv(value.get('first_outcome_players_keys'))}",
            "raw_odds_sample_first_nested_player_keys="
            f"{_debug_csv(value.get('first_nested_player_keys'))}",
            "raw_odds_sample_parsed_outcome_candidate_count="
            f"{_debug_value(value.get('parsed_outcome_candidate_count'))}",
            "raw_odds_sample_parsed_outcome_candidate_reasons="
            f"{_debug_csv(value.get('parsed_outcome_candidate_reasons'))}",
            "raw_odds_sample_direct_fixture_rejection_reason="
            f"{_debug_value(value.get('direct_fixture_rejection_reason'))}",
        ]
    )
    for index, sample in enumerate(
        _debug_samples(value.get("parsed_outcome_candidate_samples")),
        1,
    ):
        lines.append(
            "raw_odds_sample_candidate_"
            f"{index}: name={_debug_value(sample.get('name'))} "
            f"ids={_debug_value(sample.get('ids'))} "
            f"outcome_key={_debug_value(sample.get('outcome_key'))} "
            f"player_key={_debug_value(sample.get('player_key'))} "
            f"bookmakerOutcomeId={_debug_value(sample.get('bookmakerOutcomeId'))} "
            f"participantId={_debug_value(sample.get('participantId'))} "
            f"selectionId={_debug_value(sample.get('selectionId'))} "
            f"price={_debug_value(sample.get('price'))} "
            f"mapped_team={_debug_value(sample.get('mapped_team'))} "
            f"reason={_debug_value(sample.get('reason'))} "
            f"exchangeMeta={_debug_value(sample.get('exchangeMeta'))}"
        )
    return lines


def _debug_value(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _table_row(event: ProviderOddsEvent) -> dict[str, str]:
    return {
        "provider": event.provider,
        "bookmaker": event.bookmaker,
        "game": event.game,
        "tournament": event.tournament or "",
        "start_time": event.start_time.isoformat() if event.start_time else "",
        "team_a": event.team_a,
        "odds_a": f"{event.odds_a:g}",
        "team_b": event.team_b,
        "odds_b": f"{event.odds_b:g}",
        "market_type": event.market_type,
        "event_url": event.event_url or "",
    }


if __name__ == "__main__":
    raise SystemExit(main())
