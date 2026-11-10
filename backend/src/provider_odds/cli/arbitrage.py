from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from provider_odds.arbitrage import (
    ArbitrageConfig,
    calculate_arbitrage,
    extract_matches,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Calculate offline two-way Stake/Polymarket arbitrage from matched events."
    )
    parser.add_argument("--matches-file", type=Path, required=True)
    parser.add_argument("--format", choices=["table", "json"], default="table")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--polymarket-fee-rate", type=float, default=0.0)
    args = parser.parse_args(argv)

    try:
        payload = json.loads(args.matches_file.read_text(encoding="utf-8"))
        matches = extract_matches(payload)
        result = calculate_arbitrage(
            matches,
            config=ArbitrageConfig(
                polymarket_fee_rate=args.polymarket_fee_rate,
            ),
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        print(format_table(result, debug=args.debug))
    return 0


def format_table(result: dict[str, Any], *, debug: bool = False) -> str:
    opportunities = _opportunities(result.get("opportunities"))
    lines: list[str] = []
    if opportunities:
        lines.append(format_opportunities_table(opportunities))
    else:
        lines.append("(no opportunities)")

    if debug:
        lines.append("")
        lines.extend(_format_debug(result))

    return "\n".join(lines)


def format_opportunities_table(opportunities: list[dict[str, Any]]) -> str:
    columns = [
        "game",
        "start_time",
        "stake_team",
        "stake_odds",
        "polymarket_team",
        "pm_raw_odds",
        "pm_eff_odds",
        "edge_percent",
        "cancel_risk",
        "stake_url",
        "polymarket_url",
    ]
    rows = [_table_row(opportunity) for opportunity in opportunities]
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


def _table_row(opportunity: dict[str, Any]) -> dict[str, str]:
    return {
        "game": _text(opportunity.get("game")),
        "start_time": _text(opportunity.get("start_time")),
        "stake_team": _text(opportunity.get("stake_team")),
        "stake_odds": _number(opportunity.get("stake_odds")),
        "polymarket_team": _text(opportunity.get("polymarket_team")),
        "pm_raw_odds": _number(opportunity.get("polymarket_raw_odds")),
        "pm_eff_odds": _number(opportunity.get("polymarket_effective_odds")),
        "edge_percent": _number(opportunity.get("edge_percent")),
        "cancel_risk": str(bool(opportunity.get("cancel_risk"))).lower(),
        "stake_url": _text(opportunity.get("stake_url")),
        "polymarket_url": _text(opportunity.get("polymarket_url")),
    }


def _format_debug(result: dict[str, Any]) -> list[str]:
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    debug = result.get("debug") if isinstance(result.get("debug"), dict) else {}
    return [
        "debug:",
        f"match_count={_text(summary.get('match_count'))}",
        f"opportunity_count={_text(summary.get('opportunity_count'))}",
        f"reason_counts={_counts(debug.get('reason_counts'))}",
    ]


def _opportunities(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _counts(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    return ",".join(f"{_text(key)}:{_text(count)}" for key, count in value.items())


def _number(value: object) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.6f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return _text(value)


def _text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


if __name__ == "__main__":
    raise SystemExit(main())
