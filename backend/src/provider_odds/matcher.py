from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

from provider_odds.models import ProviderOddsEvent

H2H_MARKET_TYPES = {
    "h2h",
    "head_to_head",
    "headtohead",
    "match_winner",
    "matchwinner",
    "winner",
    "moneyline",
    "money_line",
}
GAME_ALIASES = {
    "16": "dota2",
    "dota2": "dota2",
    "dota 2": "dota2",
    "dota": "dota2",
    "esport dota": "dota2",
    "17": "cs2",
    "cs2": "cs2",
    "counter strike": "cs2",
    "counterstrike": "cs2",
    "18": "lol",
    "league-of-legends": "lol",
    "league of legends": "lol",
    "league legends": "lol",
    "league_of_legends": "lol",
    "leagueoflegends": "lol",
    "lol": "lol",
}
PLACEHOLDER_TEAM_NAMES = {
    "a",
    "b",
    "away",
    "home",
    "no",
    "over",
    "tba",
    "tbd",
    "team a",
    "team b",
    "team 1",
    "team 2",
    "to be announced",
    "to be decided",
    "under",
    "yes",
}
LOL_TEAM_ALIASES = {
    "lyon gaming": "lyon",
}


def match_events(
    left_events: Sequence[ProviderOddsEvent | dict[str, Any]],
    right_events: Sequence[ProviderOddsEvent | dict[str, Any]],
    *,
    max_start_time_diff_minutes: int = 180,
    min_score: float = 0.82,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for left_event in left_events:
        left = _PreparedEvent.from_event(left_event)
        if not left.is_matchable:
            continue

        for right_event in right_events:
            right = _PreparedEvent.from_event(right_event)
            if not right.is_matchable:
                continue
            if left.game != right.game:
                continue
            if left.market_type != right.market_type:
                continue

            start_time_diff = _start_time_diff_minutes(left.start_time, right.start_time)
            if (
                start_time_diff is not None
                and start_time_diff > max_start_time_diff_minutes
            ):
                continue

            orientation = _best_orientation(left, right)
            if orientation["team_score"] < min_score:
                continue

            matches.append(
                {
                    "left_event": _event_dict(left_event),
                    "right_event": _event_dict(right_event),
                    "orientation": orientation["orientation"],
                    "score": orientation["team_score"],
                    "team_score": orientation["team_score"],
                    "tournament_score": _text_similarity(
                        left.normalized_tournament,
                        right.normalized_tournament,
                    ),
                    "start_time_diff_minutes": start_time_diff,
                    "normalized_team_names": {
                        "left_team_a": left.team_a_norm,
                        "left_team_b": left.team_b_norm,
                        "right_team_a": right.team_a_norm,
                        "right_team_b": right.team_b_norm,
                    },
                    "team_similarities": orientation["team_similarities"],
                }
            )

    return sorted(
        matches,
        key=lambda item: (
            item["score"],
            -(item["start_time_diff_minutes"] or 0),
        ),
        reverse=True,
    )


class _PreparedEvent:
    def __init__(
        self,
        *,
        original: ProviderOddsEvent | dict[str, Any],
        game: str,
        market_type: str,
        start_time: datetime | None,
        team_a: str,
        team_b: str,
        team_a_norm: str,
        team_b_norm: str,
        normalized_tournament: str,
        is_matchable: bool,
    ) -> None:
        self.original = original
        self.game = game
        self.market_type = market_type
        self.start_time = start_time
        self.team_a = team_a
        self.team_b = team_b
        self.team_a_norm = team_a_norm
        self.team_b_norm = team_b_norm
        self.normalized_tournament = normalized_tournament
        self.is_matchable = is_matchable

    @classmethod
    def from_event(
        cls,
        event: ProviderOddsEvent | dict[str, Any],
    ) -> "_PreparedEvent":
        game = normalize_game(_field(event, "game"))
        market_type = normalize_market_type(_field(event, "market_type"))
        team_a = _clean_original_text(_field(event, "team_a"))
        team_b = _clean_original_text(_field(event, "team_b"))
        team_a_norm = canonical_team_name(team_a, game=game)
        team_b_norm = canonical_team_name(team_b, game=game)
        is_matchable = bool(
            game
            and market_type == "h2h"
            and team_a
            and team_b
            and team_a_norm
            and team_b_norm
            and team_a_norm != team_b_norm
            and not _is_placeholder_team(team_a_norm)
            and not _is_placeholder_team(team_b_norm)
        )
        return cls(
            original=event,
            game=game,
            market_type=market_type,
            start_time=parse_start_time(_field(event, "start_time")),
            team_a=team_a,
            team_b=team_b,
            team_a_norm=team_a_norm,
            team_b_norm=team_b_norm,
            normalized_tournament=canonical_text(_field(event, "tournament")),
            is_matchable=is_matchable,
        )


def normalize_game(value: object) -> str:
    text = canonical_text(value)
    return GAME_ALIASES.get(text, text)


def normalize_market_type(value: object) -> str:
    text = canonical_text(value).replace(" ", "_")
    if text in H2H_MARKET_TYPES:
        return "h2h"
    return text


def canonical_team_name(value: object, *, game: object = "") -> str:
    text = canonical_text(value)
    if normalize_game(game) == "lol":
        return LOL_TEAM_ALIASES.get(text, text)
    return text


def canonical_text(value: object) -> str:
    text = _clean_original_text(value).casefold()
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"e[\s_.-]+sports", "esports", text)
    text = re.sub(r"[-_.]+", " ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def parse_start_time(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _best_orientation(
    left: _PreparedEvent,
    right: _PreparedEvent,
) -> dict[str, Any]:
    same = (
        _team_similarity(left.team_a_norm, right.team_a_norm),
        _team_similarity(left.team_b_norm, right.team_b_norm),
    )
    swapped = (
        _team_similarity(left.team_a_norm, right.team_b_norm),
        _team_similarity(left.team_b_norm, right.team_a_norm),
    )
    same_score = _team_pair_score(same)
    swapped_score = _team_pair_score(swapped)

    if swapped_score > same_score:
        return {
            "orientation": "swapped",
            "team_score": swapped_score,
            "team_similarities": {
                "left_team_a": swapped[0],
                "left_team_b": swapped[1],
            },
        }
    return {
        "orientation": "same",
        "team_score": same_score,
        "team_similarities": {
            "left_team_a": same[0],
            "left_team_b": same[1],
        },
    }


