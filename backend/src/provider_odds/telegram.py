from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from provider_odds.env import load_dotenv

TELEGRAM_BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID_ENV = "TELEGRAM_CHAT_ID"
TELEGRAM_API_BASE_URL = "https://api.telegram.org"
DEFAULT_TELEGRAM_DEDUP_STATE_PATH = Path(".cache/telegram_sent_opportunities.json")
DEFAULT_TELEGRAM_DEDUP_TTL_HOURS = 6.0
DEFAULT_TELEGRAM_REALERT_EDGE_IMPROVEMENT = 0.3


class TelegramConfigurationError(ValueError):
    pass


class TelegramSendError(RuntimeError):
    pass


def format_arbitrage_alert(opportunity: dict[str, Any]) -> str:
    game = _display_game(opportunity.get("game")) or "CS2"
    team_a = _text(opportunity.get("team_a"))
    team_b = _text(opportunity.get("team_b"))
    lines = [
        f"⚡ {game} ARB",
        "",
        f"{team_a} vs {team_b}",
        "",
        "Stake: "
        f"{_text(opportunity.get('stake_team'))} @ {_odds(opportunity.get('stake_odds'))}",
        "Poly: "
        f"{_text(opportunity.get('polymarket_team'))} @ "
        f"{_odds(_polymarket_display_odds(opportunity))}",
    ]
    if _is_cancel_risk(opportunity.get("cancel_risk")):
        lines.extend(["", "Cancel risk"])

    return "\n".join(lines)


def build_arbitrage_alert_messages(
    opportunities: list[dict[str, Any]],
) -> list[str]:
    return [
        format_arbitrage_alert(opportunity)
        for opportunity in opportunities
        if isinstance(opportunity, dict)
    ]


