from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from provider_odds.arbitrage import ArbitrageConfig, calculate_arbitrage
from provider_odds.cli.arbitrage import format_opportunities_table
from provider_odds.providers.base import ProviderConfigurationError, ProviderRequestError
from provider_odds.providers.oddspapi import SUPPORTED_MAPPING_PROFILES
from provider_odds.snapshot import load_event_file, run_snapshot_match
from provider_odds.telegram import (
    TELEGRAM_BOT_TOKEN_ENV,
    TELEGRAM_CHAT_ID_ENV,
    TelegramConfigurationError,
    TelegramSendError,
    build_arbitrage_alert_messages,
    send_arbitrage_alerts,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch or read normalized provider events, match them, and print a snapshot."
    )
    parser.add_argument("--left-file", type=Path)
    parser.add_argument("--right-file", type=Path)
    parser.add_argument("--left-provider", choices=["oddspapi"])
    parser.add_argument("--left-sport")
    parser.add_argument("--left-bookmaker")
    parser.add_argument("--left-tournament-id", action="append")
    parser.add_argument("--left-fixture-id")
    parser.add_argument(
        "--left-mapping-profile",
        choices=sorted(SUPPORTED_MAPPING_PROFILES),
    )
    parser.add_argument("--right-provider", choices=["polymarket"])
    parser.add_argument("--right-sport")
    parser.add_argument("--right-event-slug", action="append")
    parser.add_argument("--right-market-slug", action="append")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--format", choices=["table", "json"], default="table")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--max-start-time-diff-minutes", type=int, default=180)
    parser.add_argument("--min-score", type=float, default=0.82)
    parser.add_argument("--find-arbitrage", action="store_true")
    parser.add_argument("--polymarket-fee-rate", type=float, default=0.03)
    parser.add_argument("--telegram-alert", action="store_true")
    parser.add_argument("--telegram-dry-run", action="store_true")
    parser.add_argument("--telegram-bot-token-env", default=TELEGRAM_BOT_TOKEN_ENV)
    parser.add_argument("--telegram-chat-id-env", default=TELEGRAM_CHAT_ID_ENV)
    args = parser.parse_args(argv)
    if (args.telegram_alert or args.telegram_dry_run) and not args.find_arbitrage:
        parser.error("--telegram-alert/--telegram-dry-run requires --find-arbitrage")

    try:
        left_events = load_event_file(args.left_file) if args.left_file else None
        right_events = load_event_file(args.right_file) if args.right_file else None
        snapshot = run_snapshot_match(
            left_events=left_events,
            right_events=right_events,
            left_provider=args.left_provider,
            right_provider=args.right_provider,
            left_sport=args.left_sport,
            right_sport=args.right_sport,
            left_bookmaker=args.left_bookmaker,
            left_tournament_id=_joined_values(args.left_tournament_id),
            left_fixture_id=args.left_fixture_id,
            left_mapping_profile=args.left_mapping_profile,
            right_event_slug=_joined_values(args.right_event_slug),
            right_market_slug=_joined_values(args.right_market_slug),
            env_file=args.env_file,
            max_start_time_diff_minutes=args.max_start_time_diff_minutes,
            min_score=args.min_score,
        )
        if args.find_arbitrage:
            arbitrage = calculate_arbitrage(
                _matches(snapshot),
                config=ArbitrageConfig(
                    polymarket_fee_rate=args.polymarket_fee_rate,
                ),
            )
            snapshot["arbitrage"] = {
                "summary": arbitrage["summary"],
                "opportunities": arbitrage["opportunities"],
            }
        if args.telegram_alert or args.telegram_dry_run:
            arbitrage = snapshot.get("arbitrage")
            opportunities = (
                _events(arbitrage.get("opportunities"))
                if isinstance(arbitrage, dict)
                else []
            )
            snapshot["telegram"] = send_arbitrage_alerts(
                opportunities,
                dry_run=args.telegram_dry_run,
                env_file=args.env_file,
                bot_token_env=args.telegram_bot_token_env,
                chat_id_env=args.telegram_chat_id_env,
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
        print(json.dumps(snapshot, indent=2))
    else:
        print(format_snapshot_table(snapshot, debug=args.debug))

    return 0


def format_snapshot_table(snapshot: dict[str, Any], *, debug: bool = False) -> str:
    lines = [format_summary(snapshot)]
    matches = _matches(snapshot)
    if matches:
        lines.append("")
        lines.append(format_matches_table(matches))
    else:
        lines.append("")
        lines.append("(no matches)")

    arbitrage = snapshot.get("arbitrage")
    if isinstance(arbitrage, dict):
        lines.append("")
        lines.extend(_format_arbitrage(arbitrage))

    telegram = snapshot.get("telegram")
    if isinstance(telegram, dict):
        lines.append("")
        lines.extend(_format_telegram(snapshot, telegram))

    if debug:
        lines.append("")
        lines.extend(_format_unmatched_debug(snapshot))

    return "\n".join(lines)


def format_summary(snapshot: dict[str, Any]) -> str:
    summary = snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else {}
    return "\n".join(
        [
            "summary:",
            f"left_count={_text(summary.get('left_count'))}",
            f"right_count={_text(summary.get('right_count'))}",
            f"matched_count={_text(summary.get('matched_count'))}",
            f"unmatched_left_count={_text(summary.get('unmatched_left_count'))}",
            f"unmatched_right_count={_text(summary.get('unmatched_right_count'))}",
        ]
    )


def format_matches_table(matches: list[dict[str, Any]]) -> str:
    columns = [
        "game",
        "left_provider",
        "right_provider",
        "left_teams",
        "right_teams",
        "orientation",
        "score",
        "start_time_diff_minutes",
        "left_odds",
        "right_odds",
        "left_url",
        "right_url",
    ]
    rows = [_table_row(match) for match in matches]
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


def _table_row(match: dict[str, Any]) -> dict[str, str]:
    left = _event(match.get("left_event"))
    right = _event(match.get("right_event"))
    return {
        "game": _text(left.get("game")),
        "left_provider": _text(left.get("provider")),
        "right_provider": _text(right.get("provider")),
        "left_teams": _teams(left),
        "right_teams": _teams(right),
        "orientation": _text(match.get("orientation")),
        "score": _number(match.get("score")),
        "start_time_diff_minutes": _number(match.get("start_time_diff_minutes")),
        "left_odds": _odds(left),
        "right_odds": _odds(right),
        "left_url": _text(left.get("event_url")),
        "right_url": _text(right.get("event_url")),
    }


def _format_unmatched_debug(snapshot: dict[str, Any]) -> list[str]:
    lines = ["unmatched_left_samples:"]
    left_samples = _events(snapshot.get("unmatched_left"))[:5]
    if not left_samples:
        lines.append("(none)")
    for index, event in enumerate(left_samples, 1):
        lines.append(_unmatched_line(index, event))

    lines.append("unmatched_right_samples:")
    right_samples = _events(snapshot.get("unmatched_right"))[:5]
    if not right_samples:
        lines.append("(none)")
    for index, event in enumerate(right_samples, 1):
        lines.append(_unmatched_line(index, event))
    return lines


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


def _format_telegram(snapshot: dict[str, Any], telegram: dict[str, Any]) -> list[str]:
    lines = [
        "telegram:",
        f"dry_run={_bool(telegram.get('dry_run'))}",
        f"message_count={_text(telegram.get('message_count') or 0)}",
        f"sent_count={_text(telegram.get('sent_count') or 0)}",
    ]
    skipped_reason = _text(telegram.get("skipped_reason"))
    if skipped_reason:
        lines.append(f"skipped_reason={skipped_reason}")

    if telegram.get("dry_run"):
        arbitrage = snapshot.get("arbitrage")
        opportunities = (
            _events(arbitrage.get("opportunities"))
            if isinstance(arbitrage, dict)
            else []
        )
        messages = build_arbitrage_alert_messages(opportunities)
        for index, message in enumerate(messages, 1):
            lines.extend(["", f"message_{index}:", message])
    return lines


def _unmatched_line(index: int, event: dict[str, Any]) -> str:
    return (
        f"{index}: provider={_text(event.get('provider'))} "
        f"teams={_teams(event)} "
        f"start_time={_text(event.get('start_time'))} "
        f"event_url={_text(event.get('event_url'))}"
    )


def _matches(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    value = snapshot.get("matches")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _events(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _event(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _teams(event: dict[str, Any]) -> str:
    return f"{_text(event.get('team_a'))} / {_text(event.get('team_b'))}"


def _odds(event: dict[str, Any]) -> str:
    return f"{_number(event.get('odds_a'))} / {_number(event.get('odds_b'))}"


def _number(value: object) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.3f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return _text(value)


def _bool(value: object) -> str:
    return str(bool(value)).lower()


def _text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _joined_values(values: list[str] | None) -> str | None:
    if not values:
        return None
    parts: list[str] = []
    for value in values:
        parts.extend(part.strip() for part in value.split(",") if part.strip())
    return ",".join(parts) if parts else None


if __name__ == "__main__":
    raise SystemExit(main())