def _team_pair_score(scores: tuple[float, float]) -> float:
    if min(scores) < 0.72:
        return 0.0
    return sum(scores) / 2


def _team_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if _is_placeholder_team(left) or _is_placeholder_team(right):
        return 0.0
    if left == right:
        return 1.0
    if min(len(left), len(right)) <= 3:
        return 0.0

    direct = SequenceMatcher(None, left, right).ratio()
    left_tokens = " ".join(sorted(left.split()))
    right_tokens = " ".join(sorted(right.split()))
    token_sorted = SequenceMatcher(None, left_tokens, right_tokens).ratio()
    return max(direct, token_sorted)


def _text_similarity(left: str, right: str) -> float | None:
    if not left or not right:
        return None
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def _start_time_diff_minutes(
    left: datetime | None,
    right: datetime | None,
) -> float | None:
    if left is None or right is None:
        return None
    return abs((left - right).total_seconds()) / 60


def _is_placeholder_team(value: str) -> bool:
    if value in PLACEHOLDER_TEAM_NAMES:
        return True
    if len(value) < 2:
        return True
    return False


def _event_dict(event: ProviderOddsEvent | dict[str, Any]) -> dict[str, Any]:
    if isinstance(event, ProviderOddsEvent):
        return event.to_dict()
    data = dict(event)
    start_time = data.get("start_time")
    if isinstance(start_time, datetime):
        data["start_time"] = start_time.isoformat()
    data.setdefault("raw", {})
    return data


def _field(event: ProviderOddsEvent | dict[str, Any], name: str) -> object:
    if isinstance(event, dict):
        return event.get(name)
    return getattr(event, name)


def _clean_original_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()
