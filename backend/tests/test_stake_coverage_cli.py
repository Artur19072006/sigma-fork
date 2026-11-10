from __future__ import annotations

import io
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from provider_odds.cli.stake_coverage import (
    format_coverage_table,
    main as stake_coverage_main,
    run_stake_coverage,
)
from provider_odds.models import ProviderOddsEvent
from provider_odds.providers.base import ProviderRequestError
from provider_odds.providers.oddspapi import (
    MAPPING_PROFILE_ODDSPAPI_STAKE_CS2_MARKET171_SELECTION_ORDER,
)


class FakeCoverageProvider:
    def __init__(
        self,
        tournaments: list[dict[str, object]],
        events_by_tournament_id: dict[str, list[ProviderOddsEvent]] | None = None,
        errors_by_tournament_id: dict[str, Exception | list[Exception]] | None = None,
        candidate_errors: list[Exception] | None = None,
        market_debug_by_tournament_id: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self.tournaments = tournaments
        self.events_by_tournament_id = events_by_tournament_id or {}
        self.errors_by_tournament_id = errors_by_tournament_id or {}
        self.candidate_errors = candidate_errors or []
        self.market_debug_by_tournament_id = market_debug_by_tournament_id or {}
        self.tournament_candidate_calls: list[dict[str, object]] = []
        self.fetch_event_calls: list[dict[str, object]] = []
        self.last_debug: dict[str, object] = {}

    def fetch_tournament_candidates(self, **kwargs: object) -> list[dict[str, object]]:
        self.tournament_candidate_calls.append(kwargs)
        if self.candidate_errors:
            raise self.candidate_errors.pop(0)
        raw_tournaments = list(self.tournaments)
        raw_keywords = kwargs.get("tournament_keywords") or []
        keywords = (
            [str(raw_keywords)]
            if isinstance(raw_keywords, str)
            else [str(value) for value in raw_keywords]
            if isinstance(raw_keywords, list)
            else []
        )
        if keywords:
            candidates = [
                tournament
                for tournament in raw_tournaments
                if any(
                    keyword.casefold() in _candidate_name(tournament).casefold()
                    for keyword in keywords
                )
            ]
        else:
            candidates = raw_tournaments
        max_candidates = int(kwargs.get("max_candidates") or len(candidates))
        returned = list(candidates[:max_candidates])
        self.last_debug = {
            "tournaments_count": len(raw_tournaments),
            "candidate_count": len(candidates),
            "printed_count": len(returned),
        }
        return returned

    def fetch_events(self, **kwargs: object) -> list[ProviderOddsEvent]:
        self.fetch_event_calls.append(kwargs)
        tournament_id = str(kwargs.get("tournament_id"))
        error = self.errors_by_tournament_id.get(tournament_id)
        if isinstance(error, list):
            if error:
                raise error.pop(0)
        elif error:
            raise error
        if tournament_id in self.market_debug_by_tournament_id:
            self.last_debug = dict(self.market_debug_by_tournament_id[tournament_id])
        return list(self.events_by_tournament_id.get(tournament_id, []))


def _event(
    *,
    tournament: str = "CCT South American Series",
    team_a: str = "Yawara eSports",
    team_b: str = "Blitzkrieg",
    game: str = "cs2",
) -> ProviderOddsEvent:
    return ProviderOddsEvent(
        provider="oddspapi",
        bookmaker="stake",
        game=game,
        tournament=tournament,
        start_time=datetime(2026, 6, 28, 16, 0, tzinfo=timezone.utc),
        team_a=team_a,
        odds_a=1.8,
        team_b=team_b,
        odds_b=2.0,
        market_type="h2h",
        event_url=None,
        raw={},
    )


def _candidate_name(candidate: dict[str, object]) -> str:
    return str(candidate.get("name") or candidate.get("tournamentName") or "")


class StakeCoverageCliTest(unittest.TestCase):
    def test_coverage_lists_candidates_without_fetching_fixtures(self) -> None:
        provider = FakeCoverageProvider(
            [
                {
                    "id": "51042",
                    "name": "CCT South American Series",
                    "score": "0.91",
                },
                {"id": "32873", "name": "Open Qualifier"},
                {"id": "999", "name": "Unknown Showmatch"},
            ]
        )

        result = run_stake_coverage(
            sport="cs2",
            bookmaker="stake",
            fetch_fixtures=False,
            provider=provider,
        )

        self.assertEqual(provider.fetch_event_calls, [])
        self.assertEqual(result["summary"]["candidate_count"], 3)
        self.assertEqual(result["summary"]["kept_quality_count"], 1)
        self.assertEqual(result["summary"]["rejected_quality_count"], 2)
        rows = result["candidates"]
        self.assertEqual(rows[0]["tournament_id"], "51042")
        self.assertEqual(rows[0]["raw_score"], "0.91")
        self.assertEqual(rows[0]["source"], "discovered")
        self.assertEqual(rows[0]["matched_filter"], "")
        self.assertEqual(rows[0]["inferred_quality"], "b")
        self.assertEqual(rows[0]["quality_decision"], "keep")
        self.assertEqual(rows[0]["fixture_status"], "not_fetched")
        self.assertEqual(rows[1]["quality_decision"], "reject")
        self.assertEqual(rows[2]["inferred_quality"], "unknown")
        self.assertEqual(rows[2]["quality_decision"], "reject")
        self.assertEqual(result["summary"]["raw_candidate_count"], 3)
        self.assertEqual(result["summary"]["filtered_candidate_count"], 3)
        self.assertEqual(result["summary"]["explicit_tournament_count"], 0)

    def test_name_filter_matches_cct_south_american_series(self) -> None:
        provider = FakeCoverageProvider(
            [
                {"id": "101", "name": "PGL Major Antwerp 2022"},
                {"id": "51042", "name": "CCT South American Series"},
                {"id": "32873", "name": "CCT Europe Series"},
            ]
        )

        result = run_stake_coverage(
            sport="cs2",
            bookmaker="stake",
            name_contains=["CCT South"],
            provider=provider,
        )

        self.assertEqual(
            [row["tournament_id"] for row in result["candidates"]],
            ["51042"],
        )
        row = result["candidates"][0]
        self.assertEqual(row["source"], "discovered")
        self.assertEqual(row["matched_filter"], "name_contains:CCT South")
        self.assertEqual(result["summary"]["raw_candidate_count"], 3)
        self.assertEqual(result["summary"]["filtered_candidate_count"], 1)
        self.assertEqual(result["summary"]["candidate_count"], 1)

    def test_name_filter_is_case_insensitive(self) -> None:
        provider = FakeCoverageProvider(
            [
                {"id": "101", "name": "PGL Major Antwerp 2022"},
                {"id": "51042", "name": "CCT South American Series"},
            ]
        )

        result = run_stake_coverage(
            sport="cs2",
            bookmaker="stake",
            name_contains=["cct south american"],
            provider=provider,
        )

        self.assertEqual(
            [row["tournament_id"] for row in result["candidates"]],
            ["51042"],
        )
        self.assertEqual(
            result["candidates"][0]["matched_filter"],
            "name_contains:cct south american",
        )

    def test_explicit_tournament_id_is_included_outside_printed_candidates(self) -> None:
        provider = FakeCoverageProvider(
            [
                {"id": "101", "name": "PGL Major Antwerp 2022"},
                {"id": "202", "name": "ESL Archive Cup"},
                {"id": "51042", "name": "CCT South American Series"},
            ]
        )

        result = run_stake_coverage(
            sport="cs2",
            bookmaker="stake",
            max_tournaments=2,
            tournament_id=["51042"],
            provider=provider,
        )

        self.assertEqual(
            [row["tournament_id"] for row in result["candidates"]],
            ["51042"],
        )
        self.assertEqual(result["candidates"][0]["source"], "explicit_id")
        self.assertEqual(
            result["candidates"][0]["matched_filter"],
            "tournament_id:51042",
        )
        self.assertEqual(result["summary"]["raw_candidate_count"], 3)
        self.assertEqual(result["summary"]["filtered_candidate_count"], 3)
        self.assertEqual(result["summary"]["explicit_tournament_count"], 1)

    def test_fetch_fixtures_only_fetches_filtered_candidates(self) -> None:
        provider = FakeCoverageProvider(
            [
                {"id": "101", "name": "PGL Major Antwerp 2022"},
                {"id": "51042", "name": "CCT South American Series"},
                {"id": "32873", "name": "CCT Europe Series"},
            ],
            events_by_tournament_id={
                "101": [_event(tournament="PGL Major Antwerp")],
                "51042": [_event()],
                "32873": [_event(tournament="CCT Europe Series")],
            },
        )

        result = run_stake_coverage(
            sport="cs2",
            bookmaker="stake",
            name_contains=["CCT South"],
            fetch_fixtures=True,
            request_delay_seconds=0,
            provider=provider,
        )

        self.assertEqual(
            [call["tournament_id"] for call in provider.fetch_event_calls],
            ["51042"],
        )
        self.assertEqual(result["candidates"][0]["fixture_status"], "fetched")
        self.assertEqual(result["summary"]["total_fixture_count"], 1)

    def test_default_no_filter_behavior_remains_first_n_candidates(self) -> None:
        provider = FakeCoverageProvider(
            [
                {"id": "101", "name": "PGL Major Antwerp 2022"},
                {"id": "202", "name": "ESL Archive Cup"},
                {"id": "51042", "name": "CCT South American Series"},
            ]
        )

        result = run_stake_coverage(
            sport="cs2",
            bookmaker="stake",
            max_tournaments=2,
            provider=provider,
        )

        self.assertEqual(
            [row["tournament_id"] for row in result["candidates"]],
            ["101", "202"],
        )
        self.assertEqual(
            [row["source"] for row in result["candidates"]],
            ["discovered", "discovered"],
        )
        self.assertEqual(
            [row["matched_filter"] for row in result["candidates"]],
            ["", ""],
        )
        self.assertEqual(result["summary"]["candidate_count"], 2)
        self.assertEqual(result["summary"]["raw_candidate_count"], 3)
        self.assertEqual(result["summary"]["filtered_candidate_count"], 3)

    def test_coverage_fetches_fixtures_and_summarizes_counts(self) -> None:
        provider = FakeCoverageProvider(
            [
                {"id": "51042", "name": "CCT South American Series"},
                {"id": "32873", "name": "CCT Europe Series"},
            ],
            events_by_tournament_id={
                "51042": [
                    _event(),
                    _event(team_a="Sharks", team_b="Dusty Roots"),
                ],
                "32873": [],
            },
        )

        result = run_stake_coverage(
            sport="cs2",
            bookmaker="stake",
            fetch_fixtures=True,
            request_delay_seconds=0,
            provider=provider,
        )

        self.assertEqual(
            [call["tournament_id"] for call in provider.fetch_event_calls],
            ["51042", "32873"],
        )
        rows = result["candidates"]
        self.assertEqual(rows[0]["fixture_status"], "fetched")
        self.assertEqual(rows[0]["fixture_count"], 2)
        self.assertEqual(
            rows[0]["sample_team_pairs"],
            ["Yawara eSports vs Blitzkrieg", "Sharks vs Dusty Roots"],
        )
        self.assertEqual(rows[1]["fixture_status"], "fixture_not_found")
        self.assertEqual(result["summary"]["fetched_tournament_count"], 1)
        self.assertEqual(result["summary"]["fixture_not_found_count"], 1)
        self.assertEqual(result["summary"]["tournaments_with_fixtures_count"], 1)
        self.assertEqual(result["summary"]["total_fixture_count"], 2)

    def test_lol_team_contains_filters_fixture_matches(self) -> None:
        provider = FakeCoverageProvider(
            [
                {"id": "19000", "name": "Prime Pokal"},
                {"id": "19050", "name": "Prime League Candidate"},
            ],
            events_by_tournament_id={
                "19000": [
                    _event(
                        tournament="Prime Pokal",
                        team_a="Eintracht Frankfurt",
                        team_b="Team Orange Gaming",
                        game="lol",
                    )
                ],
                "19050": [
                    _event(
                        tournament="Prime League Candidate",
                        team_a="ROSSMANN Centaurs",
                        team_b="BIG",
                        game="lol",
                    ),
                    _event(
                        tournament="Prime League Candidate",
                        team_a="G2 NORD",
                        team_b="E WIE EINFACH E-SPORTS",
                        game="lol",
                    ),
                ],
            },
        )

        result = run_stake_coverage(
            sport="lol",
            bookmaker="stake",
            name_contains=["Prime", "League"],
            team_contains=["ROSSMANN", "BIG"],
            fetch_fixtures=True,
            request_delay_seconds=0,
            provider=provider,
        )

        rows = result["candidates"]
        self.assertEqual([row["tournament_id"] for row in rows], ["19050"])
        self.assertEqual(rows[0]["fixture_count"], 2)
        self.assertEqual(
            rows[0]["matched_team_pairs"],
            ["ROSSMANN Centaurs vs BIG"],
        )
        self.assertEqual(result["summary"]["team_filter_count"], 2)
        self.assertEqual(result["summary"]["matched_team_candidate_count"], 1)
        self.assertEqual(
            [call["tournament_id"] for call in provider.fetch_event_calls],
            ["19000", "19050"],
        )

    def test_lol_raw_market_audit_redacts_api_key(self) -> None:
        provider = FakeCoverageProvider(
            [{"id": "81001", "name": "Mid-Season Invitational", "category": "LoL"}],
            events_by_tournament_id={"81001": []},
            market_debug_by_tournament_id={
                "81001": {
                    "market_explorer": [
                        {
                            "fixtureId": "fixture-lol-1",
                            "participant1Name": "T1",
                            "participant2Name": "Gen.G",
                            "tournamentId": "81001",
                            "tournamentName": "Mid-Season Invitational",
                            "categoryName": "League of Legends",
                            "startTime": "2026-07-10T16:00:00Z",
                            "bookmaker_name": "stake",
                            "markets": [
                                {
                                    "market_path": "markets.999",
                                    "marketId": "999",
                                    "key": "999",
                                    "marketName": "Series Winner",
                                    "marketActive": True,
                                    "suspended": False,
                                    "outcome_count": 2,
                                    "way_type": "2-way",
                                    "looks_like_match_winner": True,
                                    "outcome_names": ["T1", "Gen.G"],
                                    "decimal_odds": ["1.80", "2.05"],
                                    "outcomes": [
                                        {
                                            "name": "T1",
                                            "price": "1.80",
                                            "exchangeMeta": (
                                                "href=https://example.test?"
                                                "apiKey=secret-key"
                                            ),
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                }
            },
        )

        result = run_stake_coverage(
            sport="lol",
            bookmaker="stake",
            fetch_fixtures=True,
            request_delay_seconds=0,
            provider=provider,
            include_fixture_market_debug=True,
        )
        output = format_coverage_table(result, market_debug=True)
        serialized = json.dumps(result)

        self.assertNotIn("secret-key", serialized)
        self.assertNotIn("secret-key", output)
        self.assertIn("apiKey=***", serialized)
        self.assertIn("marketName=Series Winner", output)
        self.assertIn("way_type=2-way", output)
        self.assertIn("looks_like_match_winner=True", output)

    def test_lol_prime_league_pro_division_quality_alias_keeps_b(
        self,
    ) -> None:
        provider = FakeCoverageProvider(
            [
                {"id": "19000", "name": "Prime Pokal"},
                {"id": "19050", "name": "Prime League Pro Division"},
            ]
        )

        result = run_stake_coverage(
            sport="lol",
            bookmaker="stake",
            name_contains=["Prime"],
            fetch_fixtures=False,
            provider=provider,
        )

        rows = {
            row["tournament_id"]: row
            for row in result["candidates"]
        }
        self.assertEqual(rows["19050"]["inferred_quality"], "b")
        self.assertEqual(rows["19050"]["quality_decision"], "keep")
        self.assertEqual(rows["19000"]["quality_decision"], "reject")
        self.assertEqual(result["summary"]["kept_quality_count"], 1)
        self.assertEqual(result["summary"]["rejected_quality_count"], 1)

    def test_coverage_stops_cleanly_on_persistent_rate_limit(self) -> None:
        provider = FakeCoverageProvider(
            [
                {"id": "51042", "name": "CCT South American Series"},
                {"id": "32873", "name": "CCT Europe Series"},
            ],
            errors_by_tournament_id={
                "51042": [
                    ProviderRequestError(
                        "OddsPapi returned HTTP 429: "
                        '{"code":"RATE_LIMITED","retryAfter":0}'
                    ),
                    ProviderRequestError(
                        "OddsPapi returned HTTP 429: "
                        '{"code":"RATE_LIMITED","retryAfter":0}'
                    ),
                ]
            },
        )
        sleeps: list[float] = []

        result = run_stake_coverage(
            sport="cs2",
            bookmaker="stake",
            fetch_fixtures=True,
            request_delay_seconds=0,
            rate_limit_retries=1,
            provider=provider,
            sleeper=sleeps.append,
        )

        self.assertEqual(
            [call["tournament_id"] for call in provider.fetch_event_calls],
            ["51042", "51042"],
        )
        self.assertEqual(sleeps, [0.25])
        self.assertEqual(result["summary"]["stopped_reason"], "rate_limited")
        self.assertEqual(result["summary"]["rate_limit_count"], 2)
        self.assertEqual(result["candidates"][0]["fixture_status"], "rate_limited")
        self.assertEqual(result["candidates"][1]["fixture_status"], "not_fetched")

    def test_main_json_uses_default_cs2_mapping_profile_flag(self) -> None:
        provider = FakeCoverageProvider(
            [{"id": "51042", "name": "CCT South American Series"}],
            errors_by_tournament_id={
                "51042": ProviderRequestError(
                    "OddsPapi returned HTTP 404 for /v4/odds: "
                    '{"code":"FIXTURE_NOT_FOUND"}'
                )
            },
        )

        with patch(
            "provider_odds.cli.stake_coverage._build_provider",
            return_value=provider,
        ):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                result = stake_coverage_main(
                    [
                        "--fetch-fixtures",
                        "--stake-mapping-profile",
                        "--stake-request-delay-seconds",
                        "0",
                        "--format",
                        "json",
                    ]
                )

        self.assertEqual(result, 0)
        self.assertEqual(
            provider.fetch_event_calls[0]["mapping_profile"],
            MAPPING_PROFILE_ODDSPAPI_STAKE_CS2_MARKET171_SELECTION_ORDER,
        )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["summary"]["fixture_not_found_count"], 1)
        self.assertEqual(
            payload["candidates"][0]["fixture_status"],
            "fixture_not_found",
        )

    def test_table_output_contains_required_columns_and_summary(self) -> None:
        provider = FakeCoverageProvider(
            [{"id": "51042", "name": "CCT South American Series"}],
            events_by_tournament_id={"51042": [_event()]},
        )

        with patch(
            "provider_odds.cli.stake_coverage._build_provider",
            return_value=provider,
        ):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                result = stake_coverage_main(
                    [
                        "--fetch-fixtures",
                        "--stake-request-delay-seconds",
                        "0",
                    ]
                )

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("stake coverage:", output)
        self.assertIn("tournament_id", output)
        self.assertIn("raw_score", output)
        self.assertIn("matched_filter", output)
        self.assertIn("source", output)
        self.assertIn("inferred_quality", output)
        self.assertIn("quality_decision", output)
        self.assertIn("fixture_status", output)
        self.assertIn("sample_team_pairs", output)
        self.assertIn("51042", output)
        self.assertIn("fetched", output)
        self.assertIn("summary:", output)
        self.assertIn("candidate_count=1", output)
        self.assertNotIn("apiKey=", output)


if __name__ == "__main__":
    unittest.main()
