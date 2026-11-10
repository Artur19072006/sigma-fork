"""Bounded, allowlisted dashboard projections of scanner state.

This module deliberately knows nothing about browser collection, matching,
evaluation, Telegram, or credentials. It reads already-sanitized files and
returns a stable public projection for the dashboard.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any


SPORTS = ("CS2", "Dota 2", "Valorant", "League of Legends")
MAX_SOURCE_BYTES = 1_048_576
MAX_OPPORTUNITIES = 100
_SECRET_PATTERN = re.compile(
    r"(?:authorization|bearer|password|secret|token|cookie|session)", re.I
)


class DashboardDataError(ValueError):
    """A state file is malformed or exceeds the read-only contract."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def safe_time(value: object) -> str | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None
        return iso_utc(value)
    parsed = parse_time(value)
    return iso_utc(parsed) if parsed is not None else None


def read_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        if path.stat().st_size > MAX_SOURCE_BYTES:
            raise DashboardDataError("dashboard source exceeds size bound")
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DashboardDataError("dashboard source is unreadable") from exc
    except json.JSONDecodeError as exc:
        raise DashboardDataError("dashboard source is not valid JSON") from exc
    if not isinstance(value, dict):
        raise DashboardDataError("dashboard source root must be an object")
    return value


def safe_text(value: object, *, fallback: str = "Unavailable", limit: int = 160) -> str:
    if not isinstance(value, str):
        return fallback
    text = " ".join(value.split())
    if not text:
        return fallback
    if _SECRET_PATTERN.search(text):
        return "Redacted"
    return text[:limit]


def safe_number(
    value: object,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    default: float = 0.0,
) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    if minimum is not None and number < minimum:
        return default
    if maximum is not None and number > maximum:
        return default
    return number


def safe_int(value: object, *, maximum: int = 1_000_000_000) -> int:
    number = safe_number(value, minimum=0, maximum=maximum)
    return int(number)


def canonical_sport(value: object) -> str | None:
    text = safe_text(value, fallback="", limit=40).casefold()
    aliases = {
        "cs2": "CS2",
        "counter-strike": "CS2",
        "counter strike": "CS2",
        "dota 2": "Dota 2",
        "dota2": "Dota 2",
        "valorant": "Valorant",
        "league of legends": "League of Legends",
        "league_of_legends": "League of Legends",
        "lol": "League of Legends",
    }
    return aliases.get(text)


def _stable_id(item: dict[str, Any]) -> str:
    identity = "|".join(
        str(item.get(key, ""))
        for key in ("sport", "team_a", "team_b", "start_time", "duel_side")
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:18]


