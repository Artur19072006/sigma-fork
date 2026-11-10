from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import unicodedata
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from provider_odds.arbitrage import ArbitrageConfig, calculate_arbitrage
from provider_odds.cli.arbitrage import format_opportunities_table
from provider_odds.cli.snapshot import format_matches_table
from provider_odds.matcher import canonical_team_name
from provider_odds.models import ProviderOddsEvent
from provider_odds.providers import OddsPapiProvider, PolymarketProvider
from provider_odds.providers.base import ProviderConfigurationError, ProviderRequestError
from provider_odds.providers.oddspapi import (
    MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER,
    SUPPORTED_MAPPING_PROFILES,
    resolve_sport_id,
)
from provider_odds.snapshot import build_snapshot
from provider_odds.telegram import (
    DEFAULT_TELEGRAM_DEDUP_TTL_HOURS,
    DEFAULT_TELEGRAM_REALERT_EDGE_IMPROVEMENT,
    TELEGRAM_BOT_TOKEN_ENV,
    TELEGRAM_CHAT_ID_ENV,
    TelegramConfigurationError,
    TelegramSendError,
    build_arbitrage_alert_messages,
    send_arbitrage_alerts,
)


POLYMARKET_KEYSET_URL = "https://gamma-api.polymarket.com/events/keyset"
DEFAULT_TOURNAMENT_LIST_CACHE_PATH = Path(".cache/oddspapi_tournaments.json")
DEFAULT_ACTIVE_TOURNAMENT_CACHE_PATH = Path(".cache/active_tournament_ids.json")
LOL_TOURNAMENT_SCOPE_CHOICES = ("msi", "all")
DEFAULT_LOL_TOURNAMENT_SCOPE = "all"
DEFAULT_POLYMARKET_KEYSET_EMPTY_TARGET_PAGES_STOP = 3
DOTA2_POLYMARKET_PRESEED_SEARCHES = (
    "Dota 2",
    "Dota2",
    "Dota",
    "European Pro League Dota 2",
    "Esports World Cup Dota 2",
)
DOTA2_POLYMARKET_PAGE_PRESEED_URLS = (
    "https://polymarket.com/esports/dota-2",
    "https://polymarket.com/uk/esports/dota-2",
)
DOTA_TOURNAMENT_POLICY_REASON_LABELS = {
    "ewc_or_draw_risk_tournament": "EWC",
    "bo2_or_ambiguous_format": "BO2",
    "draw_or_three_way": "draw_risk",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manually scan Stake/OddsPapi and Polymarket events for arbitrage."
    )
    parser.add_argument("--sport", required=True)
    parser.add_argument("--bookmaker", default="stake")
    parser.add_argument("--stake-tournament-id", action="append")
    parser.add_argument("--stake-auto-discover-tournaments", action="store_true")
    parser.add_argument("--stake-auto-max-tournaments", type=int, default=10)
    parser.add_argument("--stake-auto-min-score", type=float, default=0.5)
    parser.add_argument("--stake-request-delay-seconds", type=float, default=1.0)
    parser.add_argument("--stake-rate-limit-retries", type=int, default=1)
    parser.add_argument(
        "--stake-mapping-profile",
        choices=sorted(SUPPORTED_MAPPING_PROFILES),
    )
    parser.add_argument(
        "--tournament-list-cache-path",
        type=Path,
        default=DEFAULT_TOURNAMENT_LIST_CACHE_PATH,
    )
    parser.add_argument("--tournament-list-cache-ttl-hours", type=float, default=24)
    parser.add_argument("--refresh-tournament-list-cache", action="store_true")
    parser.add_argument("--disable-tournament-list-cache", action="store_true")
    parser.add_argument(
        "--active-tournament-cache-path",
        type=Path,
        default=DEFAULT_ACTIVE_TOURNAMENT_CACHE_PATH,
    )
    parser.add_argument("--active-tournament-cache-ttl-hours", type=float, default=168)
    parser.add_argument("--disable-active-tournament-cache", action="store_true")
    parser.add_argument("--refresh-active-tournament-cache", action="store_true")
    parser.add_argument(
        "--tournament-resolution-mode",
        choices=["cache-only", "normal", "refresh"],
        default="normal",
    )
    parser.add_argument("--max-selected-tournament-ids-per-sport", type=int, default=10)
    parser.add_argument("--max-discovery-candidates-per-bucket", type=int, default=3)
    parser.add_argument("--max-oddspapi-requests-per-run", type=int, default=8)
    parser.add_argument(
        "--lol-tournament-scope",
        choices=LOL_TOURNAMENT_SCOPE_CHOICES,
        default=DEFAULT_LOL_TOURNAMENT_SCOPE,
        help="Restrict LoL tournament discovery; msi opts back into MSI-only buckets.",
    )
    parser.add_argument("--polymarket-search", action="append")
    parser.add_argument("--polymarket-preseed-search", action="append")
    parser.add_argument("--polymarket-preseed-sport-defaults", action="store_true")
    parser.add_argument("--polymarket-page-preseed-url", action="append")
    parser.add_argument("--polymarket-page-preseed-dota2-defaults", action="store_true")
    parser.add_argument("--polymarket-search-from-stake-teams", action="store_true")
    parser.add_argument("--max-generated-polymarket-searches", type=int, default=30)
    parser.add_argument("--polymarket-page-url", action="append")
    parser.add_argument("--polymarket-keyset-url", action="append")
    parser.add_argument("--polymarket-keyset-tag-id", action="append")
    parser.add_argument("--polymarket-keyset-window-hours", type=float, default=48)
    parser.add_argument("--polymarket-keyset-limit", type=int, default=100)
    parser.add_argument("--polymarket-keyset-locale", default="en")
    parser.add_argument("--polymarket-keyset-max-pages", type=int, default=3)
    parser.add_argument("--polymarket-keyset-adaptive", action="store_true")
    parser.add_argument(
        "--polymarket-keyset-empty-target-pages-stop",
        type=int,
        default=DEFAULT_POLYMARKET_KEYSET_EMPTY_TARGET_PAGES_STOP,
    )
    parser.add_argument("--debug-polymarket-candidates", action="store_true")
    parser.add_argument("--polymarket-diagnostic-output", type=Path)
    parser.add_argument("--max-polymarket-events", type=int, default=100)
    parser.add_argument(
        "--min-tournament-quality",
        choices=["s", "a", "b", "c", "unknown"],
        default="b",
    )
    parser.add_argument("--allow-unknown-tournaments", action="store_true")
    parser.add_argument("--include-low-tier-tournaments", action="store_true")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--format", choices=["table", "json"], default="table")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--max-start-time-diff-minutes", type=int, default=180)
    parser.add_argument("--min-score", type=float, default=0.82)
    parser.add_argument("--find-arbitrage", action="store_true")
    parser.add_argument("--polymarket-fee-rate", type=float, default=0.03)
    parser.add_argument("--telegram-alert", action="store_true")
    parser.add_argument("--telegram-dry-run", action="store_true")
    parser.add_argument(
        "--telegram-dedup-ttl-hours",
        type=float,
        default=DEFAULT_TELEGRAM_DEDUP_TTL_HOURS,
    )
    parser.add_argument(
        "--telegram-realert-edge-improvement",
        type=float,
        default=DEFAULT_TELEGRAM_REALERT_EDGE_IMPROVEMENT,
    )
    parser.add_argument("--telegram-bot-token-env", default=TELEGRAM_BOT_TOKEN_ENV)
    parser.add_argument("--telegram-chat-id-env", default=TELEGRAM_CHAT_ID_ENV)
    args = parser.parse_args(argv)

    if args.debug_polymarket_candidates:
        if args.telegram_alert or args.telegram_dry_run:
            parser.error(
                "--debug-polymarket-candidates cannot be combined with Telegram flags"
            )
        try:
            result = run_polymarket_candidate_diagnostic(
                sport=args.sport,
                polymarket_keyset_url=args.polymarket_keyset_url,
                polymarket_keyset_tag_id=args.polymarket_keyset_tag_id,
                polymarket_keyset_window_hours=args.polymarket_keyset_window_hours,
                polymarket_keyset_limit=args.polymarket_keyset_limit,
                polymarket_keyset_locale=args.polymarket_keyset_locale,
                polymarket_keyset_max_pages=args.polymarket_keyset_max_pages,
                polymarket_keyset_adaptive=args.polymarket_keyset_adaptive,
                polymarket_keyset_empty_target_pages_stop=(
                    args.polymarket_keyset_empty_target_pages_stop
                ),
                max_polymarket_events=args.max_polymarket_events,
                lol_tournament_scope=args.lol_tournament_scope,
                min_tournament_quality=args.min_tournament_quality,
                allow_unknown_tournaments=args.allow_unknown_tournaments,
                include_low_tier_tournaments=args.include_low_tier_tournaments,
                env_file=args.env_file,
            )
        except (OSError, ValueError, ProviderConfigurationError, ProviderRequestError) as exc:
            print(str(exc), file=sys.stderr)
            return 2

        if args.polymarket_diagnostic_output:
            args.polymarket_diagnostic_output.write_text(
                json.dumps(result, indent=2),
                encoding="utf-8",
            )
        if args.format == "json":
            print(json.dumps(result, indent=2))
        else:
            print(format_polymarket_candidate_diagnostic(result))
        return 0

    if (args.telegram_alert or args.telegram_dry_run) and not args.find_arbitrage:
        parser.error("--telegram-alert/--telegram-dry-run requires --find-arbitrage")
    if not args.stake_tournament_id and not args.stake_auto_discover_tournaments:
        parser.error(
            "--stake-tournament-id is required unless "
            "--stake-auto-discover-tournaments is used"
        )

    try:
        result = run_scan(
            sport=args.sport,
            bookmaker=args.bookmaker,
            stake_tournament_id=_joined_values(args.stake_tournament_id),
            stake_auto_discover_tournaments=args.stake_auto_discover_tournaments,
            stake_auto_max_tournaments=args.stake_auto_max_tournaments,
            stake_auto_min_score=args.stake_auto_min_score,
            stake_request_delay_seconds=args.stake_request_delay_seconds,
            stake_rate_limit_retries=args.stake_rate_limit_retries,
            stake_mapping_profile=args.stake_mapping_profile,
            tournament_list_cache_path=args.tournament_list_cache_path,
            tournament_list_cache_ttl_hours=args.tournament_list_cache_ttl_hours,
            refresh_tournament_list_cache=args.refresh_tournament_list_cache,
            disable_tournament_list_cache=args.disable_tournament_list_cache,
            active_tournament_cache_path=args.active_tournament_cache_path,
            active_tournament_cache_ttl_hours=args.active_tournament_cache_ttl_hours,
            disable_active_tournament_cache=args.disable_active_tournament_cache,
            refresh_active_tournament_cache=args.refresh_active_tournament_cache,
            tournament_resolution_mode=args.tournament_resolution_mode,
            max_selected_tournament_ids_per_sport=(
                args.max_selected_tournament_ids_per_sport
            ),
            max_discovery_candidates_per_bucket=(
                args.max_discovery_candidates_per_bucket
            ),
            max_oddspapi_requests_per_run=args.max_oddspapi_requests_per_run,
            lol_tournament_scope=args.lol_tournament_scope,
            polymarket_search=args.polymarket_search,
            polymarket_preseed_search=args.polymarket_preseed_search,
            polymarket_preseed_sport_defaults=(
                args.polymarket_preseed_sport_defaults
            ),
            polymarket_page_preseed_url=args.polymarket_page_preseed_url,
            polymarket_page_preseed_dota2_defaults=(
                args.polymarket_page_preseed_dota2_defaults
            ),
            polymarket_search_from_stake_teams=(
                args.polymarket_search_from_stake_teams
            ),
            max_generated_polymarket_searches=(
                args.max_generated_polymarket_searches
            ),
            polymarket_page_url=args.polymarket_page_url,
            polymarket_keyset_url=args.polymarket_keyset_url,
            polymarket_keyset_tag_id=args.polymarket_keyset_tag_id,
            polymarket_keyset_window_hours=args.polymarket_keyset_window_hours,
            polymarket_keyset_limit=args.polymarket_keyset_limit,
            polymarket_keyset_locale=args.polymarket_keyset_locale,
            polymarket_keyset_max_pages=args.polymarket_keyset_max_pages,
            polymarket_keyset_adaptive=args.polymarket_keyset_adaptive,
            polymarket_keyset_empty_target_pages_stop=(
                args.polymarket_keyset_empty_target_pages_stop
            ),
            max_polymarket_events=args.max_polymarket_events,
            min_tournament_quality=args.min_tournament_quality,
            allow_unknown_tournaments=args.allow_unknown_tournaments,
            include_low_tier_tournaments=args.include_low_tier_tournaments,
            env_file=args.env_file,
            max_start_time_diff_minutes=args.max_start_time_diff_minutes,
            min_score=args.min_score,
            find_arbitrage=args.find_arbitrage,
            polymarket_fee_rate=args.polymarket_fee_rate,
            telegram_alert=args.telegram_alert,
            telegram_dry_run=args.telegram_dry_run,
            telegram_dedup_ttl_hours=args.telegram_dedup_ttl_hours,
            telegram_realert_edge_improvement=(
                args.telegram_realert_edge_improvement
            ),
            telegram_bot_token_env=args.telegram_bot_token_env,
            telegram_chat_id_env=args.telegram_chat_id_env,
        )
    except (
        OSError,
        ValueError,
        ProviderConfigurationError,
        ProviderRequestError,
        TelegramConfigurationError,
        TelegramSendError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        print(format_scan_table(result, debug=args.debug))
    return 0


def run_scan(
    *,
    sport: str,
    bookmaker: str,
    stake_tournament_id: str | None = None,
    stake_auto_discover_tournaments: bool = False,
    stake_auto_max_tournaments: int = 10,
    stake_auto_min_score: float = 0.5,
    stake_request_delay_seconds: float = 1.0,
    stake_rate_limit_retries: int = 1,
    stake_mapping_profile: str | None = None,
    tournament_list_cache_path: Path = DEFAULT_TOURNAMENT_LIST_CACHE_PATH,
    tournament_list_cache_ttl_hours: float = 24,
    refresh_tournament_list_cache: bool = False,
    disable_tournament_list_cache: bool = False,
    active_tournament_cache_path: Path = DEFAULT_ACTIVE_TOURNAMENT_CACHE_PATH,
    active_tournament_cache_ttl_hours: float = 168,
    disable_active_tournament_cache: bool = False,
    refresh_active_tournament_cache: bool = False,
    tournament_resolution_mode: str = "normal",
    max_selected_tournament_ids_per_sport: int = 10,
    max_discovery_candidates_per_bucket: int = 3,
    max_oddspapi_requests_per_run: int = 8,
    lol_tournament_scope: str = DEFAULT_LOL_TOURNAMENT_SCOPE,
    polymarket_search: list[str] | None = None,
    polymarket_preseed_search: list[str] | None = None,
    polymarket_preseed_sport_defaults: bool = False,
    polymarket_page_preseed_url: list[str] | None = None,
    polymarket_page_preseed_dota2_defaults: bool = False,
    polymarket_search_from_stake_teams: bool = False,
    max_generated_polymarket_searches: int = 30,
    polymarket_page_url: list[str] | None = None,
    polymarket_keyset_url: list[str] | None = None,
    polymarket_keyset_tag_id: list[str] | None = None,
    polymarket_keyset_window_hours: float = 48,
    polymarket_keyset_limit: int = 100,
    polymarket_keyset_locale: str = "en",
    polymarket_keyset_max_pages: int = 3,
    polymarket_keyset_adaptive: bool = False,
    polymarket_keyset_empty_target_pages_stop: int = (
        DEFAULT_POLYMARKET_KEYSET_EMPTY_TARGET_PAGES_STOP
    ),
    max_polymarket_events: int = 100,
    min_tournament_quality: str = "b",
    allow_unknown_tournaments: bool = False,
    include_low_tier_tournaments: bool = False,
    env_file: Path = Path(".env"),
    max_start_time_diff_minutes: int = 180,
    min_score: float = 0.82,
    find_arbitrage: bool = False,
    polymarket_fee_rate: float = 0.03,
    telegram_alert: bool = False,
    telegram_dry_run: bool = False,
    telegram_dedup_ttl_hours: float = DEFAULT_TELEGRAM_DEDUP_TTL_HOURS,
    telegram_realert_edge_improvement: float = (
        DEFAULT_TELEGRAM_REALERT_EDGE_IMPROVEMENT
    ),
    telegram_bot_token_env: str = TELEGRAM_BOT_TOKEN_ENV,
    telegram_chat_id_env: str = TELEGRAM_CHAT_ID_ENV,
    providers: dict[str, object] | None = None,
    page_fetcher: Callable[[str], str] | None = None,
    keyset_fetcher: Callable[[str], object] | None = None,
    stake_sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if (telegram_alert or telegram_dry_run) and not find_arbitrage:
        raise ValueError("--telegram-alert/--telegram-dry-run requires --find-arbitrage")

    stake_provider = _provider("oddspapi", env_file, providers)
    polymarket_provider = _provider("polymarket", env_file, providers)
    lol_scope = _normalize_lol_tournament_scope(lol_tournament_scope, sport=sport)
    stake_mapping_profile = _default_stake_mapping_profile(
        sport=sport,
        bookmaker=bookmaker,
        mapping_profile=stake_mapping_profile,
    )
    user_searches = _explicit_search_values(polymarket_search)
    preseed_searches = _polymarket_preseed_searches(
        sport=sport,
        explicit_searches=polymarket_preseed_search,
        include_sport_defaults=polymarket_preseed_sport_defaults,
    )
    page_preseed_urls = _polymarket_page_preseed_urls(
        sport=sport,
        explicit_urls=polymarket_page_preseed_url,
        include_dota2_defaults=polymarket_page_preseed_dota2_defaults,
    )
    keyset_discovery = discover_polymarket_keyset_events(
        polymarket_provider,
        sport=sport,
        keyset_urls=_dedupe_searches(polymarket_keyset_url or []),
        tag_ids=_explicit_search_values(polymarket_keyset_tag_id),
        window_hours=polymarket_keyset_window_hours,
        limit=polymarket_keyset_limit,
        locale=polymarket_keyset_locale,
        max_pages=polymarket_keyset_max_pages,
        adaptive=polymarket_keyset_adaptive,
        empty_target_pages_stop=polymarket_keyset_empty_target_pages_stop,
        max_events=max_polymarket_events,
        fetcher=keyset_fetcher,
    )
    scoped_keyset_discovery = _filter_lol_tournament_scope_events(
        keyset_discovery["events"],
        sport=sport,
        scope=lol_scope,
    )
    page_preseed_discovery = discover_polymarket_page_preseed_events(
        polymarket_provider,
        sport=sport,
        page_urls=page_preseed_urls,
        max_events=max_polymarket_events,
        fetcher=page_fetcher,
    )
    preseed_discovery = discover_polymarket_preseed_events(
        polymarket_provider,
        sport=sport,
        searches=preseed_searches,
        max_events=max_polymarket_events,
    )
    seed_events_before_quality = _dedupe_events(
        [
            *scoped_keyset_discovery["events"],
            *page_preseed_discovery["events"],
            *preseed_discovery["events"],
        ]
    )
    tournament_quality = filter_polymarket_events_by_tournament_quality(
        seed_events_before_quality,
        sport=sport,
        min_quality=min_tournament_quality,
        allow_unknown=allow_unknown_tournaments,
        include_low_tier=include_low_tier_tournaments,
    )
    seed_events = tournament_quality["events"]
    tournament_bucket_sources = _polymarket_tournament_bucket_sources(
        keyset_events=scoped_keyset_discovery["events"],
        page_events=page_preseed_discovery["events"],
        search_events=preseed_discovery["events"],
        kept_events=seed_events,
    )
    dota_tournament_policy_debug = _dota_tournament_policy_debug(
        sport=sport,
        keyset_discovery=keyset_discovery,
        page_preseed_discovery=page_preseed_discovery,
        preseed_discovery=preseed_discovery,
    )
    manual_tournament_ids = _split_values(stake_tournament_id)
    auto_discovery = discover_stake_tournaments_from_polymarket(
        stake_provider,
        sport=sport,
        bookmaker=bookmaker,
        polymarket_events=seed_events,
        enabled=stake_auto_discover_tournaments,
        max_tournaments=stake_auto_max_tournaments,
        min_score=stake_auto_min_score,
        mapping_profile=stake_mapping_profile,
        max_start_time_diff_minutes=max_start_time_diff_minutes,
        match_min_score=min_score,
        request_delay_seconds=stake_request_delay_seconds,
        rate_limit_retries=stake_rate_limit_retries,
        sleeper=stake_sleep,
        tournament_list_cache_path=tournament_list_cache_path,
        tournament_list_cache_ttl_hours=tournament_list_cache_ttl_hours,
        refresh_tournament_list_cache=refresh_tournament_list_cache,
        disable_tournament_list_cache=disable_tournament_list_cache,
        active_tournament_cache_path=active_tournament_cache_path,
        active_tournament_cache_ttl_hours=active_tournament_cache_ttl_hours,
        disable_active_tournament_cache=disable_active_tournament_cache,
        refresh_active_tournament_cache=refresh_active_tournament_cache,
        tournament_resolution_mode=tournament_resolution_mode,
        max_selected_tournament_ids_per_sport=max_selected_tournament_ids_per_sport,
        max_discovery_candidates_per_bucket=max_discovery_candidates_per_bucket,
        max_oddspapi_requests_per_run=max_oddspapi_requests_per_run,
    )
    auto_stake_events = [
        event
        for event in auto_discovery.get("events", [])
        if isinstance(event, ProviderOddsEvent)
    ]
    auto_event_tournament_ids = _text_list(auto_discovery.get("event_tournament_ids"))
    auto_selected_ids = _text_list(auto_discovery.get("selected_ids"))
    tournament_ids = _dedupe_searches(
        [*manual_tournament_ids, *auto_selected_ids]
    )
    selected_limit = max(int(max_selected_tournament_ids_per_sport), 0)
    if selected_limit:
        tournament_ids = tournament_ids[:selected_limit]
    else:
        tournament_ids = []
    if not tournament_ids and not stake_auto_discover_tournaments:
        raise ProviderConfigurationError(
            "Missing --stake-tournament-id; use --stake-auto-discover-tournaments "
            "to discover Stake/OddsPapi tournaments from Polymarket"
        )
    stake_fetch_ids = [
        item for item in tournament_ids if item not in set(auto_event_tournament_ids)
    ]
    stake_fetch = _fetch_stake_events(
        stake_provider,
        sport=sport,
        bookmaker=bookmaker,
        tournament_ids=stake_fetch_ids,
        mapping_profile=stake_mapping_profile,
        request_delay_seconds=stake_request_delay_seconds,
        rate_limit_retries=stake_rate_limit_retries,
        sleeper=stake_sleep,
    )
    stake_events = _dedupe_events([*auto_stake_events, *stake_fetch["events"]])
    stake_requested_tournament_ids = _dedupe_searches(
        [
            *_text_list(auto_discovery.get("requested_tournament_ids")),
            *stake_fetch["requested_tournament_ids"],
        ]
    )
    stake_skipped_tournament_ids = _dedupe_searches(
        [
            *_text_list(auto_discovery.get("skipped_tournament_ids")),
            *stake_fetch["skipped_tournament_ids"],
        ]
    )
    stake_skipped_tournament_reasons = {
        **_text_mapping(auto_discovery.get("skipped_tournament_reasons")),
        **stake_fetch["skipped_tournament_reasons"],
    }
    stake_rate_limit_count = (
        _int_value(auto_discovery.get("rate_limit_count"))
        + stake_fetch["rate_limit_count"]
    )
    stake_error_count = (
        _int_value(auto_discovery.get("error_count")) + stake_fetch["error_count"]
    )
    stake_stopped_reason = (
        auto_discovery.get("stopped_reason") or stake_fetch.get("stopped_reason") or ""
    )
    oddspapi_key_debug = _oddspapi_key_debug(stake_provider)
    generated_searches = (
        generate_polymarket_team_searches(
            stake_events,
            max_searches=max_generated_polymarket_searches,
        )
        if polymarket_search_from_stake_teams
        else []
    )
    polymarket_searches = _dedupe_searches([*user_searches, *generated_searches])
    if not polymarket_searches:
        polymarket_searches = (
            []
            if _sport_filter_key(sport) == "dota2"
            and page_preseed_discovery["enabled"]
            else [sport]
        )
    page_discovery = discover_polymarket_page_slugs(
        _dedupe_searches(polymarket_page_url or []),
        sport=sport,
        stake_events=stake_events,
        fetcher=page_fetcher,
    )

    polymarket_events_before_scope = _dedupe_events(
        [
            *_fetch_polymarket_events(
                polymarket_provider,
                sport=sport,
                searches=polymarket_searches,
                event_slugs=page_discovery["event_slugs"],
                market_slugs=page_discovery["market_slugs"],
                max_events=max_polymarket_events,
            ),
            *seed_events,
        ]
    )
    scoped_polymarket_events = _filter_lol_tournament_scope_events(
        polymarket_events_before_scope,
        sport=sport,
        scope=lol_scope,
    )
    polymarket_events = scoped_polymarket_events["events"]

    snapshot = build_snapshot(
        stake_events,
        polymarket_events,
        max_start_time_diff_minutes=max_start_time_diff_minutes,
        min_score=min_score,
    )
    matches = _events(snapshot.get("matches"))
    active_cache_failures = _mark_active_tournament_cache_failures(
        active_tournament_cache_path=active_tournament_cache_path,
        enabled=(
            stake_auto_discover_tournaments
            and not disable_active_tournament_cache
            and auto_discovery.get("active_tournament_cache_enabled")
        ),
        sport=sport,
        bookmaker=bookmaker,
        active_cache_hits=_text_list(
            auto_discovery.get("active_tournament_cache_hits")
        ),
        requested_tournament_ids=stake_fetch["requested_tournament_ids"],
        skipped_tournament_reasons=stake_skipped_tournament_reasons,
        stake_events=stake_events,
        matches=matches,
    )
    result: dict[str, Any] = {
        "summary": {
            "stake_count": len(snapshot["left_events"]),
            "stake_auto_discovery_enabled": stake_auto_discover_tournaments,
            "stake_tournament_candidate_count": auto_discovery["candidate_count"],
            "stake_tournament_selected_count": len(tournament_ids),
            "stake_tournament_requested_count": len(stake_requested_tournament_ids),
            "stake_tournament_rejected_count": auto_discovery["rejected_count"],
            "stake_rate_limit_count": stake_rate_limit_count,
            "stake_requested_tournament_count": len(stake_requested_tournament_ids),
            "stake_skipped_tournament_count": len(stake_skipped_tournament_ids),
            "stake_error_count": stake_error_count,
            "polymarket_count": len(snapshot["right_events"]),
            "matched_count": len(matches),
            "opportunity_count": 0,
            "telegram_sent_count": 0,
        },
        "stake_events": snapshot["left_events"],
        "polymarket_events": snapshot["right_events"],
        "matches": matches,
        "debug": {
            "lol_tournament_scope": lol_scope,
            "stake_requested_tournament_count": len(stake_requested_tournament_ids),
            "stake_auto_discovery_enabled": stake_auto_discover_tournaments,
            "polymarket_price_sample": _polymarket_price_debug_sample(
                snapshot["right_events"],
            ),
            "polymarket_tournament_hints_sample": auto_discovery[
                "polymarket_tournament_hints_sample"
            ][:5],
            "stake_tournament_candidate_count": auto_discovery["candidate_count"],
            "stake_tournament_selected_count": len(tournament_ids),
            "stake_tournament_requested_count": len(stake_requested_tournament_ids),
            "stake_tournament_raw_candidate_sample": auto_discovery[
                "raw_candidate_sample"
            ][:10],
            "stake_tournament_attempted_ids": auto_discovery["attempted_ids"],
            "stake_tournament_attempted_names": auto_discovery["attempted_names"],
            "stake_tournament_attempt_results": auto_discovery["attempt_results"][:10],
            "stake_tournament_team_match_sample": auto_discovery[
                "team_match_sample"
            ][:10],
            "stake_tournament_known_51042_seen": auto_discovery[
                "known_51042_seen"
            ],
            "stake_tournament_known_51042_name": auto_discovery[
                "known_51042_name"
            ],
            "stake_tournament_known_51042_rank": auto_discovery[
                "known_51042_rank"
            ],
            "polymarket_tournament_bucket_count": tournament_quality[
                "bucket_count"
            ],
            "polymarket_tournament_buckets_sample": tournament_quality[
                "buckets_sample"
            ][:10],
            "polymarket_tournament_bucket_sources": tournament_bucket_sources,
            "tournament_quality_kept_count": tournament_quality["kept_count"],
            "tournament_quality_rejected_count": tournament_quality[
                "rejected_count"
            ],
            "tournament_quality_kept_sample": tournament_quality["kept_sample"][:10],
            "tournament_quality_rejected_sample": tournament_quality[
                "rejected_sample"
            ][:10],
            "tournament_quality_by_bucket": tournament_quality["by_bucket"],
            "rejected_tournament_reasons": _rejected_tournament_reasons(
                tournament_quality["by_bucket"]
            ),
            "stake_tournament_selected_ids": tournament_ids,
            "stake_tournament_selected_names": auto_discovery["selected_names"],
            "stake_tournament_rejected_count": auto_discovery["rejected_count"],
            "stake_tournament_selection_sample": auto_discovery[
                "selection_sample"
            ][:5],
            "stake_tournament_verified_match_count": auto_discovery[
                "verified_match_count"
            ],
            "stake_candidate_count_by_bucket": auto_discovery[
                "candidate_count_by_bucket"
            ],
            "stake_candidate_sample_by_bucket": auto_discovery[
                "candidate_sample_by_bucket"
            ],
            "stake_candidate_score_breakdown_by_bucket": auto_discovery[
                "candidate_score_breakdown_by_bucket"
            ],
            "stake_candidate_core_token_match_by_bucket": auto_discovery[
                "candidate_core_token_match_by_bucket"
            ],
            "stake_candidate_generic_token_match_by_bucket": auto_discovery[
                "candidate_generic_token_match_by_bucket"
            ],
            "stake_candidate_fetch_keywords_by_bucket": auto_discovery[
                "candidate_fetch_keywords_by_bucket"
            ],
            "polymarket_team_pairs_by_bucket": auto_discovery[
                "polymarket_team_pairs_by_bucket"
            ],
            "polymarket_team_pair_count_by_bucket": auto_discovery[
                "polymarket_team_pair_count_by_bucket"
            ],
            "lol_bucket_team_sample": auto_discovery["lol_bucket_team_sample"],
            "lol_candidate_count": auto_discovery["lol_candidate_count"],
            "lol_candidate_score_breakdown": auto_discovery[
                "lol_candidate_score_breakdown"
            ],
            "lol_candidate_fetch_keywords": auto_discovery[
                "lol_candidate_fetch_keywords"
            ],
            "lol_verified_team_overlap": auto_discovery["lol_verified_team_overlap"],
            "rejected_lol_candidate_reasons": auto_discovery[
                "rejected_lol_candidate_reasons"
            ],
            "stake_attempted_ids_by_bucket": auto_discovery[
                "attempted_ids_by_bucket"
            ],
            "stake_attempt_results_by_bucket": auto_discovery[
                "attempt_results_by_bucket"
            ],
            "stake_selected_ids_by_bucket": auto_discovery[
                "selected_ids_by_bucket"
            ],
            "stake_missing_bucket_names": auto_discovery["missing_bucket_names"],
            "stake_rate_limit_count": stake_rate_limit_count,
            "stake_auto_stop_reason": auto_discovery.get("stopped_reason") or "",
            "stake_stopped_reason": stake_stopped_reason,
            "stake_skipped_tournament_count": len(stake_skipped_tournament_ids),
            "stake_skipped_tournament_ids": stake_skipped_tournament_ids,
            "stake_skipped_tournament_reasons": stake_skipped_tournament_reasons,
            "stake_error_count": stake_error_count,
            "oddspapi_tournament_list_cache_hit": auto_discovery[
                "oddspapi_tournament_list_cache_hit"
            ],
            "oddspapi_tournament_list_request_count": auto_discovery[
                "oddspapi_tournament_list_request_count"
            ],
            "active_tournament_cache_enabled": auto_discovery[
                "active_tournament_cache_enabled"
            ],
            "active_tournament_cache_hit_count": auto_discovery[
                "active_tournament_cache_hit_count"
            ],
            "active_tournament_cache_miss_count": auto_discovery[
                "active_tournament_cache_miss_count"
            ],
            "active_tournament_cache_hits": auto_discovery[
                "active_tournament_cache_hits"
            ],
            "active_tournament_cache_misses": auto_discovery[
                "active_tournament_cache_misses"
            ],
            "active_tournament_cache_updated": (
                auto_discovery["active_tournament_cache_updated"]
                or bool(active_cache_failures)
            ),
            "active_tournament_cache_failures": active_cache_failures,
            "selected_tournament_ids_by_bucket": auto_discovery[
                "selected_tournament_ids_by_bucket"
            ],
            "unresolved_tournament_buckets": auto_discovery[
                "unresolved_tournament_buckets"
            ],
            "rejected_tournament_candidates_by_bucket": auto_discovery[
                "rejected_tournament_candidates_by_bucket"
            ],
            "oddspapi_batch_tournament_ids": _dedupe_searches(
                [
                    *_text_list(auto_discovery.get("oddspapi_batch_tournament_ids")),
                    *_text_list(stake_fetch.get("batch_tournament_ids")),
                ]
            ),
            "oddspapi_batch_request_count": (
                _int_value(auto_discovery.get("oddspapi_batch_request_count"))
                + _int_value(stake_fetch.get("batch_request_count"))
            ),
            "oddspapi_total_request_count": (
                _int_value(auto_discovery.get("oddspapi_request_count"))
                + _int_value(stake_fetch.get("request_count"))
            ),
            "oddspapi_key_pool_size": oddspapi_key_debug["pool_size"],
            "oddspapi_key_index": oddspapi_key_debug["key_index"],
            "oddspapi_key_failover_count": oddspapi_key_debug["failover_count"],
            "oddspapi_all_keys_exhausted": oddspapi_key_debug["all_keys_exhausted"],
            "discovery_skipped_due_to_mode": auto_discovery[
                "discovery_skipped_due_to_mode"
            ],
            "discovery_skipped_due_to_budget": auto_discovery[
                "discovery_skipped_due_to_budget"
            ],
            "polymarket_user_search_count": len(user_searches),
            "polymarket_generated_search_count": len(generated_searches),
            "polymarket_generated_searches_sample": generated_searches[:5],
            "polymarket_preseed_enabled": preseed_discovery["enabled"],
            "polymarket_preseed_searches": preseed_discovery["searches"],
            "polymarket_preseed_event_count_raw": preseed_discovery[
                "event_count_raw"
            ],
            "polymarket_preseed_event_count_after_safety_filter": (
                preseed_discovery["event_count_after_safety_filter"]
            ),
            "polymarket_preseed_event_count_after_sport_filter": (
                preseed_discovery["event_count_after_sport_filter"]
            ),
            "polymarket_preseed_event_urls_sample": preseed_discovery[
                "event_urls_sample"
            ][:5],
            "polymarket_preseed_reject_reason_counts": preseed_discovery[
                "reject_reason_counts"
            ],
            "polymarket_preseed_rejected_samples": preseed_discovery[
                "rejected_samples"
            ][:10],
            "polymarket_page_preseed_enabled": page_preseed_discovery["enabled"],
            "polymarket_page_preseed_urls": page_preseed_discovery["page_urls"],
            "polymarket_page_preseed_candidate_slug_count": len(
                page_preseed_discovery["candidate_slugs"]
            ),
            "polymarket_page_preseed_candidate_slugs_sample": (
                page_preseed_discovery["candidate_slugs"][:5]
            ),
            "polymarket_page_preseed_event_count_raw": (
                page_preseed_discovery["event_count_raw"]
            ),
            "polymarket_page_preseed_event_count_after_safety_filter": (
                page_preseed_discovery["event_count_after_safety_filter"]
            ),
            "polymarket_page_preseed_event_count_after_sport_filter": (
                page_preseed_discovery["event_count_after_sport_filter"]
            ),
            "polymarket_page_preseed_event_urls_sample": (
                page_preseed_discovery["event_urls_sample"][:5]
            ),
            "polymarket_page_preseed_reject_reason_counts": (
                page_preseed_discovery["reject_reason_counts"]
            ),
            "polymarket_page_preseed_rejected_samples": (
                page_preseed_discovery["rejected_samples"][:10]
            ),
            "polymarket_preseed_sport_filter_rejected_count": (
                preseed_discovery["sport_filter_rejected_count"]
            ),
            "polymarket_preseed_sport_filter_rejected_sample": (
                preseed_discovery["sport_filter_rejected_sample"][:5]
            ),
            "polymarket_page_url_count": len(page_discovery["page_urls"]),
            "polymarket_page_candidate_slug_count_raw": len(
                page_discovery["raw_candidate_slugs"]
            ),
            "polymarket_page_candidate_slug_count": len(
                page_discovery["candidate_slugs"]
            ),
            "polymarket_page_candidate_slug_count_kept": len(
                page_discovery["candidate_slugs"]
            ),
            "polymarket_page_candidate_slugs_sample": page_discovery[
                "candidate_slugs"
            ][:5],
            "polymarket_page_rejected_slug_count": len(
                page_discovery["rejected_slugs"]
            ),
            "polymarket_page_rejected_slugs_sample": page_discovery[
                "rejected_slugs"
            ][:5],
            "polymarket_page_fetch_error_count": page_discovery[
                "fetch_error_count"
            ],
            "polymarket_page_no_candidate_count": page_discovery[
                "no_candidate_count"
            ],
            "polymarket_keyset_url_count": len(keyset_discovery["urls"]),
            "polymarket_keyset_page_count": keyset_discovery["page_count"],
            "polymarket_keyset_adaptive_enabled": keyset_discovery[
                "adaptive_enabled"
            ],
            "polymarket_keyset_adaptive_pages_fetched": keyset_discovery[
                "adaptive_pages_fetched"
            ],
            "polymarket_keyset_adaptive_stop_reason": keyset_discovery[
                "adaptive_stop_reason"
            ],
            "polymarket_keyset_empty_target_pages_streak": keyset_discovery[
                "empty_target_pages_streak"
            ],
            "polymarket_keyset_page_details": keyset_discovery["page_details"][:10],
            "polymarket_keyset_event_count_raw": keyset_discovery[
                "event_count_raw"
            ],
            "polymarket_keyset_normalized_count": len(keyset_discovery["events"]),
            "polymarket_keyset_scope_kept_count": len(
                scoped_keyset_discovery["events"]
            ),
            "polymarket_keyset_scope_rejected_count": len(
                scoped_keyset_discovery["rejected_events"]
            ),
            "polymarket_keyset_scope_rejected_sample": scoped_keyset_discovery[
                "rejected_sample"
            ][:5],
            "polymarket_keyset_event_count_before_sport_filter": keyset_discovery[
                "event_count_before_sport_filter"
            ],
            "polymarket_keyset_event_count_after_sport_filter": keyset_discovery[
                "event_count_after_sport_filter"
            ],
            "polymarket_keyset_sport_filter_rejected_count": keyset_discovery[
                "sport_filter_rejected_count"
            ],
            "polymarket_keyset_sport_filter_rejected_sample": keyset_discovery[
                "sport_filter_rejected_sample"
            ][:5],
            "polymarket_keyset_sport_filter_kept_sample": keyset_discovery[
                "sport_filter_kept_sample"
            ][:5],
            "polymarket_keyset_rejected_count": keyset_discovery["rejected_count"],
            "polymarket_keyset_sample_slugs": keyset_discovery["sample_slugs"][:5],
            "polymarket_keyset_next_cursor_seen": keyset_discovery[
                "next_cursor_seen"
            ],
            "polymarket_lol_scope_rejected_count": len(
                scoped_polymarket_events["rejected_events"]
            ),
            "polymarket_lol_scope_rejected_sample": scoped_polymarket_events[
                "rejected_sample"
            ][:5],
        },
    }
    if dota_tournament_policy_debug:
        result["debug"].update(dota_tournament_policy_debug)

    if find_arbitrage:
        arbitrage = calculate_arbitrage(
            matches,
            config=ArbitrageConfig(polymarket_fee_rate=polymarket_fee_rate),
        )
        result["arbitrage"] = {
            "summary": arbitrage["summary"],
            "opportunities": arbitrage["opportunities"],
            "debug": arbitrage["debug"],
        }
        result["debug"]["opportunity_price_debug"] = (
            _opportunity_price_debug_sample(
                arbitrage["opportunities"],
                matches,
            )
        )
        result["summary"]["opportunity_count"] = arbitrage["summary"][
            "opportunity_count"
        ]

    if telegram_alert or telegram_dry_run:
        arbitrage = result.get("arbitrage")
        opportunities = (
            _events(arbitrage.get("opportunities"))
            if isinstance(arbitrage, dict)
            else []
        )
        telegram = send_arbitrage_alerts(
            opportunities,
            dry_run=telegram_dry_run,
            env_file=env_file,
            bot_token_env=telegram_bot_token_env,
            chat_id_env=telegram_chat_id_env,
            dedup_ttl_hours=telegram_dedup_ttl_hours,
            realert_edge_improvement=telegram_realert_edge_improvement,
        )
        result["telegram"] = telegram
        result["summary"]["telegram_sent_count"] = telegram["sent_count"]

    if _sport_filter_key(sport) == "lol":
        debug = result.get("debug")
        if isinstance(debug, dict):
            debug.update(
                {
                    "stake_events_sample": _event_debug_sample(
                        snapshot.get("left_events"),
                    ),
                    "polymarket_events_sample": _event_debug_sample(
                        snapshot.get("right_events"),
                    ),
                    "unmatched_stake_sample": _event_debug_sample(
                        snapshot.get("unmatched_left"),
                    ),
                    "unmatched_polymarket_sample": _event_debug_sample(
                        snapshot.get("unmatched_right"),
                    ),
                }
            )

    return result


def run_polymarket_candidate_diagnostic(
    *,
    sport: str,
    polymarket_keyset_url: list[str] | None = None,
    polymarket_keyset_tag_id: list[str] | None = None,
    polymarket_keyset_window_hours: float = 48,
    polymarket_keyset_limit: int = 100,
    polymarket_keyset_locale: str = "en",
    polymarket_keyset_max_pages: int = 3,
    polymarket_keyset_adaptive: bool = False,
    polymarket_keyset_empty_target_pages_stop: int = (
        DEFAULT_POLYMARKET_KEYSET_EMPTY_TARGET_PAGES_STOP
    ),
    max_polymarket_events: int = 100,
    lol_tournament_scope: str = DEFAULT_LOL_TOURNAMENT_SCOPE,
    min_tournament_quality: str = "b",
    allow_unknown_tournaments: bool = False,
    include_low_tier_tournaments: bool = False,
    env_file: Path = Path(".env"),
    providers: dict[str, object] | None = None,
    keyset_fetcher: Callable[[str], object] | None = None,
) -> dict[str, Any]:
    polymarket_provider = _provider("polymarket", env_file, providers)
    lol_scope = _normalize_lol_tournament_scope(lol_tournament_scope, sport=sport)
    keyset_discovery = discover_polymarket_keyset_events(
        polymarket_provider,
        sport=sport,
        keyset_urls=_dedupe_searches(polymarket_keyset_url or []),
        tag_ids=_explicit_search_values(polymarket_keyset_tag_id),
        window_hours=polymarket_keyset_window_hours,
        limit=polymarket_keyset_limit,
        locale=polymarket_keyset_locale,
        max_pages=polymarket_keyset_max_pages,
        adaptive=polymarket_keyset_adaptive,
        empty_target_pages_stop=polymarket_keyset_empty_target_pages_stop,
        max_events=max_polymarket_events,
        fetcher=keyset_fetcher,
    )
    scoped_keyset_discovery = _filter_lol_tournament_scope_events(
        keyset_discovery["events"],
        sport=sport,
        scope=lol_scope,
    )
    tournament_quality = filter_polymarket_events_by_tournament_quality(
        scoped_keyset_discovery["events"],
        sport=sport,
        min_quality=min_tournament_quality,
        allow_unknown=allow_unknown_tournaments,
        include_low_tier=include_low_tier_tournaments,
    )
    kept_events = tournament_quality["events"]

    rejection_counts = Counter()
    rejection_counts.update(
        _int_counter(keyset_discovery.get("normalize_reject_reason_counts"))
    )
    if keyset_discovery["sport_filter_rejected_count"]:
        rejection_counts["sport_filter_mismatch"] += keyset_discovery[
            "sport_filter_rejected_count"
        ]
    if scoped_keyset_discovery["rejected_events"]:
        rejection_counts[f"lol_scope_{lol_scope}_rejected"] += len(
            scoped_keyset_discovery["rejected_events"]
        )
    for record in tournament_quality["by_bucket"].values():
        if not isinstance(record, dict) or bool(record.get("kept")):
            continue
        reason = _text(record.get("reason")) or "tournament_quality_rejected"
        rejection_counts[reason] += _int_value(record.get("count"))

    raw_samples = _dict_rows(keyset_discovery.get("raw_event_samples"))
    raw_msi_samples = [
        sample for sample in raw_samples if _raw_polymarket_sample_has_msi(sample)
    ]
    rejected_samples = [
        *_dict_rows(keyset_discovery.get("normalize_rejected_samples")),
        *_dict_rows(keyset_discovery.get("sport_filter_rejected_details")),
        *_polymarket_event_diagnostic_samples(
            scoped_keyset_discovery["rejected_events"],
            reason=f"lol_scope_{lol_scope}_rejected",
        ),
        *_polymarket_event_diagnostic_samples(
            tournament_quality.get("rejected_events"),
            bucket_records=tournament_quality["by_bucket"],
        ),
    ]

    summary = {
        "sport": sport,
        "lol_tournament_scope": lol_scope,
        "raw_event_count": keyset_discovery["event_count_raw"],
        "normalized_event_count": keyset_discovery[
            "event_count_before_sport_filter"
        ],
        "sport_filtered_event_count": keyset_discovery[
            "event_count_after_sport_filter"
        ],
        "scope_kept_event_count": len(scoped_keyset_discovery["events"]),
        "kept_event_count": len(kept_events),
        "rejected_event_count": sum(rejection_counts.values()),
        "raw_msi_signal_count": len(raw_msi_samples),
        "keyset_page_count": keyset_discovery["page_count"],
        "keyset_raw_event_count_by_page": keyset_discovery[
            "raw_event_count_by_page"
        ],
        "keyset_adaptive_enabled": keyset_discovery["adaptive_enabled"],
        "keyset_adaptive_pages_fetched": keyset_discovery[
            "adaptive_pages_fetched"
        ],
        "keyset_adaptive_stop_reason": keyset_discovery["adaptive_stop_reason"],
        "keyset_empty_target_pages_streak": keyset_discovery[
            "empty_target_pages_streak"
        ],
        "keyset_next_cursor_seen": keyset_discovery["next_cursor_seen"],
        "keyset_max_pages_stopped_early": keyset_discovery[
            "max_pages_stopped_early"
        ],
    }
    return {
        "summary": summary,
        "filters": {
            "sport": sport,
            "lol_tournament_scope": lol_scope,
            "min_tournament_quality": min_tournament_quality,
            "allow_unknown_tournaments": allow_unknown_tournaments,
            "include_low_tier_tournaments": include_low_tier_tournaments,
            "max_polymarket_events": max_polymarket_events,
        },
        "keyset": {
            "urls": keyset_discovery["urls"],
            "tag_ids_used": keyset_discovery["tag_ids_used"],
            "page_count": keyset_discovery["page_count"],
            "raw_event_count_by_page": keyset_discovery[
                "raw_event_count_by_page"
            ],
            "adaptive_enabled": keyset_discovery["adaptive_enabled"],
            "adaptive_pages_fetched": keyset_discovery["adaptive_pages_fetched"],
            "adaptive_stop_reason": keyset_discovery["adaptive_stop_reason"],
            "empty_target_pages_streak": keyset_discovery[
                "empty_target_pages_streak"
            ],
            "page_details": keyset_discovery["page_details"],
            "next_cursor_seen": keyset_discovery["next_cursor_seen"],
            "next_cursors": keyset_discovery["next_cursors"],
            "max_pages_stopped_early": keyset_discovery[
                "max_pages_stopped_early"
            ],
            "sample_slugs": keyset_discovery["sample_slugs"][:10],
        },
        "rejected_reason_counts": dict(sorted(rejection_counts.items())),
        "raw_event_samples": raw_samples[:10],
        "raw_msi_signal_samples": raw_msi_samples[:10],
        "kept_event_samples": _polymarket_event_diagnostic_samples(kept_events),
        "rejected_event_samples": rejected_samples[:10],
        "tournament_quality_by_bucket": tournament_quality["by_bucket"],
    }


def format_polymarket_candidate_diagnostic(result: dict[str, Any]) -> str:
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    keyset = result.get("keyset") if isinstance(result.get("keyset"), dict) else {}
    lines = [
        "polymarket candidate diagnostic:",
        f"sport={_text(summary.get('sport'))}",
        f"lol_tournament_scope={_text(summary.get('lol_tournament_scope'))}",
        f"keyset_tag_ids_used={_csv(keyset.get('tag_ids_used'))}",
        f"keyset_page_count={_text(summary.get('keyset_page_count') or 0)}",
        "keyset_raw_event_count_by_page="
        f"{_csv(summary.get('keyset_raw_event_count_by_page'))}",
        f"raw_event_count={_text(summary.get('raw_event_count') or 0)}",
        f"normalized_event_count={_text(summary.get('normalized_event_count') or 0)}",
        "sport_filtered_event_count="
        f"{_text(summary.get('sport_filtered_event_count') or 0)}",
        f"scope_kept_event_count={_text(summary.get('scope_kept_event_count') or 0)}",
        f"kept_event_count={_text(summary.get('kept_event_count') or 0)}",
        f"rejected_event_count={_text(summary.get('rejected_event_count') or 0)}",
        "rejected_reason_counts="
        f"{_mapping_csv(result.get('rejected_reason_counts'))}",
        f"raw_msi_signal_count={_text(summary.get('raw_msi_signal_count') or 0)}",
        "keyset_next_cursor_seen="
        f"{_bool(summary.get('keyset_next_cursor_seen'))}",
        "keyset_max_pages_stopped_early="
        f"{_bool(summary.get('keyset_max_pages_stopped_early'))}",
        "keyset_adaptive_enabled="
        f"{_bool(summary.get('keyset_adaptive_enabled'))}",
        "keyset_adaptive_pages_fetched="
        f"{_text(summary.get('keyset_adaptive_pages_fetched') or 0)}",
        "keyset_adaptive_stop_reason="
        f"{_text(summary.get('keyset_adaptive_stop_reason'))}",
        "keyset_empty_target_pages_streak="
        f"{_text(summary.get('keyset_empty_target_pages_streak') or 0)}",
    ]
    for page in _dict_rows(keyset.get("page_details"))[:10]:
        lines.append(
            "keyset_page_"
            f"{_text(page.get('page'))}: raw={_text(page.get('raw_event_count') or 0)} "
            f"normalized={_text(page.get('normalized_count') or 0)} "
            f"sport_kept={_text(page.get('sport_kept_count') or 0)} "
            f"new_target_markets={_text(page.get('new_target_markets') or 0)} "
            "empty_target_pages_streak="
            f"{_text(page.get('empty_target_pages_streak') or 0)} "
            f"rejected={_text(page.get('rejected_count') or 0)} "
            f"next_cursor={_text(page.get('next_cursor'))}"
        )

    lines.append("")
    lines.append("kept_event_samples:")
    kept_samples = _dict_rows(result.get("kept_event_samples"))
    if kept_samples:
        lines.extend(_format_polymarket_diagnostic_samples(kept_samples))
    else:
        lines.append("(none)")

    lines.append("")
    lines.append("rejected_event_samples:")
    rejected_samples = _dict_rows(result.get("rejected_event_samples"))
    if rejected_samples:
        lines.extend(_format_polymarket_diagnostic_samples(rejected_samples))
    else:
        lines.append("(none)")

    lines.append("")
    lines.append("raw_msi_signal_samples:")
    raw_msi_samples = _dict_rows(result.get("raw_msi_signal_samples"))
    if raw_msi_samples:
        lines.extend(_format_polymarket_diagnostic_samples(raw_msi_samples))
    else:
        lines.append("(none)")
    return "\n".join(lines)


def format_scan_table(result: dict[str, Any], *, debug: bool = False) -> str:
    lines = [format_scan_summary(result)]
    matches = _events(result.get("matches"))
    if matches:
        lines.append("")
        lines.append(format_matches_table(matches))
    else:
        lines.append("")
        lines.append("(no matches)")

    arbitrage = result.get("arbitrage")
    if isinstance(arbitrage, dict):
        lines.append("")
        lines.extend(_format_arbitrage(arbitrage))

    telegram = result.get("telegram")
    if isinstance(telegram, dict):
        lines.append("")
        lines.extend(_format_telegram(result, telegram))

    if debug:
        debug_payload = result.get("debug")
        debug_data = debug_payload if isinstance(debug_payload, dict) else {}
        lines.append("")
        lines.append("debug:")
        lines.append(
            "lol_tournament_scope="
            f"{_text(debug_data.get('lol_tournament_scope'))}"
        )
        lines.append(
            "stake_requested_tournament_count="
            f"{_text(debug_data.get('stake_requested_tournament_count') or 0)}"
        )
        lines.append(
            "stake_auto_discovery_enabled="
            f"{_bool(debug_data.get('stake_auto_discovery_enabled'))}"
        )
        lines.append(
            "polymarket_price_sample="
            f"{_csv(debug_data.get('polymarket_price_sample'))}"
        )
        if "opportunity_price_debug" in debug_data:
            lines.append(
                "opportunity_price_debug="
                f"{_csv(debug_data.get('opportunity_price_debug'))}"
            )
        lines.append(
            "polymarket_tournament_hints_sample="
            f"{_csv(debug_data.get('polymarket_tournament_hints_sample'))}"
        )
        lines.append(
            "stake_tournament_candidate_count="
            f"{_text(debug_data.get('stake_tournament_candidate_count') or 0)}"
        )
        lines.append(
            "stake_tournament_selected_count="
            f"{_text(debug_data.get('stake_tournament_selected_count') or 0)}"
        )
        lines.append(
            "stake_tournament_requested_count="
            f"{_text(debug_data.get('stake_tournament_requested_count') or 0)}"
        )
        lines.append(
            "stake_tournament_raw_candidate_sample="
            f"{_candidate_sample_csv(debug_data.get('stake_tournament_raw_candidate_sample'))}"
        )
        lines.append(
            "stake_tournament_attempted_ids="
            f"{_csv(debug_data.get('stake_tournament_attempted_ids'))}"
        )
        lines.append(
            "stake_tournament_attempted_names="
            f"{_csv(debug_data.get('stake_tournament_attempted_names'))}"
        )
        lines.append(
            "stake_tournament_attempt_results="
            f"{_attempt_results_csv(debug_data.get('stake_tournament_attempt_results'))}"
        )
        lines.append(
            "stake_tournament_team_match_sample="
            f"{_team_match_sample_csv(debug_data.get('stake_tournament_team_match_sample'))}"
        )
        lines.append(
            "stake_tournament_known_51042_seen="
            f"{_bool(debug_data.get('stake_tournament_known_51042_seen'))}"
        )
        lines.append(
            "stake_tournament_known_51042_name="
            f"{_text(debug_data.get('stake_tournament_known_51042_name'))}"
        )
        lines.append(
            "stake_tournament_known_51042_rank="
            f"{_text(debug_data.get('stake_tournament_known_51042_rank') or 0)}"
        )
        lines.append(
            "polymarket_tournament_bucket_count="
            f"{_text(debug_data.get('polymarket_tournament_bucket_count') or 0)}"
        )
        lines.append(
            "polymarket_tournament_buckets_sample="
            f"{_csv(debug_data.get('polymarket_tournament_buckets_sample'))}"
        )
        lines.append(
            "polymarket_tournament_bucket_sources="
            f"{_mapping_csv(debug_data.get('polymarket_tournament_bucket_sources'))}"
        )
        lines.append(
            "tournament_quality_kept_count="
            f"{_text(debug_data.get('tournament_quality_kept_count') or 0)}"
        )
        lines.append(
            "tournament_quality_rejected_count="
            f"{_text(debug_data.get('tournament_quality_rejected_count') or 0)}"
        )
        lines.append(
            "tournament_quality_kept_sample="
            f"{_csv(debug_data.get('tournament_quality_kept_sample'))}"
        )
        lines.append(
            "tournament_quality_rejected_sample="
            f"{_csv(debug_data.get('tournament_quality_rejected_sample'))}"
        )
        lines.append(
            "tournament_quality_by_bucket="
            f"{_bucket_mapping_csv(debug_data.get('tournament_quality_by_bucket'))}"
        )
        lines.append(
            "rejected_tournament_reasons="
            f"{_mapping_csv(debug_data.get('rejected_tournament_reasons'))}"
        )
        if "dota_tournament_policy_rejected_count" in debug_data:
            lines.append(
                "dota_tournament_policy_rejected_count="
                f"{_text(debug_data.get('dota_tournament_policy_rejected_count') or 0)}"
            )
            lines.append(
                "dota_tournament_policy_rejected_sample="
                f"{_polymarket_rejected_samples_csv(debug_data.get('dota_tournament_policy_rejected_sample'))}"
            )
            lines.append(
                "dota_unsafe_tournament_reasons="
                f"{_mapping_csv(debug_data.get('dota_unsafe_tournament_reasons'))}"
            )
        lines.append(
            "stake_tournament_selected_ids="
            f"{_csv(debug_data.get('stake_tournament_selected_ids'))}"
        )
        lines.append(
            "stake_tournament_selected_names="
            f"{_csv(debug_data.get('stake_tournament_selected_names'))}"
        )
        lines.append(
            "stake_tournament_rejected_count="
            f"{_text(debug_data.get('stake_tournament_rejected_count') or 0)}"
        )
        lines.append(
            "stake_tournament_selection_sample="
            f"{_selection_sample_csv(debug_data.get('stake_tournament_selection_sample'))}"
        )
        lines.append(
            "stake_tournament_verified_match_count="
            f"{_mapping_csv(debug_data.get('stake_tournament_verified_match_count'))}"
        )
        lines.append(
            "stake_candidate_count_by_bucket="
            f"{_mapping_csv(debug_data.get('stake_candidate_count_by_bucket'))}"
        )
        lines.append(
            "stake_candidate_sample_by_bucket="
            f"{_nested_candidate_sample_csv(debug_data.get('stake_candidate_sample_by_bucket'))}"
        )
        lines.append(
            "stake_candidate_score_breakdown_by_bucket="
            f"{_nested_candidate_score_breakdown_csv(debug_data.get('stake_candidate_score_breakdown_by_bucket'))}"
        )
        lines.append(
            "stake_candidate_core_token_match_by_bucket="
            f"{_nested_candidate_core_match_csv(debug_data.get('stake_candidate_core_token_match_by_bucket'))}"
        )
        lines.append(
            "stake_candidate_generic_token_match_by_bucket="
            f"{_nested_candidate_generic_match_csv(debug_data.get('stake_candidate_generic_token_match_by_bucket'))}"
        )
        lines.append(
            "stake_candidate_fetch_keywords_by_bucket="
            f"{_bucket_list_mapping_csv(debug_data.get('stake_candidate_fetch_keywords_by_bucket'))}"
        )
        lines.append(
            "polymarket_team_pairs_by_bucket="
            f"{_bucket_list_mapping_csv(debug_data.get('polymarket_team_pairs_by_bucket'))}"
        )
        lines.append(
            "polymarket_team_pair_count_by_bucket="
            f"{_mapping_csv(debug_data.get('polymarket_team_pair_count_by_bucket'))}"
        )
        lines.append(
            "lol_bucket_team_sample="
            f"{_bucket_list_mapping_csv(debug_data.get('lol_bucket_team_sample'))}"
        )
        lines.append(
            "lol_candidate_count="
            f"{_mapping_csv(debug_data.get('lol_candidate_count'))}"
        )
        lines.append(
            "lol_candidate_score_breakdown="
            f"{_nested_candidate_score_breakdown_csv(debug_data.get('lol_candidate_score_breakdown'))}"
        )
        lines.append(
            "lol_candidate_fetch_keywords="
            f"{_bucket_list_mapping_csv(debug_data.get('lol_candidate_fetch_keywords'))}"
        )
        lines.append(
            "lol_verified_team_overlap="
            f"{_nested_attempt_results_csv(debug_data.get('lol_verified_team_overlap'))}"
        )
        lines.append(
            "rejected_lol_candidate_reasons="
            f"{_nested_candidate_score_breakdown_csv(debug_data.get('rejected_lol_candidate_reasons'))}"
        )
        lines.append(
            "stake_attempted_ids_by_bucket="
            f"{_bucket_list_mapping_csv(debug_data.get('stake_attempted_ids_by_bucket'))}"
        )
        lines.append(
            "stake_attempt_results_by_bucket="
            f"{_nested_attempt_results_csv(debug_data.get('stake_attempt_results_by_bucket'))}"
        )
        lines.append(
            "stake_selected_ids_by_bucket="
            f"{_bucket_list_mapping_csv(debug_data.get('stake_selected_ids_by_bucket'))}"
        )
        lines.append(
            "stake_missing_bucket_names="
            f"{_csv(debug_data.get('stake_missing_bucket_names'))}"
        )
        lines.append(
            "stake_rate_limit_count="
            f"{_text(debug_data.get('stake_rate_limit_count') or 0)}"
        )
        lines.append(
            "stake_auto_stop_reason="
            f"{_text(debug_data.get('stake_auto_stop_reason'))}"
        )
        lines.append(
            "stake_stopped_reason="
            f"{_text(debug_data.get('stake_stopped_reason'))}"
        )
        lines.append(
            "stake_skipped_tournament_count="
            f"{_text(debug_data.get('stake_skipped_tournament_count') or 0)}"
        )
        lines.append(
            "stake_skipped_tournament_ids="
            f"{_csv(debug_data.get('stake_skipped_tournament_ids'))}"
        )
        lines.append(
            "stake_skipped_tournament_reasons="
            f"{_mapping_csv(debug_data.get('stake_skipped_tournament_reasons'))}"
        )
        lines.append(
            "stake_error_count="
            f"{_text(debug_data.get('stake_error_count') or 0)}"
        )
        lines.append(
            "oddspapi_tournament_list_cache_hit="
            f"{_bool(debug_data.get('oddspapi_tournament_list_cache_hit'))}"
        )
        lines.append(
            "oddspapi_tournament_list_request_count="
            f"{_text(debug_data.get('oddspapi_tournament_list_request_count') or 0)}"
        )
        lines.append(
            "active_tournament_cache_enabled="
            f"{_bool(debug_data.get('active_tournament_cache_enabled'))}"
        )
        lines.append(
            "active_tournament_cache_hit_count="
            f"{_text(debug_data.get('active_tournament_cache_hit_count') or 0)}"
        )
        lines.append(
            "active_tournament_cache_miss_count="
            f"{_text(debug_data.get('active_tournament_cache_miss_count') or 0)}"
        )
        lines.append(
            "active_tournament_cache_hits="
            f"{_csv(debug_data.get('active_tournament_cache_hits'))}"
        )
        lines.append(
            "active_tournament_cache_misses="
            f"{_csv(debug_data.get('active_tournament_cache_misses'))}"
        )
        lines.append(
            "active_tournament_cache_updated="
            f"{_bool(debug_data.get('active_tournament_cache_updated'))}"
        )
        lines.append(
            "active_tournament_cache_failures="
            f"{_active_cache_failures_csv(debug_data.get('active_tournament_cache_failures'))}"
        )
        lines.append(
            "selected_tournament_ids_by_bucket="
            f"{_bucket_list_mapping_csv(debug_data.get('selected_tournament_ids_by_bucket'))}"
        )
        lines.append(
            "unresolved_tournament_buckets="
            f"{_csv(debug_data.get('unresolved_tournament_buckets'))}"
        )
        lines.append(
            "rejected_tournament_candidates_by_bucket="
            f"{_nested_candidate_score_breakdown_csv(debug_data.get('rejected_tournament_candidates_by_bucket'))}"
        )
        lines.append(
            "oddspapi_batch_tournament_ids="
            f"{_csv(debug_data.get('oddspapi_batch_tournament_ids'))}"
        )
        lines.append(
            "oddspapi_batch_request_count="
            f"{_text(debug_data.get('oddspapi_batch_request_count') or 0)}"
        )
        lines.append(
            "oddspapi_total_request_count="
            f"{_text(debug_data.get('oddspapi_total_request_count') or 0)}"
        )
        lines.append(
            "oddspapi_key_pool_size="
            f"{_text(debug_data.get('oddspapi_key_pool_size') or 0)}"
        )
        lines.append(
            "oddspapi_key_index="
            f"{_text(debug_data.get('oddspapi_key_index'))}"
        )
        lines.append(
            "oddspapi_key_failover_count="
            f"{_text(debug_data.get('oddspapi_key_failover_count') or 0)}"
        )
        lines.append(
            "oddspapi_all_keys_exhausted="
            f"{_bool(debug_data.get('oddspapi_all_keys_exhausted'))}"
        )
        lines.append(
            "discovery_skipped_due_to_mode="
            f"{_csv(debug_data.get('discovery_skipped_due_to_mode'))}"
        )
        lines.append(
            "discovery_skipped_due_to_budget="
            f"{_csv(debug_data.get('discovery_skipped_due_to_budget'))}"
        )
        lines.append(
            "polymarket_user_search_count="
            f"{_text(debug_data.get('polymarket_user_search_count') or 0)}"
        )
        lines.append(
            "polymarket_generated_search_count="
            f"{_text(debug_data.get('polymarket_generated_search_count') or 0)}"
        )
        lines.append(
            "polymarket_generated_searches_sample="
            f"{_csv(debug_data.get('polymarket_generated_searches_sample'))}"
        )
        lines.append(
            "polymarket_preseed_enabled="
            f"{_bool(debug_data.get('polymarket_preseed_enabled'))}"
        )
        lines.append(
            "polymarket_preseed_searches="
            f"{_csv(debug_data.get('polymarket_preseed_searches'))}"
        )
        lines.append(
            "polymarket_preseed_event_count_raw="
            f"{_text(debug_data.get('polymarket_preseed_event_count_raw') or 0)}"
        )
        lines.append(
            "polymarket_preseed_event_count_after_safety_filter="
            f"{_text(debug_data.get('polymarket_preseed_event_count_after_safety_filter') or 0)}"
        )
        lines.append(
            "polymarket_preseed_event_count_after_sport_filter="
            f"{_text(debug_data.get('polymarket_preseed_event_count_after_sport_filter') or 0)}"
        )
        lines.append(
            "polymarket_preseed_event_urls_sample="
            f"{_csv(debug_data.get('polymarket_preseed_event_urls_sample'))}"
        )
        lines.append(
            "polymarket_preseed_reject_reason_counts="
            f"{_mapping_csv(debug_data.get('polymarket_preseed_reject_reason_counts'))}"
        )
        lines.append(
            "polymarket_preseed_rejected_samples="
            f"{_polymarket_rejected_samples_csv(debug_data.get('polymarket_preseed_rejected_samples'))}"
        )
        lines.append(
            "polymarket_page_preseed_enabled="
            f"{_bool(debug_data.get('polymarket_page_preseed_enabled'))}"
        )
        lines.append(
            "polymarket_page_preseed_urls="
            f"{_csv(debug_data.get('polymarket_page_preseed_urls'))}"
        )
        lines.append(
            "polymarket_page_preseed_candidate_slug_count="
            f"{_text(debug_data.get('polymarket_page_preseed_candidate_slug_count') or 0)}"
        )
        lines.append(
            "polymarket_page_preseed_candidate_slugs_sample="
            f"{_csv(debug_data.get('polymarket_page_preseed_candidate_slugs_sample'))}"
        )
        lines.append(
            "polymarket_page_preseed_event_count_raw="
            f"{_text(debug_data.get('polymarket_page_preseed_event_count_raw') or 0)}"
        )
        lines.append(
            "polymarket_page_preseed_event_count_after_safety_filter="
            f"{_text(debug_data.get('polymarket_page_preseed_event_count_after_safety_filter') or 0)}"
        )
        lines.append(
            "polymarket_page_preseed_event_count_after_sport_filter="
            f"{_text(debug_data.get('polymarket_page_preseed_event_count_after_sport_filter') or 0)}"
        )
        lines.append(
            "polymarket_page_preseed_event_urls_sample="
            f"{_csv(debug_data.get('polymarket_page_preseed_event_urls_sample'))}"
        )
        lines.append(
            "polymarket_page_preseed_reject_reason_counts="
            f"{_mapping_csv(debug_data.get('polymarket_page_preseed_reject_reason_counts'))}"
        )
        lines.append(
            "polymarket_page_preseed_rejected_samples="
            f"{_polymarket_rejected_samples_csv(debug_data.get('polymarket_page_preseed_rejected_samples'))}"
        )
        lines.append(
            "polymarket_preseed_sport_filter_rejected_count="
            f"{_text(debug_data.get('polymarket_preseed_sport_filter_rejected_count') or 0)}"
        )
        lines.append(
            "polymarket_preseed_sport_filter_rejected_sample="
            f"{_csv(debug_data.get('polymarket_preseed_sport_filter_rejected_sample'))}"
        )
        lines.append(
            "polymarket_page_url_count="
            f"{_text(debug_data.get('polymarket_page_url_count') or 0)}"
        )
        lines.append(
            "polymarket_page_candidate_slug_count="
            f"{_text(debug_data.get('polymarket_page_candidate_slug_count') or 0)}"
        )
        lines.append(
            "polymarket_page_candidate_slug_count_raw="
            f"{_text(debug_data.get('polymarket_page_candidate_slug_count_raw') or 0)}"
        )
        lines.append(
            "polymarket_page_candidate_slug_count_kept="
            f"{_text(debug_data.get('polymarket_page_candidate_slug_count_kept') or 0)}"
        )
        lines.append(
            "polymarket_page_candidate_slugs_sample="
            f"{_csv(debug_data.get('polymarket_page_candidate_slugs_sample'))}"
        )
        lines.append(
            "polymarket_page_rejected_slug_count="
            f"{_text(debug_data.get('polymarket_page_rejected_slug_count') or 0)}"
        )
        lines.append(
            "polymarket_page_rejected_slugs_sample="
            f"{_csv(debug_data.get('polymarket_page_rejected_slugs_sample'))}"
        )
        lines.append(
            "polymarket_page_fetch_error_count="
            f"{_text(debug_data.get('polymarket_page_fetch_error_count') or 0)}"
        )
        lines.append(
            "polymarket_page_no_candidate_count="
            f"{_text(debug_data.get('polymarket_page_no_candidate_count') or 0)}"
        )
        lines.append(
            "polymarket_keyset_url_count="
            f"{_text(debug_data.get('polymarket_keyset_url_count') or 0)}"
        )
        lines.append(
            "polymarket_keyset_page_count="
            f"{_text(debug_data.get('polymarket_keyset_page_count') or 0)}"
        )
        lines.append(
            "polymarket_keyset_adaptive_enabled="
            f"{_bool(debug_data.get('polymarket_keyset_adaptive_enabled'))}"
        )
        lines.append(
            "polymarket_keyset_adaptive_pages_fetched="
            f"{_text(debug_data.get('polymarket_keyset_adaptive_pages_fetched') or 0)}"
        )
        lines.append(
            "polymarket_keyset_adaptive_stop_reason="
            f"{_text(debug_data.get('polymarket_keyset_adaptive_stop_reason'))}"
        )
        lines.append(
            "polymarket_keyset_empty_target_pages_streak="
            f"{_text(debug_data.get('polymarket_keyset_empty_target_pages_streak') or 0)}"
        )
        for page in _dict_rows(debug_data.get("polymarket_keyset_page_details"))[:10]:
            lines.append(
                "polymarket_keyset_page "
                f"page={_text(page.get('page'))} "
                f"raw={_text(page.get('raw_event_count') or 0)} "
                f"sport_kept={_text(page.get('sport_kept_count') or 0)} "
                f"new_target_markets={_text(page.get('new_target_markets') or 0)} "
                "empty_target_pages_streak="
                f"{_text(page.get('empty_target_pages_streak') or 0)}"
            )
        lines.append(
            "polymarket_keyset_event_count_raw="
            f"{_text(debug_data.get('polymarket_keyset_event_count_raw') or 0)}"
        )
        lines.append(
            "polymarket_keyset_normalized_count="
            f"{_text(debug_data.get('polymarket_keyset_normalized_count') or 0)}"
        )
        lines.append(
            "polymarket_keyset_scope_kept_count="
            f"{_text(debug_data.get('polymarket_keyset_scope_kept_count') or 0)}"
        )
        lines.append(
            "polymarket_keyset_scope_rejected_count="
            f"{_text(debug_data.get('polymarket_keyset_scope_rejected_count') or 0)}"
        )
        lines.append(
            "polymarket_keyset_scope_rejected_sample="
            f"{_csv(debug_data.get('polymarket_keyset_scope_rejected_sample'))}"
        )
        lines.append(
            "polymarket_keyset_event_count_before_sport_filter="
            f"{_text(debug_data.get('polymarket_keyset_event_count_before_sport_filter') or 0)}"
        )
        lines.append(
            "polymarket_keyset_event_count_after_sport_filter="
            f"{_text(debug_data.get('polymarket_keyset_event_count_after_sport_filter') or 0)}"
        )
        lines.append(
            "polymarket_keyset_sport_filter_rejected_count="
            f"{_text(debug_data.get('polymarket_keyset_sport_filter_rejected_count') or 0)}"
        )
        lines.append(
            "polymarket_keyset_sport_filter_rejected_sample="
            f"{_csv(debug_data.get('polymarket_keyset_sport_filter_rejected_sample'))}"
        )
        lines.append(
            "polymarket_keyset_sport_filter_kept_sample="
            f"{_csv(debug_data.get('polymarket_keyset_sport_filter_kept_sample'))}"
        )
        lines.append(
            "polymarket_keyset_rejected_count="
            f"{_text(debug_data.get('polymarket_keyset_rejected_count') or 0)}"
        )
        lines.append(
            "polymarket_keyset_sample_slugs="
            f"{_csv(debug_data.get('polymarket_keyset_sample_slugs'))}"
        )
        lines.append(
            "polymarket_keyset_next_cursor_seen="
            f"{_bool(debug_data.get('polymarket_keyset_next_cursor_seen'))}"
        )
        lines.append(
            "polymarket_lol_scope_rejected_count="
            f"{_text(debug_data.get('polymarket_lol_scope_rejected_count') or 0)}"
        )
        lines.append(
            "polymarket_lol_scope_rejected_sample="
            f"{_csv(debug_data.get('polymarket_lol_scope_rejected_sample'))}"
        )
        if any(
            key in debug_data
            for key in (
                "stake_events_sample",
                "polymarket_events_sample",
                "unmatched_stake_sample",
                "unmatched_polymarket_sample",
            )
        ):
            lines.append(
                "stake_events_sample="
                f"{_csv(debug_data.get('stake_events_sample'))}"
            )
            lines.append(
                "polymarket_events_sample="
                f"{_csv(debug_data.get('polymarket_events_sample'))}"
            )
            lines.append(
                "unmatched_stake_sample="
                f"{_csv(debug_data.get('unmatched_stake_sample'))}"
            )
            lines.append(
                "unmatched_polymarket_sample="
                f"{_csv(debug_data.get('unmatched_polymarket_sample'))}"
            )
        lines.append(
            f"polymarket_count={_text(result.get('summary', {}).get('polymarket_count') or 0)}"
        )
        lines.append(f"stake_event_urls={_event_urls(result.get('stake_events'))}")
        lines.append(
            f"polymarket_event_urls={_event_urls(result.get('polymarket_events'))}"
        )

    return "\n".join(lines)


def generate_polymarket_team_searches(
    stake_events: list[ProviderOddsEvent],
    *,
    max_searches: int = 30,
) -> list[str]:
    if max_searches < 1:
        return []

    searches: list[str] = []
    seen: set[str] = set()
    for event in stake_events:
        team_a = _text(event.team_a)
        team_b = _text(event.team_b)
        if not team_a or not team_b:
            continue

        variants = [
            _team_pair_query(team_a, team_b),
            _team_pair_query(_safe_team_search_variant(team_a), team_b),
            _team_pair_query(team_a, _safe_team_search_variant(team_b)),
            _team_pair_query(
                _safe_team_search_variant(team_a),
                _safe_team_search_variant(team_b),
            ),
        ]
        for query in variants:
            key = query.casefold()
            if not query or key in seen:
                continue
            seen.add(key)
            searches.append(query)
            if len(searches) >= max_searches:
                return searches
    return searches


def _polymarket_preseed_searches(
    *,
    sport: str,
    explicit_searches: list[str] | None,
    include_sport_defaults: bool,
) -> list[str]:
    searches = _explicit_search_values(explicit_searches)
    if include_sport_defaults and _sport_filter_key(sport) == "dota2":
        searches.extend(DOTA2_POLYMARKET_PRESEED_SEARCHES)
    return _dedupe_searches(searches)


def _polymarket_page_preseed_urls(
    *,
    sport: str,
    explicit_urls: list[str] | None,
    include_dota2_defaults: bool,
) -> list[str]:
    urls = _explicit_search_values(explicit_urls)
    if include_dota2_defaults and _sport_filter_key(sport) == "dota2":
        urls.extend(DOTA2_POLYMARKET_PAGE_PRESEED_URLS)
    return _dedupe_searches(urls)


def discover_polymarket_page_preseed_events(
    provider: object,
    *,
    sport: str,
    page_urls: list[str],
    max_events: int,
    fetcher: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    page_urls = _dedupe_searches(page_urls)
    if not page_urls:
        return {
            "enabled": False,
            "page_urls": [],
            "events": [],
            "event_slugs": [],
            "market_slugs": [],
            "candidate_slugs": [],
            "event_count_raw": 0,
            "event_count_after_safety_filter": 0,
            "event_count_after_sport_filter": 0,
            "event_urls_sample": [],
            "reject_reason_counts": {},
            "rejected_samples": [],
        }

    page_discovery = discover_polymarket_page_slugs(
        page_urls,
        sport=sport,
        fetcher=fetcher,
    )
    events_before_sport_filter: list[ProviderOddsEvent] = []
    reject_reason_counts: Counter[str] = Counter()
    rejected_samples: list[dict[str, Any]] = []
    raw_count = 0

    for slug in page_discovery["event_slugs"]:
        fetched = provider.fetch_events(
            sport=sport,
            market_type="h2h",
            event_slug=slug,
            max_events=max_events,
        )
        clean_events = [
            event for event in fetched if isinstance(event, ProviderOddsEvent)
        ]
        raw_count += _polymarket_fetch_raw_count(provider, clean_events)
        events_before_sport_filter.extend(clean_events)
        provider_debug = getattr(provider, "last_debug", {})
        if isinstance(provider_debug, dict):
            reject_reason_counts.update(
                _int_counter(provider_debug.get("reject_reason_counts"))
            )
            rejected_samples.extend(
                _dict_rows(provider_debug.get("rejected_samples"))
            )

    for slug in page_discovery["market_slugs"]:
        fetched = provider.fetch_events(
            sport=sport,
            market_type="h2h",
            market_slug=slug,
            max_events=max_events,
        )
        clean_events = [
            event for event in fetched if isinstance(event, ProviderOddsEvent)
        ]
        raw_count += _polymarket_fetch_raw_count(provider, clean_events)
        events_before_sport_filter.extend(clean_events)
        provider_debug = getattr(provider, "last_debug", {})
        if isinstance(provider_debug, dict):
            reject_reason_counts.update(
                _int_counter(provider_debug.get("reject_reason_counts"))
            )
            rejected_samples.extend(
                _dict_rows(provider_debug.get("rejected_samples"))
            )

    deduped_events = _dedupe_events(events_before_sport_filter)
    sport_filter = _filter_polymarket_keyset_events_by_sport(
        deduped_events,
        sport=sport,
    )
    return {
        "enabled": True,
        "page_urls": page_urls,
        "events": sport_filter["events"],
        "event_slugs": page_discovery["event_slugs"],
        "market_slugs": page_discovery["market_slugs"],
        "candidate_slugs": page_discovery["candidate_slugs"],
        "event_count_raw": raw_count,
        "event_count_after_safety_filter": len(deduped_events),
        "event_count_after_sport_filter": len(sport_filter["events"]),
        "event_urls_sample": _provider_event_urls(sport_filter["events"])[:10],
        "reject_reason_counts": dict(sorted(reject_reason_counts.items())),
        "rejected_samples": _compact_polymarket_rejected_samples(
            rejected_samples
        )[:10],
    }


def discover_polymarket_preseed_events(
    provider: object,
    *,
    sport: str,
    searches: list[str],
    max_events: int,
) -> dict[str, Any]:
    searches = _dedupe_searches(searches)
    if not searches:
        return {
            "enabled": False,
            "searches": [],
            "events": [],
            "event_count_raw": 0,
            "event_count_after_safety_filter": 0,
            "event_count_after_sport_filter": 0,
            "event_urls_sample": [],
            "reject_reason_counts": {},
            "rejected_samples": [],
            "sport_filter_rejected_count": 0,
            "sport_filter_rejected_sample": [],
        }

    events_before_sport_filter: list[ProviderOddsEvent] = []
    reject_reason_counts: Counter[str] = Counter()
    rejected_samples: list[dict[str, Any]] = []
    raw_count = 0
    for search in searches:
        fetched = provider.fetch_events(
            sport=sport,
            market_type="h2h",
            search=search,
            max_events=max_events,
        )
        clean_events = [
            event for event in fetched if isinstance(event, ProviderOddsEvent)
        ]
        raw_count += _polymarket_fetch_raw_count(provider, clean_events)
        events_before_sport_filter.extend(clean_events)
        provider_debug = getattr(provider, "last_debug", {})
        if isinstance(provider_debug, dict):
            reject_reason_counts.update(
                _int_counter(provider_debug.get("reject_reason_counts"))
            )
            rejected_samples.extend(
                _dict_rows(provider_debug.get("rejected_samples"))
            )

    deduped_events = _dedupe_events(events_before_sport_filter)
    sport_filter = _filter_polymarket_keyset_events_by_sport(
        deduped_events,
        sport=sport,
    )
    return {
        "enabled": True,
        "searches": searches,
        "events": sport_filter["events"],
        "event_count_raw": raw_count,
        "event_count_after_safety_filter": len(deduped_events),
        "event_count_after_sport_filter": len(sport_filter["events"]),
        "event_urls_sample": _provider_event_urls(sport_filter["events"])[:10],
        "reject_reason_counts": dict(sorted(reject_reason_counts.items())),
        "rejected_samples": _compact_polymarket_rejected_samples(
            rejected_samples
        )[:10],
        "sport_filter_rejected_count": len(sport_filter["rejected_events"]),
        "sport_filter_rejected_sample": sport_filter["rejected_sample"],
    }


def _polymarket_fetch_raw_count(
    provider: object,
    events: list[ProviderOddsEvent],
) -> int:
    debug = getattr(provider, "last_debug", {})
    if isinstance(debug, dict):
        fetched_count = _int_value(debug.get("fetched_count"))
        if fetched_count:
            return fetched_count
    return len(events)


def _compact_polymarket_rejected_samples(
    samples: list[dict[str, Any]],
) -> list[dict[str, str]]:
    compact: list[dict[str, str]] = []
    keys = (
        "reason",
        "slug",
        "event_title",
        "question",
        "market_title",
        "detected_best_of",
        "outcome_count",
        "outcome_names",
        "team_names",
        "draw_or_three_way_source",
        "draw_or_three_way_matched_text",
        "unsafe_flags",
        "active",
        "closed",
        "sport_guess",
        "category",
        "tags",
    )
    for sample in samples[:10]:
        row: dict[str, str] = {}
        for key in keys:
            value = _text(sample.get(key))
            if value:
                row[key] = value
        if "sport_guess" not in row:
            sport_guess = _text(
                sample.get("sport")
                or sample.get("category")
                or sample.get("tags")
            )
            if sport_guess:
                row["sport_guess"] = sport_guess
        if row:
            compact.append(row)
    return compact


def _provider_event_urls(events: list[ProviderOddsEvent]) -> list[str]:
    return [
        _text(event.event_url)
        for event in events
        if _text(event.event_url)
    ]


def _polymarket_tournament_bucket_sources(
    *,
    keyset_events: list[ProviderOddsEvent],
    page_events: list[ProviderOddsEvent],
    search_events: list[ProviderOddsEvent],
    kept_events: list[ProviderOddsEvent],
) -> dict[str, str]:
    keyset_buckets = {
        _polymarket_event_tournament_bucket(event) for event in keyset_events
    }
    page_buckets = {
        _polymarket_event_tournament_bucket(event) for event in page_events
    }
    search_buckets = {
        _polymarket_event_tournament_bucket(event) for event in search_events
    }
    sources: dict[str, str] = {}
    for bucket in sorted(
        {_polymarket_event_tournament_bucket(event) for event in kept_events}
    ):
        source_names = []
        if bucket in page_buckets:
            source_names.append("page")
        if bucket in keyset_buckets:
            source_names.append("keyset")
        if bucket in search_buckets:
            source_names.append("search")
        if len(source_names) > 1:
            sources[bucket] = "both"
        elif source_names:
            sources[bucket] = source_names[0]
    return sources


def _dota_tournament_policy_debug(
    *,
    sport: str,
    keyset_discovery: dict[str, Any],
    page_preseed_discovery: dict[str, Any],
    preseed_discovery: dict[str, Any],
) -> dict[str, Any]:
    if _sport_filter_key(sport) != "dota2":
        return {}

    unsafe_reasons: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    sources = (
        (
            keyset_discovery.get("normalize_reject_reason_counts"),
            keyset_discovery.get("normalize_rejected_samples"),
        ),
        (
            page_preseed_discovery.get("reject_reason_counts"),
            page_preseed_discovery.get("rejected_samples"),
        ),
        (
            preseed_discovery.get("reject_reason_counts"),
            preseed_discovery.get("rejected_samples"),
        ),
    )
    for reason_counts, rejected_samples in sources:
        for reason, count in _int_counter(reason_counts).items():
            label = DOTA_TOURNAMENT_POLICY_REASON_LABELS.get(reason)
            if label:
                unsafe_reasons[label] += count
        for sample in _dict_rows(rejected_samples):
            if _text(sample.get("reason")) in DOTA_TOURNAMENT_POLICY_REASON_LABELS:
                samples.append(sample)

    return {
        "dota_tournament_policy_rejected_count": sum(unsafe_reasons.values()),
        "dota_tournament_policy_rejected_sample": (
            _compact_polymarket_rejected_samples(samples)[:10]
        ),
        "dota_unsafe_tournament_reasons": dict(sorted(unsafe_reasons.items())),
    }


def discover_polymarket_page_slugs(
    page_urls: list[str],
    *,
    sport: str,
    stake_events: list[ProviderOddsEvent] | None = None,
    fetcher: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    fetcher = fetcher or fetch_polymarket_page_html
    raw_event_slugs: list[str] = []
    raw_market_slugs: list[str] = []
    fetch_error_count = 0
    no_candidate_count = 0

    for page_url in page_urls:
        try:
            html_text = fetcher(page_url)
        except (OSError, ValueError, UnicodeDecodeError):
            fetch_error_count += 1
            continue

        candidates = extract_polymarket_page_slug_candidates(html_text, sport=sport)
        if not candidates["event_slugs"] and not candidates["market_slugs"]:
            no_candidate_count += 1
        raw_event_slugs.extend(candidates["event_slugs"])
        raw_market_slugs.extend(candidates["market_slugs"])

    raw_event_slugs = _dedupe_searches(raw_event_slugs)
    raw_market_slugs = _dedupe_searches(raw_market_slugs)
    raw_candidate_slugs = _dedupe_searches([*raw_event_slugs, *raw_market_slugs])
    if stake_events:
        team_tokens = _stake_page_slug_team_tokens(stake_events)
        event_slugs = [
            slug
            for slug in raw_event_slugs
            if _slug_matches_stake_team_tokens(slug, team_tokens)
        ]
        market_slugs = [
            slug
            for slug in raw_market_slugs
            if _slug_matches_stake_team_tokens(slug, team_tokens)
        ]
    else:
        event_slugs = raw_event_slugs
        market_slugs = raw_market_slugs
    candidate_slugs = _dedupe_searches([*event_slugs, *market_slugs])
    kept_keys = {slug.casefold() for slug in candidate_slugs}
    rejected_slugs = [
        slug for slug in raw_candidate_slugs if slug.casefold() not in kept_keys
    ]
    return {
        "page_urls": page_urls,
        "event_slugs": event_slugs,
        "market_slugs": market_slugs,
        "raw_candidate_slugs": raw_candidate_slugs,
        "candidate_slugs": candidate_slugs,
        "rejected_slugs": rejected_slugs,
        "fetch_error_count": fetch_error_count,
        "no_candidate_count": no_candidate_count,
    }


def fetch_polymarket_page_html(page_url: str) -> str:
    request = Request(
        page_url,
        headers={"User-Agent": "provider-odds-scan/0.1"},
    )
    with urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8", errors="replace")


def discover_polymarket_keyset_events(
    provider: object,
    *,
    sport: str,
    keyset_urls: list[str] | None = None,
    tag_ids: list[str] | None = None,
    window_hours: float = 48,
    limit: int = 100,
    locale: str = "en",
    max_pages: int = 3,
    adaptive: bool = False,
    empty_target_pages_stop: int = DEFAULT_POLYMARKET_KEYSET_EMPTY_TARGET_PAGES_STOP,
    max_events: int = 100,
    fetcher: Callable[[str], object] | None = None,
) -> dict[str, Any]:
    urls = _polymarket_keyset_urls(
        keyset_urls=keyset_urls,
        tag_ids=tag_ids,
        window_hours=window_hours,
        limit=limit,
        locale=locale,
    )
    fetcher = fetcher or fetch_polymarket_keyset_json
    page_limit = max(int(max_pages), 1)
    empty_target_page_limit = max(int(empty_target_pages_stop), 1)
    adaptive_enabled = bool(adaptive)
    normalized_events: list[ProviderOddsEvent] = []
    sample_slugs: list[str] = []
    raw_event_samples: list[dict[str, str]] = []
    raw_event_count_by_page: list[int] = []
    page_details: list[dict[str, object]] = []
    normalize_reject_reason_counts: Counter[str] = Counter()
    normalize_rejected_samples: list[dict[str, str]] = []
    normalize_kept_samples: list[dict[str, str]] = []
    next_cursors: list[str] = []
    page_count = 0
    event_count_raw = 0
    rejected_count = 0
    fetch_error_count = 0
    next_cursor_seen = False
    max_pages_stopped_early = False
    empty_target_pages_streak = 0
    seen_target_event_keys: set[str] = set()
    stop_reasons: list[str] = []

    for start_url in urls:
        next_url = start_url
        seen_cursors: set[str] = set()
        empty_target_pages_streak_for_url = 0
        url_stop_reason = ""
        for page_index in range(page_limit):
            try:
                payload = fetcher(next_url)
            except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                fetch_error_count += 1
                url_stop_reason = "fetch_error"
                break

            page_count += 1
            raw_count = _keyset_raw_event_count(payload)
            event_count_raw += raw_count
            raw_event_count_by_page.append(raw_count)
            raw_event_samples.extend(_keyset_raw_event_samples(payload))
            sample_slugs.extend(_keyset_sample_slugs(payload))
            page_events = _normalize_keyset_payload(
                provider,
                payload,
                sport=sport,
                max_events=max_events,
            )
            normalized_events.extend(page_events)
            provider_debug = getattr(provider, "last_debug", {})
            fetched_count_for_page = 0
            if isinstance(provider_debug, dict):
                fetched_count = _int_value(provider_debug.get("fetched_count"))
                normalized_count = _int_value(provider_debug.get("normalized_count"))
                rejected_count += max(fetched_count - normalized_count, 0)
                fetched_count_for_page = fetched_count
                normalize_reject_reason_counts.update(
                    _int_counter(provider_debug.get("reject_reason_counts"))
                )
                normalize_rejected_samples.extend(
                    _dict_rows(provider_debug.get("rejected_samples"))
                )
                normalize_kept_samples.extend(
                    _dict_rows(provider_debug.get("normalized_samples"))
                )

            page_sport_filter = _filter_polymarket_keyset_events_by_sport(
                page_events,
                sport=sport,
            )
            sport_kept_count = len(page_sport_filter["events"])
            new_target_markets = 0
            for event in page_sport_filter["events"]:
                key = _event_key(event)
                if key in seen_target_event_keys:
                    continue
                seen_target_event_keys.add(key)
                new_target_markets += 1
            if new_target_markets:
                empty_target_pages_streak_for_url = 0
            else:
                empty_target_pages_streak_for_url += 1
            empty_target_pages_streak = empty_target_pages_streak_for_url

            cursor = _keyset_next_cursor(payload)
            page_details.append(
                {
                    "page": page_count,
                    "url": next_url,
                    "raw_event_count": raw_count,
                    "normalized_count": len(page_events),
                    "sport_kept_count": sport_kept_count,
                    "new_target_markets": new_target_markets,
                    "empty_target_pages_streak": empty_target_pages_streak_for_url,
                    "rejected_count": max(
                        fetched_count_for_page - len(page_events),
                        0,
                    ),
                    "next_cursor": cursor,
                }
            )
            if not cursor:
                url_stop_reason = "no_next_cursor"
                break
            next_cursor_seen = True
            next_cursors.append(cursor)
            if page_index >= page_limit - 1:
                max_pages_stopped_early = True
                url_stop_reason = "max_pages"
                break
            if (
                adaptive_enabled
                and empty_target_pages_streak_for_url >= empty_target_page_limit
            ):
                url_stop_reason = "empty_target_page_streak"
                break
            if cursor in seen_cursors:
                url_stop_reason = "cursor_loop"
                break
            seen_cursors.add(cursor)
            next_url = _url_with_query_param(start_url, "after_cursor", cursor)
        if not url_stop_reason and page_count:
            url_stop_reason = "max_pages"
        if url_stop_reason:
            stop_reasons.append(url_stop_reason)

    events_before_sport_filter = _dedupe_events(normalized_events)
    sport_filter = _filter_polymarket_keyset_events_by_sport(
        events_before_sport_filter,
        sport=sport,
    )
    events = sport_filter["events"]
    return {
        "urls": urls,
        "events": events,
        "page_count": page_count,
        "event_count_raw": event_count_raw,
        "raw_event_count_by_page": raw_event_count_by_page,
        "raw_event_samples": raw_event_samples[:20],
        "rejected_count": rejected_count,
        "sample_slugs": _dedupe_searches(sample_slugs),
        "next_cursor_seen": next_cursor_seen,
        "next_cursors": next_cursors[:10],
        "max_pages_stopped_early": max_pages_stopped_early,
        "adaptive_enabled": adaptive_enabled,
        "adaptive_pages_fetched": page_count,
        "adaptive_stop_reason": _polymarket_keyset_stop_reason(
            urls,
            stop_reasons,
        ),
        "empty_target_pages_streak": empty_target_pages_streak,
        "tag_ids_used": _polymarket_keyset_tag_ids(urls),
        "page_details": page_details,
        "fetch_error_count": fetch_error_count,
        "event_count_before_sport_filter": len(events_before_sport_filter),
        "event_count_after_sport_filter": len(events),
        "sport_filter_rejected_count": len(sport_filter["rejected_events"]),
        "sport_filter_rejected_sample": sport_filter["rejected_sample"],
        "sport_filter_rejected_details": sport_filter["rejected_details"],
        "sport_filter_kept_sample": sport_filter["kept_sample"],
        "normalize_reject_reason_counts": dict(
            sorted(normalize_reject_reason_counts.items())
        ),
        "normalize_rejected_samples": normalize_rejected_samples[:10],
        "normalize_kept_samples": normalize_kept_samples[:10],
    }


def _normalize_lol_tournament_scope(value: str, *, sport: str) -> str:
    if _sport_filter_key(sport) != "lol":
        return "all"
    scope = _text(value).casefold() or DEFAULT_LOL_TOURNAMENT_SCOPE
    if scope not in LOL_TOURNAMENT_SCOPE_CHOICES:
        raise ValueError(
            "Unsupported --lol-tournament-scope: "
            f"{value}. Expected one of: {', '.join(LOL_TOURNAMENT_SCOPE_CHOICES)}"
        )
    return scope


def _default_stake_mapping_profile(
    *,
    sport: str,
    bookmaker: str,
    mapping_profile: str | None,
) -> str | None:
    if mapping_profile:
        return mapping_profile
    if _text(bookmaker).casefold() == "stake" and _sport_filter_key(sport) == "lol":
        return MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER
    return None


def _filter_lol_tournament_scope_events(
    events: list[ProviderOddsEvent],
    *,
    sport: str,
    scope: str,
) -> dict[str, Any]:
    if _sport_filter_key(sport) != "lol" or scope == "all":
        return {
            "events": events,
            "rejected_events": [],
            "rejected_sample": [],
        }

    kept_events: list[ProviderOddsEvent] = []
    rejected_events: list[ProviderOddsEvent] = []
    for event in events:
        bucket_name = _polymarket_event_tournament_bucket(event)
        if _lol_tournament_bucket_in_scope(bucket_name, scope=scope):
            kept_events.append(event)
        else:
            rejected_events.append(event)
    return {
        "events": kept_events,
        "rejected_events": rejected_events,
        "rejected_sample": _tournament_scope_sample(rejected_events),
    }


def _lol_tournament_bucket_in_scope(bucket_name: str, *, scope: str) -> bool:
    if scope == "all":
        return True
    if scope == "msi":
        return _lol_tournament_is_msi(bucket_name)
    return False


def _tournament_scope_sample(events: list[ProviderOddsEvent]) -> list[str]:
    samples: list[str] = []
    for event in events[:5]:
        bucket_name = _polymarket_event_tournament_bucket(event)
        teams = " vs ".join(
            part for part in (_text(event.team_a), _text(event.team_b)) if part
        )
        sample = " | ".join(part for part in (bucket_name, teams) if part)
        if sample:
            samples.append(sample)
    return samples


def build_polymarket_keyset_url(
    *,
    tag_ids: list[str],
    window_hours: float = 48,
    limit: int = 100,
    locale: str = "en",
    now: datetime | None = None,
) -> str:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    window_end = current + timedelta(hours=float(window_hours))
    params: list[tuple[str, str]] = [
        ("tag_id", _text(tag_id)) for tag_id in tag_ids if _text(tag_id)
    ]
    params.extend(
        [
            ("active", "true"),
            ("closed", "false"),
            ("order", "startTime"),
            ("ascending", "true"),
            ("limit", str(max(int(limit), 1))),
            ("start_time_min", _utc_iso_z(current)),
            ("start_time_max", _utc_iso_z(window_end)),
            ("locale", _text(locale) or "en"),
        ]
    )
    return f"{POLYMARKET_KEYSET_URL}?{urlencode(params)}"


def fetch_polymarket_keyset_json(url: str) -> object:
    request = Request(url, headers={"User-Agent": "provider-odds-scan/0.1"})
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def extract_polymarket_page_slug_candidates(
    html_text: str,
    *,
    sport: str,
) -> dict[str, list[str]]:
    text = _normalized_html_text(html_text)
    event_slugs: list[str] = []
    market_slugs: list[str] = []

    for match in re.finditer(r"/event/([A-Za-z0-9][A-Za-z0-9_-]*)", text):
        slug = _slug_text(match.group(1))
        if _slug_matches_sport(slug, sport):
            event_slugs.append(slug)

    for path in re.findall(
        r"/(?:uk/)?(?:sports|esports)/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)+",
        text,
    ):
        parts = [part for part in path.strip("/").split("/") if part]
        if parts[0] == "uk":
            parts = parts[1:]
        if len(parts) < 3 or parts[0] not in {"sports", "esports"}:
            continue
        sport_segment = parts[1]
        slug = _slug_text(parts[-1])
        if _sport_segment_matches(sport_segment, sport):
            event_slugs.append(slug)

    for key, slug in re.findall(
        r'"(eventSlug|event_slug|slug|marketSlug|market_slug)"\s*:\s*"([^"]+)"',
        text,
    ):
        clean_slug = _slug_text(slug)
        if not _slug_matches_sport(clean_slug, sport):
            continue
        if key in {"marketSlug", "market_slug"}:
            market_slugs.append(clean_slug)
        else:
            event_slugs.append(clean_slug)

    return {
        "event_slugs": _dedupe_searches(event_slugs),
        "market_slugs": _dedupe_searches(market_slugs),
    }


def format_scan_summary(result: dict[str, Any]) -> str:
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    return "\n".join(
        [
            "scan summary:",
            f"stake_count={_text(summary.get('stake_count') or 0)}",
            f"polymarket_count={_text(summary.get('polymarket_count') or 0)}",
            f"matched_count={_text(summary.get('matched_count') or 0)}",
            f"opportunity_count={_text(summary.get('opportunity_count') or 0)}",
            f"telegram_sent_count={_text(summary.get('telegram_sent_count') or 0)}",
        ]
    )


def discover_stake_tournaments_from_polymarket(
    provider: object,
    *,
    sport: str,
    bookmaker: str,
    polymarket_events: list[ProviderOddsEvent],
    enabled: bool,
    max_tournaments: int,
    min_score: float,
    mapping_profile: str | None,
    max_start_time_diff_minutes: int,
    match_min_score: float,
    request_delay_seconds: float,
    rate_limit_retries: int,
    sleeper: Callable[[float], None],
    tournament_list_cache_path: Path = DEFAULT_TOURNAMENT_LIST_CACHE_PATH,
    tournament_list_cache_ttl_hours: float = 24,
    refresh_tournament_list_cache: bool = False,
    disable_tournament_list_cache: bool = False,
    active_tournament_cache_path: Path = DEFAULT_ACTIVE_TOURNAMENT_CACHE_PATH,
    active_tournament_cache_ttl_hours: float = 168,
    disable_active_tournament_cache: bool = False,
    refresh_active_tournament_cache: bool = False,
    tournament_resolution_mode: str = "normal",
    max_selected_tournament_ids_per_sport: int = 10,
    max_discovery_candidates_per_bucket: int = 3,
    max_oddspapi_requests_per_run: int = 8,
) -> dict[str, Any]:
    if not enabled:
        return _empty_stake_tournament_discovery(enabled=False)

    if _should_use_cached_tournament_resolution(
        provider,
        active_tournament_cache_path=active_tournament_cache_path,
        disable_active_tournament_cache=disable_active_tournament_cache,
    ):
        return _discover_stake_tournaments_with_cache(
            provider,
            sport=sport,
            bookmaker=bookmaker,
            polymarket_events=polymarket_events,
            max_tournaments=max_tournaments,
            min_score=min_score,
            mapping_profile=mapping_profile,
            max_start_time_diff_minutes=max_start_time_diff_minutes,
            match_min_score=match_min_score,
            request_delay_seconds=request_delay_seconds,
            rate_limit_retries=rate_limit_retries,
            sleeper=sleeper,
            tournament_list_cache_path=tournament_list_cache_path,
            tournament_list_cache_ttl_hours=tournament_list_cache_ttl_hours,
            refresh_tournament_list_cache=refresh_tournament_list_cache,
            disable_tournament_list_cache=disable_tournament_list_cache,
            active_tournament_cache_path=active_tournament_cache_path,
            active_tournament_cache_ttl_hours=active_tournament_cache_ttl_hours,
            disable_active_tournament_cache=disable_active_tournament_cache,
            refresh_active_tournament_cache=refresh_active_tournament_cache,
            tournament_resolution_mode=tournament_resolution_mode,
            max_selected_tournament_ids_per_sport=(
                max_selected_tournament_ids_per_sport
            ),
            max_discovery_candidates_per_bucket=max_discovery_candidates_per_bucket,
            max_oddspapi_requests_per_run=max_oddspapi_requests_per_run,
        )

    fetch_candidates = getattr(provider, "fetch_tournament_candidates", None)
    if not callable(fetch_candidates):
        raise ValueError("Stake auto discovery requires fetch_tournament_candidates")

    bucket_events = _polymarket_events_by_tournament_bucket(
        polymarket_events,
        sport=sport,
    )
    hints = _polymarket_tournament_hints(polymarket_events, sport=sport)
    rate_limit_count = 0
    stopped_reason = ""

    bucket_contexts: list[dict[str, Any]] = []
    all_scored: list[dict[str, Any]] = []
    candidate_count_by_bucket: dict[str, int] = {}
    candidate_sample_by_bucket: dict[str, list[dict[str, Any]]] = {}
    candidate_score_breakdown_by_bucket: dict[str, list[dict[str, Any]]] = {}
    candidate_core_token_match_by_bucket: dict[str, list[dict[str, Any]]] = {}
    candidate_generic_token_match_by_bucket: dict[str, list[dict[str, Any]]] = {}
    candidate_fetch_keywords_by_bucket: dict[str, list[str]] = {}
    team_pairs_by_bucket: dict[str, list[str]] = {}
    team_pair_count_by_bucket: dict[str, int] = {}
    rejected_lol_candidate_reasons: dict[str, list[dict[str, str]]] = {}
    lol_verified_team_overlap: dict[str, list[dict[str, Any]]] = {}
    for bucket_name, bucket_items in bucket_events.items():
        bucket_hints = _polymarket_tournament_hints(bucket_items, sport=sport)
        team_pairs = _polymarket_team_pairs(bucket_items, sport=sport)
        team_pair_events = _polymarket_team_pair_events(team_pairs, sport=sport)
        team_pairs_by_bucket[bucket_name] = _polymarket_team_pair_texts(team_pairs)
        team_pair_count_by_bucket[bucket_name] = len(team_pair_events)
        bucket_context = {
            "bucket": bucket_name,
            "events": bucket_items,
            "hints": bucket_hints,
            "team_pair_events": team_pair_events,
            "candidates": [],
            "index": 0,
        }
        bucket_contexts.append(bucket_context)
        fetch_result = _fetch_stake_tournament_candidates_for_bucket(
            fetch_candidates,
            sport=sport,
            bucket_name=bucket_name,
            hints=bucket_hints,
            request_delay_seconds=request_delay_seconds,
            rate_limit_retries=rate_limit_retries,
            sleeper=sleeper,
        )
        rate_limit_count += fetch_result["rate_limit_count"]
        candidate_fetch_keywords_by_bucket[bucket_name] = fetch_result["keywords"]
        if fetch_result["stopped_reason"] == "rate_limited":
            stopped_reason = "rate_limited"
            break

        candidates = fetch_result["candidates"]
        rejected_lol_candidate_reasons[bucket_name] = (
            _rejected_lol_candidate_reasons_for_bucket(
                bucket_name,
                candidates,
                sport=sport,
            )
        )
        scored = _score_stake_tournament_candidates(
            candidates,
            hints=bucket_hints,
            sport=sport,
        )
        scored_for_attempts = scored
        if _sport_filter_key(sport) == "lol":
            scored_for_attempts = [
                item for item in scored if float(item.get("score") or 0) > 0
            ]
        candidate_count_by_bucket[bucket_name] = len(candidates)
        candidate_sample_by_bucket[bucket_name] = (
            _stake_tournament_raw_candidate_sample(scored)
        )
        candidate_score_breakdown_by_bucket[bucket_name] = (
            _stake_tournament_score_breakdown_sample(scored)
        )
        candidate_core_token_match_by_bucket[bucket_name] = (
            _stake_tournament_core_token_match_sample(scored)
        )
        candidate_generic_token_match_by_bucket[bucket_name] = (
            _stake_tournament_generic_token_match_sample(scored)
        )
        all_scored.extend(scored_for_attempts)
        bucket_context["candidates"] = scored_for_attempts

    scored = _dedupe_scored_tournament_candidates(all_scored)
    max_candidate_fetches = max(int(max_tournaments), 0)
    attempt_candidates = _bucket_round_robin_candidates(
        bucket_contexts,
        max_candidates=max_candidate_fetches,
    )
    candidates_to_fetch = attempt_candidates
    sample_by_id = {
        _text(item.get("id")): item for item in scored[:5] if _text(item.get("id"))
    }
    known_51042 = _known_tournament_candidate(scored, "51042")

    events: list[ProviderOddsEvent] = []
    selected_ids: list[str] = []
    selected_names: list[str] = []
    requested_tournament_ids: list[str] = []
    skipped_tournament_ids: list[str] = []
    skipped_tournament_reasons: dict[str, str] = {}
    verified_match_count: dict[str, int] = {}
    matched_polymarket_keys: set[str] = set()
    attempted_ids: list[str] = []
    attempted_names: list[str] = []
    attempt_results: list[dict[str, Any]] = []
    team_match_sample: list[dict[str, str]] = []
    error_count = 0
    fetched_event_count = 0
    delay_seconds = max(float(request_delay_seconds), 0.0)
    attempted_ids_by_bucket: dict[str, list[str]] = {}
    attempt_results_by_bucket: dict[str, list[dict[str, Any]]] = {}
    selected_ids_by_bucket: dict[str, list[str]] = {}

    if not bucket_contexts and not stopped_reason:
        stopped_reason = "no_tournament_buckets"
    elif (
        not stopped_reason
        and not any(context["team_pair_events"] for context in bucket_contexts)
    ):
        stopped_reason = "no_polymarket_team_pairs"
    elif not stopped_reason and not hints:
        stopped_reason = "no_polymarket_hints"
    elif not stopped_reason and not candidates_to_fetch:
        if any(context["team_pair_events"] for context in bucket_contexts) and not scored:
            stopped_reason = "no_stake_tournament_candidates"
        else:
            stopped_reason = "candidates_exhausted"

    for index, item in enumerate([] if stopped_reason else candidates_to_fetch):
        candidate = item["candidate"]
        bucket_name = _text(item.get("bucket"))
        team_pair_events = [
            event
            for event in item.get("team_pair_events", [])
            if isinstance(event, ProviderOddsEvent)
        ]
        candidate_id = _text(candidate.get("id"))
        if not candidate_id:
            continue
        candidate_name = _text(candidate.get("name"))
        attempted_ids.append(candidate_id)
        attempted_ids_by_bucket.setdefault(bucket_name, []).append(candidate_id)
        if candidate_name:
            attempted_names.append(candidate_name)

        stake_fetch = _fetch_stake_events(
            provider,
            sport=sport,
            bookmaker=bookmaker,
            tournament_ids=[candidate_id],
            mapping_profile=mapping_profile,
            request_delay_seconds=request_delay_seconds,
            rate_limit_retries=rate_limit_retries,
            sleeper=sleeper,
        )
        requested_tournament_ids.extend(stake_fetch["requested_tournament_ids"])
        skipped_tournament_ids.extend(stake_fetch["skipped_tournament_ids"])
        skipped_tournament_reasons.update(stake_fetch["skipped_tournament_reasons"])
        rate_limit_count += stake_fetch["rate_limit_count"]
        error_count += stake_fetch["error_count"]

        candidate_events = [
            event
            for event in stake_fetch["events"]
            if isinstance(event, ProviderOddsEvent)
        ]
        fetched_event_count += len(candidate_events)
        candidate_matches = _events(
            build_snapshot(
                candidate_events,
                team_pair_events,
                max_start_time_diff_minutes=max_start_time_diff_minutes,
                min_score=match_min_score,
            ).get("matches")
        )
        verified_count = len(candidate_matches)
        verified_match_count[candidate_id] = verified_count
        candidate["verified_match_count"] = verified_count
        required_overlap = _required_verified_team_overlap(
            sport=sport,
            team_pair_event_count=len(team_pair_events),
        )
        verified_for_selection = verified_count >= required_overlap
        if candidate_id in sample_by_id:
            sample_by_id[candidate_id]["verified_match_count"] = verified_count
        if _sport_filter_key(sport) == "lol" and verified_count:
            lol_verified_team_overlap.setdefault(bucket_name, []).append(
                {
                    "id": candidate_id,
                    "name": candidate_name,
                    "verified_match_count": verified_count,
                    "required_match_count": required_overlap,
                    "result": (
                        "selected"
                        if verified_for_selection
                        else "insufficient_team_overlap"
                    ),
                }
            )
        team_match_sample.extend(
            _stake_tournament_team_match_sample(
                candidate_id,
                candidate_name,
                candidate_matches,
            )
        )

        attempt_result = {
            "id": candidate_id,
            "name": candidate_name,
            "bucket": bucket_name,
            "score": candidate.get("score", 0),
            "fetched_count": len(candidate_events),
            "verified_match_count": verified_count,
            "result": "no_team_match",
        }
        if stake_fetch["skipped_tournament_ids"]:
            attempt_result["result"] = _text(
                stake_fetch["skipped_tournament_reasons"].get(candidate_id)
            ) or "skipped"
        elif stake_fetch.get("stopped_reason") == "rate_limited":
            attempt_result["result"] = "rate_limited"
        elif not candidate_events:
            attempt_result["result"] = "no_fixtures"
        elif verified_for_selection:
            attempt_result["result"] = "selected"
        elif verified_count:
            attempt_result["result"] = "insufficient_team_overlap"
        attempt_results.append(attempt_result)
        attempt_results_by_bucket.setdefault(bucket_name, []).append(attempt_result)

        if stake_fetch.get("stopped_reason") == "rate_limited":
            stopped_reason = "rate_limited"
            break

        if verified_for_selection:
            selected_ids.append(candidate_id)
            selected_ids_by_bucket.setdefault(bucket_name, []).append(candidate_id)
            if candidate_name:
                selected_names.append(candidate_name)
            events.extend(candidate_events)
            for match in candidate_matches:
                right_event = match.get("right_event")
                key = _event_key(right_event) if isinstance(right_event, dict) else ""
                if key:
                    matched_polymarket_keys.add(key)

        all_team_pair_events = [
            event
            for context in bucket_contexts
            for event in context["team_pair_events"]
            if isinstance(event, ProviderOddsEvent)
        ]
        if all_team_pair_events and len(matched_polymarket_keys) >= len(all_team_pair_events):
            stopped_reason = "all_polymarket_events_matched"
            break

        if delay_seconds and index < len(candidates_to_fetch) - 1:
            sleeper(delay_seconds)

    if not stopped_reason:
        if not selected_ids:
            if fetched_event_count <= 0:
                stopped_reason = "no_valid_tournament_fixtures"
            else:
                stopped_reason = "no_team_verified_tournaments"
        elif len(attempt_candidates) > len(candidates_to_fetch):
            stopped_reason = "request_budget_reached"
        elif candidates_to_fetch and len(candidates_to_fetch) >= max_candidate_fetches:
            stopped_reason = "request_budget_reached"
        else:
            stopped_reason = "candidates_exhausted"

    return {
        "enabled": True,
        "candidate_count": len(scored),
        "selected_ids": selected_ids,
        "selected_names": selected_names,
        "rejected_count": max(len(scored) - len(selected_ids), 0),
        "selection_sample": scored[:5],
        "raw_candidate_sample": _stake_tournament_raw_candidate_sample(scored),
        "polymarket_tournament_hints_sample": _polymarket_tournament_hints_sample(
            hints
        ),
        "events": _dedupe_events(events),
        "event_tournament_ids": _dedupe_searches(selected_ids),
        "requested_tournament_ids": _dedupe_searches(requested_tournament_ids),
        "attempted_ids": _dedupe_searches(attempted_ids),
        "attempted_names": attempted_names,
        "attempt_results": attempt_results,
        "team_match_sample": team_match_sample[:10],
        "skipped_tournament_ids": _dedupe_searches(skipped_tournament_ids),
        "skipped_tournament_reasons": skipped_tournament_reasons,
        "verified_match_count": verified_match_count,
        "candidate_count_by_bucket": candidate_count_by_bucket,
        "candidate_sample_by_bucket": candidate_sample_by_bucket,
        "candidate_score_breakdown_by_bucket": candidate_score_breakdown_by_bucket,
        "candidate_core_token_match_by_bucket": (
            candidate_core_token_match_by_bucket
        ),
        "candidate_generic_token_match_by_bucket": (
            candidate_generic_token_match_by_bucket
        ),
        "candidate_fetch_keywords_by_bucket": candidate_fetch_keywords_by_bucket,
        "polymarket_team_pairs_by_bucket": team_pairs_by_bucket,
        "polymarket_team_pair_count_by_bucket": team_pair_count_by_bucket,
        "lol_bucket_team_sample": (
            team_pairs_by_bucket if _sport_filter_key(sport) == "lol" else {}
        ),
        "lol_candidate_count": (
            candidate_count_by_bucket if _sport_filter_key(sport) == "lol" else {}
        ),
        "lol_candidate_score_breakdown": (
            candidate_score_breakdown_by_bucket
            if _sport_filter_key(sport) == "lol"
            else {}
        ),
        "lol_candidate_fetch_keywords": (
            candidate_fetch_keywords_by_bucket
            if _sport_filter_key(sport) == "lol"
            else {}
        ),
        "lol_verified_team_overlap": lol_verified_team_overlap,
        "rejected_lol_candidate_reasons": rejected_lol_candidate_reasons,
        "attempted_ids_by_bucket": {
            key: _dedupe_searches(value)
            for key, value in attempted_ids_by_bucket.items()
        },
        "attempt_results_by_bucket": attempt_results_by_bucket,
        "selected_ids_by_bucket": {
            key: _dedupe_searches(value)
            for key, value in selected_ids_by_bucket.items()
        },
        "missing_bucket_names": [
            bucket_name
            for bucket_name in bucket_events
            if bucket_name not in selected_ids_by_bucket
        ],
        "known_51042_seen": bool(known_51042),
        "known_51042_name": _text(known_51042.get("name")) if known_51042 else "",
        "known_51042_rank": int(known_51042.get("rank", 0)) if known_51042 else 0,
        "rate_limit_count": rate_limit_count,
        "stopped_reason": stopped_reason,
        "error_count": error_count,
        "oddspapi_tournament_list_cache_hit": False,
        "oddspapi_tournament_list_request_count": 0,
        "active_tournament_cache_enabled": False,
        "active_tournament_cache_hit_count": 0,
        "active_tournament_cache_miss_count": 0,
        "active_tournament_cache_hits": [],
        "active_tournament_cache_misses": [],
        "active_tournament_cache_updated": False,
        "selected_tournament_ids_by_bucket": {
            key: _dedupe_searches(value)
            for key, value in selected_ids_by_bucket.items()
        },
        "unresolved_tournament_buckets": [
            bucket_name
            for bucket_name in bucket_events
            if bucket_name not in selected_ids_by_bucket
        ],
        "rejected_tournament_candidates_by_bucket": {},
        "oddspapi_batch_tournament_ids": [],
        "oddspapi_batch_request_count": 0,
        "oddspapi_request_count": len(requested_tournament_ids) + rate_limit_count,
        "discovery_skipped_due_to_mode": [],
        "discovery_skipped_due_to_budget": [],
    }


def _empty_stake_tournament_discovery(*, enabled: bool) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "candidate_count": 0,
        "selected_ids": [],
        "selected_names": [],
        "rejected_count": 0,
        "selection_sample": [],
        "raw_candidate_sample": [],
        "polymarket_tournament_hints_sample": [],
        "events": [],
        "event_tournament_ids": [],
        "requested_tournament_ids": [],
        "attempted_ids": [],
        "attempted_names": [],
        "attempt_results": [],
        "team_match_sample": [],
        "skipped_tournament_ids": [],
        "skipped_tournament_reasons": {},
        "verified_match_count": {},
        "candidate_count_by_bucket": {},
        "candidate_sample_by_bucket": {},
        "candidate_score_breakdown_by_bucket": {},
        "candidate_core_token_match_by_bucket": {},
        "candidate_generic_token_match_by_bucket": {},
        "candidate_fetch_keywords_by_bucket": {},
        "polymarket_team_pairs_by_bucket": {},
        "polymarket_team_pair_count_by_bucket": {},
        "lol_bucket_team_sample": {},
        "lol_candidate_count": {},
        "lol_candidate_score_breakdown": {},
        "lol_candidate_fetch_keywords": {},
        "lol_verified_team_overlap": {},
        "rejected_lol_candidate_reasons": {},
        "attempted_ids_by_bucket": {},
        "attempt_results_by_bucket": {},
        "selected_ids_by_bucket": {},
        "missing_bucket_names": [],
        "known_51042_seen": False,
        "known_51042_name": "",
        "known_51042_rank": 0,
        "rate_limit_count": 0,
        "stopped_reason": "",
        "error_count": 0,
        "oddspapi_tournament_list_cache_hit": False,
        "oddspapi_tournament_list_request_count": 0,
        "active_tournament_cache_enabled": False,
        "active_tournament_cache_hit_count": 0,
        "active_tournament_cache_miss_count": 0,
        "active_tournament_cache_hits": [],
        "active_tournament_cache_misses": [],
        "active_tournament_cache_updated": False,
        "selected_tournament_ids_by_bucket": {},
        "unresolved_tournament_buckets": [],
        "rejected_tournament_candidates_by_bucket": {},
        "oddspapi_batch_tournament_ids": [],
        "oddspapi_batch_request_count": 0,
        "oddspapi_request_count": 0,
        "discovery_skipped_due_to_mode": [],
        "discovery_skipped_due_to_budget": [],
    }


def _should_use_cached_tournament_resolution(
    provider: object,
    *,
    active_tournament_cache_path: Path,
    disable_active_tournament_cache: bool,
) -> bool:
    if callable(getattr(provider, "fetch_tournament_list", None)):
        return True
    return (
        not disable_active_tournament_cache
        and active_tournament_cache_path.exists()
        and callable(getattr(provider, "fetch_events_for_tournaments", None))
    )


def _discover_stake_tournaments_with_cache(
    provider: object,
    *,
    sport: str,
    bookmaker: str,
    polymarket_events: list[ProviderOddsEvent],
    max_tournaments: int,
    min_score: float,
    mapping_profile: str | None,
    max_start_time_diff_minutes: int,
    match_min_score: float,
    request_delay_seconds: float,
    rate_limit_retries: int,
    sleeper: Callable[[float], None],
    tournament_list_cache_path: Path,
    tournament_list_cache_ttl_hours: float,
    refresh_tournament_list_cache: bool,
    disable_tournament_list_cache: bool,
    active_tournament_cache_path: Path,
    active_tournament_cache_ttl_hours: float,
    disable_active_tournament_cache: bool,
    refresh_active_tournament_cache: bool,
    tournament_resolution_mode: str,
    max_selected_tournament_ids_per_sport: int,
    max_discovery_candidates_per_bucket: int,
    max_oddspapi_requests_per_run: int,
) -> dict[str, Any]:
    bucket_events = _polymarket_events_by_tournament_bucket(
        polymarket_events,
        sport=sport,
    )
    hints = _polymarket_tournament_hints(polymarket_events, sport=sport)
    active_cache_enabled = not disable_active_tournament_cache
    active_cache = (
        _read_json_cache(active_tournament_cache_path)
        if active_cache_enabled
        else {}
    )
    selected_limit = max(int(max_selected_tournament_ids_per_sport), 0)
    per_bucket_limit = max(int(max_discovery_candidates_per_bucket), 1)
    request_budget = max(int(max_oddspapi_requests_per_run), 0)
    request_count = 0
    tournament_list_request_count = 0
    batch_request_count = 0
    rate_limit_count = 0
    error_count = 0
    stopped_reason = ""
    active_cache_updated = False
    tournament_list_cache_hit = False

    selected_ids: list[str] = []
    selected_names: list[str] = []
    selected_ids_by_bucket: dict[str, list[str]] = {}
    attempted_ids: list[str] = []
    attempted_names: list[str] = []
    attempted_ids_by_bucket: dict[str, list[str]] = {}
    attempt_results: list[dict[str, Any]] = []
    attempt_results_by_bucket: dict[str, list[dict[str, Any]]] = {}
    team_match_sample: list[dict[str, str]] = []
    verified_match_count: dict[str, int] = {}
    skipped_tournament_ids: list[str] = []
    skipped_tournament_reasons: dict[str, str] = {}
    active_cache_hits: list[str] = []
    active_cache_misses: list[str] = []
    unresolved_buckets: list[str] = []
    discovery_skipped_due_to_mode: list[str] = []
    discovery_skipped_due_to_budget: list[str] = []
    rejected_candidates_by_bucket: dict[str, list[dict[str, Any]]] = {}
    candidate_count_by_bucket: dict[str, int] = {}
    candidate_sample_by_bucket: dict[str, list[dict[str, Any]]] = {}
    candidate_score_breakdown_by_bucket: dict[str, list[dict[str, Any]]] = {}
    candidate_core_token_match_by_bucket: dict[str, list[dict[str, Any]]] = {}
    candidate_generic_token_match_by_bucket: dict[str, list[dict[str, Any]]] = {}
    candidate_fetch_keywords_by_bucket: dict[str, list[str]] = {}
    team_pairs_by_bucket: dict[str, list[str]] = {}
    team_pair_count_by_bucket: dict[str, int] = {}
    rejected_lol_candidate_reasons: dict[str, list[dict[str, str]]] = {}
    lol_verified_team_overlap: dict[str, list[dict[str, Any]]] = {}
    bucket_contexts: list[dict[str, Any]] = []
    scored_all: list[dict[str, Any]] = []

    for bucket_name, bucket_items in bucket_events.items():
        bucket_hints = _polymarket_tournament_hints(bucket_items, sport=sport)
        team_pairs = _polymarket_team_pairs(bucket_items, sport=sport)
        team_pair_events = _polymarket_team_pair_events(team_pairs, sport=sport)
        team_pairs_by_bucket[bucket_name] = _polymarket_team_pair_texts(team_pairs)
        team_pair_count_by_bucket[bucket_name] = len(team_pair_events)
        candidate_fetch_keywords_by_bucket[bucket_name] = []

        cache_entry = (
            _active_tournament_cache_entry(
                active_cache,
                sport=sport,
                bookmaker=bookmaker,
                bucket_name=bucket_name,
                ttl_hours=active_tournament_cache_ttl_hours,
                refresh=refresh_active_tournament_cache,
            )
            if active_cache_enabled
            else {}
        )
        if cache_entry:
            tournament_id = _text(cache_entry.get("tournament_id"))
            tournament_name = _text(cache_entry.get("tournament_name"))
            if tournament_id and len(selected_ids) < selected_limit:
                selected_ids.append(tournament_id)
                selected_names.append(tournament_name)
                selected_ids_by_bucket.setdefault(bucket_name, []).append(tournament_id)
                active_cache_hits.append(f"{bucket_name}:{tournament_id}")
                continue

        active_cache_misses.append(bucket_name)
        if tournament_resolution_mode == "cache-only":
            unresolved_buckets.append(bucket_name)
            discovery_skipped_due_to_mode.append(bucket_name)
            continue
        bucket_contexts.append(
            {
                "bucket": bucket_name,
                "events": bucket_items,
                "hints": bucket_hints,
                "team_pair_events": team_pair_events,
            }
        )

    tournament_candidates: list[dict[str, Any]] = []
    if bucket_contexts:
        if request_count >= request_budget:
            discovery_skipped_due_to_budget.extend(
                _text(context.get("bucket")) for context in bucket_contexts
            )
        else:
            list_result = _load_or_fetch_tournament_list(
                provider,
                sport=sport,
                bookmaker=bookmaker,
                cache_path=tournament_list_cache_path,
                ttl_hours=tournament_list_cache_ttl_hours,
                refresh=(
                    refresh_tournament_list_cache
                    or tournament_resolution_mode == "refresh"
                ),
                disabled=disable_tournament_list_cache,
                request_budget=request_budget - request_count,
            )
            tournament_candidates = list_result["candidates"]
            tournament_list_cache_hit = bool(list_result["cache_hit"])
            tournament_list_request_count = int(list_result["request_count"])
            request_count += tournament_list_request_count
            if list_result.get("skipped_due_to_budget"):
                discovery_skipped_due_to_budget.extend(
                    _text(context.get("bucket")) for context in bucket_contexts
                )

    candidates_to_verify: list[dict[str, Any]] = []
    for context in bucket_contexts:
        bucket_name = _text(context.get("bucket"))
        scored = _score_strict_tournament_candidates(
            bucket_name,
            tournament_candidates,
            sport=sport,
        )
        rejected_lol_candidate_reasons[bucket_name] = (
            _rejected_lol_candidate_reasons_for_bucket(
                bucket_name,
                tournament_candidates,
                sport=sport,
            )
        )
        accepted = [
            item for item in scored if float(item.get("score") or 0) >= min_score
        ]
        if _sport_filter_key(sport) == "lol" and not accepted:
            team_probe_scored = _score_lol_team_probe_candidates(
                bucket_name,
                tournament_candidates,
                team_pair_events=[
                    event
                    for event in context.get("team_pair_events", [])
                    if isinstance(event, ProviderOddsEvent)
                ],
                sport=sport,
            )
            if team_probe_scored:
                scored = _dedupe_scored_tournament_candidates(
                    [*scored, *team_probe_scored]
                )
                accepted = [
                    item
                    for item in scored
                    if float(item.get("score") or 0) >= min_score
                ]
        scored_all.extend(scored)
        rejected_candidates_by_bucket[bucket_name] = [
            item for item in scored if item not in accepted
        ][:5]
        limited = accepted[:per_bucket_limit]
        candidate_count_by_bucket[bucket_name] = len(tournament_candidates)
        candidate_sample_by_bucket[bucket_name] = (
            _stake_tournament_raw_candidate_sample(limited)
        )
        candidate_score_breakdown_by_bucket[bucket_name] = (
            _stake_tournament_score_breakdown_sample(scored[:10])
        )
        candidate_core_token_match_by_bucket[bucket_name] = (
            _stake_tournament_core_token_match_sample(scored[:10])
        )
        candidate_generic_token_match_by_bucket[bucket_name] = (
            _stake_tournament_generic_token_match_sample(scored[:10])
        )
        if not limited:
            unresolved_buckets.append(bucket_name)
        for candidate in limited:
            candidates_to_verify.append(
                {
                    "bucket": bucket_name,
                    "candidate": candidate,
                    "team_pair_events": context.get("team_pair_events", []),
                }
            )

    candidates_to_verify = _dedupe_candidate_attempts(
        candidates_to_verify,
        max_candidates=max(max(int(max_tournaments), 0) - len(selected_ids), 0),
    )
    if candidates_to_verify and request_count >= request_budget:
        discovery_skipped_due_to_budget.extend(
            _text(item.get("bucket")) for item in candidates_to_verify
        )
        candidates_to_verify = []

    verification_events_by_id: dict[str, list[ProviderOddsEvent]] = {}
    verification_fetch: dict[str, Any] = {}
    if candidates_to_verify:
        verify_ids = _dedupe_searches(
            [
                _text(item.get("candidate", {}).get("id"))
                for item in candidates_to_verify
                if isinstance(item.get("candidate"), dict)
            ]
        )
        if verify_ids:
            verification_fetch = _fetch_stake_events(
                provider,
                sport=sport,
                bookmaker=bookmaker,
                tournament_ids=verify_ids,
                mapping_profile=mapping_profile,
                request_delay_seconds=request_delay_seconds,
                rate_limit_retries=rate_limit_retries,
                sleeper=sleeper,
            )
            rate_limit_count += verification_fetch["rate_limit_count"]
            error_count += verification_fetch["error_count"]
            batch_request_count += int(verification_fetch.get("batch_request_count") or 0)
            request_count += int(verification_fetch.get("request_count") or 0)
            skipped_tournament_ids.extend(verification_fetch["skipped_tournament_ids"])
            skipped_tournament_reasons.update(
                verification_fetch["skipped_tournament_reasons"]
            )
            verification_events_by_id = _events_by_tournament_id(
                [
                    event
                    for event in verification_fetch.get("events", [])
                    if isinstance(event, ProviderOddsEvent)
                ],
                requested_ids=verify_ids,
            )
            if verification_fetch.get("stopped_reason") == "rate_limited":
                stopped_reason = "rate_limited"

    for item in [] if stopped_reason == "rate_limited" else candidates_to_verify:
        candidate = item["candidate"]
        bucket_name = _text(item.get("bucket"))
        candidate_id = _text(candidate.get("id"))
        candidate_name = _text(candidate.get("name"))
        attempted_ids.append(candidate_id)
        attempted_ids_by_bucket.setdefault(bucket_name, []).append(candidate_id)
        if candidate_name:
            attempted_names.append(candidate_name)
        candidate_events = verification_events_by_id.get(candidate_id, [])
        team_pair_events = [
            event
            for event in item.get("team_pair_events", [])
            if isinstance(event, ProviderOddsEvent)
        ]
        candidate_matches = _events(
            build_snapshot(
                candidate_events,
                team_pair_events,
                max_start_time_diff_minutes=max_start_time_diff_minutes,
                min_score=match_min_score,
            ).get("matches")
        )
        verified_count = len(candidate_matches)
        verified_match_count[candidate_id] = verified_count
        required_overlap = _required_verified_team_overlap(
            sport=sport,
            team_pair_event_count=len(team_pair_events),
        )
        verified_for_selection = verified_count >= required_overlap
        team_match_sample.extend(
            _stake_tournament_team_match_sample(
                candidate_id,
                candidate_name,
                candidate_matches,
            )
        )
        if _sport_filter_key(sport) == "lol" and verified_count:
            lol_verified_team_overlap.setdefault(bucket_name, []).append(
                {
                    "id": candidate_id,
                    "name": candidate_name,
                    "verified_match_count": verified_count,
                    "required_match_count": required_overlap,
                    "result": (
                        "selected"
                        if verified_for_selection
                        else "insufficient_team_overlap"
                    ),
                }
            )
        result = "selected" if verified_for_selection else "no_team_match"
        if candidate_id in skipped_tournament_reasons:
            result = skipped_tournament_reasons[candidate_id]
        elif not candidate_events:
            result = "no_fixtures"
        elif verified_count and not verified_for_selection:
            result = "insufficient_team_overlap"
        attempt_result = {
            "id": candidate_id,
            "name": candidate_name,
            "bucket": bucket_name,
            "score": candidate.get("score", 0),
            "fetched_count": len(candidate_events),
            "verified_match_count": verified_count,
            "result": result,
        }
        attempt_results.append(attempt_result)
        attempt_results_by_bucket.setdefault(bucket_name, []).append(attempt_result)
        if (
            verified_for_selection
            and candidate_id not in selected_ids
            and len(selected_ids) < selected_limit
        ):
            selected_ids.append(candidate_id)
            selected_names.append(candidate_name)
            selected_ids_by_bucket.setdefault(bucket_name, []).append(candidate_id)
            if active_cache_enabled:
                _update_active_tournament_cache_success(
                    active_cache,
                    sport=sport,
                    bookmaker=bookmaker,
                    bucket_name=bucket_name,
                    tournament_id=candidate_id,
                    tournament_name=candidate_name,
                    fixture_count=len(candidate_events),
                    aliases=[bucket_name, candidate_name],
                )
                active_cache_updated = True

    for context in bucket_contexts:
        bucket_name = _text(context.get("bucket"))
        if bucket_name not in selected_ids_by_bucket and bucket_name not in unresolved_buckets:
            unresolved_buckets.append(bucket_name)

    if active_cache_enabled and active_cache_updated:
        _write_json_cache(active_tournament_cache_path, active_cache)

    if not stopped_reason:
        if discovery_skipped_due_to_budget:
            stopped_reason = "request_budget_reached"
        elif not bucket_events:
            stopped_reason = "no_tournament_buckets"
        elif selected_ids:
            stopped_reason = "cache_resolved" if active_cache_hits else "candidates_exhausted"
        elif unresolved_buckets:
            stopped_reason = "unresolved_tournament_buckets"

    scored = _dedupe_scored_tournament_candidates(scored_all)
    known_51042 = _known_tournament_candidate(scored, "51042")
    if not known_51042 and "51042" in selected_ids:
        known_51042 = {"id": "51042", "name": "", "rank": 0}
    return {
        "enabled": True,
        "candidate_count": len(scored),
        "selected_ids": selected_ids,
        "selected_names": selected_names,
        "rejected_count": max(len(scored) - len(selected_ids), 0),
        "selection_sample": scored[:5],
        "raw_candidate_sample": _stake_tournament_raw_candidate_sample(scored),
        "polymarket_tournament_hints_sample": _polymarket_tournament_hints_sample(
            hints
        ),
        "events": [],
        "event_tournament_ids": [],
        "requested_tournament_ids": [],
        "attempted_ids": _dedupe_searches(attempted_ids),
        "attempted_names": attempted_names,
        "attempt_results": attempt_results,
        "team_match_sample": team_match_sample[:10],
        "skipped_tournament_ids": _dedupe_searches(skipped_tournament_ids),
        "skipped_tournament_reasons": skipped_tournament_reasons,
        "verified_match_count": verified_match_count,
        "candidate_count_by_bucket": candidate_count_by_bucket,
        "candidate_sample_by_bucket": candidate_sample_by_bucket,
        "candidate_score_breakdown_by_bucket": candidate_score_breakdown_by_bucket,
        "candidate_core_token_match_by_bucket": (
            candidate_core_token_match_by_bucket
        ),
        "candidate_generic_token_match_by_bucket": (
            candidate_generic_token_match_by_bucket
        ),
        "candidate_fetch_keywords_by_bucket": candidate_fetch_keywords_by_bucket,
        "polymarket_team_pairs_by_bucket": team_pairs_by_bucket,
        "polymarket_team_pair_count_by_bucket": team_pair_count_by_bucket,
        "lol_bucket_team_sample": (
            team_pairs_by_bucket if _sport_filter_key(sport) == "lol" else {}
        ),
        "lol_candidate_count": (
            candidate_count_by_bucket if _sport_filter_key(sport) == "lol" else {}
        ),
        "lol_candidate_score_breakdown": (
            candidate_score_breakdown_by_bucket
            if _sport_filter_key(sport) == "lol"
            else {}
        ),
        "lol_candidate_fetch_keywords": (
            candidate_fetch_keywords_by_bucket
            if _sport_filter_key(sport) == "lol"
            else {}
        ),
        "lol_verified_team_overlap": lol_verified_team_overlap,
        "rejected_lol_candidate_reasons": rejected_lol_candidate_reasons,
        "attempted_ids_by_bucket": {
            key: _dedupe_searches(value)
            for key, value in attempted_ids_by_bucket.items()
        },
        "attempt_results_by_bucket": attempt_results_by_bucket,
        "selected_ids_by_bucket": {
            key: _dedupe_searches(value)
            for key, value in selected_ids_by_bucket.items()
        },
        "missing_bucket_names": _dedupe_searches(unresolved_buckets),
        "known_51042_seen": bool(known_51042),
        "known_51042_name": _text(known_51042.get("name")) if known_51042 else "",
        "known_51042_rank": int(known_51042.get("rank", 0)) if known_51042 else 0,
        "rate_limit_count": rate_limit_count,
        "stopped_reason": stopped_reason,
        "error_count": error_count,
        "oddspapi_tournament_list_cache_hit": tournament_list_cache_hit,
        "oddspapi_tournament_list_request_count": tournament_list_request_count,
        "active_tournament_cache_enabled": active_cache_enabled,
        "active_tournament_cache_hit_count": len(active_cache_hits),
        "active_tournament_cache_miss_count": len(active_cache_misses),
        "active_tournament_cache_hits": active_cache_hits,
        "active_tournament_cache_misses": active_cache_misses,
        "active_tournament_cache_updated": active_cache_updated,
        "selected_tournament_ids_by_bucket": selected_ids_by_bucket,
        "unresolved_tournament_buckets": _dedupe_searches(unresolved_buckets),
        "rejected_tournament_candidates_by_bucket": rejected_candidates_by_bucket,
        "oddspapi_batch_tournament_ids": _dedupe_searches(
            _text_list(verification_fetch.get("batch_tournament_ids"))
            if verification_fetch
            else []
        ),
        "oddspapi_batch_request_count": batch_request_count,
        "oddspapi_request_count": request_count,
        "discovery_skipped_due_to_mode": _dedupe_searches(
            discovery_skipped_due_to_mode
        ),
        "discovery_skipped_due_to_budget": _dedupe_searches(
            discovery_skipped_due_to_budget
        ),
    }


def _fetch_stake_tournament_candidates_for_bucket(
    fetch_candidates: Callable[..., object],
    *,
    sport: str,
    bucket_name: str,
    hints: list[dict[str, Any]],
    request_delay_seconds: float,
    rate_limit_retries: int,
    sleeper: Callable[[float], None],
) -> dict[str, Any]:
    candidate_keywords = _stake_tournament_candidate_keywords_for_bucket(
        bucket_name,
        hints,
    )
    rate_limit_count = 0
    attempts = 0
    max_retries = max(int(rate_limit_retries), 0)
    while True:
        try:
            candidate_args: dict[str, object] = {
                "sport": sport,
                "max_candidates": 100,
            }
            if candidate_keywords:
                candidate_args["tournament_keywords"] = candidate_keywords
            candidates = fetch_candidates(**candidate_args)
            if not candidates and candidate_keywords:
                candidates = fetch_candidates(sport=sport, max_candidates=100)
            return {
                "candidates": [
                    item for item in candidates if isinstance(item, dict)
                ],
                "rate_limit_count": rate_limit_count,
                "stopped_reason": "",
                "keywords": candidate_keywords,
            }
        except ProviderRequestError as exc:
            if not _is_rate_limit_error(exc):
                raise
            rate_limit_count += 1
            if attempts >= max_retries:
                return {
                    "candidates": [],
                    "rate_limit_count": rate_limit_count,
                    "stopped_reason": "rate_limited",
                    "keywords": candidate_keywords,
                }
            attempts += 1
            _sleep_for_rate_limit(
                exc,
                fallback_delay_seconds=max(float(request_delay_seconds), 0.0),
                sleeper=sleeper,
            )


def _read_json_cache(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _cache_timestamp() -> str:
    return _utc_iso_z(datetime.now(timezone.utc))


def _parse_cache_time(value: object) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _cache_entry_is_fresh(value: object, *, ttl_hours: float) -> bool:
    fetched_at = _parse_cache_time(value)
    if fetched_at is None:
        return False
    return datetime.now(timezone.utc) - fetched_at <= timedelta(
        hours=max(float(ttl_hours), 0.0)
    )


def _sport_cache_key(sport: str) -> str:
    return _sport_filter_key(sport)


def _bookmaker_cache_key(bookmaker: str) -> str:
    return _text(bookmaker).casefold()


def _bucket_cache_key(bucket_name: str) -> str:
    return _tournament_bucket_key(bucket_name)


def _active_tournament_cache_entry(
    cache: dict[str, Any],
    *,
    sport: str,
    bookmaker: str,
    bucket_name: str,
    ttl_hours: float,
    refresh: bool,
) -> dict[str, Any]:
    if refresh:
        return {}
    sport_cache = cache.get(_sport_cache_key(sport))
    if not isinstance(sport_cache, dict):
        return {}
    bookmaker_cache = sport_cache.get(_bookmaker_cache_key(bookmaker))
    if not isinstance(bookmaker_cache, dict):
        return {}
    entry = bookmaker_cache.get(_bucket_cache_key(bucket_name))
    if not isinstance(entry, dict):
        return {}
    if entry.get("stale") is True:
        return {}
    if not _cache_entry_is_fresh(entry.get("last_verified_at"), ttl_hours=ttl_hours):
        return {}
    if not _text(entry.get("tournament_id")):
        return {}
    return entry


def _update_active_tournament_cache_success(
    cache: dict[str, Any],
    *,
    sport: str,
    bookmaker: str,
    bucket_name: str,
    tournament_id: str,
    tournament_name: str,
    fixture_count: int,
    aliases: list[str],
) -> None:
    sport_cache = cache.setdefault(_sport_cache_key(sport), {})
    if not isinstance(sport_cache, dict):
        return
    bookmaker_cache = sport_cache.setdefault(_bookmaker_cache_key(bookmaker), {})
    if not isinstance(bookmaker_cache, dict):
        return
    bookmaker_cache[_bucket_cache_key(bucket_name)] = {
        "tournament_id": _numeric_text(tournament_id),
        "tournament_name": tournament_name,
        "last_verified_at": _cache_timestamp(),
        "fixture_count": int(fixture_count),
        "source": "verified_team_overlap",
        "aliases": _dedupe_searches(aliases),
    }


def _mark_active_tournament_cache_failures(
    *,
    active_tournament_cache_path: Path,
    enabled: bool,
    sport: str,
    bookmaker: str,
    active_cache_hits: list[str],
    requested_tournament_ids: list[str],
    skipped_tournament_reasons: dict[str, str],
    stake_events: list[ProviderOddsEvent],
    matches: list[dict[str, Any]],
) -> list[dict[str, str]]:
    if not enabled or not active_cache_hits:
        return []
    requested_ids = set(_text_list(requested_tournament_ids))
    if not requested_ids:
        return []

    events_by_id = _events_by_tournament_id(
        stake_events,
        requested_ids=_text_list(requested_tournament_ids),
    )
    matched_ids = {
        tournament_id
        for tournament_id in (
            _event_dict_tournament_id(match.get("left_event"))
            for match in matches
            if isinstance(match, dict)
        )
        if tournament_id
    }
    failures: list[dict[str, str]] = []
    for hit in active_cache_hits:
        if ":" not in hit:
            continue
        bucket_name, tournament_id = hit.rsplit(":", 1)
        tournament_id = _text(tournament_id)
        if not tournament_id or tournament_id not in requested_ids:
            continue
        reason = skipped_tournament_reasons.get(tournament_id, "")
        if not reason and not events_by_id.get(tournament_id):
            reason = "no_fixtures"
        if not reason and tournament_id not in matched_ids:
            reason = "no_team_overlap"
        if reason:
            failures.append(
                {
                    "bucket": bucket_name,
                    "tournament_id": tournament_id,
                    "reason": reason,
                }
            )
    if not failures:
        return []

    cache = _read_json_cache(active_tournament_cache_path)
    for failure in failures:
        sport_cache = cache.get(_sport_cache_key(sport))
        if not isinstance(sport_cache, dict):
            continue
        bookmaker_cache = sport_cache.get(_bookmaker_cache_key(bookmaker))
        if not isinstance(bookmaker_cache, dict):
            continue
        entry = bookmaker_cache.get(_bucket_cache_key(failure["bucket"]))
        if not isinstance(entry, dict):
            continue
        entry["fail_count"] = _int_value(entry.get("fail_count")) + 1
        entry["last_failed_at"] = _cache_timestamp()
        entry["last_failure_reason"] = failure["reason"]
        entry["stale"] = True
    _write_json_cache(active_tournament_cache_path, cache)
    return failures


def _event_dict_tournament_id(event: object) -> str:
    if not isinstance(event, dict):
        return ""
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    raw_event = raw.get("event") if isinstance(raw.get("event"), dict) else {}
    return _text(
        raw.get("tournament_id")
        or raw.get("tournamentId")
        or raw_event.get("tournamentId")
        or raw_event.get("tournament_id")
    )


def _load_or_fetch_tournament_list(
    provider: object,
    *,
    sport: str,
    bookmaker: str,
    cache_path: Path,
    ttl_hours: float,
    refresh: bool,
    disabled: bool,
    request_budget: int,
) -> dict[str, Any]:
    sport_key = _sport_cache_key(sport)
    cache = {} if disabled else _read_json_cache(cache_path)
    sports_cache = cache.get("sports") if isinstance(cache.get("sports"), dict) else {}
    cache_entry = sports_cache.get(sport_key) if isinstance(sports_cache, dict) else None
    if (
        not disabled
        and not refresh
        and isinstance(cache_entry, dict)
        and _cache_entry_is_fresh(cache_entry.get("fetched_at"), ttl_hours=ttl_hours)
    ):
        candidates = cache_entry.get("candidates")
        return {
            "candidates": _dicts_from_cache(candidates),
            "cache_hit": True,
            "request_count": 0,
            "skipped_due_to_budget": False,
        }

    if request_budget <= 0:
        return {
            "candidates": [],
            "cache_hit": False,
            "request_count": 0,
            "skipped_due_to_budget": True,
        }

    fetch_tournament_list = getattr(provider, "fetch_tournament_list", None)
    if callable(fetch_tournament_list):
        candidates = fetch_tournament_list(sport=sport)
    else:
        fetch_candidates = getattr(provider, "fetch_tournament_candidates", None)
        if not callable(fetch_candidates):
            candidates = []
        else:
            candidates = fetch_candidates(sport=sport, max_candidates=100)
    clean_candidates = [item for item in candidates if isinstance(item, dict)]
    if not disabled:
        sport_id = _resolve_sport_id_safe(sport)
        cache.setdefault("sports", {})
        if isinstance(cache["sports"], dict):
            cache["sports"][sport_key] = {
                "sport": sport,
                "sport_id": sport_id,
                "bookmaker": bookmaker,
                "source": "oddspapi",
                "fetched_at": _cache_timestamp(),
                "candidates": clean_candidates,
            }
            _write_json_cache(cache_path, cache)
    return {
        "candidates": clean_candidates,
        "cache_hit": False,
        "request_count": 1,
        "skipped_due_to_budget": False,
    }


def _resolve_sport_id_safe(sport: str) -> str:
    try:
        return resolve_sport_id(sport)
    except ValueError:
        return _text(sport)


def _dicts_from_cache(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


_STRICT_TOURNAMENT_CORE_TOKENS = {
    "cct",
    "esl",
    "ewc",
    "iem",
    "lck",
    "lcs",
    "lec",
    "lpl",
    "lta",
    "msi",
    "pgl",
    "prime",
    "worlds",
    "xse",
    "thunderpick",
    "draculan",
    "united21",
}


_STRICT_TOURNAMENT_GENERIC_TOKENS = {
    "america",
    "american",
    "championship",
    "cup",
    "league",
    "open",
    "playoff",
    "playoffs",
    "pro",
    "qualifier",
    "regional",
    "season",
    "series",
    "south",
    "stage",
}


_STRICT_TOURNAMENT_PENALTY_TOKENS = {
    "archive",
    "archives",
    "challenger",
    "challengers",
    "qualifier",
    "qualifiers",
}


def _score_strict_tournament_candidates(
    bucket_name: str,
    candidates: list[dict[str, Any]],
    *,
    sport: str = "",
) -> list[dict[str, Any]]:
    scored = [
        _score_strict_tournament_candidate(bucket_name, candidate, sport=sport)
        for candidate in candidates
    ]
    scored = [item for item in scored if float(item.get("score") or 0) > 0]
    scored.sort(
        key=lambda item: (
            -float(item.get("score") or 0),
            -len(item.get("matched_core_tokens") or []),
            -len(item.get("matched_tokens") or []),
            _text(item.get("name")),
        )
    )
    for index, item in enumerate(scored, 1):
        item["rank"] = index
    return scored


def _score_lol_team_probe_candidates(
    bucket_name: str,
    candidates: list[dict[str, Any]],
    *,
    team_pair_events: list[ProviderOddsEvent],
    sport: str,
) -> list[dict[str, Any]]:
    if _sport_filter_key(sport) != "lol":
        return []
    team_tokens = _lol_team_probe_tokens_from_events(team_pair_events)
    if not team_tokens:
        return []
    scored: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_id = _text(
            candidate.get("id")
            or candidate.get("tournament_id")
            or candidate.get("tournamentId")
        )
        if not candidate_id:
            continue
        candidate_name = _text(
            candidate.get("name")
            or candidate.get("tournament_name")
            or candidate.get("tournamentName")
            or candidate.get("title")
        )
        candidate_text = _stake_tournament_candidate_text(candidate)
        rejection_reason = _lol_stake_candidate_rejection_reason(
            bucket_name,
            candidate_text,
            sport=sport,
        )
        if rejection_reason:
            continue
        candidate_tokens = _lol_team_probe_tokens(candidate_text)
        matched_team_tokens = candidate_tokens & team_tokens
        if not matched_team_tokens:
            continue
        score = 0.52 + min(0.24, len(matched_team_tokens) * 0.06)
        scored.append(
            {
                "id": candidate_id,
                "name": candidate_name,
                "score": round(min(score, 0.76), 4),
                "matched_tokens": sorted(matched_team_tokens),
                "rejected_tokens": [],
                "core_tokens": [],
                "matched_core_tokens": [],
                "generic_tokens": [],
                "core_token_match": False,
                "generic_token_match": False,
                "source_hint": bucket_name,
                "reason": "lol_team_probe_team_token_overlap",
                "verification_mode": "lol_team_probe",
                "matched_team_tokens": sorted(matched_team_tokens),
            }
        )
    scored.sort(
        key=lambda item: (
            -len(item.get("matched_team_tokens") or []),
            -float(item.get("score") or 0),
            _text(item.get("name")),
            _text(item.get("id")),
        )
    )
    for index, item in enumerate(scored, 1):
        item["rank"] = index
    return scored


def _score_strict_tournament_candidate(
    bucket_name: str,
    candidate: dict[str, Any],
    *,
    sport: str = "",
) -> dict[str, Any]:
    candidate_id = _text(
        candidate.get("id")
        or candidate.get("tournament_id")
        or candidate.get("tournamentId")
    )
    candidate_name = _text(
        candidate.get("name")
        or candidate.get("tournament_name")
        or candidate.get("tournamentName")
        or candidate.get("title")
    )
    bucket_tokens = _strict_tournament_tokens(bucket_name)
    candidate_text = _stake_tournament_candidate_text(candidate)
    candidate_tokens = _strict_tournament_tokens(candidate_text)
    bucket_core = _strict_core_tokens(bucket_tokens)
    candidate_core = _strict_core_tokens(candidate_tokens)
    matched_tokens = bucket_tokens & candidate_tokens
    matched_core = bucket_core & candidate_core
    rejected_tokens = _direction_rejected_tokens(candidate_tokens, bucket_tokens)
    lol_rejection_reason = _lol_stake_candidate_rejection_reason(
        bucket_name,
        candidate_text,
        sport=sport,
    )
    reason = ""
    score = 0.0
    if lol_rejection_reason:
        reason = lol_rejection_reason
    elif not bucket_tokens or not candidate_tokens:
        reason = "missing_tokens"
    elif _tournament_bucket_key(bucket_name) == _tournament_bucket_key(candidate_name):
        score = 1.0
        reason = "exact_match"
    elif bucket_core and not matched_core:
        reason = "missing_core_token"
    elif not bucket_core and matched_tokens <= _STRICT_TOURNAMENT_GENERIC_TOKENS:
        reason = "generic_only"
    elif bucket_core and matched_tokens <= _STRICT_TOURNAMENT_GENERIC_TOKENS:
        reason = "generic_only"
    else:
        coverage = len(matched_tokens) / max(len(bucket_tokens), 1)
        score = 0.55 if matched_core else 0.25
        score += min(0.35, coverage * 0.35)
        if matched_core:
            score += min(0.1, len(matched_core) * 0.05)
        if rejected_tokens:
            score -= 0.35
            reason = "rejected_direction_token"
        elif candidate_tokens & _STRICT_TOURNAMENT_PENALTY_TOKENS:
            score -= 0.15
            reason = "penalized_candidate_token"
        else:
            reason = "token_match"
    score = max(0.0, min(score, 1.0))
    return {
        "id": candidate_id,
        "name": candidate_name,
        "score": round(score, 4),
        "matched_tokens": sorted(matched_tokens),
        "rejected_tokens": sorted(rejected_tokens),
        "core_tokens": sorted(bucket_core),
        "matched_core_tokens": sorted(matched_core),
        "generic_tokens": sorted(matched_tokens & _STRICT_TOURNAMENT_GENERIC_TOKENS),
        "core_token_match": bool(matched_core),
        "generic_token_match": bool(matched_tokens & _STRICT_TOURNAMENT_GENERIC_TOKENS),
        "source_hint": bucket_name,
        "reason": reason,
    }


def _rejected_lol_candidate_reasons_for_bucket(
    bucket_name: str,
    candidates: list[dict[str, Any]],
    *,
    sport: str,
) -> list[dict[str, str]]:
    if _sport_filter_key(sport) != "lol":
        return []
    rejected: list[dict[str, str]] = []
    for candidate in candidates:
        candidate_text = _stake_tournament_candidate_text(candidate)
        reason = _lol_stake_candidate_rejection_reason(
            bucket_name,
            candidate_text,
            sport=sport,
        )
        if not reason:
            continue
        rejected.append(
            {
                "id": _text(
                    candidate.get("id")
                    or candidate.get("tournament_id")
                    or candidate.get("tournamentId")
                ),
                "name": _text(
                    candidate.get("name")
                    or candidate.get("tournament_name")
                    or candidate.get("tournamentName")
                    or candidate.get("title")
                ),
                "reason": reason,
            }
        )
        if len(rejected) >= 10:
            break
    return rejected


def _required_verified_team_overlap(
    *,
    sport: str,
    team_pair_event_count: int,
) -> int:
    if _sport_filter_key(sport) != "lol":
        return 1
    event_count = max(int(team_pair_event_count), 0)
    if event_count <= 1:
        return 1
    return min(2, event_count)


def _strict_tournament_tokens(value: str) -> set[str]:
    tokens = set(_tournament_bucket_key(value).split())
    if "american" in tokens:
        tokens.add("america")
    if "america" in tokens:
        tokens.add("american")
    if "united" in tokens and "21" in tokens:
        tokens.add("united21")
    if {"mid", "season", "invitational"} <= tokens:
        tokens.add("msi")
    return {token for token in tokens if token}


def _strict_core_tokens(tokens: set[str]) -> set[str]:
    core = tokens & _STRICT_TOURNAMENT_CORE_TOKENS
    if "dracula" in tokens or "draculan" in tokens:
        core.add("draculan")
    return core


def _dedupe_candidate_attempts(
    candidates: list[dict[str, Any]],
    *,
    max_candidates: int,
) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        candidate = item.get("candidate")
        if not isinstance(candidate, dict):
            continue
        candidate_id = _text(candidate.get("id"))
        if not candidate_id or candidate_id in seen:
            continue
        seen.add(candidate_id)
        deduped.append(item)
        if len(deduped) >= max_candidates:
            break
    return deduped


def _events_by_tournament_id(
    events: list[ProviderOddsEvent],
    *,
    requested_ids: list[str],
) -> dict[str, list[ProviderOddsEvent]]:
    grouped: dict[str, list[ProviderOddsEvent]] = {}
    for event in events:
        tournament_id = _provider_event_tournament_id(event)
        if tournament_id:
            grouped.setdefault(tournament_id, []).append(event)
    if not grouped and len(requested_ids) == 1:
        grouped[requested_ids[0]] = list(events)
    return grouped


def _provider_event_tournament_id(event: ProviderOddsEvent) -> str:
    raw = event.raw if isinstance(event.raw, dict) else {}
    raw_event = raw.get("event") if isinstance(raw.get("event"), dict) else {}
    return _text(
        raw.get("tournament_id")
        or raw.get("tournamentId")
        or raw_event.get("tournamentId")
        or raw_event.get("tournament_id")
    )


def _numeric_text(value: object) -> object:
    text = _text(value)
    if text.isdigit():
        return int(text)
    return text


def _dedupe_scored_tournament_candidates(
    scored: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    best_by_id: dict[str, dict[str, Any]] = {}
    for item in scored:
        candidate_id = _text(item.get("id"))
        if not candidate_id:
            continue
        previous = best_by_id.get(candidate_id)
        if previous is None or float(item.get("score") or 0) > float(
            previous.get("score") or 0
        ):
            best_by_id[candidate_id] = dict(item)
    deduped = sorted(
        best_by_id.values(),
        key=lambda item: (
            -float(item.get("score") or 0),
            -_int_value(item.get("id")),
            _text(item.get("name")),
        ),
    )
    for index, item in enumerate(deduped, 1):
        item["rank"] = index
    return deduped


def _bucket_round_robin_candidates(
    contexts: list[dict[str, Any]],
    *,
    max_candidates: int,
) -> list[dict[str, Any]]:
    if max_candidates <= 0:
        return []
    queued: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    indexes = {index: 0 for index, _context in enumerate(contexts)}
    best_bucket_by_id = _best_candidate_bucket_by_id(contexts)
    while len(queued) < max_candidates:
        added = False
        for context_index, context in enumerate(contexts):
            bucket_name = _text(context.get("bucket"))
            candidates = [
                item
                for item in context.get("candidates", [])
                if isinstance(item, dict) and _text(item.get("id"))
            ]
            candidate_index = indexes[context_index]
            while candidate_index < len(candidates):
                candidate = candidates[candidate_index]
                candidate_index += 1
                candidate_id = _text(candidate.get("id"))
                if candidate_id in seen_ids:
                    continue
                if best_bucket_by_id.get(candidate_id) != bucket_name:
                    continue
                seen_ids.add(candidate_id)
                indexes[context_index] = candidate_index
                queued.append(
                    {
                        "bucket": bucket_name,
                        "candidate": candidate,
                        "team_pair_events": context.get("team_pair_events", []),
                    }
                )
                added = True
                break
            indexes[context_index] = candidate_index
            if len(queued) >= max_candidates:
                break
        if not added:
            break
    return queued


def _best_candidate_bucket_by_id(contexts: list[dict[str, Any]]) -> dict[str, str]:
    best: dict[str, tuple[tuple[float, int, int], str]] = {}
    for context_index, context in enumerate(contexts):
        bucket_name = _text(context.get("bucket"))
        for candidate in context.get("candidates", []):
            if not isinstance(candidate, dict):
                continue
            candidate_id = _text(candidate.get("id"))
            if not candidate_id:
                continue
            priority = (
                len(candidate.get("matched_core_tokens") or []),
                float(candidate.get("score") or 0),
                -context_index,
            )
            previous = best.get(candidate_id)
            if previous is None or priority > previous[0]:
                best[candidate_id] = (priority, bucket_name)
    return {candidate_id: bucket for candidate_id, (_priority, bucket) in best.items()}


def _score_stake_tournament_candidates(
    candidates: list[dict[str, Any]],
    *,
    hints: list[dict[str, Any]],
    sport: str = "",
) -> list[dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_text = _stake_tournament_candidate_text(candidate)
        candidate_tokens = _stake_auto_tokens(candidate_text)
        candidate_core_labels = _stake_tournament_core_labels(candidate_text)
        best_score = 0.0
        best_matched_tokens: set[str] = set()
        best_rejected_tokens: set[str] = set()
        best_core_labels: set[str] = set()
        best_matched_core_labels: set[str] = set()
        best_generic_tokens: set[str] = set()
        best_source_hint = ""
        rejection_reason = ""
        for hint in hints:
            hint_tokens = hint.get("tokens")
            if not isinstance(hint_tokens, set) or not hint_tokens:
                continue
            hint_core_labels = _stake_tournament_core_labels(
                _text(hint.get("source_hint")),
            )
            (
                score,
                matched_tokens,
                rejected_tokens,
                score_details,
            ) = _score_stake_tournament_hint(
                candidate_tokens,
                hint_tokens,
                candidate_core_labels=candidate_core_labels,
                hint_core_labels=hint_core_labels,
            )
            if score > best_score or (
                score == best_score
                and not best_source_hint
                and (matched_tokens or rejected_tokens)
            ):
                best_score = score
                best_matched_tokens = matched_tokens
                best_rejected_tokens = rejected_tokens
                best_core_labels = hint_core_labels
                best_matched_core_labels = score_details["matched_core_labels"]
                best_generic_tokens = score_details["matched_generic_tokens"]
                best_source_hint = _text(hint.get("source_hint"))
        if _sport_filter_key(sport) == "lol":
            rejection_reason = _lol_stake_candidate_rejection_reason(
                best_source_hint,
                candidate_text,
                sport=sport,
            )
            if rejection_reason:
                best_score = 0.0
                best_matched_tokens = set()
                best_rejected_tokens = set()
                best_matched_core_labels = set()
                best_generic_tokens = set()
        scored.append(
            {
                "id": _text(candidate.get("id")),
                "name": _text(candidate.get("name")),
                "score": round(best_score, 4),
                "matched_tokens": sorted(best_matched_tokens),
                "rejected_tokens": sorted(best_rejected_tokens),
                "core_tokens": sorted(best_core_labels),
                "matched_core_tokens": sorted(best_matched_core_labels),
                "generic_tokens": sorted(best_generic_tokens),
                "core_token_match": bool(best_matched_core_labels),
                "generic_token_match": bool(best_generic_tokens),
                "source_hint": best_source_hint,
                "reason": rejection_reason,
            }
        )
    scored = sorted(
        scored,
        key=lambda item: (
            -float(item["score"]),
            -_int_value(item.get("id")),
            _text(item["name"]),
            _text(item["id"]),
        ),
    )
    for index, item in enumerate(scored, 1):
        item["rank"] = index
    return scored


def _stake_tournament_raw_candidate_sample(
    scored: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "id": _text(item.get("id")),
            "name": _text(item.get("name")),
            "score": item.get("score", 0),
        }
        for item in scored[:10]
    ]


def _stake_tournament_score_breakdown_sample(
    scored: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "id": _text(item.get("id")),
            "name": _text(item.get("name")),
            "score": item.get("score", 0),
            "core": _csv(item.get("matched_core_tokens")),
            "generic": _csv(item.get("generic_tokens")),
            "matched": _csv(item.get("matched_tokens")),
            "reason": _text(item.get("reason")),
        }
        for item in scored[:10]
    ]


def _stake_tournament_core_token_match_sample(
    scored: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "id": _text(item.get("id")),
            "name": _text(item.get("name")),
            "core": _csv(item.get("core_tokens")),
            "matched_core": _csv(item.get("matched_core_tokens")),
        }
        for item in scored[:10]
    ]


def _stake_tournament_generic_token_match_sample(
    scored: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "id": _text(item.get("id")),
            "name": _text(item.get("name")),
            "generic": _csv(item.get("generic_tokens")),
        }
        for item in scored[:10]
    ]


def _known_tournament_candidate(
    scored: list[dict[str, Any]],
    tournament_id: str,
) -> dict[str, Any]:
    for item in scored:
        if _text(item.get("id")) == tournament_id:
            return item
    return {}


def _score_stake_tournament_hint(
    candidate_tokens: set[str],
    hint_tokens: set[str],
    *,
    candidate_core_labels: set[str],
    hint_core_labels: set[str],
) -> tuple[float, set[str], set[str], dict[str, set[str]]]:
    if not candidate_tokens or not hint_tokens:
        return 0.0, set(), set(), _stake_tournament_empty_score_details()

    matched_tokens = candidate_tokens & hint_tokens
    if not matched_tokens:
        return 0.0, set(), set(), _stake_tournament_empty_score_details()

    rejected_tokens = _direction_rejected_tokens(candidate_tokens, hint_tokens)
    matched_core_labels = candidate_core_labels & hint_core_labels
    matched_generic_tokens = matched_tokens & _STAKE_AUTO_GENERIC_TOURNAMENT_TOKENS
    matched_specific_tokens = (
        matched_tokens
        - matched_generic_tokens
        - _STAKE_AUTO_CORE_LABEL_WORDS
    )

    if hint_core_labels:
        if matched_core_labels:
            score = 0.72
            score += min(0.18, len(matched_generic_tokens) * 0.06)
            score += min(0.08, len(matched_specific_tokens) * 0.04)
        else:
            score = min(0.18, len(matched_generic_tokens) * 0.04)
            score += min(0.04, len(matched_specific_tokens) * 0.02)
    else:
        score = min(0.35, len(matched_generic_tokens) * 0.04)
        score += min(0.25, len(matched_specific_tokens) * 0.08)

    score -= sum(
        _STAKE_AUTO_REJECTED_TOKEN_PENALTIES.get(token, 0.25)
        for token in rejected_tokens
    )
    return (
        max(0.0, min(score, 1.0)),
        matched_tokens,
        rejected_tokens,
        {
            "matched_core_labels": matched_core_labels,
            "matched_generic_tokens": matched_generic_tokens,
        },
    )


def _stake_tournament_empty_score_details() -> dict[str, set[str]]:
    return {
        "matched_core_labels": set(),
        "matched_generic_tokens": set(),
    }


def _direction_rejected_tokens(
    candidate_tokens: set[str],
    hint_tokens: set[str],
) -> set[str]:
    rejected: set[str] = set()
    if "south" in hint_tokens and "north" in candidate_tokens:
        rejected.add("north")
    if {"south", "america"} <= hint_tokens:
        for token in ("europe", "european", "asia", "asian"):
            if token in candidate_tokens:
                rejected.add(token)
    return rejected


def _only_generic_tournament_tokens(tokens: set[str]) -> bool:
    return bool(tokens) and tokens <= _STAKE_AUTO_GENERIC_TOURNAMENT_TOKENS


def _stake_tournament_core_labels(value: str) -> set[str]:
    key = _tournament_bucket_key(value)
    labels: set[str] = set()
    if not key:
        return labels
    tokens = set(key.split())
    if "cct" in tokens:
        labels.add("cct")
    if "thunderpick" in tokens:
        labels.add("thunderpick")
    if "xse" in tokens:
        labels.add("xse")
    if "draculan" in tokens or "dracula" in tokens:
        labels.add("super_draculan")
    if {"gamers", "club"} <= tokens:
        labels.add("gamers_club")
    if "united21" in tokens or ("united" in tokens and "21" in tokens):
        labels.add("united21")
    if "msi" in tokens or {"mid", "season", "invitational"} <= tokens:
        labels.add("msi")
    if "worlds" in tokens or {"world", "championship"} <= tokens:
        labels.add("worlds")
    if "ewc" in tokens or {"esports", "world", "cup"} <= tokens:
        labels.add("ewc")
    for label in ("lec", "lck", "lpl", "lcs", "lta"):
        if label in tokens:
            labels.add(label)
    if "prime" in tokens and "league" in tokens:
        labels.add("prime_league")
    if (
        ("european" in tokens or "europe" in tokens)
        and "pro" in tokens
        and "league" in tokens
    ):
        labels.add("european_pro_league")
    return labels


def _polymarket_tournament_hints(
    events: list[ProviderOddsEvent],
    *,
    sport: str,
) -> list[dict[str, Any]]:
    hints: list[dict[str, Any]] = []
    for event in events:
        raw = event.raw if isinstance(event.raw, dict) else {}
        raw_texts = _text_values_from_raw(raw)
        tournament_texts = [
            event.tournament or "",
            *_tournament_suffix_texts(raw_texts),
            *_slug_tournament_texts(raw),
        ]
        tournament_texts = _dedupe_searches(
            [text for text in tournament_texts if _text(text)]
        )
        scoring_text = " ".join(tournament_texts)
        tokens = _stake_auto_tokens(scoring_text)
        if not tokens:
            continue
        source_hint = _first_non_empty(tournament_texts)
        hints.append(
            {
                "sport": _text(event.game or sport),
                "title": _first_non_empty(raw_texts),
                "tournament": source_hint,
                "slug": _text(raw.get("event_slug") or raw.get("slug")),
                "team_a": event.team_a,
                "team_b": event.team_b,
                "start_time": event.start_time.isoformat() if event.start_time else "",
                "source_hint": source_hint,
                "tokens": tokens,
            }
        )
    return hints


def _polymarket_tournament_hints_sample(
    hints: list[dict[str, Any]],
) -> list[str]:
    samples: list[str] = []
    for hint in hints[:5]:
        teams = " vs ".join(
            part for part in (_text(hint.get("team_a")), _text(hint.get("team_b"))) if part
        )
        source_hint = _text(hint.get("source_hint"))
        if teams and source_hint:
            samples.append(f"{teams} | {source_hint}")
        elif source_hint:
            samples.append(source_hint)
        elif teams:
            samples.append(teams)
    return samples


def _stake_tournament_candidate_keywords(hints: list[dict[str, Any]]) -> list[str]:
    keywords: list[str] = []
    for hint in hints:
        source_hint = _text(hint.get("source_hint"))
        if source_hint:
            keywords.append(source_hint)
        tokens = hint.get("tokens")
        if not isinstance(tokens, set):
            continue
        if {"cct", "south", "america"} <= tokens:
            keywords.extend(
                [
                    "CCT South America",
                    "CCT South American",
                    "South America",
                    "South American",
                ]
            )
        elif {"south", "america"} <= tokens:
            keywords.extend(["South America", "South American"])
        elif "cct" in tokens:
            keywords.append("CCT")
    return _dedupe_searches(keywords)[:20]


def _stake_tournament_candidate_keywords_for_bucket(
    bucket_name: str,
    hints: list[dict[str, Any]],
) -> list[str]:
    keywords: list[str] = []
    bucket_name = _text(bucket_name)
    bucket_core = _stake_tournament_core_labels(bucket_name)
    keywords.extend(_stake_tournament_bucket_keyword_aliases(bucket_name, bucket_core))

    for hint in hints:
        source_hint = _text(hint.get("source_hint"))
        if not source_hint:
            continue
        hint_core = _stake_tournament_core_labels(source_hint)
        if hint_core & bucket_core:
            keywords.extend(
                _stake_tournament_bucket_keyword_aliases(source_hint, hint_core)
            )

    if keywords:
        return _dedupe_searches(keywords)[:20]
    return _stake_tournament_candidate_keywords(hints)


def _stake_tournament_bucket_keyword_aliases(
    value: str,
    core_labels: set[str],
) -> list[str]:
    key = _tournament_bucket_key(value)
    aliases: list[str] = []
    if "cct" in core_labels:
        if "south" in key and ("america" in key or "american" in key):
            aliases.extend(
                [
                    "CCT South American Series",
                    "CCT South America Series",
                    "CCT South American",
                    "CCT South America",
                ]
            )
        elif "europe" in key or "european" in key:
            aliases.extend(["CCT Europe", "CCT European"])
        else:
            aliases.append("CCT")
    if "thunderpick" in core_labels:
        aliases.extend(["Thunderpick World Championship", "Thunderpick"])
    if "xse" in core_labels:
        aliases.extend(["XSE Pro League", "XSE"])
    if "super_draculan" in core_labels:
        aliases.extend(["Super DraculaN", "DraculaN"])
    if "gamers_club" in core_labels:
        aliases.extend(["Gamers Club"])
    if "united21" in core_labels:
        aliases.extend(["United21", "United 21"])
    if "msi" in core_labels:
        aliases.extend(["MSI", "Mid-Season Invitational", "Mid Season Invitational"])
    if "worlds" in core_labels:
        aliases.extend(["Worlds", "World Championship"])
    if "ewc" in core_labels:
        aliases.extend(["Esports World Cup", "EWC"])
    for label in ("lec", "lck", "lpl", "lcs", "lta"):
        if label in core_labels:
            aliases.append(label.upper())
    if "prime_league" in core_labels:
        aliases.extend(
            [
                "Prime League 1st Division",
                "Prime League Pro Division",
                "Prime League 1. Liga",
                "Techniker Prime League 1st Division",
            ]
        )
    if "european_pro_league" in core_labels:
        aliases.append("European Pro League")
    return aliases


def filter_polymarket_events_by_tournament_quality(
    events: list[ProviderOddsEvent],
    *,
    sport: str,
    min_quality: str,
    allow_unknown: bool,
    include_low_tier: bool,
) -> dict[str, Any]:
    bucket_records: dict[str, dict[str, Any]] = {}
    kept_events: list[ProviderOddsEvent] = []
    rejected_events: list[ProviderOddsEvent] = []
    for event in events:
        bucket_name = _polymarket_event_tournament_bucket(event)
        quality = _tournament_quality_for_sport(bucket_name, sport=sport)
        keep, reason = _tournament_quality_keep_decision(
            quality,
            sport=sport,
            bucket_name=bucket_name,
            min_quality=min_quality,
            allow_unknown=allow_unknown,
            include_low_tier=include_low_tier,
        )
        record = bucket_records.setdefault(
            bucket_name,
            {
                "bucket": bucket_name,
                "quality": quality,
                "kept": keep,
                "reason": reason,
                "count": 0,
            },
        )
        record["count"] = int(record["count"]) + 1
        if keep:
            record["kept"] = True
            kept_events.append(event)
        else:
            rejected_events.append(event)

    return {
        "events": kept_events,
        "rejected_events": rejected_events,
        "bucket_count": len(bucket_records),
        "buckets_sample": list(bucket_records)[:10],
        "kept_count": len(kept_events),
        "rejected_count": len(rejected_events),
        "kept_sample": _tournament_quality_sample(kept_events, bucket_records),
        "rejected_sample": _tournament_quality_sample(
            rejected_events,
            bucket_records,
        ),
        "by_bucket": bucket_records,
    }


def _tournament_quality_keep_decision(
    quality: str,
    *,
    sport: str,
    bucket_name: str = "",
    min_quality: str,
    allow_unknown: bool,
    include_low_tier: bool,
) -> tuple[bool, str]:
    sport_key = _sport_filter_key(sport)
    if sport_key == "lol":
        rejection_reason = _lol_tournament_rejection_reason(bucket_name)
        if rejection_reason:
            return False, rejection_reason
        quality = _tournament_quality_key(quality)
        min_quality = _tournament_quality_key(min_quality)
        if quality == "unknown":
            return False, "lol_unknown_tournament"
        if _TOURNAMENT_QUALITY_ORDER[quality] >= _TOURNAMENT_QUALITY_ORDER[min_quality]:
            return True, f"quality_{quality}_meets_min_{min_quality}"
        return False, f"quality_{quality}_below_min_{min_quality}"
    if sport_key != "cs2":
        return True, "non_cs2_not_filtered"
    quality = _tournament_quality_key(quality)
    min_quality = _tournament_quality_key(min_quality)
    if quality == "unknown":
        if allow_unknown or min_quality == "unknown":
            return True, "unknown_allowed"
        return False, "unknown_tournament"
    if quality == "c" and include_low_tier:
        return True, "low_tier_included"
    if _TOURNAMENT_QUALITY_ORDER[quality] >= _TOURNAMENT_QUALITY_ORDER[min_quality]:
        return True, f"quality_{quality}_meets_min_{min_quality}"
    return False, f"quality_{quality}_below_min_{min_quality}"


def _tournament_quality_key(value: str) -> str:
    key = _text(value).casefold()
    if key in _TOURNAMENT_QUALITY_ORDER:
        return key
    return "unknown"


def _tournament_quality_sample(
    events: list[ProviderOddsEvent],
    bucket_records: dict[str, dict[str, Any]],
) -> list[str]:
    samples: list[str] = []
    for event in events[:10]:
        bucket_name = _polymarket_event_tournament_bucket(event)
        record = bucket_records.get(bucket_name, {})
        samples.append(
            "|".join(
                part
                for part in (
                    bucket_name,
                    _text(record.get("quality")),
                    _text(record.get("reason")),
                    f"{event.team_a} vs {event.team_b}",
                )
                if part
            )
        )
    return samples


def _rejected_tournament_reasons(
    bucket_records: dict[str, dict[str, Any]],
) -> dict[str, str]:
    return {
        _text(bucket): _text(record.get("reason"))
        for bucket, record in bucket_records.items()
        if isinstance(record, dict)
        and not bool(record.get("kept"))
        and _text(record.get("reason"))
    }


def _tournament_quality_for_sport(bucket_name: str, *, sport: str) -> str:
    sport_key = _sport_filter_key(sport)
    if sport_key == "lol":
        return _lol_tournament_quality(bucket_name)
    if sport_key != "cs2":
        return "unknown"
    return _cs2_tournament_quality(bucket_name)


def _cs2_tournament_quality(bucket_name: str) -> str:
    normalized = _tournament_bucket_key(bucket_name)
    if not normalized:
        return "unknown"
    if any(
        token in normalized
        for token in (
            "open qualifier",
            "challenger",
            "challengers",
            "play in",
            "playin",
        )
    ):
        return "c"
    if normalized in _CS2_TOURNAMENT_QUALITY:
        return _CS2_TOURNAMENT_QUALITY[normalized]
    if "thunderpick" in normalized and "world championship" in normalized:
        if "regional" in normalized or "series" in normalized:
            return "b"
        return "a"
    if "cct" in normalized and "south" in normalized and "america" in normalized:
        return "b"
    if "cct" in normalized and "europe" in normalized and "series" in normalized:
        return "b"
    return "unknown"


def _lol_tournament_quality(bucket_name: str) -> str:
    normalized = _tournament_bucket_key(bucket_name)
    if not normalized or _lol_tournament_rejection_reason(bucket_name):
        return "unknown"
    tokens = set(normalized.split())
    if _lol_tournament_is_msi(normalized):
        return "s"
    if "worlds" in tokens or {"league", "legends", "world"} <= tokens:
        return "s"
    if "ewc" in tokens or {"esports", "world", "cup"} <= tokens:
        return "s"
    if tokens & {"lec", "lck", "lpl", "lcs", "lta"}:
        return "a"
    if "prime" in tokens and "league" in tokens:
        if _lol_prime_league_is_first_division(normalized):
            return "b"
        return "unknown"
    return "unknown"


def _lol_tournament_is_msi(bucket_name: str) -> bool:
    normalized = _tournament_bucket_key(bucket_name)
    if not normalized:
        return False
    tokens = set(normalized.split())
    return "msi" in tokens or {"mid", "season", "invitational"} <= tokens


def _lol_tournament_rejection_reason(bucket_name: str) -> str:
    normalized = _tournament_bucket_key(bucket_name)
    if not normalized:
        return "lol_unknown_tournament"
    prime_reason = _lol_prime_league_lower_or_uncertain_reason(normalized)
    if prime_reason:
        return prime_reason
    tokens = set(normalized.split())
    if "academy" in tokens:
        return "lol_blocked_academy"
    if "challenger" in tokens or "challengers" in tokens:
        return "lol_blocked_challengers"
    if "nacl" in tokens:
        return "lol_blocked_nacl"
    if "emea" in tokens and "masters" in tokens:
        return "lol_blocked_emea_masters"
    if "erl" in tokens:
        return "lol_blocked_erl_lower_division"
    if "qualifier" in tokens or "qualifiers" in tokens:
        return "lol_blocked_qualifier"
    if "promotion" in tokens:
        return "lol_blocked_promotion"
    if "relegation" in tokens:
        return "lol_blocked_relegation"
    if "cup" in tokens and not (
        "ewc" in tokens or {"esports", "world", "cup"} <= tokens
    ):
        return "lol_blocked_regional_cup"
    return ""


def _lol_stake_candidate_rejection_reason(
    bucket_name: str,
    candidate_text: str,
    *,
    sport: str,
) -> str:
    if _sport_filter_key(sport) != "lol":
        return ""
    prime_candidate_reason = _lol_prime_league_candidate_rejection_reason(
        bucket_name,
        candidate_text,
    )
    if prime_candidate_reason:
        return prime_candidate_reason
    bucket_key = _tournament_bucket_key(bucket_name)
    candidate_key = _tournament_bucket_key(candidate_text)
    candidate_reason = _lol_tournament_rejection_reason(candidate_text)
    if candidate_reason and candidate_reason != "lol_unknown_tournament":
        if (
            candidate_reason == "lol_prime_league_subdivision_required"
            and _lol_prime_league_is_first_division(bucket_key)
            and _lol_prime_league_is_generic(candidate_key)
        ):
            candidate_reason = ""
        else:
            return candidate_reason
    if _lol_prime_league_is_generic(candidate_key):
        if not _lol_prime_league_is_first_division(bucket_key):
            return "lol_prime_league_subdivision_required"
    return ""


def _lol_prime_league_candidate_rejection_reason(
    bucket_name: str,
    candidate_text: str,
) -> str:
    bucket_key = _tournament_bucket_key(bucket_name)
    if not _lol_prime_league_is_first_division(bucket_key):
        return ""
    candidate_key = _tournament_bucket_key(candidate_text)
    candidate_tokens = set(candidate_key.split())
    if "prime" not in candidate_tokens:
        return ""
    if "pokal" in candidate_tokens and not _lol_prime_league_is_first_division(
        candidate_key
    ):
        return "rejected_prime_pokal_not_prime_league"
    if "league" in candidate_tokens:
        return ""
    if _lol_prime_league_is_first_division(candidate_key):
        return ""
    return "rejected_generic_prime_only"


def _lol_prime_league_lower_or_uncertain_reason(normalized: str) -> str:
    tokens = set(normalized.split())
    if not ("prime" in tokens and "league" in tokens):
        return ""
    if _lol_prime_league_is_first_division(normalized):
        return ""
    if (
        re.search(r"\b(?:2nd|second|3rd|third)\b", normalized)
        or re.search(r"\bdivision\s+[23]\b", normalized)
        or re.search(r"\b[23]\s+division\b", normalized)
        or re.search(r"\b(?:2|3)\s+liga\b", normalized)
    ):
        return "lol_blocked_prime_league_lower_division"
    if any(
        token in tokens
        for token in (
            "promotion",
            "relegation",
            "qualifier",
            "qualifiers",
            "challenger",
            "challengers",
        )
    ):
        return "lol_blocked_prime_league_sub_event"
    if _lol_prime_league_is_generic(normalized):
        return "lol_prime_league_subdivision_required"
    return "lol_prime_league_subdivision_required"


def _lol_prime_league_is_first_division(normalized: str) -> bool:
    tokens = set(normalized.split())
    return bool(
        re.search(r"\b(?:1st|first)\s+division\b", normalized)
        or re.search(r"\bdivision\s+1\b", normalized)
        or re.search(r"\b1\s+division\b", normalized)
        or re.search(r"\b1\s+liga\b", normalized)
        or (
            "prime" in tokens
            and "league" in tokens
            and "pro" in tokens
            and "division" in tokens
        )
    )


def _lol_prime_league_is_generic(normalized: str) -> bool:
    tokens = set(normalized.split())
    return "prime" in tokens and "league" in tokens and not (
        "division" in tokens or "liga" in tokens
    )


def _polymarket_events_by_tournament_bucket(
    events: list[ProviderOddsEvent],
    *,
    sport: str,
) -> dict[str, list[ProviderOddsEvent]]:
    grouped: dict[str, list[ProviderOddsEvent]] = {}
    for event in events:
        bucket_name = _polymarket_event_tournament_bucket(event)
        grouped.setdefault(bucket_name, []).append(event)
    return grouped


def _polymarket_event_tournament_bucket(event: ProviderOddsEvent) -> str:
    raw = event.raw if isinstance(event.raw, dict) else {}
    raw_texts = _text_values_from_raw(raw)
    candidates: list[str] = []
    candidates.extend(_tournament_suffix_texts(raw_texts))
    if event.tournament:
        candidates.extend(_tournament_suffix_texts([event.tournament]))
        candidates.append(event.tournament)
    candidates.extend(
        _text(item)
        for item in (
            raw.get("tournament"),
            raw.get("series"),
            raw.get("league"),
        )
        if _text(item)
    )
    candidates.extend(_slug_tournament_texts(raw))
    for candidate in candidates:
        bucket_name = _canonical_tournament_bucket_name(candidate)
        if bucket_name:
            return bucket_name
    return "Unknown"


def _canonical_tournament_bucket_name(value: str) -> str:
    text = _text(value)
    key = _tournament_bucket_key(text)
    if not key:
        return ""
    if "cct" in key and "challenger" in key:
        return "CCT Challengers"
    if "cct" in key and "open qualifier" in key:
        return "CCT Open Qualifier"
    if "cct" in key and "south" in key and "america" in key:
        return "CCT South America Series"
    if "cct" in key and "europe" in key and "series" in key:
        return "CCT Europe Series"
    if "super" in key and "dracula" in key:
        return "Super DraculaN"
    if "xse" in key and "challenger" in key:
        return "XSE Challenger League"
    if "xse" in key and "pro league" in key:
        return "XSE Pro League 2026" if "2026" in key else "XSE Pro League"
    if "thunderpick" in key and "world championship" in key:
        if "regional" in key or "series" in key:
            return "Thunderpick World Championship Regional Series"
        return "Thunderpick World Championship"
    if "european pro league" in key:
        return "European Pro League"
    if "united21" in key or "united 21" in key:
        return "United21"
    if "gamers club" in key and "liga" in key:
        return "Gamers Club Liga Serie A"
    if "urban riga" in key:
        return "Urban Riga Open"
    lol_rejection = _lol_tournament_rejection_reason(text)
    if lol_rejection and lol_rejection != "lol_unknown_tournament":
        cleaned = re.sub(r"\b(?:bo1|bo3|bo5)\b", " ", text, flags=re.I)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:")
        return cleaned[:80] or "Unknown"
    if _lol_tournament_is_msi(key):
        return "MSI"
    if "worlds" in key or (
        "league" in key and "legends" in key and "world" in key
    ):
        return "Worlds"
    if "ewc" in key or (
        "esports" in key and "world" in key and "cup" in key
    ):
        return "Esports World Cup"
    for label in ("lec", "lck", "lpl", "lcs", "lta"):
        if re.search(rf"\b{label}\b", key):
            return label.upper()
    if "prime league" in key or "techniker prime league" in key:
        if _lol_prime_league_is_first_division(key):
            if "techniker" in key:
                return "Techniker Prime League 1st Division"
            if "liga" in key:
                return "Prime League 1. Liga"
            return "Prime League 1st Division"
        if _lol_prime_league_lower_or_uncertain_reason(key):
            cleaned = re.sub(r"\b(?:bo1|bo3|bo5)\b", " ", text, flags=re.I)
            cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:")
            return cleaned[:80] or "Prime League"
    if "open qualifier" in key:
        return "Open Qualifier"
    cleaned = re.sub(r"\b(?:bo1|bo3|bo5)\b", " ", text, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:")
    return cleaned[:80] or "Unknown"


def _tournament_bucket_key(value: str) -> str:
    text = unicodedata.normalize("NFKD", _text(value))
    text = text.encode("ascii", "ignore").decode("ascii").casefold()
    text = text.replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _polymarket_team_pairs(
    events: list[ProviderOddsEvent],
    *,
    sport: str,
) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in events:
        raw = event.raw if isinstance(event.raw, dict) else {}
        _append_team_pair(
            pairs,
            seen,
            team_a=event.team_a,
            team_b=event.team_b,
            sport=event.game or sport,
            start_time=event.start_time,
            source="normalized",
        )
        for text in _text_values_from_raw(raw):
            for team_a, team_b in _team_pairs_from_text(text):
                _append_team_pair(
                    pairs,
                    seen,
                    team_a=team_a,
                    team_b=team_b,
                    sport=event.game or sport,
                    start_time=event.start_time,
                    source="text",
                )
        for key in ("event_slug", "market_slug", "slug"):
            for team_a, team_b in _team_pairs_from_slug(_text(raw.get(key))):
                _append_team_pair(
                    pairs,
                    seen,
                    team_a=team_a,
                    team_b=team_b,
                    sport=event.game or sport,
                    start_time=event.start_time,
                    source="slug",
                )
    return pairs


def _append_team_pair(
    pairs: list[dict[str, Any]],
    seen: set[str],
    *,
    team_a: str,
    team_b: str,
    sport: str,
    start_time: datetime | None,
    source: str,
) -> None:
    team_a = _clean_team_hint(team_a)
    team_b = _clean_team_hint(team_b)
    if not team_a or not team_b or team_a.casefold() == team_b.casefold():
        return
    if (
        source != "normalized"
        and _sport_filter_key(sport) == "lol"
        and _has_more_specific_normalized_team_pair(
            pairs,
            team_a=team_a,
            team_b=team_b,
            sport=sport,
            start_time=start_time,
        )
    ):
        return
    key = "|".join(
        [
            _team_pair_norm(team_a),
            _team_pair_norm(team_b),
            _text(sport),
            start_time.isoformat() if start_time else "",
        ]
    )
    if key in seen:
        return
    seen.add(key)
    pairs.append(
        {
            "team_a": team_a,
            "team_b": team_b,
            "sport": _text(sport),
            "start_time": start_time,
            "source": source,
            "key": key,
        }
    )


def _has_more_specific_normalized_team_pair(
    pairs: list[dict[str, Any]],
    *,
    team_a: str,
    team_b: str,
    sport: str,
    start_time: datetime | None,
) -> bool:
    for pair in pairs:
        if _text(pair.get("source")) != "normalized":
            continue
        if _text(pair.get("sport")) != _text(sport):
            continue
        pair_start = pair.get("start_time")
        if pair_start != start_time:
            continue
        existing_a = _text(pair.get("team_a"))
        existing_b = _text(pair.get("team_b"))
        if (
            _team_hint_matches_or_contains(existing_a, team_a)
            and _team_hint_matches_or_contains(existing_b, team_b)
        ) or (
            _team_hint_matches_or_contains(existing_a, team_b)
            and _team_hint_matches_or_contains(existing_b, team_a)
        ):
            return True
    return False


def _team_hint_matches_or_contains(left: str, right: str) -> bool:
    left_key = _team_pair_norm(left)
    right_key = _team_pair_norm(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    shorter, longer = (
        (left_key, right_key)
        if len(left_key) <= len(right_key)
        else (right_key, left_key)
    )
    return len(shorter) >= 2 and (
        longer.startswith(shorter) or (len(shorter) >= 4 and shorter in longer)
    )


def _polymarket_team_pair_texts(pairs: list[dict[str, Any]]) -> list[str]:
    texts: list[str] = []
    for pair in pairs[:10]:
        teams = " vs ".join(
            part
            for part in (_text(pair.get("team_a")), _text(pair.get("team_b")))
            if part
        )
        if teams:
            texts.append(teams)
    return texts


def _team_pairs_from_text(text: str) -> list[tuple[str, str]]:
    clean = _text(text)
    if not re.search(r"\bvs\b", clean, flags=re.I):
        return []
    match = re.search(
        r"(?P<left>.+?)\s+\bvs\b\s+(?P<right>.+)",
        clean,
        flags=re.I,
    )
    if not match:
        return []
    left = re.split(r":", match.group("left"))[-1]
    right = re.split(
        r"\s+\(BO\d+\)|\s+-\s+|\s+map\s+\d+|\s+match\s+winner",
        match.group("right"),
        maxsplit=1,
        flags=re.I,
    )[0]
    left = _clean_team_hint(left)
    right = _clean_team_hint(right)
    return [(left, right)] if left and right else []


def _team_pairs_from_slug(slug: str) -> list[tuple[str, str]]:
    clean = _text(slug).casefold().replace("_", "-").strip("-")
    if "-vs-" not in clean:
        return []
    clean = re.sub(r"(?:-|^)20\d{2}(?:-\d{2}){0,2}.*$", "", clean)
    clean = re.sub(
        r"^(?:cs2|counter-strike|dota2|dota-2|lol|league-of-legends|"
        r"leagueoflegends)-",
        "",
        clean,
    )
    left, right = clean.split("-vs-", 1)
    right_tokens: list[str] = []
    for token in right.split("-"):
        if token in {
            "bo1",
            "bo3",
            "bo5",
            "cct",
            "match",
            "map",
            "winner",
            "msi",
            "lck",
            "lcs",
            "lec",
            "lpl",
            "lta",
            "worlds",
            "ewc",
            "prime",
            "techniker",
            "mid",
            "season",
            "invitational",
        }:
            break
        right_tokens.append(token)
    team_a = _clean_team_hint(left.replace("-", " "))
    team_b = _clean_team_hint(" ".join(right_tokens).replace("-", " "))
    return [(team_a, team_b)] if team_a and team_b else []


def _polymarket_team_pair_events(
    pairs: list[dict[str, Any]],
    *,
    sport: str,
) -> list[ProviderOddsEvent]:
    events: list[ProviderOddsEvent] = []
    for pair in pairs:
        events.append(
            ProviderOddsEvent(
                provider="polymarket",
                bookmaker="polymarket",
                game=_text(pair.get("sport")) or sport,
                tournament=None,
                start_time=(
                    pair.get("start_time")
                    if isinstance(pair.get("start_time"), datetime)
                    else None
                ),
                team_a=_text(pair.get("team_a")),
                odds_a=1.0,
                team_b=_text(pair.get("team_b")),
                odds_b=1.0,
                market_type="h2h",
                event_url=None,
                raw={
                    "team_pair_key": _text(pair.get("key")),
                    "team_pair_source": _text(pair.get("source")),
                },
            )
        )
    return events


def _stake_tournament_team_match_sample(
    candidate_id: str,
    candidate_name: str,
    matches: list[dict[str, Any]],
) -> list[dict[str, str]]:
    samples: list[dict[str, str]] = []
    for match in matches[:5]:
        left = match.get("left_event") if isinstance(match.get("left_event"), dict) else {}
        right = (
            match.get("right_event")
            if isinstance(match.get("right_event"), dict)
            else {}
        )
        samples.append(
            {
                "id": candidate_id,
                "name": candidate_name,
                "stake": _teams_text(left),
                "polymarket": _teams_text(right),
                "score": _text(match.get("score")),
            }
        )
    return samples


def _teams_text(event: dict[str, Any]) -> str:
    return " vs ".join(
        part
        for part in (_text(event.get("team_a")), _text(event.get("team_b")))
        if part
    )


def _clean_team_hint(value: str) -> str:
    text = _text(value)
    text = re.sub(
        r"\b(?:counter-strike|cs2|dota\s*2|lol|league\s+of\s+legends|"
        r"leagueoflegends)\b\s*:?",
        " ",
        text,
        flags=re.I,
    )
    text = re.sub(r"\b(?:match\s+winner|winner)\b", " ", text, flags=re.I)
    return _text(text.strip(" -:"))


def _team_pair_norm(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", _text(value))
    normalized = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    return "".join(re.findall(r"[a-z0-9]+", normalized))


_LOL_TEAM_PROBE_STOPWORDS = {
    "and",
    "club",
    "division",
    "esport",
    "esports",
    "gaming",
    "league",
    "legend",
    "legends",
    "sport",
    "sports",
    "team",
}


def _lol_team_probe_tokens_from_events(
    events: list[ProviderOddsEvent],
) -> set[str]:
    tokens: set[str] = set()
    for event in events:
        tokens.update(_lol_team_probe_tokens(event.team_a))
        tokens.update(_lol_team_probe_tokens(event.team_b))
    return tokens


def _lol_team_probe_tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", _text(value))
    normalized = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    tokens: set[str] = set()
    for token in re.findall(r"[a-z0-9]+", normalized):
        if len(token) < 2 or token in _LOL_TEAM_PROBE_STOPWORDS:
            continue
        tokens.add(token)
        if token.endswith("s") and len(token) > 4:
            tokens.add(token[:-1])
    return tokens


def _tournament_suffix_texts(texts: list[str]) -> list[str]:
    suffixes: list[str] = []
    for text in texts:
        clean = _text(text)
        if not clean:
            continue
        for separator in (" - ", " \u2013 ", " \u2014 "):
            if separator in clean:
                suffix = clean.rsplit(separator, 1)[-1]
                if suffix:
                    suffixes.append(suffix)
    return suffixes


def _slug_tournament_texts(raw: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for key in ("event_slug", "market_slug", "slug"):
        slug = _text(raw.get(key))
        if not slug:
            continue
        without_date = re.sub(r"(?:-|_)?20\d{2}(?:-|_)?\d{2}(?:-|_)?\d{2}", "", slug)
        texts.append(without_date.replace("-", " ").replace("_", " "))
    return texts


def _first_non_empty(values: list[str]) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _text_values_from_raw(value: object) -> list[str]:
    if isinstance(value, dict):
        texts: list[str] = []
        for key, item in value.items():
            if key in {
                "title",
                "name",
                "question",
                "slug",
                "event_slug",
                "market_slug",
                "category",
                "categoryName",
                "tournament",
                "league",
                "series",
                "description",
            }:
                texts.append(_text(item))
            if key in {"event", "market", "source_fields"}:
                texts.extend(_text_values_from_raw(item))
        return texts
    if isinstance(value, list):
        texts: list[str] = []
        for item in value:
            texts.extend(_text_values_from_raw(item))
        return texts
    return []


def _stake_tournament_candidate_text(candidate: dict[str, Any]) -> str:
    return " ".join(
        _text(candidate.get(key))
        for key in ("name", "slug", "category", "categoryName", "categorySlug")
        if _text(candidate.get(key))
    )


def _fetch_stake_events(
    provider: object,
    *,
    sport: str,
    bookmaker: str,
    tournament_ids: list[str],
    mapping_profile: str | None,
    request_delay_seconds: float,
    rate_limit_retries: int,
    sleeper: Callable[[float], None],
) -> dict[str, Any]:
    tournament_ids = _dedupe_searches(tournament_ids)
    events: list[ProviderOddsEvent] = []
    skipped_tournament_ids: list[str] = []
    skipped_tournament_reasons: dict[str, str] = {}
    rate_limit_count = 0
    stopped_reason = ""
    delay_seconds = max(float(request_delay_seconds), 0.0)
    max_retries = max(int(rate_limit_retries), 0)
    batch_fetch = getattr(provider, "fetch_events_for_tournaments", None)
    if callable(batch_fetch) and tournament_ids:
        attempts = 0
        while True:
            try:
                fetched = batch_fetch(
                    sport=sport,
                    market_type="h2h",
                    bookmaker=bookmaker,
                    tournament_ids=tournament_ids,
                    mapping_profile=mapping_profile,
                )
                events.extend(
                    event for event in fetched if isinstance(event, ProviderOddsEvent)
                )
                return {
                    "events": events,
                    "requested_tournament_ids": tournament_ids,
                    "skipped_tournament_ids": skipped_tournament_ids,
                    "skipped_tournament_reasons": skipped_tournament_reasons,
                    "rate_limit_count": rate_limit_count,
                    "stopped_reason": stopped_reason,
                    "error_count": 0,
                    "request_count": 1,
                    "batch_request_count": 1,
                    "batch_tournament_ids": tournament_ids,
                }
            except ProviderRequestError as exc:
                skip_reason = _recoverable_stake_tournament_error(exc)
                if skip_reason:
                    return {
                        "events": events,
                        "requested_tournament_ids": tournament_ids,
                        "skipped_tournament_ids": tournament_ids,
                        "skipped_tournament_reasons": {
                            tournament_id: skip_reason
                            for tournament_id in tournament_ids
                        },
                        "rate_limit_count": rate_limit_count,
                        "stopped_reason": stopped_reason,
                        "error_count": len(tournament_ids),
                        "request_count": 1,
                        "batch_request_count": 1,
                        "batch_tournament_ids": tournament_ids,
                    }
                if not _is_rate_limit_error(exc):
                    raise
                rate_limit_count += 1
                if attempts >= max_retries:
                    return {
                        "events": events,
                        "requested_tournament_ids": tournament_ids,
                        "skipped_tournament_ids": skipped_tournament_ids,
                        "skipped_tournament_reasons": skipped_tournament_reasons,
                        "rate_limit_count": rate_limit_count,
                        "stopped_reason": "rate_limited",
                        "error_count": 1,
                        "request_count": attempts + 1,
                        "batch_request_count": attempts + 1,
                        "batch_tournament_ids": tournament_ids,
                    }
                attempts += 1
                _sleep_for_rate_limit(
                    exc,
                    fallback_delay_seconds=delay_seconds,
                    sleeper=sleeper,
                )

    for index, item in enumerate(tournament_ids):
        attempts = 0
        while True:
            try:
                events.extend(
                    provider.fetch_events(
                        sport=sport,
                        market_type="h2h",
                        bookmaker=bookmaker,
                        tournament_id=item,
                        mapping_profile=mapping_profile,
                    )
                )
                break
            except ProviderRequestError as exc:
                skip_reason = _recoverable_stake_tournament_error(exc)
                if skip_reason:
                    skipped_tournament_ids.append(item)
                    skipped_tournament_reasons[item] = skip_reason
                    break

                if not _is_rate_limit_error(exc):
                    raise
                rate_limit_count += 1
                if attempts >= max_retries:
                    stopped_reason = "rate_limited"
                    return {
                        "events": events,
                        "requested_tournament_ids": tournament_ids,
                        "skipped_tournament_ids": skipped_tournament_ids,
                        "skipped_tournament_reasons": skipped_tournament_reasons,
                        "rate_limit_count": rate_limit_count,
                        "stopped_reason": stopped_reason,
                        "error_count": len(skipped_tournament_ids) + 1,
                        "request_count": index + attempts + 1,
                        "batch_request_count": 0,
                        "batch_tournament_ids": [],
                    }
                attempts += 1
                _sleep_for_rate_limit(
                    exc,
                    fallback_delay_seconds=delay_seconds,
                    sleeper=sleeper,
                )

        if delay_seconds and index < len(tournament_ids) - 1:
            sleeper(delay_seconds)
    return {
        "events": events,
        "requested_tournament_ids": tournament_ids,
        "skipped_tournament_ids": skipped_tournament_ids,
        "skipped_tournament_reasons": skipped_tournament_reasons,
        "rate_limit_count": rate_limit_count,
        "stopped_reason": stopped_reason,
        "error_count": len(skipped_tournament_ids),
        "request_count": len(tournament_ids) + rate_limit_count,
        "batch_request_count": 0,
        "batch_tournament_ids": [],
    }


def _fetch_polymarket_events(
    provider: object,
    *,
    sport: str,
    searches: list[str] | None,
    event_slugs: list[str] | None,
    market_slugs: list[str] | None,
    max_events: int,
) -> list[ProviderOddsEvent]:
    events: list[ProviderOddsEvent] = []
    search_values = [sport] if searches is None else searches
    for search in search_values:
        events.extend(
            provider.fetch_events(
                sport=sport,
                market_type="h2h",
                search=search,
                max_events=max_events,
            )
        )
    for slug in event_slugs or []:
        events.extend(
            provider.fetch_events(
                sport=sport,
                market_type="h2h",
                event_slug=slug,
                max_events=max_events,
            )
        )
    for slug in market_slugs or []:
        events.extend(
            provider.fetch_events(
                sport=sport,
                market_type="h2h",
                market_slug=slug,
                max_events=max_events,
            )
        )
    return events


def _recoverable_stake_tournament_error(exc: ProviderRequestError) -> str:
    text = str(exc).casefold()
    if "fixture_not_found" in text or "fixture not found" in text:
        return "fixture_not_found"
    if "http 404" in text and "fixture" in text and "not" in text and "found" in text:
        return "fixture_not_found"
    return ""


def _is_rate_limit_error(exc: ProviderRequestError) -> bool:
    text = str(exc).casefold()
    return (
        "http 429" in text
        or "rate_limited" in text
        or "rate limit" in text
        or "rate limited" in text
        or "quota exceeded" in text
        or "all_keys_exhausted" in text
    )


def _sleep_for_rate_limit(
    exc: ProviderRequestError,
    *,
    fallback_delay_seconds: float,
    sleeper: Callable[[float], None],
) -> None:
    delay = _rate_limit_delay_seconds(exc)
    if delay is None:
        delay = max(float(fallback_delay_seconds), 0.0)
    else:
        delay = max(delay + 0.25, 0.25)
    if delay > 0:
        sleeper(delay)


def _rate_limit_delay_seconds(exc: ProviderRequestError) -> float | None:
    text = str(exc)
    retry_ms = re.search(r'"?retry_?ms"?\s*[:=]\s*"?(\d+(?:\.\d+)?)', text, re.I)
    if retry_ms:
        return float(retry_ms.group(1)) / 1000.0
    retry_after = re.search(
        r'"?retry_?after"?\s*[:=]\s*"?(\d+(?:\.\d+)?)',
        text,
        re.I,
    )
    if retry_after:
        return float(retry_after.group(1))
    return None


def _polymarket_keyset_urls(
    *,
    keyset_urls: list[str] | None,
    tag_ids: list[str] | None,
    window_hours: float,
    limit: int,
    locale: str,
) -> list[str]:
    urls = _dedupe_searches(keyset_urls or [])
    clean_tag_ids = _dedupe_searches(tag_ids or [])
    if clean_tag_ids:
        urls.append(
            build_polymarket_keyset_url(
                tag_ids=clean_tag_ids,
                window_hours=window_hours,
                limit=limit,
                locale=locale,
            )
        )
    return _dedupe_searches(urls)


def _polymarket_keyset_tag_ids(urls: list[str]) -> list[str]:
    tag_ids: list[str] = []
    for url in urls:
        parsed = urlsplit(url)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key == "tag_id" and value:
                tag_ids.append(value)
    return _dedupe_searches(tag_ids)


def _polymarket_keyset_stop_reason(
    urls: list[str],
    stop_reasons: list[str],
) -> str:
    if not urls:
        return "no_urls"
    reasons = _dedupe_searches(stop_reasons)
    if not reasons:
        return "not_started"
    if len(reasons) == 1:
        return reasons[0]
    return ",".join(reasons)


def _normalize_keyset_payload(
    provider: object,
    payload: object,
    *,
    sport: str,
    max_events: int,
) -> list[ProviderOddsEvent]:
    normalize = getattr(provider, "normalize_response", None)
    if not callable(normalize):
        raise ValueError("Polymarket keyset discovery requires normalize_response")
    events = normalize(
        payload,
        sport=sport,
        market_type="h2h",
        max_events=max_events,
        source="gamma_keyset",
    )
    return [event for event in events if isinstance(event, ProviderOddsEvent)]


def _filter_polymarket_keyset_events_by_sport(
    events: list[ProviderOddsEvent],
    *,
    sport: str,
) -> dict[str, Any]:
    kept_events: list[ProviderOddsEvent] = []
    rejected_events: list[ProviderOddsEvent] = []
    for event in events:
        if _polymarket_keyset_event_matches_sport(event, sport=sport):
            kept_events.append(event)
        else:
            rejected_events.append(event)
    return {
        "events": kept_events,
        "rejected_events": rejected_events,
        "kept_sample": _polymarket_keyset_filter_sample(kept_events),
        "rejected_sample": _polymarket_keyset_filter_sample(rejected_events),
        "rejected_details": _polymarket_event_diagnostic_samples(
            rejected_events,
            reason="sport_filter_mismatch",
        ),
    }


def _polymarket_keyset_event_matches_sport(
    event: ProviderOddsEvent,
    *,
    sport: str,
) -> bool:
    desired = _sport_filter_key(sport)
    if not desired:
        return True

    raw = event.raw if isinstance(event.raw, dict) else {}
    raw_event = raw.get("event") if isinstance(raw.get("event"), dict) else {}
    raw_market = raw.get("market") if isinstance(raw.get("market"), dict) else {}

    slug_sports = {
        sport_key
        for sport_key in (
            _sport_from_slug_prefix(_text(value))
            for value in (
                raw.get("event_slug"),
                raw.get("market_slug"),
                raw.get("slug"),
                raw_event.get("slug"),
                raw_market.get("slug"),
                event.event_url,
            )
        )
        if sport_key
    }
    if desired in slug_sports:
        return True
    if slug_sports:
        return False

    text_sports = _sport_keys_from_text(_polymarket_keyset_sport_text(event))
    if desired in text_sports:
        return True
    if text_sports:
        return False

    return False


def _polymarket_keyset_filter_sample(
    events: list[ProviderOddsEvent],
) -> list[str]:
    samples: list[str] = []
    for event in events[:5]:
        raw = event.raw if isinstance(event.raw, dict) else {}
        title = _text(raw.get("question"))
        raw_event = raw.get("event") if isinstance(raw.get("event"), dict) else {}
        if raw_event:
            title = _text(raw_event.get("title")) or title
        slug = _text(raw.get("event_slug") or raw.get("slug"))
        teams = " vs ".join(
            part for part in (_text(event.team_a), _text(event.team_b)) if part
        )
        sample = " | ".join(part for part in (slug, title, teams) if part)
        if sample:
            samples.append(sample)
    return samples


def _polymarket_event_diagnostic_samples(
    events: object,
    *,
    reason: str = "",
    bucket_records: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    if not isinstance(events, list):
        return []
    samples: list[dict[str, str]] = []
    for event in events[:10]:
        if not isinstance(event, ProviderOddsEvent):
            continue
        samples.append(
            _polymarket_event_diagnostic_sample(
                event,
                reason=reason,
                bucket_records=bucket_records,
            )
        )
    return samples


def _polymarket_event_diagnostic_sample(
    event: ProviderOddsEvent,
    *,
    reason: str = "",
    bucket_records: dict[str, dict[str, Any]] | None = None,
) -> dict[str, str]:
    raw = event.raw if isinstance(event.raw, dict) else {}
    raw_event = raw.get("event") if isinstance(raw.get("event"), dict) else {}
    raw_market = raw.get("market") if isinstance(raw.get("market"), dict) else {}
    bucket_name = _polymarket_event_tournament_bucket(event)
    record = (bucket_records or {}).get(bucket_name, {})
    sample_reason = reason
    if not sample_reason and isinstance(record, dict):
        sample_reason = _text(record.get("reason"))
    market_title = _text(raw.get("question")) or _text(raw_market.get("question"))
    event_title = _text(raw_event.get("title")) or market_title
    outcomes = raw.get("original_outcomes")
    outcome_text = (
        "|".join(_text(item) for item in outcomes)
        if isinstance(outcomes, list)
        else f"{event.team_a}|{event.team_b}"
    )
    return {
        "reason": sample_reason,
        "event_title": event_title,
        "slug": _text(raw.get("event_slug") or raw.get("slug")),
        "url": event.event_url or "",
        "tournament": _text(event.tournament or raw.get("tournament")),
        "category": _keyset_first_text(raw_event, "category", "categoryName"),
        "bucket": bucket_name,
        "start_time": event.start_time.isoformat() if event.start_time else "",
        "market_title": market_title,
        "market_id": _text(raw.get("market_id")),
        "outcomes": outcome_text,
    }


def _format_polymarket_diagnostic_samples(
    samples: list[dict[str, Any]],
) -> list[str]:
    lines: list[str] = []
    for index, sample in enumerate(samples[:10], 1):
        lines.append(
            f"{index}. reason={_text(sample.get('reason'))} "
            f"title={_text(sample.get('event_title'))} "
            f"slug={_text(sample.get('slug'))} "
            f"url={_text(sample.get('url'))} "
            f"bucket={_text(sample.get('bucket'))} "
            f"start={_text(sample.get('start_time'))} "
            f"market={_text(sample.get('market_title') or sample.get('question'))} "
            f"outcomes={_text(sample.get('outcomes'))}"
        )
    return lines


def _raw_polymarket_sample_has_msi(sample: dict[str, Any]) -> bool:
    text = " ".join(
        _text(sample.get(key))
        for key in (
            "event_title",
            "slug",
            "url",
            "tournament",
            "category",
            "bucket",
            "market_title",
        )
    )
    return _lol_tournament_is_msi(text)


def _polymarket_keyset_sport_text(event: ProviderOddsEvent) -> str:
    raw = event.raw if isinstance(event.raw, dict) else {}
    raw_event = raw.get("event") if isinstance(raw.get("event"), dict) else {}
    raw_market = raw.get("market") if isinstance(raw.get("market"), dict) else {}
    parts: list[str] = []
    for record in (raw_event, raw_market, raw):
        for key in (
            "sport",
            "sportSlug",
            "sportName",
            "game",
            "category",
            "categorySlug",
            "categoryName",
            "series",
            "tags",
            "title",
            "question",
            "name",
            "slug",
            "event_slug",
            "market_slug",
            "description",
        ):
            parts.extend(_text_values_for_sport_filter(record.get(key)))
    return " ".join(part for part in parts if part)


def _text_values_for_sport_filter(value: object) -> list[str]:
    if isinstance(value, str):
        return [_text(value)]
    if isinstance(value, dict):
        texts: list[str] = []
        for key in (
            "name",
            "title",
            "label",
            "slug",
            "sport",
            "sportSlug",
            "category",
            "categoryName",
        ):
            texts.extend(_text_values_for_sport_filter(value.get(key)))
        return texts
    if isinstance(value, list):
        texts: list[str] = []
        for item in value:
            texts.extend(_text_values_for_sport_filter(item))
        return texts
    return []


def _sport_filter_key(sport: str) -> str:
    normalized = _normalize_sport_filter_text(sport)
    aliases = {
        "cs2": "cs2",
        "counter strike": "cs2",
        "counterstrike": "cs2",
        "dota": "dota2",
        "dota 2": "dota2",
        "dota2": "dota2",
        "league legends": "lol",
        "league of legends": "lol",
        "leagueoflegends": "lol",
        "lol": "lol",
    }
    return aliases.get(normalized, normalized)


def _sport_from_slug_prefix(slug: str) -> str:
    slug_text = _slug_text(slug).casefold().replace("_", "-")
    if not slug_text:
        return ""
    prefix_aliases = {
        "cs2": "cs2",
        "counter-strike": "cs2",
        "counterstrike": "cs2",
        "dota": "dota2",
        "dota-2": "dota2",
        "dota2": "dota2",
        "league-of-legends": "lol",
        "leagueoflegends": "lol",
        "lol": "lol",
        "overwatch": "overwatch",
        "rainbow-six": "rainbow_six",
        "rainbow6": "rainbow_six",
        "r6": "rainbow_six",
        "hok": "hok",
        "honor-of-kings": "hok",
        "mlp": "mlp",
        "mobile-legends": "mlp",
        "mlbb": "mlp",
        "valorant": "valorant",
    }
    for prefix, sport_key in prefix_aliases.items():
        if slug_text == prefix or slug_text.startswith(f"{prefix}-"):
            return sport_key
    return ""


def _sport_keys_from_text(text: str) -> set[str]:
    normalized = _normalize_sport_filter_text(text)
    if not normalized:
        return set()
    sport_aliases = {
        "cs2": ("cs2", "counter strike", "counterstrike"),
        "dota2": ("dota 2", "dota2", "dota"),
        "lol": ("league of legends", "leagueoflegends", "league legends", "lol"),
        "overwatch": ("overwatch",),
        "rainbow_six": ("rainbow six siege", "rainbow six", "rainbow6", "r6 siege"),
        "hok": ("honor of kings", "honorofkings", "hok"),
        "mlp": ("mobile legends", "mobilelegends", "mlbb", "mlp"),
        "valorant": ("valorant",),
    }
    matches: set[str] = set()
    for sport_key, aliases in sport_aliases.items():
        for alias in aliases:
            if _sport_filter_alias_in_text(alias, normalized):
                matches.add(sport_key)
                break
    return matches


def _sport_filter_alias_in_text(alias: str, normalized_text: str) -> bool:
    normalized_alias = _normalize_sport_filter_text(alias)
    if not normalized_alias:
        return False
    if " " in normalized_alias:
        return normalized_alias in normalized_text
    return bool(
        re.search(
            rf"(?<![a-z0-9]){re.escape(normalized_alias)}(?![a-z0-9])",
            normalized_text,
        )
    )


def _normalize_sport_filter_text(value: object) -> str:
    text = unicodedata.normalize("NFKD", _text(value))
    text = text.encode("ascii", "ignore").decode("ascii").casefold()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _keyset_raw_event_count(payload: object) -> int:
    return len(_keyset_event_records(payload))


def _keyset_sample_slugs(payload: object) -> list[str]:
    slugs: list[str] = []
    for event in _keyset_event_records(payload):
        slugs.append(_text(event.get("slug")))
        for market in _keyset_market_records(event):
            slugs.append(_text(market.get("slug")))
    return [slug for slug in slugs if slug]


def _keyset_raw_event_samples(payload: object) -> list[dict[str, str]]:
    samples: list[dict[str, str]] = []
    for event in _keyset_event_records(payload)[:10]:
        markets = _keyset_market_records(event)
        market = markets[0] if markets else {}
        samples.append(
            {
                "reason": "raw_keyset_sample",
                "event_title": _keyset_first_text(
                    event,
                    "title",
                    "name",
                    "question",
                ),
                "slug": _keyset_first_text(
                    event,
                    "slug",
                    "eventSlug",
                )
                or _keyset_first_text(market, "eventSlug", "slug"),
                "url": _keyset_event_url(event, market),
                "tournament": _keyset_first_text(
                    event,
                    "tournament",
                    "tournamentName",
                    "series",
                    "league",
                    "competition",
                ),
                "category": _keyset_first_text(
                    event,
                    "category",
                    "categoryName",
                    "sport",
                    "sportName",
                ),
                "bucket": _canonical_tournament_bucket_name(
                    " ".join(
                        part
                        for part in (
                            _keyset_first_text(
                                event,
                                "tournament",
                                "tournamentName",
                                "series",
                                "league",
                                "competition",
                            ),
                            _keyset_first_text(event, "title", "name"),
                            _keyset_first_text(event, "category", "categoryName"),
                        )
                        if part
                    )
                ),
                "start_time": _keyset_first_text(
                    event,
                    "gameStartTime",
                    "startTime",
                    "start_time",
                    "startDate",
                    "endDate",
                ),
                "market_title": _keyset_first_text(
                    market,
                    "question",
                    "title",
                    "name",
                    "marketName",
                ),
                "market_id": _keyset_first_text(market, "id", "conditionId"),
                "outcomes": "|".join(_keyset_outcome_names(market)),
                "market_count": _text(len(markets)),
            }
        )
    return samples


def _keyset_event_url(event: dict[str, Any], market: dict[str, Any]) -> str:
    url = _keyset_first_text(event, "url", "eventUrl", "event_url") or _keyset_first_text(
        market,
        "url",
        "marketUrl",
        "market_url",
    )
    if url:
        if url.startswith("http://") or url.startswith("https://"):
            return url
        if url.startswith("/"):
            return f"https://polymarket.com{url}"
    slug = _keyset_first_text(event, "slug", "eventSlug") or _keyset_first_text(
        market,
        "eventSlug",
    )
    if slug:
        return f"https://polymarket.com/event/{slug}"
    market_slug = _keyset_first_text(market, "slug")
    if market_slug:
        return f"https://polymarket.com/market/{market_slug}"
    return ""


def _keyset_outcome_names(market: dict[str, Any]) -> list[str]:
    for key in ("outcomes", "outcomeNames", "shortOutcomes"):
        names = _keyset_sequence(market.get(key))
        if names:
            outcome_names: list[str] = []
            for item in names:
                text = (
                    _text(
                        item.get("name")
                        or item.get("title")
                        or item.get("label")
                        or item.get("value")
                    )
                    if isinstance(item, dict)
                    else _text(item)
                )
                if text:
                    outcome_names.append(text)
            return outcome_names
    return []


def _keyset_sequence(value: object) -> list[object]:
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
            return _keyset_sequence(parsed)
        return [part.strip() for part in text.split(",") if part.strip()]
    return []


def _keyset_first_text(record: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = record.get(key)
        values = _text_values_for_sport_filter(value)
        if values:
            return _text(values[0])
        if isinstance(value, (dict, list)):
            continue
        text = _text(value)
        if text:
            return text
    return ""


def _keyset_event_records(value: object) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    for key in ("events", "data", "results", "records"):
        child = value.get(key)
        if isinstance(child, list):
            return [item for item in child if isinstance(item, dict)]
        if isinstance(child, dict):
            records = _keyset_event_records(child)
            if records:
                return records
    if _keyset_market_records(value):
        return [value]
    return []


def _keyset_market_records(event: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("markets", "eventMarkets"):
        value = event.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [item for item in value.values() if isinstance(item, dict)]
    return []


def _keyset_next_cursor(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("after_cursor", "afterCursor", "next_cursor", "nextCursor"):
        cursor = _text(payload.get(key))
        if cursor:
            return cursor
    cursor_value = payload.get("cursor")
    if isinstance(cursor_value, dict):
        for key in ("after_cursor", "afterCursor", "next_cursor", "nextCursor"):
            cursor = _text(cursor_value.get(key))
            if cursor:
                return cursor
    else:
        cursor = _text(cursor_value)
        if cursor and cursor.casefold() not in {"true", "false"}:
            return cursor

    next_value = _text(payload.get("next"))
    if next_value.startswith("http://") or next_value.startswith("https://"):
        parsed_next = urlsplit(next_value)
        for key, value in parse_qsl(parsed_next.query, keep_blank_values=True):
            if key == "after_cursor" and value:
                return value
    return next_value


def _url_with_query_param(url: str, key: str, value: str) -> str:
    parsed = urlsplit(url)
    query = [
        (query_key, query_value)
        for query_key, query_value in parse_qsl(
            parsed.query,
            keep_blank_values=True,
        )
        if query_key != key
    ]
    query.append((key, value))
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query),
            parsed.fragment,
        )
    )


def _safe_team_search_variant(team: str) -> str:
    text = _text(team)
    text = re.sub(r"\b(?:e[\s.-]*sports|esports|academy)\b", " ", text, flags=re.I)
    return _text(text)


def _team_pair_query(team_a: str, team_b: str) -> str:
    team_a = _text(team_a)
    team_b = _text(team_b)
    if not team_a or not team_b:
        return ""
    if team_a.casefold() == team_b.casefold():
        return ""
    return f"{team_a} {team_b}"


def _dedupe_events(events: list[ProviderOddsEvent]) -> list[ProviderOddsEvent]:
    seen: set[str] = set()
    deduped: list[ProviderOddsEvent] = []
    for event in events:
        key = _event_key(event)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(event)
    return deduped


def _event_key(event: ProviderOddsEvent | dict[str, Any]) -> str:
    if isinstance(event, ProviderOddsEvent):
        raw = event.raw if isinstance(event.raw, dict) else {}
        start_time = event.start_time.isoformat() if event.start_time else ""
        values = (
            event.provider,
            event.bookmaker,
            event.game,
            event.market_type,
            start_time,
            event.team_a,
            event.team_b,
        )
        event_url = event.event_url
    elif isinstance(event, dict):
        raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
        values = (
            event.get("provider"),
            event.get("bookmaker"),
            event.get("game"),
            event.get("market_type"),
            event.get("start_time"),
            event.get("team_a"),
            event.get("team_b"),
        )
        event_url = event.get("event_url")
    else:
        return ""

    for value in (
        event_url,
        raw.get("market_id"),
        raw.get("event_id"),
        raw.get("condition_id"),
    ):
        if value:
            return str(value)
    return "|".join(str(value or "") for value in values)


def _provider(
    name: str,
    env_file: Path,
    providers: dict[str, object] | None,
) -> object:
    if providers and name in providers:
        return providers[name]
    if name == "oddspapi":
        return OddsPapiProvider.from_env(env_file)
    if name == "polymarket":
        return PolymarketProvider.from_env(env_file)
    raise ProviderConfigurationError(f"Unsupported provider: {name}")


def _oddspapi_key_debug(provider: object) -> dict[str, object]:
    debug = getattr(provider, "last_debug", {}) or {}
    if not isinstance(debug, dict):
        debug = {}
    return {
        "pool_size": _int_value(debug.get("oddspapi_key_pool_size")),
        "key_index": _text(debug.get("oddspapi_key_index")),
        "failover_count": _int_value(debug.get("oddspapi_key_failover_count")),
        "all_keys_exhausted": bool(debug.get("oddspapi_all_keys_exhausted")),
    }


def _format_arbitrage(arbitrage: dict[str, Any]) -> list[str]:
    summary = arbitrage.get("summary") if isinstance(arbitrage.get("summary"), dict) else {}
    opportunities = _events(arbitrage.get("opportunities"))
    lines = [
        "arbitrage:",
        f"opportunity_count={_text(summary.get('opportunity_count') or 0)}",
    ]
    if opportunities:
        lines.append("")
        lines.append(format_opportunities_table(opportunities))
    return lines


def _format_telegram(result: dict[str, Any], telegram: dict[str, Any]) -> list[str]:
    lines = [
        "telegram:",
        f"dry_run={_bool(telegram.get('dry_run'))}",
        f"message_count={_text(telegram.get('message_count') or 0)}",
        f"sent_count={_text(telegram.get('sent_count') or 0)}",
        "telegram_dedup_suppressed_count="
        f"{_text(telegram.get('dedup_suppressed_count') or 0)}",
        "telegram_dedup_sent_new_count="
        f"{_text(telegram.get('dedup_sent_new_count') or 0)}",
        "telegram_dedup_state_size="
        f"{_text(telegram.get('dedup_state_size') or 0)}",
    ]
    skipped_reason = _text(telegram.get("skipped_reason"))
    if skipped_reason:
        lines.append(f"skipped_reason={skipped_reason}")

    if telegram.get("dry_run"):
        arbitrage = result.get("arbitrage")
        opportunities = (
            _events(arbitrage.get("opportunities"))
            if isinstance(arbitrage, dict)
            else []
        )
        messages = build_arbitrage_alert_messages(opportunities)
        for index, message in enumerate(messages, 1):
            lines.extend(["", f"message_{index}:", message])
    return lines


def _split_values(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _explicit_search_values(values: list[str] | None) -> list[str]:
    if not values:
        return []
    searches: list[str] = []
    for value in values:
        searches.extend(_split_values(value))
    return _dedupe_searches(searches)


def _dedupe_searches(searches: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for search in searches:
        text = _text(search)
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        deduped.append(text)
    return deduped


def _normalized_html_text(value: str) -> str:
    text = html.unescape(value)
    text = text.replace("\\/", "/").replace("\\u002F", "/")
    return unquote(text)


def _slug_text(value: str) -> str:
    parsed = urlsplit(value)
    text = parsed.path or value
    text = text.strip().strip("/")
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9_-]", "", text)


def _slug_matches_sport(slug: str, sport: str) -> bool:
    normalized = slug.casefold().replace("_", "-")
    return any(token in normalized for token in _sport_slug_tokens(sport))


def _sport_segment_matches(segment: str, sport: str) -> bool:
    normalized_segment = segment.casefold().replace("_", "-")
    return normalized_segment in _sport_slug_tokens(sport)


def _sport_slug_tokens(sport: str) -> set[str]:
    normalized = _text(sport).casefold().replace("_", "-")
    aliases = {
        "cs2": {"cs2", "counter-strike", "counterstrike"},
        "counter-strike": {"cs2", "counter-strike", "counterstrike"},
        "counterstrike": {"cs2", "counter-strike", "counterstrike"},
        "dota2": {"dota2", "dota-2", "dota"},
        "dota-2": {"dota2", "dota-2", "dota"},
        "dota": {"dota2", "dota-2", "dota"},
        "lol": {"lol", "league-of-legends", "leagueoflegends"},
        "league-of-legends": {"lol", "league-of-legends", "leagueoflegends"},
        "leagueoflegends": {"lol", "league-of-legends", "leagueoflegends"},
    }
    return aliases.get(normalized, {normalized})


_STAKE_TEAM_SLUG_STOPWORDS = {
    "a",
    "an",
    "and",
    "academy",
    "club",
    "da",
    "das",
    "de",
    "do",
    "dos",
    "e",
    "esport",
    "esports",
    "fc",
    "gaming",
    "jr",
    "junior",
    "pro",
    "sport",
    "sports",
    "team",
    "the",
    "v",
    "vs",
}


_STAKE_AUTO_TOURNAMENT_STOPWORDS = {
    "and",
    "bet",
    "bo1",
    "bo2",
    "bo3",
    "bo5",
    "counter",
    "cs2",
    "esport",
    "esports",
    "game",
    "games",
    "gaming",
    "group",
    "h2h",
    "league",
    "market",
    "match",
    "stage",
    "strike",
    "team",
    "the",
    "winner",
    "with",
}


_STAKE_AUTO_IMPORTANT_TOKEN_WEIGHTS = {
    "cct": 5.0,
    "thunderpick": 5.0,
    "xse": 5.0,
    "draculan": 5.0,
    "gamers": 4.0,
    "united21": 4.0,
    "european": 3.0,
    "pro": 2.0,
}


_STAKE_AUTO_GENERIC_TOURNAMENT_TOKENS = {
    "america",
    "american",
    "championship",
    "league",
    "open",
    "playoff",
    "playoffs",
    "qualifier",
    "regional",
    "series",
    "south",
    "stage",
}


_STAKE_AUTO_CORE_LABEL_WORDS = {
    "cct",
    "club",
    "dracula",
    "draculan",
    "european",
    "gamers",
    "pro",
    "super",
    "thunderpick",
    "united",
    "united21",
    "xse",
}


_STAKE_AUTO_REJECTED_TOKEN_PENALTIES = {
    "north": 0.7,
    "europe": 0.8,
    "european": 0.8,
    "asia": 0.8,
    "asian": 0.8,
}


_TOURNAMENT_QUALITY_ORDER = {
    "unknown": 0,
    "c": 1,
    "b": 2,
    "a": 3,
    "s": 4,
}


_CS2_TOURNAMENT_QUALITY = {
    "cct south america series": "b",
    "cct south american series": "b",
    "cct europe series": "b",
    "super draculan": "b",
    "xse pro league": "b",
    "xse pro league 2026": "a",
    "thunderpick world championship": "a",
    "thunderpick world championship regional series": "b",
    "european pro league": "c",
    "united21": "c",
    "united 21": "c",
    "gamers club liga serie a": "c",
    "urban riga open": "c",
    "cct challengers": "c",
    "cct open qualifier": "c",
    "xse challenger league": "c",
    "open qualifier": "c",
}


def _stake_auto_tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", _text(value))
    normalized = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    tokens: set[str] = set()
    for token in re.findall(r"[a-z0-9]+", normalized):
        if len(token) < 3 or token in _STAKE_AUTO_TOURNAMENT_STOPWORDS:
            continue
        tokens.add(token)
        if token.endswith("american"):
            tokens.add("america")
        if token.endswith("s") and len(token) > 4:
            tokens.add(token[:-1])
    if {"mid", "season", "invitational"} <= tokens:
        tokens.add("msi")
    return tokens


def _stake_page_slug_team_tokens(stake_events: list[ProviderOddsEvent]) -> set[str]:
    tokens: set[str] = set()
    for event in stake_events:
        tokens.update(_team_slug_match_tokens(event.team_a))
        tokens.update(_team_slug_match_tokens(event.team_b))
    return tokens


def _team_slug_match_tokens(team: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", _text(team))
    normalized = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    tokens: set[str] = set()
    for token in re.findall(r"[a-z0-9]+", normalized):
        if len(token) < 3 or token in _STAKE_TEAM_SLUG_STOPWORDS:
            continue
        tokens.add(token)
        if len(token) >= 5:
            tokens.add(token[:3])
    return tokens


def _slug_matches_stake_team_tokens(slug: str, team_tokens: set[str]) -> bool:
    if not team_tokens:
        return False
    normalized = _slug_text(slug).casefold().replace("_", "-")
    slug_tokens = set(re.findall(r"[a-z0-9]+", normalized))
    return bool(slug_tokens & team_tokens)


def _utc_iso_z(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _int_value(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _int_counter(value: object) -> Counter[str]:
    counts: Counter[str] = Counter()
    if not isinstance(value, dict):
        return counts
    for key, item in value.items():
        text = _text(key)
        if text:
            counts[text] += _int_value(item)
    return counts


def _joined_values(values: list[str] | None) -> str:
    parts: list[str] = []
    for value in values or []:
        parts.extend(_split_values(value))
    return ",".join(parts)


def _events(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _dict_rows(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _event_urls(value: object) -> str:
    return ",".join(
        _text(event.get("event_url"))
        for event in _events(value)
        if _text(event.get("event_url"))
    )


def _event_debug_sample(value: object, *, limit: int = 10) -> list[str]:
    return [_event_debug_line(event) for event in _events(value)[:limit]]


def _polymarket_price_debug_sample(value: object, *, limit: int = 5) -> list[str]:
    samples: list[str] = []
    for event in _events(value):
        if not _is_polymarket_event(event):
            continue
        samples.append(_polymarket_price_debug_line(event))
        if len(samples) >= limit:
            break
    return samples


def _opportunity_price_debug_sample(
    opportunities: object,
    matches: list[dict[str, Any]],
    *,
    limit: int = 10,
) -> list[str]:
    samples: list[str] = []
    for fallback_index, opportunity in enumerate(_events(opportunities)):
        match = _opportunity_match(opportunity, matches, fallback_index)
        polymarket_event = _polymarket_event_from_match(match)
        samples.append(
            _opportunity_price_debug_line(
                opportunity,
                polymarket_event,
            )
        )
        if len(samples) >= limit:
            break
    return samples


def _polymarket_price_debug_line(event: dict[str, Any]) -> str:
    raw = _event_raw(event)
    outcomes = _polymarket_outcome_names(event)
    odds_a = _float_or_none(event.get("odds_a"))
    odds_b = _float_or_none(event.get("odds_b"))
    raw_sources = _polymarket_raw_source_values(raw)
    parts = [
        f"slug={_event_debug_slug(event)}",
        f"market_id={_event_dict_market_id(event)}",
        f"teams={_text(event.get('team_a'))} vs {_text(event.get('team_b'))}",
        f"outcomes={'|'.join(outcomes)}",
        f"odds={_price_debug_number(odds_a)}/{_price_debug_number(odds_b)}",
        "implied_prices="
        f"{_price_debug_number(_inverse_price(odds_a))}/"
        f"{_price_debug_number(_inverse_price(odds_b))}",
        f"price_source={_polymarket_price_source(raw)}",
        f"executable_buy_prices={_side_prices_debug(raw.get('executable_buy_prices'))}",
    ]
    if raw_sources:
        parts.append(f"raw_source={raw_sources}")
    return "|".join(parts)


def _opportunity_price_debug_line(
    opportunity: dict[str, Any],
    polymarket_event: dict[str, Any],
) -> str:
    raw_odds = _float_or_none(opportunity.get("polymarket_raw_odds"))
    effective_odds = _float_or_none(opportunity.get("polymarket_effective_odds"))
    executable_buy_price = _float_or_none(
        opportunity.get("polymarket_executable_buy_price")
    )
    raw_price = executable_buy_price or _inverse_price(raw_odds)
    all_in_price = _float_or_none(
        opportunity.get("polymarket_all_in_price")
    ) or _inverse_price(effective_odds)
    fee_per_share = _float_or_none(
        opportunity.get("polymarket_fee_per_share")
    )
    if (
        fee_per_share is None
        and raw_price is not None
        and all_in_price is not None
    ):
        fee_per_share = all_in_price - raw_price
    price_source = _text(
        opportunity.get("polymarket_price_source")
    )
    team = _text(opportunity.get("polymarket_team"))
    raw = _event_raw(polymarket_event)
    side_index = _polymarket_side_index(polymarket_event, team)
    if not price_source:
        price_source = _polymarket_price_source(raw, side_index=side_index)
    raw_sources = _polymarket_raw_source_values(
        raw,
        side_index=side_index,
        outcome_name=team,
    )
    parts = [
        f"polymarket_team={team}",
        f"pm_raw_odds={_price_debug_number(raw_odds)}",
        f"pm_eff_odds={_price_debug_number(effective_odds)}",
        f"pm_raw_price={_price_debug_number(raw_price)}",
        f"executable_buy_price={_price_debug_number(executable_buy_price)}",
        f"pm_fee_per_share={_price_debug_number(fee_per_share)}",
        f"pm_all_in_price={_price_debug_number(all_in_price)}",
        f"pm_price_source={price_source}",
        f"polymarket_slug={_event_debug_slug(polymarket_event)}",
        f"polymarket_market_id={_event_dict_market_id(polymarket_event)}",
    ]
    if raw_sources:
        parts.append(f"raw_source={raw_sources}")
    return "|".join(parts)


def _opportunity_match(
    opportunity: dict[str, Any],
    matches: list[dict[str, Any]],
    fallback_index: int,
) -> dict[str, Any]:
    source_ids = (
        opportunity.get("source_ids")
        if isinstance(opportunity.get("source_ids"), dict)
        else {}
    )
    match_index = _int_or_none(source_ids.get("match_index"))
    if match_index is not None and 0 <= match_index < len(matches):
        return matches[match_index]
    if 0 <= fallback_index < len(matches):
        return matches[fallback_index]
    return {}


def _polymarket_event_from_match(match: dict[str, Any]) -> dict[str, Any]:
    for key in ("left_event", "right_event"):
        event = match.get(key) if isinstance(match.get(key), dict) else {}
        if _is_polymarket_event(event):
            return event
    return {}


def _is_polymarket_event(event: dict[str, Any]) -> bool:
    return (
        _text(event.get("provider")).casefold() == "polymarket"
        or _text(event.get("bookmaker")).casefold() == "polymarket"
    )


def _event_raw(event: dict[str, Any]) -> dict[str, Any]:
    return event.get("raw") if isinstance(event.get("raw"), dict) else {}


def _polymarket_raw_market(raw: dict[str, Any]) -> dict[str, Any]:
    market = raw.get("market") if isinstance(raw.get("market"), dict) else {}
    return market or raw


def _polymarket_price_source(
    raw: dict[str, Any],
    *,
    side_index: int | None = None,
) -> str:
    if side_index is not None:
        side_source = _side_text(raw.get("price_sources"), side_index)
        if side_source:
            return side_source
        executable_source = _side_text(raw.get("executable_price_sources"), side_index)
        if executable_source:
            return executable_source
    sources = [
        _text(source)
        for source in _debug_sequence(raw.get("price_sources"))
        if _text(source)
    ]
    if sources:
        return "/".join(dict.fromkeys(sources))
    market = _polymarket_raw_market(raw)
    for key in ("outcomePrices", "prices"):
        if key in market:
            return key
    if raw.get("original_prices") not in (None, ""):
        return "normalized_original_prices"
    return "event_odds"


def _polymarket_outcome_names(event: dict[str, Any]) -> list[str]:
    raw = _event_raw(event)
    market = _polymarket_raw_market(raw)
    for value in (raw.get("original_outcomes"), market.get("outcomes")):
        outcomes = [_text(item) for item in _debug_sequence(value) if _text(item)]
        if len(outcomes) >= 2:
            return outcomes[:2]
    return [_text(event.get("team_a")), _text(event.get("team_b"))]


def _side_text(value: object, side_index: int) -> str:
    sequence = _debug_sequence(value)
    if 0 <= side_index < len(sequence):
        return _text(sequence[side_index])
    return ""


def _side_prices_debug(value: object) -> str:
    values = _debug_sequence(value)
    if not values:
        return ""
    return "/".join(_price_debug_number(item) for item in values)


def _polymarket_side_index(event: dict[str, Any], team: str) -> int | None:
    target = canonical_team_name(team, game=_text(event.get("game")))
    outcomes = _polymarket_outcome_names(event)
    for index, outcome in enumerate(outcomes[:2]):
        if canonical_team_name(outcome, game=_text(event.get("game"))) == target:
            return index
    for index, key in enumerate(("team_a", "team_b")):
        if canonical_team_name(_text(event.get(key)), game=_text(event.get("game"))) == target:
            return index
    return None


def _polymarket_raw_source_values(
    raw: dict[str, Any],
    *,
    side_index: int | None = None,
    outcome_name: str = "",
) -> str:
    market = _polymarket_raw_market(raw)
    if not market:
        return ""
    parts: list[str] = []
    included: set[str] = set()
    for key in (
        "outcomePrices",
        "prices",
        "bestBid",
        "bestAsk",
        "lastTradePrice",
        "clobTokenIds",
        "tokenIds",
        "token_ids",
    ):
        if key in market:
            included.add(key)
            text = _polymarket_source_field_text(
                key,
                market.get(key),
                side_index=side_index,
            )
            if text:
                parts.append(text)
    for key in ("tokens", "clobTokens"):
        if key in market:
            included.add(key)
            text = _polymarket_token_source_text(
                key,
                market.get(key),
                side_index=side_index,
                outcome_name=outcome_name,
            )
            if text:
                parts.append(text)
    extra_count = 0
    for key in sorted(market):
        if key in included:
            continue
        key_text = _text(key)
        lower_key = key_text.casefold()
        if not any(fragment in lower_key for fragment in ("bid", "ask", "book")):
            continue
        text = _polymarket_source_field_text(
            key_text,
            market.get(key),
            side_index=side_index,
        )
        if text:
            parts.append(text)
            extra_count += 1
        if extra_count >= 5:
            break
    return ";".join(parts[:12])


def _polymarket_source_field_text(
    key: str,
    value: object,
    *,
    side_index: int | None,
) -> str:
    sequence = _debug_sequence(value)
    if side_index is not None and 0 <= side_index < len(sequence):
        return f"{key}[{side_index}]={_compact_debug_value(sequence[side_index])}"
    if sequence:
        return f"{key}={_compact_debug_value(sequence)}"
    if value not in (None, ""):
        return f"{key}={_compact_debug_value(value)}"
    return ""


def _polymarket_token_source_text(
    key: str,
    value: object,
    *,
    side_index: int | None,
    outcome_name: str,
) -> str:
    tokens = [item for item in _debug_sequence(value) if isinstance(item, dict)]
    if not tokens:
        return _polymarket_source_field_text(key, value, side_index=side_index)
    token_index = side_index
    if token_index is None:
        token_index = _token_index_for_outcome(tokens, outcome_name)
    if token_index is not None and 0 <= token_index < len(tokens):
        return f"{key}[{token_index}]={_compact_token_debug(tokens[token_index])}"
    return f"{key}={_compact_debug_value([_compact_token_debug(item) for item in tokens[:2]])}"


def _token_index_for_outcome(tokens: list[dict[str, Any]], outcome_name: str) -> int | None:
    if not outcome_name:
        return None
    target = canonical_team_name(outcome_name)
    for index, token in enumerate(tokens):
        for key in ("outcome", "name", "title"):
            if canonical_team_name(_text(token.get(key))) == target:
                return index
    return None


def _compact_token_debug(token: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "outcome",
        "name",
        "token_id",
        "tokenId",
        "id",
        "price",
        "bestBid",
        "bestAsk",
        "lastTradePrice",
    ):
        if key in token and token.get(key) not in (None, ""):
            parts.append(f"{key}:{_compact_debug_value(token.get(key), limit=60)}")
    return ",".join(parts[:6])


def _debug_sequence(value: object) -> list[object]:
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
        if text.startswith("[") or text.startswith("{"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return [text]
            return _debug_sequence(parsed)
        return [part.strip() for part in text.split(",") if part.strip()]
    return []


def _compact_debug_value(value: object, *, limit: int = 120) -> str:
    if isinstance(value, list):
        text = "/".join(_compact_debug_value(item, limit=40) for item in value[:4])
        if len(value) > 4:
            text = f"{text}/..."
    elif isinstance(value, dict):
        parts: list[str] = []
        preferred_keys = (
            "outcome",
            "name",
            "token_id",
            "tokenId",
            "id",
            "price",
            "bestBid",
            "bestAsk",
            "lastTradePrice",
        )
        for key in preferred_keys:
            if key in value and value.get(key) not in (None, ""):
                parts.append(f"{key}:{_compact_debug_value(value.get(key), limit=40)}")
        if not parts:
            for key, item in list(value.items())[:4]:
                parts.append(f"{_text(key)}:{_compact_debug_value(item, limit=40)}")
        text = ",".join(parts[:6])
    else:
        text = _text(value)
    if len(text) > limit:
        return f"{text[: limit - 3]}..."
    return text


def _float_or_none(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _inverse_price(value: float | None) -> float | None:
    if value is None or value <= 0:
        return None
    return 1.0 / value


def _price_debug_number(value: object) -> str:
    number = _float_or_none(value)
    if number is None:
        return ""
    return f"{number:.6f}".rstrip("0").rstrip(".")


def _event_debug_line(event: dict[str, Any]) -> str:
    team_a = _text(event.get("team_a"))
    team_b = _text(event.get("team_b"))
    game = _text(event.get("game"))
    team_key_a = canonical_team_name(team_a, game=game)
    team_key_b = canonical_team_name(team_b, game=game)
    tournament = _text(event.get("tournament"))
    bucket = _event_debug_bucket(event)
    parts = [
        f"provider={_text(event.get('provider'))}",
        f"teams={team_a} vs {team_b}",
        f"team_keys={team_key_a} vs {team_key_b}",
    ]
    start_time = _text(event.get("start_time"))
    if start_time:
        parts.append(f"start={start_time}")
    parts.append(
        "odds="
        f"{_event_debug_number(event.get('odds_a'))}/"
        f"{_event_debug_number(event.get('odds_b'))}"
    )
    if tournament:
        parts.append(f"tournament={tournament}")
    if bucket and bucket != tournament:
        parts.append(f"bucket={bucket}")

    tournament_id = _event_dict_tournament_id(event)
    if tournament_id:
        parts.append(f"tournament_id={tournament_id}")
    market_id = _event_dict_market_id(event)
    if market_id:
        parts.append(f"market_id={market_id}")
    mapping = _event_debug_mapping(event)
    if mapping:
        parts.append(f"mapping={mapping}")
    market_active = _event_debug_market_active(event)
    if market_active:
        parts.append(f"market_active={market_active}")
    slug = _event_debug_slug(event)
    if slug:
        parts.append(f"slug={slug}")
    url = _text(event.get("event_url"))
    if url:
        parts.append(f"url={url}")
    return "|".join(parts)


def _event_debug_bucket(event: dict[str, Any]) -> str:
    tournament = _text(event.get("tournament"))
    if not tournament:
        return ""
    return _canonical_tournament_bucket_name(tournament)


def _event_dict_market_id(event: dict[str, Any]) -> str:
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    raw_market = raw.get("market") if isinstance(raw.get("market"), dict) else {}
    return _text(
        raw.get("market_id")
        or raw.get("marketId")
        or raw.get("condition_id")
        or raw.get("conditionId")
        or raw_market.get("marketId")
        or raw_market.get("market_id")
        or raw_market.get("id")
        or raw_market.get("conditionId")
    )


def _event_debug_mapping(event: dict[str, Any]) -> str:
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    return _text(raw.get("mapping_profile") or raw.get("mapping_source"))


def _event_debug_market_active(event: dict[str, Any]) -> str:
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    active = raw.get("active")
    if isinstance(active, dict):
        market_value = active.get("market")
        if market_value is not None:
            return _text(market_value)
    raw_market = raw.get("market") if isinstance(raw.get("market"), dict) else {}
    for key in (
        "active",
        "isActive",
        "enabled",
        "isEnabled",
        "status",
        "state",
    ):
        if key in raw_market and raw_market.get(key) is not None:
            return _text(raw_market.get(key))
    return ""


def _event_debug_slug(event: dict[str, Any]) -> str:
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    raw_event = raw.get("event") if isinstance(raw.get("event"), dict) else {}
    raw_market = raw.get("market") if isinstance(raw.get("market"), dict) else {}
    return _text(
        raw.get("event_slug")
        or raw.get("market_slug")
        or raw.get("slug")
        or raw_event.get("slug")
        or raw_market.get("slug")
    )


def _event_debug_number(value: object) -> str:
    try:
        return f"{float(value):.3f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return _text(value)


def _csv(value: object) -> str:
    if not isinstance(value, list):
        return _text(value)
    return ",".join(_text(item) for item in value)


def _text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text(item) for item in value if _text(item)]


def _text_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {_text(key): _text(item) for key, item in value.items() if _text(key)}


def _mapping_csv(value: object) -> str:
    if not isinstance(value, dict):
        return _text(value)
    return ",".join(f"{_text(key)}={_text(item)}" for key, item in value.items())


def _polymarket_rejected_samples_csv(value: object) -> str:
    rows: list[str] = []
    fields = (
        ("reason", "reason"),
        ("slug", "slug"),
        ("title", "event_title"),
        ("question", "question"),
        ("market", "market_title"),
        ("detected_best_of", "detected_best_of"),
        ("outcome_count", "outcome_count"),
        ("outcome_names", "outcome_names"),
        ("team_names", "team_names"),
        ("draw_source", "draw_or_three_way_source"),
        ("draw_match", "draw_or_three_way_matched_text"),
        ("unsafe_flags", "unsafe_flags"),
        ("active", "active"),
        ("closed", "closed"),
        ("sport", "sport_guess"),
        ("category", "category"),
        ("tags", "tags"),
    )
    for sample in _dict_rows(value)[:10]:
        parts: list[str] = []
        for label, key in fields:
            text = _text(sample.get(key))
            if text:
                parts.append(f"{label}:{text}")
        if parts:
            rows.append("|".join(parts))
    return ";".join(rows)


def _bucket_mapping_csv(value: object) -> str:
    if not isinstance(value, dict):
        return _text(value)
    parts: list[str] = []
    for key, item in value.items():
        if isinstance(item, dict):
            parts.append(
                ":".join(
                    part
                    for part in (
                        _text(key),
                        _text(item.get("quality")),
                        _text(item.get("reason")),
                        f"count={_text(item.get('count') or 0)}",
                    )
                    if part
                )
            )
        else:
            parts.append(f"{_text(key)}={_text(item)}")
    return ",".join(parts)


def _bucket_list_mapping_csv(value: object) -> str:
    if not isinstance(value, dict):
        return _text(value)
    parts: list[str] = []
    for key, item in value.items():
        if isinstance(item, list):
            parts.append(f"{_text(key)}=[{_csv(item)}]")
        else:
            parts.append(f"{_text(key)}={_text(item)}")
    return ",".join(parts)


def _active_cache_failures_csv(value: object) -> str:
    if not isinstance(value, list):
        return _text(value)
    parts: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        parts.append(
            ":".join(
                part
                for part in (
                    _text(item.get("bucket")),
                    _text(item.get("tournament_id")),
                    _text(item.get("reason")),
                )
                if part
            )
        )
    return ",".join(parts)


def _candidate_sample_csv(value: object) -> str:
    if not isinstance(value, list):
        return _text(value)
    parts: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        parts.append(
            ":".join(
                part
                for part in (
                    _text(item.get("id")),
                    _text(item.get("name")),
                    _text(item.get("score")),
                )
                if part
            )
        )
    return ",".join(parts)


def _nested_candidate_sample_csv(value: object) -> str:
    if not isinstance(value, dict):
        return _text(value)
    parts: list[str] = []
    for key, items in value.items():
        parts.append(f"{_text(key)}=[{_candidate_sample_csv(items)}]")
    return ",".join(parts)


def _nested_candidate_score_breakdown_csv(value: object) -> str:
    if not isinstance(value, dict):
        return _text(value)
    parts: list[str] = []
    for key, items in value.items():
        if not isinstance(items, list):
            parts.append(f"{_text(key)}={_text(items)}")
            continue
        samples: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            samples.append(
                ":".join(
                    part
                    for part in (
                        _text(item.get("id")),
                        _text(item.get("name")),
                        f"score={_text(item.get('score') or 0)}",
                        f"core={_text(item.get('core'))}",
                        f"generic={_text(item.get('generic'))}",
                        (
                            f"reason={_text(item.get('reason'))}"
                            if _text(item.get("reason"))
                            else ""
                        ),
                    )
                    if part
                )
            )
        parts.append(f"{_text(key)}=[{';'.join(samples)}]")
    return ",".join(parts)


def _nested_candidate_core_match_csv(value: object) -> str:
    if not isinstance(value, dict):
        return _text(value)
    parts: list[str] = []
    for key, items in value.items():
        if not isinstance(items, list):
            parts.append(f"{_text(key)}={_text(items)}")
            continue
        samples: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            samples.append(
                ":".join(
                    part
                    for part in (
                        _text(item.get("id")),
                        _text(item.get("name")),
                        f"core={_text(item.get('core'))}",
                        f"matched={_text(item.get('matched_core'))}",
                    )
                    if part
                )
            )
        parts.append(f"{_text(key)}=[{';'.join(samples)}]")
    return ",".join(parts)


def _nested_candidate_generic_match_csv(value: object) -> str:
    if not isinstance(value, dict):
        return _text(value)
    parts: list[str] = []
    for key, items in value.items():
        if not isinstance(items, list):
            parts.append(f"{_text(key)}={_text(items)}")
            continue
        samples: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            samples.append(
                ":".join(
                    part
                    for part in (
                        _text(item.get("id")),
                        _text(item.get("name")),
                        f"generic={_text(item.get('generic'))}",
                    )
                    if part
                )
            )
        parts.append(f"{_text(key)}=[{';'.join(samples)}]")
    return ",".join(parts)


def _attempt_results_csv(value: object) -> str:
    if not isinstance(value, list):
        return _text(value)
    parts: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        parts.append(
            ":".join(
                part
                for part in (
                    _text(item.get("id")),
                    _text(item.get("name")),
                    _text(item.get("result")),
                    f"fetched={_text(item.get('fetched_count') or 0)}",
                    f"verified={_text(item.get('verified_match_count') or 0)}",
                )
                if part
            )
        )
    return ",".join(parts)


def _nested_attempt_results_csv(value: object) -> str:
    if not isinstance(value, dict):
        return _text(value)
    parts: list[str] = []
    for key, items in value.items():
        parts.append(f"{_text(key)}=[{_attempt_results_csv(items)}]")
    return ",".join(parts)


def _team_match_sample_csv(value: object) -> str:
    if not isinstance(value, list):
        return _text(value)
    parts: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        parts.append(
            ":".join(
                part
                for part in (
                    _text(item.get("id")),
                    _text(item.get("stake")),
                    _text(item.get("polymarket")),
                    _text(item.get("score")),
                )
                if part
            )
        )
    return ",".join(parts)


def _selection_sample_csv(value: object) -> str:
    if not isinstance(value, list):
        return _text(value)
    parts: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        matched = item.get("matched_tokens")
        if not isinstance(matched, list):
            matched = item.get("overlap")
        matched_text = (
            "+".join(_text(token) for token in matched)
            if isinstance(matched, list)
            else ""
        )
        rejected = item.get("rejected_tokens")
        rejected_text = (
            "-".join(_text(token) for token in rejected)
            if isinstance(rejected, list)
            else ""
        )
        parts.append(
            ":".join(
                part
                for part in (
                    _text(item.get("id")),
                    _text(item.get("name")),
                    _text(item.get("score")),
                    matched_text,
                    rejected_text,
                    _text(item.get("verified_match_count")),
                    _text(item.get("source_hint")),
                )
                if part
            )
        )
    return ",".join(parts)


def _bool(value: object) -> str:
    return str(bool(value)).lower()


def _text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


if __name__ == "__main__":
    raise SystemExit(main())
