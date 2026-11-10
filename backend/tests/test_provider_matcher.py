from __future__ import annotations

import io
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from provider_odds.cli.match import main as match_main
from provider_odds.matcher import match_events, normalize_game
from provider_odds.models import ProviderOddsEvent


def _event(
    *,
    provider: str = "oddspapi",
    bookmaker: str = "stake",
    game: str = "cs2",
    tournament: str | None = "CCT South America Series 3",
    start_time: datetime | None = datetime(2026, 6, 27, 21, 0, tzinfo=timezone.utc),
    team_a: str = "paiN Academy",
    team_b: str = "ALKA",
    market_type: str = "h2h",
) -> ProviderOddsEvent:
    return ProviderOddsEvent(
        provider=provider,
        bookmaker=bookmaker,
        game=game,
        tournament=tournament,
        start_time=start_time,
        team_a=team_a,
        odds_a=2.12,
        team_b=team_b,
        odds_b=1.72,
        market_type=market_type,
        event_url=None,
        raw={},
    )


class ProviderMatcherTest(unittest.TestCase):
    def test_game_aliases_normalize_to_canonical_matcher_keys(self) -> None:
        cases = {
            "17": "cs2",
            17: "cs2",
            "cs2": "cs2",
            "counter-strike": "cs2",
            "counter_strike": "cs2",
            "counterstrike": "cs2",
            "16": "dota2",
            16: "dota2",
            "dota2": "dota2",
            "dota": "dota2",
            "dota-2": "dota2",
            "18": "lol",
            18: "lol",
            "lol": "lol",
            "league-of-legends": "lol",
            "league_of_legends": "lol",
            "leagueoflegends": "lol",
        }

        for game, expected in cases.items():
            with self.subTest(game=game):
                self.assertEqual(normalize_game(game), expected)

    def test_exact_same_teams_match(self) -> None:
        left = [_event()]
        right = [_event(provider="polymarket", bookmaker="polymarket")]

        matches = match_events(left, right)

        self.assertEqual(len(matches), 1)
        match = matches[0]
        self.assertEqual(match["orientation"], "same")
        self.assertEqual(match["score"], 1.0)
        self.assertEqual(match["team_score"], 1.0)
        self.assertEqual(match["start_time_diff_minutes"], 0.0)
        self.assertEqual(
            match["normalized_team_names"],
            {
                "left_team_a": "pain academy",
                "left_team_b": "alka",
                "right_team_a": "pain academy",
                "right_team_b": "alka",
            },
        )

    def test_swapped_teams_match(self) -> None:
        left = [_event(team_a="paiN Academy", team_b="ALKA")]
        right = [
            _event(
                provider="polymarket",
                bookmaker="polymarket",
                team_a="ALKA",
                team_b="paiN Academy",
            )
        ]

        matches = match_events(left, right)

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["orientation"], "swapped")
        self.assertEqual(matches[0]["score"], 1.0)

    def test_small_punctuation_and_case_differences_match(self) -> None:
        left = [_event(team_a="paiN-Academy", team_b="ALKA.")]
        right = [
            _event(
                provider="polymarket",
                bookmaker="polymarket",
                team_a="pain academy",
                team_b="alka",
            )
        ]

        matches = match_events(left, right)

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["orientation"], "same")
        self.assertEqual(matches[0]["score"], 1.0)

    def test_numeric_cs2_game_matches_polymarket_cs2_with_esports_case_variant(
        self,
    ) -> None:
        left = [
            {
                "provider": "oddspapi",
                "bookmaker": "stake",
                "game": "17",
                "team_a": "Yawara eSports",
                "team_b": "Blitzkrieg",
                "start_time": "2026-06-28T16:00:00+00:00",
                "market_type": "h2h",
                "raw": {},
            }
        ]
        right = [
            {
                "provider": "polymarket",
                "bookmaker": "polymarket",
                "game": "cs2",
                "team_a": "Yawara Esports",
                "team_b": "Blitzkrieg",
                "start_time": "2026-06-28T16:00:00+00:00",
                "market_type": "h2h",
                "event_url": (
                    "https://polymarket.com/event/cs2-yaw-bk-2026-06-28"
                ),
                "raw": {},
            }
        ]

        matches = match_events(left, right)

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["orientation"], "same")
        self.assertGreaterEqual(matches[0]["score"], 0.82)
        self.assertEqual(matches[0]["start_time_diff_minutes"], 0.0)

    def test_lol_lyon_gaming_alias_matches_lyon_with_swapped_teams(self) -> None:
        left = [
            _event(
                game="lol",
                tournament="Mid-Season Invitational",
                team_a="Furia Esports",
                team_b="Lyon Gaming",
            )
        ]
        right = [
            _event(
                provider="polymarket",
                bookmaker="polymarket",
                game="lol",
                tournament="Mid-Season Invitational",
                team_a="LYON",
                team_b="FURIA Esports",
            )
        ]

        matches = match_events(left, right)

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["orientation"], "swapped")
        self.assertEqual(matches[0]["score"], 1.0)
        self.assertEqual(
            matches[0]["normalized_team_names"],
            {
                "left_team_a": "furia esports",
                "left_team_b": "lyon",
                "right_team_a": "lyon",
                "right_team_b": "furia esports",
            },
        )

    def test_lol_bilibili_gaming_t1_still_matches(self) -> None:
        left = [
            _event(
                game="lol",
                tournament="Mid-Season Invitational",
                team_a="Bilibili Gaming",
                team_b="T1",
            )
        ]
        right = [
            _event(
                provider="polymarket",
                bookmaker="polymarket",
                game="lol",
                tournament="Mid-Season Invitational",
                team_a="Bilibili Gaming",
                team_b="T1",
            )
        ]

        matches = match_events(left, right)

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["orientation"], "same")
        self.assertEqual(matches[0]["score"], 1.0)

    def test_lyon_gaming_alias_does_not_apply_to_cs2(self) -> None:
        left = [
            _event(
                game="cs2",
                team_a="Furia Esports",
                team_b="Lyon Gaming",
            )
        ]
        right = [
            _event(
                provider="polymarket",
                bookmaker="polymarket",
                game="cs2",
                team_a="LYON",
                team_b="FURIA Esports",
            )
        ]

        matches = match_events(left, right)

        self.assertEqual(matches, [])

    def test_different_games_reject(self) -> None:
        left = [_event(game="cs2")]
        right = [_event(provider="polymarket", game="dota2")]

        matches = match_events(left, right)

        self.assertEqual(matches, [])

    def test_different_teams_reject(self) -> None:
        left = [_event(team_a="Team Spirit", team_b="Gaimin Gladiators")]
        right = [
            _event(
                provider="polymarket",
                team_a="Natus Vincere",
                team_b="Vitality",
            )
        ]

        matches = match_events(left, right)

        self.assertEqual(matches, [])

    def test_start_times_too_far_apart_reject(self) -> None:
        left = [_event(start_time=datetime(2026, 6, 27, 21, 0, tzinfo=timezone.utc))]
        right = [
            _event(
                provider="polymarket",
                start_time=datetime(2026, 6, 28, 1, 1, tzinfo=timezone.utc),
            )
        ]

        matches = match_events(left, right, max_start_time_diff_minutes=180)

        self.assertEqual(matches, [])

    def test_missing_start_time_can_still_match_by_game_and_teams(self) -> None:
        left = [_event(start_time=None)]
        right = [_event(provider="polymarket", start_time=None)]

        matches = match_events(left, right)

        self.assertEqual(len(matches), 1)
        self.assertIsNone(matches[0]["start_time_diff_minutes"])

    def test_generic_weak_team_names_do_not_false_positive(self) -> None:
        left = [_event(team_a="Team A", team_b="Team B")]
        right = [_event(provider="polymarket", team_a="Team C", team_b="Team D")]

        matches = match_events(left, right)

        self.assertEqual(matches, [])

    def test_cli_table_output_works(self) -> None:
        fixture_dir = Path(__file__).parent / "fixtures"

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            result = match_main(
                [
                    "--left-file",
                    str(fixture_dir / "matcher_stake_events.json"),
                    "--right-file",
                    str(fixture_dir / "matcher_polymarket_events.json"),
                    "--format",
                    "table",
                    "--debug",
                ]
            )

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("game", output)
        self.assertIn("left_provider", output)
        self.assertIn("right_provider", output)
        self.assertIn("oddspapi", output)
        self.assertIn("polymarket", output)
        self.assertIn("paiN Academy / ALKA", output)
        self.assertIn("ALKA / paiN Academy", output)
        self.assertIn("swapped", output)
        self.assertIn("15", output)

    def test_cli_json_output_works(self) -> None:
        fixture_dir = Path(__file__).parent / "fixtures"

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            result = match_main(
                [
                    "--left-file",
                    str(fixture_dir / "matcher_stake_events.json"),
                    "--right-file",
                    str(fixture_dir / "matcher_polymarket_events.json"),
                    "--format",
                    "json",
                ]
            )

        self.assertEqual(result, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["orientation"], "swapped")
        self.assertEqual(payload[0]["left_event"]["provider"], "oddspapi")
        self.assertEqual(payload[0]["right_event"]["provider"], "polymarket")
        self.assertEqual(payload[0]["start_time_diff_minutes"], 15.0)


if __name__ == "__main__":
    unittest.main()