def send_arbitrage_alerts(
    opportunities: list[dict[str, Any]],
    *,
    dry_run: bool = False,
    env_file: str | Path = ".env",
    bot_token_env: str = TELEGRAM_BOT_TOKEN_ENV,
    chat_id_env: str = TELEGRAM_CHAT_ID_ENV,
    dedup_state_path: str | Path = DEFAULT_TELEGRAM_DEDUP_STATE_PATH,
    dedup_ttl_hours: float = DEFAULT_TELEGRAM_DEDUP_TTL_HOURS,
    realert_edge_improvement: float = DEFAULT_TELEGRAM_REALERT_EDGE_IMPROVEMENT,
    now_func: Callable[[], datetime] | None = None,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    clean_opportunities = [
        opportunity for opportunity in opportunities if isinstance(opportunity, dict)
    ]
    messages = build_arbitrage_alert_messages(clean_opportunities)
    empty_dedup = {
        "dedup_suppressed_count": 0,
        "dedup_sent_new_count": 0,
        "dedup_state_size": 0,
    }
    if not messages:
        return {
            "enabled": True,
            "dry_run": dry_run,
            "message_count": 0,
            "sent_count": 0,
            "skipped_reason": "no_opportunities",
            **empty_dedup,
        }

    if dry_run:
        return {
            "enabled": True,
            "dry_run": True,
            "message_count": len(messages),
            "sent_count": 0,
            "skipped_reason": None,
            **empty_dedup,
        }

    now = _utc_now(now_func)
    state_path = Path(dedup_state_path)
    state = _load_dedup_state(state_path)
    state_entries = _pruned_dedup_entries(
        state,
        now=now,
        ttl_hours=dedup_ttl_hours,
    )
    send_items: list[tuple[dict[str, Any], str, str]] = []
    suppressed_count = 0
    for opportunity, message in zip(clean_opportunities, messages, strict=True):
        dedup_key = build_opportunity_dedup_key(opportunity)
        entry = state_entries.get(dedup_key)
        if _dedup_entry_suppresses(
            entry,
            opportunity,
            now=now,
            ttl_hours=dedup_ttl_hours,
            realert_edge_improvement=realert_edge_improvement,
        ):
            suppressed_count += 1
            continue
        send_items.append((opportunity, message, dedup_key))

    if not send_items:
        return {
            "enabled": True,
            "dry_run": False,
            "message_count": len(messages),
            "sent_count": 0,
            "skipped_reason": "dedup_suppressed",
            "dedup_suppressed_count": suppressed_count,
            "dedup_sent_new_count": 0,
            "dedup_state_size": len(state_entries),
        }

    token, chat_id = _telegram_credentials(
        env_file=env_file,
        bot_token_env=bot_token_env,
        chat_id_env=chat_id_env,
    )
    sent_count = 0
    for opportunity, message, dedup_key in send_items:
        send_telegram_message(
            token=token,
            chat_id=chat_id,
            text=message,
            opener=opener,
        )
        state_entries[dedup_key] = _dedup_state_entry(opportunity, now=now)
        state["opportunities"] = state_entries
        _write_dedup_state(state_path, state)
        sent_count += 1

    return {
        "enabled": True,
        "dry_run": False,
        "message_count": len(messages),
        "sent_count": sent_count,
        "skipped_reason": None,
        "dedup_suppressed_count": suppressed_count,
        "dedup_sent_new_count": sent_count,
        "dedup_state_size": len(state_entries),
    }


def send_telegram_message(
    *,
    token: str,
    chat_id: str,
    text: str,
    opener: Callable[..., Any] = urlopen,
    timeout_seconds: int = 20,
) -> None:
    url = f"{TELEGRAM_API_BASE_URL}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    data = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        response = opener(request, timeout=timeout_seconds)
        read = getattr(response, "read", None)
        if callable(read):
            read()
        close = getattr(response, "close", None)
        if callable(close):
            close()
    except (HTTPError, URLError, OSError) as exc:
        raise TelegramSendError("Telegram sendMessage request failed") from exc


def _telegram_credentials(
    *,
    env_file: str | Path,
    bot_token_env: str,
    chat_id_env: str,
) -> tuple[str, str]:
    load_dotenv(env_file)
    token = os.environ.get(bot_token_env, "").strip()
    chat_id = os.environ.get(chat_id_env, "").strip()
    missing = [
        name
        for name, value in (
            (bot_token_env, token),
            (chat_id_env, chat_id),
        )
        if not value
    ]
    if missing:
        raise TelegramConfigurationError(f"Missing {' / '.join(missing)}")
    return token, chat_id


def build_opportunity_dedup_key(opportunity: dict[str, Any]) -> str:
    team_a = _identity_text(opportunity.get("team_a"))
    team_b = _identity_text(opportunity.get("team_b"))
    matchup = "|".join(sorted(item for item in (team_a, team_b) if item))
    source_ids = (
        opportunity.get("source_ids")
        if isinstance(opportunity.get("source_ids"), dict)
        else {}
    )
    polymarket_ref = _url_identity(opportunity.get("polymarket_url")) or _identity_text(
        _first_present(
            source_ids,
            "polymarket_event_id",
            "polymarket_market_id",
            "polymarket_condition_id",
        )
    )
    identity = {
        "sport": _identity_text(opportunity.get("game")),
        "bookmaker": _identity_text(opportunity.get("bookmaker") or "stake"),
        "matchup": matchup,
        "stake_side": _identity_text(opportunity.get("stake_team")),
        "polymarket_side": _identity_text(opportunity.get("polymarket_team")),
        "polymarket_ref": polymarket_ref,
    }
    raw_key = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _dedup_state_entry(
    opportunity: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    return {
        "sent_at": _utc_iso(now),
        "edge_percent": _float_value(opportunity.get("edge_percent")),
    }


def _dedup_entry_suppresses(
    entry: object,
    opportunity: dict[str, Any],
    *,
    now: datetime,
    ttl_hours: float,
    realert_edge_improvement: float,
) -> bool:
    if not isinstance(entry, dict):
        return False
    sent_at = _parse_utc_time(entry.get("sent_at"))
    if sent_at is None:
        return False
    if now - sent_at > timedelta(hours=max(float(ttl_hours), 0.0)):
        return False
    previous_edge = _float_value(entry.get("edge_percent"))
    current_edge = _float_value(opportunity.get("edge_percent"))
    if (
        current_edge is not None
        and previous_edge is not None
        and current_edge - previous_edge >= float(realert_edge_improvement)
    ):
        return False
    return True


def _load_dedup_state(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {"version": 1, "opportunities": {}}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "opportunities": {}}
    if not isinstance(payload, dict):
        return {"version": 1, "opportunities": {}}
    opportunities = payload.get("opportunities")
    if not isinstance(opportunities, dict):
        opportunities = {}
    return {"version": 1, "opportunities": opportunities}


def _write_dedup_state(path: Path, state: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(state, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        return


def _pruned_dedup_entries(
    state: dict[str, Any],
    *,
    now: datetime,
    ttl_hours: float,
) -> dict[str, dict[str, Any]]:
    entries = state.get("opportunities")
    if not isinstance(entries, dict):
        return {}
    ttl = timedelta(hours=max(float(ttl_hours), 0.0))
    pruned: dict[str, dict[str, Any]] = {}
    for key, entry in entries.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        sent_at = _parse_utc_time(entry.get("sent_at"))
        if sent_at is None or now - sent_at > ttl:
            continue
        pruned[key] = entry
    return pruned


def _utc_now(now_func: Callable[[], datetime] | None) -> datetime:
    value = now_func() if now_func else datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc_time(value: object) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _float_value(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_present(values: dict[str, Any], *keys: str) -> object:
    for key in keys:
        if values.get(key) not in (None, ""):
            return values.get(key)
    return ""


def _url_identity(value: object) -> str:
    text = _text(value)
    if not text:
        return ""
    parsed = urlsplit(text)
    if not parsed.scheme and not parsed.netloc:
        return _identity_text(text)
    return _identity_text(
        urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    )


def _identity_text(value: object) -> str:
    return re.sub(r"\s+", " ", _text(value)).strip().casefold()


def _display_game(value: object) -> str:
    text = _text(value)
    if not text:
        return ""
    return text.replace("_", " ").upper()


def _odds(value: object) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return _text(value)


def _polymarket_display_odds(opportunity: dict[str, Any]) -> object:
    return _first_present(
        opportunity,
        "polymarket_effective_odds",
        "pm_eff_odds",
        "polymarket_raw_odds",
    )


def _is_cancel_risk(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "y"}
    return bool(value)


def _text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())
