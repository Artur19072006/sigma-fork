from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from provider_odds.matcher import match_events


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Match normalized ProviderOddsEvent JSON from two providers."
    )
    parser.add_argument("--left-file", type=Path, required=True)
    parser.add_argument("--right-file", type=Path, required=True)
    parser.add_argument("--format", choices=["table", "json"], default="table")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--max-start-time-diff-minutes", type=int, default=180)
    parser.add_argument("--min-score", type=float, default=0.82)
    args = parser.parse_args(argv)

    left_events = load_events(args.left_file)
    right_events = load_events(args.right_file)
    matches = match_events(
        left_events,
        right_events,
        max_start_time_diff_minutes=args.max_start_time_diff_minutes,
        min_score=args.min_score,
    )

    if args.format == "json":
        print(json.dumps(matches, indent=2))
    else:
        print(format_table(matches))

    return 0


def load_events(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("events", [])
    if not isinstance(payload, list):
        raise ValueError(f"{path} must contain a list or an object with an events list")
    return [event for event in payload if isinstance(event, dict)]


def format_table(matches: list[dict[str, Any]]) -> str:
    if not matches:
        return "(no matches)"

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
    }


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


def _text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


if __name__ == "__main__":
    raise SystemExit(main())