def normalize_opportunity(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    sport = canonical_sport(raw.get("sport"))
    if sport is None:
        return None
    team_a = safe_text(raw.get("team_a"), fallback="", limit=100)
    team_b = safe_text(raw.get("team_b"), fallback="", limit=100)
    if not team_a or not team_b or team_a.casefold() == team_b.casefold():
        return None
    status = safe_text(raw.get("status"), fallback="expired", limit=20).casefold()
    if status == "updated":
        status = "active"
    if status not in {"active", "suspended", "expired"}:
        status = "expired"
    observed_at = safe_time(raw.get("observed_at"))
    first_detected_at = (
        safe_time(raw.get("first_detected_at"))
        or safe_time(raw.get("discovered_at"))
        or observed_at
    )
    last_seen_at = safe_time(raw.get("last_seen_at")) or observed_at or first_detected_at
    expired_at = safe_time(raw.get("expired_at"))
    normalized = {
        "id": safe_text(raw.get("id"), fallback="", limit=80),
        "sport": sport,
        "team_a": team_a,
        "team_b": team_b,
        "tournament": safe_text(raw.get("tournament"), limit=120),
        "start_time": safe_text(raw.get("start_time"), limit=64),
        "duel_side": safe_text(raw.get("duel_side"), limit=100),
        "duel_odds": safe_number(raw.get("duel_odds"), minimum=1.0, maximum=10_000),
        "polymarket_side": safe_text(raw.get("polymarket_side"), limit=100),
        "polymarket_price": safe_number(
            raw.get("polymarket_price"), minimum=0.0001, maximum=1
        ),
        "edge_percent": safe_number(
            raw.get("edge_percent"), minimum=-100, maximum=10_000
        ),
        # discovered_at remains the frontend compatibility name for the first
        # time this stable opportunity identity was observed.
        "discovered_at": first_detected_at or "",
        "first_detected_at": first_detected_at or "",
        "last_seen_at": last_seen_at or "",
        "expired_at": expired_at if status == "expired" else None,
        "status": status,
        "status_reason": (
            safe_text(
                raw.get("status_reason", raw.get("reason")),
                fallback="",
                limit=120,
            )
            or None
        ),
        "best_ask_size": safe_number(raw.get("best_ask_size"), minimum=0),
        "taker_fee": safe_number(raw.get("taker_fee"), minimum=0, maximum=1),
        "freshness_seconds": safe_number(
            raw.get("freshness_seconds"), minimum=0, maximum=86_400
        ),
        "demo": bool(raw.get("demo", False)),
    }
    normalized["id"] = normalized["id"] or _stable_id(normalized)
    return normalized


def normalize_coverage(raw: object) -> list[dict[str, Any]]:
    source = raw if isinstance(raw, list) else []
    by_sport: dict[str, dict[str, Any]] = {}
    for item in source:
        if not isinstance(item, dict):
            continue
        sport = canonical_sport(item.get("sport"))
        if sport is None:
            continue
        by_sport[sport] = {
            "sport": sport,
            "duel_events": safe_int(item.get("duel_events")),
            "bindings": safe_int(item.get("bindings")),
            "evaluator_runs": safe_int(item.get("evaluator_runs")),
            "status": safe_text(item.get("status"), fallback="idle", limit=20),
        }
    return [
        by_sport.get(
            sport,
            {
                "sport": sport,
                "duel_events": 0,
                "bindings": 0,
                "evaluator_runs": 0,
                "status": "idle",
            },
        )
        for sport in SPORTS
    ]


class DashboardRepository:
    """Merge existing health with a separate sanitized snapshot file."""

    def __init__(
        self,
        *,
        health_path: Path | None,
        state_path: Path | None,
        demo_path: Path,
        demo_enabled: bool = False,
        stale_after_seconds: float = 120,
    ) -> None:
        self.health_path = health_path
        self.state_path = state_path
        self.demo_path = demo_path
        self.demo_enabled = bool(demo_enabled)
        self.stale_after_seconds = max(float(stale_after_seconds), 15.0)

    def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        moment = (now or utc_now()).astimezone(timezone.utc)
        health = read_json(self.health_path)
        state = read_json(self.state_path)
        demo = state is None and self.demo_enabled
        if state is None and demo:
            state = read_json(self.demo_path) or {}
        elif state is None:
            state = {}

        opportunities = [
            normalized
            for raw in list(state.get("opportunities", []))[:MAX_OPPORTUNITIES]
            if (normalized := normalize_opportunity(raw)) is not None
        ]
        opportunities.sort(
            key=lambda item: parse_time(item["discovered_at"])
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        coverage = normalize_coverage(state.get("coverage"))
        scanner = self._scanner_projection(health, demo=demo, now=moment)
        if not demo:
            opportunities = [
                self._fail_closed_status(item, scanner=scanner, now=moment)
                for item in opportunities
            ]
        active = [item for item in opportunities if item["status"] == "active"]
        cycle = state.get("cycle") if isinstance(state.get("cycle"), dict) else {}
        return {
            "version": 1,
            "generated_at": iso_utc(moment),
            "mode": "demo" if demo else "live",
            "scanner": scanner,
            "summary": {
                "cycle_count": scanner["cycle_count"],
                "failure_count": scanner["failure_count"],
                "duel_event_count": sum(item["duel_events"] for item in coverage),
                "binding_count": sum(item["bindings"] for item in coverage),
                "evaluator_runs": sum(item["evaluator_runs"] for item in coverage),
                "active_opportunities": len(active),
                "cycle_duration_seconds": safe_number(
                    cycle.get("duration_seconds"), minimum=0, maximum=3_600
                ),
            },
            "coverage": coverage,
            "active_opportunities": active,
            "recent_opportunities": opportunities,
            "notice": (
                "Sample opportunities — no live opportunity snapshot is configured."
                if demo
                else "Read-only projection of sanitized scanner state."
            ),
        }

    def _fail_closed_status(
        self,
        item: dict[str, Any],
        *,
        scanner: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        if item["status"] != "active":
            return item
        last_seen = parse_time(item.get("last_seen_at"))
        stale = (
            not scanner["online"]
            or last_seen is None
            or (now - last_seen).total_seconds() > self.stale_after_seconds
        )
        if not stale:
            return item
        return {
            **item,
            "status": "expired",
            "expired_at": iso_utc(now),
            "status_reason": "scanner_state_stale",
        }

    def _scanner_projection(
        self,
        health: dict[str, Any] | None,
        *,
        demo: bool,
        now: datetime,
    ) -> dict[str, Any]:
        if health is None:
            return {
                "state": "demo" if demo else "offline",
                "online": False,
                "ready": False,
                "last_cycle_at": None,
                "cycle_count": 0,
                "failure_count": 0,
                "consecutive_failures": 0,
                "last_error": None,
            }
        last_cycle = parse_time(health.get("last_cycle_at"))
        fresh = bool(
            last_cycle is not None
            and 0 <= (now - last_cycle).total_seconds() <= self.stale_after_seconds
        )
        status = safe_text(health.get("status"), fallback="unknown", limit=32).casefold()
        ready = health.get("ready") is True
        online = ready and fresh and status in {"healthy", "running"}
        if online:
            state = "online"
        elif status in {"failed", "degraded"}:
            state = "degraded"
        else:
            state = "offline"
        return {
            "state": state,
            "online": online,
            "ready": ready,
            "last_cycle_at": iso_utc(last_cycle) if last_cycle else None,
            "cycle_count": safe_int(health.get("cycles")),
            "failure_count": safe_int(health.get("failures")),
            "consecutive_failures": safe_int(health.get("consecutive_failures")),
            "last_error": (
                safe_text(health.get("last_error"), fallback="", limit=256) or None
            ),
        }


def _nested(raw: object, *keys: str) -> object:
    current = raw
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _runtime_opportunities(report: dict[str, Any]) -> Iterable[dict[str, Any]]:
    direct = report.get("opportunities")
    if isinstance(direct, (list, tuple)):
        for item in direct:
            if isinstance(item, dict):
                yield item
    evaluations = report.get("evaluations")
    if isinstance(evaluations, (list, tuple)):
        for evaluation in evaluations:
            if not isinstance(evaluation, dict):
                continue
            for key in ("current_opportunities", "opportunities"):
                items = evaluation.get(key)
                if isinstance(items, (list, tuple)):
                    for item in items:
                        if isinstance(item, dict):
                            yield item


def _project_runtime_opportunity(raw: dict[str, Any]) -> dict[str, Any] | None:
    match = _nested(raw, "identity", "market", "match")
    identity = raw.get("identity")
    if not isinstance(match, dict) or not isinstance(identity, dict):
        return None
    observed_at = safe_time(raw.get("observed_at"))
    return normalize_opportunity(
        {
            "sport": match.get("esport"),
            "team_a": match.get("team_a"),
            "team_b": match.get("team_b"),
            "tournament": match.get("tournament"),
            "start_time": safe_time(match.get("scheduled_start")),
            "duel_side": identity.get("bookmaker_selection"),
            "duel_odds": raw.get("bookmaker_odds"),
            "polymarket_side": identity.get("polymarket_selection"),
            "polymarket_price": raw.get("polymarket_price"),
            "edge_percent": raw.get("edge_percent"),
            "observed_at": observed_at,
            "discovered_at": observed_at,
            "last_seen_at": observed_at,
            "status": raw.get("status"),
            "reason": raw.get("reason"),
            "best_ask_size": raw.get("polymarket_size"),
            "taker_fee": raw.get("polymarket_taker_fee"),
            "freshness_seconds": raw.get("data_age_seconds"),
        }
    )


def _merge_opportunity(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> dict[str, Any]:
    first_detected_at = (
        safe_time((previous or {}).get("first_detected_at"))
        or safe_time((previous or {}).get("discovered_at"))
        or safe_time(current.get("first_detected_at"))
        or safe_time(current.get("discovered_at"))
        or ""
    )
    current_observed_at = (
        safe_time(current.get("last_seen_at"))
        or safe_time(current.get("discovered_at"))
    )
    if current["status"] == "active":
        last_seen_at = current_observed_at or first_detected_at
        expired_at = None
    else:
        last_seen_at = (
            safe_time((previous or {}).get("last_seen_at"))
            or current_observed_at
            or first_detected_at
        )
        expired_at = (
            current_observed_at
            if current["status"] == "expired"
            else safe_time((previous or {}).get("expired_at"))
        )
    return {
        **current,
        "discovered_at": first_detected_at,
        "first_detected_at": first_detected_at,
        "last_seen_at": last_seen_at,
        "expired_at": expired_at,
    }


def _history_sort_time(item: dict[str, Any]) -> datetime:
    return (
        parse_time(item.get("expired_at"))
        or parse_time(item.get("last_seen_at"))
        or parse_time(item.get("discovered_at"))
        or datetime.min.replace(tzinfo=timezone.utc)
    )


def normalize_runtime_report(
    report: dict[str, Any],
    *,
    previous: dict[str, Any] | None = None,
    now: datetime | None = None,
    duration_seconds: float | None = None,
) -> dict[str, Any]:
    """Convert one sanitized DuelMonitor report to bounded dashboard state."""
    moment = (now or utc_now()).astimezone(timezone.utc)
    rows = (
        report.get("rows")
        if isinstance(report.get("rows"), (list, tuple))
        else []
    )
    row_counts = {sport: 0 for sport in SPORTS}
    duel_coverage = report.get('duel_coverage')
    raw_coverage = duel_coverage.get('sports', {}) if isinstance(duel_coverage, dict) else {}
    coverage_sports = {canonical_sport(sport): item for sport, item in raw_coverage.items()
                       if canonical_sport(sport) is not None and isinstance(item, dict)} if isinstance(raw_coverage, dict) else {}
    for row in rows:
        if isinstance(row, dict) and (sport := canonical_sport(row.get("sport"))):
            row_counts[sport] += 1

    projected = [
        item
        for raw in _runtime_opportunities(report)
        if (item := _project_runtime_opportunity(raw)) is not None
    ]
    history: dict[str, dict[str, Any]] = {}
    if isinstance(previous, dict):
        for raw in list(previous.get("opportunities", []))[:MAX_OPPORTUNITIES]:
            if (item := normalize_opportunity(raw)) is not None:
                history[item["id"]] = item
    seen_ids: set[str] = set()
    for item in projected:
        seen_ids.add(item["id"])
        history[item["id"]] = _merge_opportunity(history.get(item["id"]), item)

    # A completed production cycle is authoritative. This also prevents an
    # active record from surviving a scanner restart when the in-memory
    # lifecycle tracker no longer contains the previous identity.
    for item_id, item in tuple(history.items()):
        if item_id in seen_ids or item["status"] not in {"active", "suspended"}:
            continue
        history[item_id] = {
            **item,
            "status": "expired",
            "expired_at": iso_utc(moment),
            "status_reason": "not_present_in_completed_cycle",
        }

    discovery = report.get("polymarket_discovery")
    discovery_count = len(discovery) if isinstance(discovery, (list, tuple)) else 0
    attempts = report.get("candidate_attempts")
    attempt_count = len(attempts) if isinstance(attempts, (list, tuple)) else 0
    evaluations = report.get("evaluations")
    evaluator_counts = {sport: 0 for sport in SPORTS}
    binding_counts = {sport: 0 for sport in SPORTS}
    if isinstance(evaluations, (list, tuple)):
        for evaluation in evaluations:
            if not isinstance(evaluation, dict):
                continue
            sport = canonical_sport(evaluation.get("sport"))
            if sport is None:
                opportunity_items = evaluation.get("current_opportunities")
                if not isinstance(opportunity_items, (list, tuple)) or not opportunity_items:
                    opportunity_items = evaluation.get("opportunities")
                first = (
                    opportunity_items[0]
                    if isinstance(opportunity_items, (list, tuple))
                    and opportunity_items
                    else None
                )
                sport = canonical_sport(
                    _nested(first, "identity", "market", "match", "esport")
                )
            if sport is None:
                continue
            evaluator_counts[sport] += 1
            bindings = evaluation.get("bindings")
            if isinstance(bindings, (list, tuple)) and any(
                isinstance(binding, dict) and binding.get("accepted") is True
                for binding in bindings
            ):
                binding_counts[sport] += 1

    ordered = sorted(history.values(), key=_history_sort_time, reverse=True)
    active_history = [item for item in ordered if item["status"] == "active"]
    inactive_history = [item for item in ordered if item["status"] != "active"]
    bounded_history = (active_history + inactive_history)[:MAX_OPPORTUNITIES]

    return {
        "version": 1,
        "generated_at": iso_utc(moment),
        "cycle": {
            "duration_seconds": safe_number(
                duration_seconds, minimum=0, maximum=3_600
            ),
            "discovery_events": discovery_count,
            "candidate_attempts": attempt_count,
        },
        "coverage": [
            {
                "sport": sport,
                "duel_events": row_counts[sport],
                "bindings": binding_counts[sport],
                "evaluator_runs": evaluator_counts[sport],
                "status": (
                    'healthy' if coverage_sports[sport].get('complete') is True else 'incomplete'
                ) if isinstance(coverage_sports.get(sport), dict) else ("healthy" if row_counts[sport] else "idle"),
                "known_match_count": coverage_sports.get(sport, {}).get('known_match_count'),
                "unknown_tournaments": coverage_sports.get(sport, {}).get('unknown_tournaments', []),
            }
            for sport in SPORTS
        ],
        "opportunities": bounded_history,
        "duel_coverage": duel_coverage if isinstance(duel_coverage, dict) else None,
    }
