from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from provider_odds.models import ProviderOddsEvent
from provider_odds.cli.probe import (
    format_market_debug,
    format_provider_debug,
    format_tournament_candidates,
    main as probe_main,
)
from provider_odds.providers.base import ProviderRequestError
from provider_odds.providers.oddspapi import (
    ODDSPAPI_API_KEYS_ENV,
    ODDSPAPI_API_KEY_ENV,
    MAPPING_PROFILE_ODDSPAPI_STAKE_CS2_MARKET171_SELECTION_ORDER,
    MAPPING_PROFILE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER,
    MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER,
    MAPPING_SOURCE_ODDSPAPI_STAKE_CS2_MARKET171_SELECTION_ORDER,
    MAPPING_SOURCE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER,
    MAPPING_SOURCE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER,
    OddsPapiAllKeysExhaustedError,
    OddsPapiProvider,
    resolve_sport_id,
)


class RecordingOddsPapiProvider(OddsPapiProvider):
    def __init__(self, responses: list[object]) -> None:
        super().__init__(api_key="test-key")
        self.responses = responses
        self.urls: list[str] = []

    def _get_json(self, url: str) -> object:
        self.urls.append(url)
        if not self.responses:
            raise AssertionError(f"Unexpected request: {url}")
        return self.responses.pop(0)


class FakeHttpResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeHttpResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _http_429(url: str, *, body: str = "quota exceeded") -> HTTPError:
    return HTTPError(
        url,
        429,
        "Too Many Requests",
        hdrs=None,
        fp=io.BytesIO(body.encode("utf-8")),
    )


def _api_key_from_request(request: object) -> str:
    url = getattr(request, "full_url", "")
    return parse_qs(urlsplit(url).query).get("apiKey", [""])[0]


def _stake_dota2_anonymous_market_payload(
    *,
    market_key: str = "161",
    outcome_ids: tuple[str, ...] = ("161", "162"),
    participant_names: bool = True,
    market_active: object = True,
    suspended: object = False,
    market_name: str = "",
    tournament_name: str = "DreamLeague",
) -> list[dict[str, object]]:
    prices = ("1.92", "1.78", "3.50")
    outcomes = {
        outcome_id: {
            "players": {
                "0": {
                    "participantId": 0,
                    "selectionId": 0,
                    "playerName": "",
                    "price": prices[index],
                }
            }
        }
        for index, outcome_id in enumerate(outcome_ids)
    }
    return [
        {
            "fixtureId": "fixture-profile-1",
            "participant1Id": 244737,
            "participant2Id": 287351,
            "sportId": 16,
            "tournamentId": 13911,
            "trueStartTime": "2026-06-28T15:00:00Z",
            "participant1Name": "Power Rangers" if participant_names else "",
            "participant2Name": "Yellow Submarine" if participant_names else "",
            "tournamentName": tournament_name,
            "bookmakerOdds": {
                "stake": {
                    "markets": {
                        market_key: {
                            "marketId": market_key,
                            "key": market_key,
                            "marketName": market_name,
                            "marketActive": market_active,
                            "suspended": suspended,
                            "outcomes": outcomes,
                        }
                    }
                }
            },
        }
    ]


def _stake_cs2_anonymous_market_payload(
    *,
    market_key: str = "171",
    outcome_ids: tuple[str, ...] = ("171", "172"),
    sport_id: object = 17,
    participant_names: bool = True,
    market_active: object = True,
    suspended: object = False,
) -> list[dict[str, object]]:
    prices = ("1.95", "1.75", "3.50")
    outcomes = {
        outcome_id: {
            "players": {
                "0": {
                    "participantId": 0,
                    "selectionId": 0,
                    "playerName": "",
                    "price": prices[index] if index < len(prices) else "3.50",
                }
            }
        }
        for index, outcome_id in enumerate(outcome_ids)
    }
    return [
        {
            "fixtureId": "fixture-cs2-profile-1",
            "participant1Id": 301001,
            "participant2Id": 301002,
            "sportId": sport_id,
            "tournamentId": 43597,
            "trueStartTime": "2026-06-27T21:00:00Z",
            "participant1Name": "Pain Academy" if participant_names else "",
            "participant2Name": "Alka Gaming" if participant_names else "",
            "tournamentName": "CCT South American Series",
            "bookmakerOdds": {
                "stake": {
                    "markets": {
                        market_key: {
                            "marketId": market_key,
                            "key": market_key,
                            "marketActive": market_active,
                            "suspended": suspended,
                            "outcomes": outcomes,
                        }
                    }
                }
            },
        }
    ]


def _stake_lol_market_payload(
    *,
    market_name: str = "Series Winner",
    outcomes: tuple[str, ...] = ("T1", "Gen.G"),
    prices: tuple[str, ...] = ("1.80", "2.05"),
) -> list[dict[str, object]]:
    return [
        {
            "fixtureId": "fixture-lol-series-1",
            "participant1Id": 401001,
            "participant2Id": 401002,
            "sportId": 18,
            "tournamentId": 81001,
            "trueStartTime": "2026-07-10T15:00:00Z",
            "participant1Name": "T1",
            "participant2Name": "Gen.G",
            "tournamentName": "LCK",
            "bookmakerOdds": {
                "stake": {
                    "markets": [
                        {
                            "marketId": "lol-series-winner",
                            "marketName": market_name,
                            "marketActive": True,
                            "suspended": False,
                            "outcomes": [
                                {"name": outcome, "price": price}
                                for outcome, price in zip(outcomes, prices, strict=False)
                            ],
                        }
                    ]
                }
            },
        }
    ]


def _stake_lol_market181_payload(
    *,
    market_key: str = "181",
    outcome_ids: tuple[str, ...] = ("181", "182"),
    prices: tuple[str, ...] = ("2.05", "1.78"),
    sport_id: object = 18,
    participant1_name: str = "G2 Esports",
    participant2_name: str = "TOP Esports",
    participant_names: bool = True,
    market_active: object = True,
    suspended: object = False,
) -> list[dict[str, object]]:
    outcomes = {
        outcome_id: {
            "players": {
                "0": {
                    "participantId": 0,
                    "selectionId": 0,
                    "playerName": "",
                    "price": prices[index] if index < len(prices) else "3.50",
                }
            }
        }
        for index, outcome_id in enumerate(outcome_ids)
    }
    return [
        {
            "fixtureId": "fixture-lol-msi-181",
            "participant1Id": 501001,
            "participant2Id": 501002,
            "sportId": sport_id,
            "tournamentId": 2527,
            "trueStartTime": "2026-07-10T16:00:00Z",
            "participant1Name": participant1_name if participant_names else "",
            "participant2Name": participant2_name if participant_names else "",
            "tournamentName": "Mid-Season Invitational",
            "categoryName": "Riot LoL",
            "bookmakerOdds": {
                "stake": {
                    "markets": {
                        market_key: {
                            "marketId": market_key,
                            "key": market_key,
                            "marketName": "Match Winner",
                            "marketActive": market_active,
                            "suspended": suspended,
                            "outcomes": outcomes,
                        }
                    }
                }
            },
        }
    ]


def _tournament_candidates_payload() -> dict[str, list[dict[str, object]]]:
    return {
        "data": [
            {
                "tournamentId": "101",
                "name": "The International 2026: Europe Closed Qualifier",
                "slug": "ti-2026-europe",
                "category": "Dota 2",
            },
            {
                "tournamentId": "202",
                "name": "Regional Main Event",
                "slug": "europe-closed-qualifier",
                "category": "Dota 2",
            },
            {
                "tournamentId": "303",
                "name": "Unrelated Cup",
                "slug": "unrelated-cup",
                "category": {
                    "name": "Valve Dota2",
                    "slug": "valve-dota2",
                },
            },
        ]
    }


class OddsPapiProviderTest(unittest.TestCase):
    def test_single_key_env_still_uses_legacy_env_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "missing.env"
            with patch.dict(
                os.environ,
                {ODDSPAPI_API_KEY_ENV: "legacy-key"},
                clear=True,
            ):
                provider = OddsPapiProvider.from_env(env_path)

        self.assertEqual(provider.api_keys, ["legacy-key"])
        self.assertEqual(provider.api_key, "legacy-key")
        self.assertEqual(provider.current_key_index, 0)

    def test_api_keys_env_parses_comma_separated_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "missing.env"
            cwd = os.getcwd()
            try:
                os.chdir(tmp)
                with patch.dict(
                    os.environ,
                    {ODDSPAPI_API_KEYS_ENV: "key_0, key_1,,key_2"},
                    clear=True,
                ):
                    provider = OddsPapiProvider.from_env(env_path)
            finally:
                os.chdir(cwd)

        self.assertEqual(provider.api_keys, ["key_0", "key_1", "key_2"])
        self.assertEqual(provider.current_key_index, 0)

    def test_api_keys_env_takes_precedence_over_legacy_single_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "missing.env"
            cwd = os.getcwd()
            try:
                os.chdir(tmp)
                with patch.dict(
                    os.environ,
                    {
                        ODDSPAPI_API_KEYS_ENV: "pooled-key-0,pooled-key-1",
                        ODDSPAPI_API_KEY_ENV: "legacy-key",
                    },
                    clear=True,
                ):
                    provider = OddsPapiProvider.from_env(env_path)
            finally:
                os.chdir(cwd)

        self.assertEqual(provider.api_keys, ["pooled-key-0", "pooled-key-1"])
        self.assertEqual(provider.api_key, "pooled-key-0")

    def test_key_pool_first_request_uses_key_zero_without_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "oddspapi_key_state.json"
            provider = OddsPapiProvider(
                api_keys=["key_0", "key_1"],
                key_state_path=state_path,
            )
            with patch(
                "provider_odds.providers.oddspapi.urlopen",
                return_value=FakeHttpResponse({"data": []}),
            ) as urlopen_mock:
                provider.fetch_tournament_list(sport="cs2")

        self.assertEqual(_api_key_from_request(urlopen_mock.call_args.args[0]), "key_0")
        self.assertEqual(provider.last_debug["oddspapi_key_index"], "key_0")

    def test_saved_current_key_index_is_used_on_next_provider_instance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "oddspapi_key_state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "current_key_index": 1,
                        "last_failover_at": "",
                        "last_error": "",
                        "failover_count": 0,
                    }
                ),
                encoding="utf-8",
            )
            provider = OddsPapiProvider(
                api_keys=["key_0", "key_1"],
                key_state_path=state_path,
            )
            with patch(
                "provider_odds.providers.oddspapi.urlopen",
                return_value=FakeHttpResponse({"data": []}),
            ) as urlopen_mock:
                provider.fetch_tournament_list(sport="cs2")

        self.assertEqual(_api_key_from_request(urlopen_mock.call_args.args[0]), "key_1")
        self.assertEqual(provider.current_key_index, 1)

    def test_rate_limit_on_key_zero_retries_with_key_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "oddspapi_key_state.json"
            provider = OddsPapiProvider(
                api_keys=["key_0", "key_1"],
                key_state_path=state_path,
            )
            seen_keys: list[str] = []

            def fake_urlopen(request: object, timeout: int) -> object:
                key = _api_key_from_request(request)
                seen_keys.append(key)
                if key == "key_0":
                    raise _http_429(getattr(request, "full_url"), body="rate limit")
                return FakeHttpResponse({"data": []})

            with patch("provider_odds.providers.oddspapi.urlopen", side_effect=fake_urlopen):
                provider.fetch_tournament_list(sport="cs2")

            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(seen_keys, ["key_0", "key_1"])
        self.assertEqual(provider.current_key_index, 1)
        self.assertEqual(state["current_key_index"], 1)
        self.assertEqual(provider.last_debug["oddspapi_key_failover_count"], 1)
        self.assertFalse(provider.last_debug["oddspapi_all_keys_exhausted"])

    def test_rate_limit_on_last_key_wraps_to_key_zero_without_exhausted_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "oddspapi_key_state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "current_key_index": 1,
                        "last_failover_at": "",
                        "last_error": "",
                        "failover_count": 0,
                    }
                ),
                encoding="utf-8",
            )
            provider = OddsPapiProvider(
                api_keys=["key_0", "key_1"],
                key_state_path=state_path,
            )
            seen_keys: list[str] = []

            def fake_urlopen(request: object, timeout: int) -> object:
                key = _api_key_from_request(request)
                seen_keys.append(key)
                if key == "key_1":
                    raise _http_429(getattr(request, "full_url"), body="quota exceeded")
                return FakeHttpResponse({"data": []})

            with patch("provider_odds.providers.oddspapi.urlopen", side_effect=fake_urlopen):
                provider.fetch_tournament_list(sport="cs2")

            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(seen_keys, ["key_1", "key_0"])
        self.assertEqual(provider.current_key_index, 0)
        self.assertEqual(state["current_key_index"], 0)
        self.assertFalse(provider.last_debug["oddspapi_all_keys_exhausted"])

    def test_all_keys_rate_limited_raises_all_keys_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "oddspapi_key_state.json"
            provider = OddsPapiProvider(
                api_keys=["secret-key-0", "secret-key-1"],
                key_state_path=state_path,
            )
            seen_keys: list[str] = []

            def fake_urlopen(request: object, timeout: int) -> object:
                key = _api_key_from_request(request)
                seen_keys.append(key)
                raise _http_429(
                    getattr(request, "full_url"),
                    body=f"quota exceeded for {key}",
                )

            with patch("provider_odds.providers.oddspapi.urlopen", side_effect=fake_urlopen):
                with self.assertRaises(OddsPapiAllKeysExhaustedError) as raised:
                    provider.fetch_tournament_list(sport="cs2")

            state_text = state_path.read_text(encoding="utf-8")
            debug_text = json.dumps(provider.last_debug)
            message = str(raised.exception)

        self.assertEqual(seen_keys, ["secret-key-0", "secret-key-1"])
        self.assertIn("all_keys_exhausted", message)
        self.assertIn("oddspapi_all_keys_exhausted=true", message)
        self.assertTrue(provider.last_debug["oddspapi_all_keys_exhausted"])
        self.assertNotIn("secret-key-0", state_text)
        self.assertNotIn("secret-key-1", state_text)
        self.assertNotIn("secret-key-0", debug_text)
        self.assertNotIn("secret-key-1", debug_text)
        self.assertNotIn("secret-key-0", message)
        self.assertNotIn("secret-key-1", message)

    def test_fetch_events_builds_v4_tournaments_url(self) -> None:
        provider = RecordingOddsPapiProvider(responses=[{"data": []}])

        events = provider.fetch_events(sport="esport-dota", bookmaker="stake")

        self.assertEqual(events, [])
        self.assertEqual(
            provider.urls,
            [
                "https://api.oddspapi.io/v4/tournaments?"
                "sportId=16&apiKey=test-key"
            ],
        )
        self.assertEqual(
            provider.last_note,
            "No OddsPapi tournaments found for sportId=16",
        )

    def test_sport_aliases_resolve_dota2_inputs_to_sport_id_16(self) -> None:
        for sport in ("16", 16, "dota2", "dota", "esport-dota"):
            with self.subTest(sport=sport):
                provider = RecordingOddsPapiProvider(responses=[{"data": []}])

                provider.fetch_events(sport=sport, bookmaker="stake")

                self.assertEqual(
                    provider.urls[0],
                    "https://api.oddspapi.io/v4/tournaments?"
                    "sportId=16&apiKey=test-key",
                )
                self.assertEqual(provider.last_debug["resolved_sport_id"], "16")

    def test_resolve_sport_id_aliases_and_numeric_inputs(self) -> None:
        cases = {
            "cs2": "17",
            "counter-strike": "17",
            "counter_strike": "17",
            "counterstrike": "17",
            "esport-counter-strike": "17",
            "17": "17",
            17: "17",
            "dota2": "16",
            "dota": "16",
            "esport-dota": "16",
            "16": "16",
            16: "16",
            "lol": "18",
            "league-of-legends": "18",
            "league_of_legends": "18",
            "leagueoflegends": "18",
            "esport-league-of-legends": "18",
            "18": "18",
            18: "18",
        }

        for sport, expected in cases.items():
            with self.subTest(sport=sport):
                self.assertEqual(resolve_sport_id(sport), expected)

    def test_resolve_sport_id_unknown_alias_raises_clear_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown OddsPapi sport alias"):
            resolve_sport_id("kraken-ipo")

    def test_sport_aliases_resolve_cs2_inputs_to_sport_id_17(self) -> None:
        for sport in (
            "17",
            17,
            "cs2",
            "counter-strike",
            "counter_strike",
            "counterstrike",
            "esport-counter-strike",
        ):
            with self.subTest(sport=sport):
                provider = RecordingOddsPapiProvider(responses=[{"data": []}])

                provider.fetch_events(sport=sport, bookmaker="stake")

                self.assertEqual(
                    provider.urls[0],
                    "https://api.oddspapi.io/v4/tournaments?"
                    "sportId=17&apiKey=test-key",
                )
                self.assertEqual(provider.last_debug["resolved_sport_id"], "17")

    def test_sport_aliases_resolve_lol_inputs_to_sport_id_18(self) -> None:
        for sport in (
            "18",
            18,
            "lol",
            "league-of-legends",
            "league_of_legends",
            "leagueoflegends",
            "esport-league-of-legends",
        ):
            with self.subTest(sport=sport):
                provider = RecordingOddsPapiProvider(responses=[{"data": []}])

                provider.fetch_events(sport=sport, bookmaker="stake")

                self.assertEqual(
                    provider.urls[0],
                    "https://api.oddspapi.io/v4/tournaments?"
                    "sportId=18&apiKey=test-key",
                )
                self.assertEqual(provider.last_debug["resolved_sport_id"], "18")

    def test_lol_series_winner_two_outcome_market_normalizes(self) -> None:
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            _stake_lol_market_payload(),
            market_type="h2h",
            bookmaker="stake",
            sport="lol",
        )

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.game, "18")
        self.assertEqual(event.tournament, "LCK")
        self.assertEqual(event.team_a, "T1")
        self.assertEqual(event.odds_a, 1.8)
        self.assertEqual(event.team_b, "Gen.G")
        self.assertEqual(event.odds_b, 2.05)
        self.assertEqual(event.market_type, "match_winner")

    def test_lol_market_audit_identifies_two_outcome_match_winner_like_market(
        self,
    ) -> None:
        provider = OddsPapiProvider(api_key="offline-test")

        provider.normalize_response(
            _stake_lol_market_payload(),
            market_type="h2h",
            bookmaker="stake",
            sport="lol",
        )

        market = provider.last_debug["market_explorer"][0]["markets"][0]
        self.assertEqual(market["marketId"], "lol-series-winner")
        self.assertEqual(market["marketName"], "Series Winner")
        self.assertEqual(market["way_type"], "2-way")
        self.assertTrue(market["looks_like_match_winner"])
        self.assertEqual(market["outcome_names"], ["T1", "Gen.G"])
        self.assertEqual(market["decimal_odds"], ["1.80", "2.05"])

    def test_lol_match_winner_aliases_normalize(self) -> None:
        for sport in ("lol", "league_of_legends", "league-of-legends"):
            with self.subTest(sport=sport):
                provider = OddsPapiProvider(api_key="offline-test")

                events = provider.normalize_response(
                    _stake_lol_market_payload(market_name="Match Winner"),
                    market_type="h2h",
                    bookmaker="stake",
                    sport=sport,
                )

                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].team_a, "T1")
                self.assertEqual(events[0].team_b, "Gen.G")

    def test_lol_draw_or_threeway_market_rejects(self) -> None:
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            _stake_lol_market_payload(
                market_name="Threeway Winner",
                outcomes=("T1", "Gen.G", "Draw"),
                prices=("1.80", "2.05", "3.50"),
            ),
            market_type="h2h",
            bookmaker="stake",
            sport="lol",
        )

        self.assertEqual(events, [])

    def test_lol_maps_props_handicaps_and_totals_reject(self) -> None:
        for market_name in (
            "Map 1 Winner",
            "Kills Handicap",
            "Total Maps",
            "Round Winner",
        ):
            with self.subTest(market_name=market_name):
                provider = OddsPapiProvider(api_key="offline-test")

                events = provider.normalize_response(
                    _stake_lol_market_payload(market_name=market_name),
                    market_type="h2h",
                    bookmaker="stake",
                    sport="lol",
                )

                self.assertEqual(events, [])

    def test_lol_outrights_and_futures_reject(self) -> None:
        for market_name in (
            "Outright Winner",
            "Tournament Winner",
            "Who will win MSI?",
        ):
            with self.subTest(market_name=market_name):
                provider = OddsPapiProvider(api_key="offline-test")

                events = provider.normalize_response(
                    _stake_lol_market_payload(market_name=market_name),
                    market_type="h2h",
                    bookmaker="stake",
                    sport="lol",
                )

                self.assertEqual(events, [])

    def test_tournament_candidates_use_requested_cs2_sport_id(self) -> None:
        for sport in ("cs2", "17"):
            with self.subTest(sport=sport):
                provider = RecordingOddsPapiProvider(
                    responses=[_tournament_candidates_payload()]
                )

                provider.fetch_tournament_candidates(
                    sport=sport,
                    tournament_keywords=["international"],
                )

                self.assertEqual(
                    provider.urls,
                    [
                        "https://api.oddspapi.io/v4/tournaments?"
                        "sportId=17&apiKey=test-key"
                    ],
                )
                self.assertEqual(provider.last_debug["resolved_sport_id"], "17")

    def test_fetch_events_builds_v4_odds_by_tournaments_url(self) -> None:
        provider = RecordingOddsPapiProvider(
            responses=[
                {
                    "data": [
                        {
                            "tournamentId": "101",
                            "name": "DreamLeague",
                            "slug": "dreamleague",
                            "category": "Dota 2",
                        },
                        {"tournamentId": "102"},
                        {"tournamentId": "103"},
                        {"tournamentId": "104"},
                        {"tournamentId": "105"},
                        {"tournamentId": "106"},
                    ]
                },
                {"data": []},
            ]
        )

        events = provider.fetch_events(sport="esport-dota", bookmaker="stake")

        self.assertEqual(events, [])
        self.assertEqual(
            provider.urls[1],
            "https://api.oddspapi.io/v4/odds-by-tournaments?"
            "tournamentIds=101,102,103,104,105&bookmakers=stake&language=en&"
            "verbosity=3&oddsFormat=decimal&apiKey=test-key",
        )
        self.assertEqual(provider.last_debug["resolved_sport_id"], "16")
        self.assertEqual(provider.last_debug["bookmaker"], "stake")
        self.assertEqual(provider.last_debug["selection_mode"], "fallback_first_n")
        self.assertEqual(
            provider.last_debug["tournaments_url"],
            "https://api.oddspapi.io/v4/tournaments?"
            "sportId=16&apiKey=***",
        )
        self.assertEqual(provider.last_debug["tournaments_count"], 6)
        self.assertEqual(
            provider.last_debug["tournament_samples"][0],
            {
                "id": "101",
                "name": "DreamLeague",
                "slug": "dreamleague",
                "category": "Dota 2",
            },
        )
        self.assertEqual(
            provider.last_debug["selected_tournament_ids"],
            ["101", "102", "103", "104", "105"],
        )
        self.assertEqual(
            provider.last_debug["odds_by_tournaments_url"],
            "https://api.oddspapi.io/v4/odds-by-tournaments?"
            "tournamentIds=101,102,103,104,105&bookmakers=stake&language=en&"
            "verbosity=3&oddsFormat=decimal&apiKey=***",
        )
        self.assertEqual(provider.last_debug["raw_odds_root_type"], "dict")
        self.assertEqual(provider.last_debug["raw_odds_top_level_keys"], ["data"])
        self.assertEqual(provider.last_debug["raw_odds_first_item_keys"], ["data"])
        self.assertEqual(provider.last_debug["normalized_events_count"], 0)
        self.assertNotIn("test-key", json.dumps(provider.last_debug))

    def test_tournament_keyword_filters_matching_tournament_ids(self) -> None:
        provider = RecordingOddsPapiProvider(
            responses=[
                {
                    "data": [
                        {
                            "tournamentId": "101",
                            "name": "Unrelated Cup",
                            "slug": "unrelated-cup",
                            "category": "Dota 2",
                        },
                        {
                            "tournamentId": "202",
                            "name": "DreamLeague Season 25",
                            "slug": "dreamleague-s25",
                            "category": "Dota 2",
                        },
                        {
                            "tournamentId": "303",
                            "name": "Closed Qualifier",
                            "slug": "closed-qualifier",
                            "category": "DreamLeague",
                        },
                    ]
                },
                {"data": []},
            ]
        )

        provider.fetch_events(
            sport="dota2",
            bookmaker="stake",
            tournament_keyword="dream",
        )

        self.assertEqual(
            provider.urls[1],
            "https://api.oddspapi.io/v4/odds-by-tournaments?"
            "tournamentIds=202,303&bookmakers=stake&language=en&"
            "verbosity=3&oddsFormat=decimal&apiKey=test-key",
        )
        self.assertEqual(provider.last_debug["selection_mode"], "tournament_keyword")
        self.assertEqual(provider.last_debug["selected_tournament_ids"], ["202", "303"])
        self.assertEqual(
            [sample["id"] for sample in provider.last_debug["selected_tournament_samples"]],
            ["202", "303"],
        )

    def test_tournament_candidates_filter_by_name(self) -> None:
        provider = RecordingOddsPapiProvider(
            responses=[_tournament_candidates_payload()]
        )

        candidates = provider.fetch_tournament_candidates(
            sport="dota2",
            tournament_keywords=["international"],
        )

        self.assertEqual([candidate["id"] for candidate in candidates], ["101"])
        self.assertEqual(provider.last_debug["request_mode"], "tournament_candidates")
        self.assertEqual(provider.last_debug["resolved_sport_id"], "16")
        self.assertEqual(provider.last_debug["candidate_count"], 1)
        self.assertEqual(provider.last_debug["printed_count"], 1)

    def test_tournament_candidates_filter_by_slug(self) -> None:
        provider = RecordingOddsPapiProvider(
            responses=[_tournament_candidates_payload()]
        )

        candidates = provider.fetch_tournament_candidates(
            sport="16",
            tournament_keywords=["europe-closed-qualifier"],
        )

        self.assertEqual([candidate["id"] for candidate in candidates], ["202"])

    def test_tournament_candidates_filter_by_category(self) -> None:
        provider = RecordingOddsPapiProvider(
            responses=[_tournament_candidates_payload()]
        )

        candidates = provider.fetch_tournament_candidates(
            sport="16",
            tournament_keywords=["valve"],
        )

        self.assertEqual([candidate["id"] for candidate in candidates], ["303"])
        self.assertEqual(candidates[0]["category"], "Valve Dota2")
        self.assertEqual(candidates[0]["categorySlug"], "valve-dota2")

    def test_tournament_candidates_multiple_keywords_match_any(self) -> None:
        provider = RecordingOddsPapiProvider(
            responses=[_tournament_candidates_payload()]
        )

        candidates = provider.fetch_tournament_candidates(
            sport="dota2",
            tournament_keywords=["international, valve"],
        )

        self.assertEqual(
            [candidate["id"] for candidate in candidates],
            ["101", "303"],
        )
        self.assertEqual(
            provider.last_debug["tournament_candidate_keywords"],
            ["international", "valve"],
        )

    def test_show_tournament_candidates_cli_does_not_call_odds_endpoints(self) -> None:
        provider = RecordingOddsPapiProvider(
            responses=[_tournament_candidates_payload()]
        )

        with patch("provider_odds.cli.probe._build_provider", return_value=provider):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                result = probe_main(
                    [
                        "--show-tournament-candidates",
                        "--sport",
                        "dota2",
                        "--tournament-keyword",
                        "international",
                        "--max-candidates",
                        "10",
                    ]
                )

        self.assertEqual(result, 0)
        self.assertEqual(
            provider.urls,
            [
                "https://api.oddspapi.io/v4/tournaments?"
                "sportId=16&apiKey=test-key"
            ],
        )
        self.assertNotIn("odds-by-tournaments", provider.urls[0])
        self.assertNotIn("/v4/odds?", provider.urls[0])
        output = stdout.getvalue()
        self.assertIn("tournament_candidates:", output)
        self.assertIn("request_mode=tournament_candidates", output)
        self.assertIn("sportId=16", output)
        self.assertIn("keywords=international", output)
        self.assertIn("candidate_count=1", output)
        self.assertIn("printed_count=1", output)
        self.assertIn(
            "candidate_1: id=101 name=The International 2026: Europe Closed Qualifier",
            output,
        )
        self.assertNotIn("test-key", output)
        self.assertNotIn("apiKey=", output)

    def test_show_tournament_candidates_cli_uses_requested_sport(self) -> None:
        for sport in ("cs2", "17"):
            with self.subTest(sport=sport):
                provider = RecordingOddsPapiProvider(
                    responses=[_tournament_candidates_payload()]
                )

                with patch("provider_odds.cli.probe._build_provider", return_value=provider):
                    with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                        result = probe_main(
                            [
                                "--show-tournament-candidates",
                                "--sport",
                                sport,
                                "--tournament-keyword",
                                "international",
                                "--debug-provider",
                            ]
                        )

                self.assertEqual(result, 0)
                self.assertEqual(
                    provider.urls,
                    [
                        "https://api.oddspapi.io/v4/tournaments?"
                        "sportId=17&apiKey=test-key"
                    ],
                )
                output = stdout.getvalue()
                self.assertIn(f"requested_sport_input={sport}", output)
                self.assertIn("resolved_sportId=17", output)
                self.assertIn("sportId=17", output)
                self.assertNotIn("sportId=16", output)
                self.assertNotIn("test-key", output)

    def test_probe_cli_accepts_supported_mapping_profiles_without_live_calls(
        self,
    ) -> None:
        for mapping_profile in (
            MAPPING_PROFILE_ODDSPAPI_STAKE_CS2_MARKET171_SELECTION_ORDER,
            MAPPING_PROFILE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER,
            MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER,
        ):
            with self.subTest(mapping_profile=mapping_profile):
                calls: list[dict[str, object]] = []

                class FakeOddsPapiProvider:
                    def fetch_events(self, **kwargs: object) -> list[ProviderOddsEvent]:
                        calls.append(kwargs)
                        return []

                with patch(
                    "provider_odds.cli.probe._build_provider",
                    return_value=FakeOddsPapiProvider(),
                ):
                    with patch("sys.stdout", new_callable=io.StringIO):
                        result = probe_main(
                            [
                                "--provider",
                                "oddspapi",
                                "--sport",
                                "cs2",
                                "--bookmaker",
                                "stake",
                                "--fixture-id",
                                "fixture-1",
                                "--mapping-profile",
                                mapping_profile,
                            ]
                        )

                self.assertEqual(result, 0)
                self.assertEqual(calls[0]["mapping_profile"], mapping_profile)

    def test_tournament_candidates_output_is_compact_and_sanitized(self) -> None:
        provider = OddsPapiProvider(api_key="test-key")
        provider.last_debug = {
            "request_mode": "tournament_candidates",
            "resolved_sport_id": "16",
            "tournament_candidate_keywords": ["international", "closed"],
            "candidate_count": 1,
            "printed_count": 1,
            "tournament_candidates": [
                {
                    "id": "101",
                    "name": "The International 2026",
                    "slug": "the-international-2026",
                    "category": "Dota 2",
                    "categoryName": "Dota 2",
                    "categorySlug": "dota-2",
                }
            ],
        }

        output = format_tournament_candidates(provider)

        self.assertIn("request_mode=tournament_candidates", output)
        self.assertIn("sportId=16", output)
        self.assertIn("keywords=international,closed", output)
        self.assertIn("candidate_count=1", output)
        self.assertIn("printed_count=1", output)
        self.assertIn(
            "candidate_1: id=101 name=The International 2026 "
            "slug=the-international-2026 category=Dota 2 "
            "categoryName=Dota 2 categorySlug=dota-2",
            output,
        )
        self.assertNotIn("test-key", output)

    def test_tournament_id_uses_direct_odds_request(self) -> None:
        provider = RecordingOddsPapiProvider(responses=[{"data": []}])

        provider.fetch_events(
            sport="esport-dota",
            bookmaker="stake",
            tournament_id="555",
        )

        self.assertEqual(
            provider.urls,
            [
                "https://api.oddspapi.io/v4/odds-by-tournaments?"
                "tournamentIds=555&bookmakers=stake&language=en&"
                "verbosity=3&oddsFormat=decimal&apiKey=test-key"
            ],
        )
        self.assertEqual(provider.last_debug["selection_mode"], "tournament_id")
        self.assertEqual(provider.last_debug["selected_tournament_ids"], ["555"])
        self.assertEqual(
            provider.last_debug["selected_tournament_samples"][0]["id"],
            "555",
        )

    def test_fixture_id_uses_v4_odds_url_without_tournament_requests(self) -> None:
        payload = [
            {
                "fixtureId": "fixture-13911-1",
                "participant1Id": 161,
                "participant2Id": 162,
                "sportId": 16,
                "tournamentId": 13911,
                "trueStartTime": "2026-06-28T15:00:00Z",
                "participant1Name": "Team Spirit",
                "participant2Name": "Gaimin Gladiators",
                "tournamentName": "DreamLeague Season 25",
                "bookmakerOdds": {
                    "stake": {
                        "markets": {
                            "161": {
                                "outcomes": {
                                    "161": {
                                        "participantId": 161,
                                        "decimalOdds": "1.74",
                                    },
                                    "162": {
                                        "participantId": 162,
                                        "decimalOdds": "2.08",
                                    },
                                }
                            }
                        }
                    }
                },
            }
        ]
        provider = RecordingOddsPapiProvider(responses=[payload])

        events = provider.fetch_events(
            sport="dota2",
            bookmaker="stake",
            fixture_id="fixture-13911-1",
            verbosity=4,
        )

        self.assertEqual(
            provider.urls,
            [
                "https://api.oddspapi.io/v4/odds?"
                "fixtureId=fixture-13911-1&bookmakers=stake&language=en&"
                "verbosity=4&oddsFormat=decimal&apiKey=test-key"
            ],
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].team_a, "Team Spirit")
        self.assertEqual(events[0].team_b, "Gaimin Gladiators")
        self.assertEqual(events[0].odds_a, 1.74)
        self.assertEqual(events[0].odds_b, 2.08)
        self.assertEqual(provider.last_debug["request_mode"], "fixture_id")
        self.assertEqual(provider.last_debug["selection_mode"], "fixture_id")
        self.assertEqual(provider.last_debug["selected_fixture_id"], "fixture-13911-1")
        self.assertEqual(
            provider.last_debug["odds_url"],
            "https://api.oddspapi.io/v4/odds?"
            "fixtureId=fixture-13911-1&bookmakers=stake&language=en&"
            "verbosity=4&oddsFormat=decimal&apiKey=***",
        )
        self.assertNotIn("tournaments", provider.urls[0])
        self.assertNotIn("odds-by-tournaments", provider.urls[0])
        self.assertIn(
            "fixtureId",
            provider.last_debug["raw_odds_top_level_fixture_keys"],
        )
        self.assertNotIn("test-key", json.dumps(provider.last_debug))

    def test_nested_v4_list_data_events_normalizes_match_winner_event(self) -> None:
        payload = [
            {
                "tournamentId": "202",
                "name": "DreamLeague Season 25",
                "data": [
                    {
                        "events": [
                            {
                                "id": "evt-1",
                                "sportId": "16",
                                "startTime": "2026-06-28T15:00:00Z",
                                "teams": [
                                    {"name": "Team Spirit"},
                                    {"name": "Gaimin Gladiators"},
                                ],
                                "eventUrl": "https://example.test/events/evt-1",
                                "bookmakers": [
                                    {
                                        "key": "stake",
                                        "markets": [
                                            {
                                                "marketType": "matchWinner",
                                                "outcomes": [
                                                    {
                                                        "name": "Team Spirit",
                                                        "odds": "1.74",
                                                    },
                                                    {
                                                        "name": "Gaimin Gladiators",
                                                        "odds": "2.08",
                                                    },
                                                ],
                                            }
                                        ],
                                    }
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            payload,
            market_type="h2h",
            bookmaker="stake",
            sport="16",
        )

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.provider, "oddspapi")
        self.assertEqual(event.bookmaker, "stake")
        self.assertEqual(event.game, "16")
        self.assertEqual(event.tournament, "DreamLeague Season 25")
        self.assertEqual(event.team_a, "Team Spirit")
        self.assertEqual(event.odds_a, 1.74)
        self.assertEqual(event.team_b, "Gaimin Gladiators")
        self.assertEqual(event.odds_b, 2.08)
        self.assertEqual(event.market_type, "match_winner")
        self.assertEqual(
            event.start_time,
            datetime(2026, 6, 28, 15, 0, tzinfo=timezone.utc),
        )

    def test_direct_v4_fixture_bookmaker_odds_normalizes_market_161(self) -> None:
        payload = [
            {
                "fixtureId": "fixture-13911-1",
                "participant1Id": 161,
                "participant2Id": 162,
                "sportId": 16,
                "tournamentId": 13911,
                "statusId": 1,
                "hasOdds": True,
                "startTime": "2026-06-28T14:30:00Z",
                "trueStartTime": "2026-06-28T15:00:00Z",
                "participant1Name": "Team Spirit",
                "participant2Name": "Gaimin Gladiators",
                "participant1ShortName": "Spirit",
                "participant2ShortName": "GG",
                "tournamentName": "DreamLeague Season 25",
                "bookmakerOdds": {
                    "stake": {
                        "markets": {
                            "161": {
                                "outcomes": {
                                    "161": {
                                        "participantId": 161,
                                        "name": "",
                                        "decimalOdds": "1.74",
                                    },
                                    "162": {
                                        "participantId": 162,
                                        "name": "",
                                        "decimalOdds": 2.08,
                                    },
                                }
                            }
                        }
                    }
                },
            }
        ]
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            payload,
            market_type="h2h",
            bookmaker="stake",
            sport="dota2",
        )

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.provider, "oddspapi")
        self.assertEqual(event.bookmaker, "stake")
        self.assertEqual(event.game, "16")
        self.assertEqual(event.tournament, "DreamLeague Season 25")
        self.assertEqual(event.team_a, "Team Spirit")
        self.assertEqual(event.team_b, "Gaimin Gladiators")
        self.assertEqual(event.market_type, "match_winner")
        self.assertEqual(event.odds_a, 1.74)
        self.assertEqual(event.odds_b, 2.08)
        self.assertEqual(
            event.start_time,
            datetime(2026, 6, 28, 15, 0, tzinfo=timezone.utc),
        )
        self.assertIsNone(event.event_url)
        self.assertEqual(event.raw["event"]["fixtureId"], "fixture-13911-1")
        self.assertEqual(event.raw["market"]["marketId"], "161")
        self.assertNotIn("apiKey", json.dumps(event.raw))

    def test_direct_v4_fixture_nested_outcome_players_normalizes_market_161(
        self,
    ) -> None:
        payload = [
            {
                "fixtureId": "fixture-13911-players",
                "participant1Id": 161,
                "participant2Id": 162,
                "sportId": 16,
                "tournamentId": 13911,
                "trueStartTime": "2026-06-28T15:00:00Z",
                "participant1Name": "Team Spirit",
                "participant2Name": "Gaimin Gladiators",
                "tournamentName": "DreamLeague Season 25",
                "bookmakerOdds": {
                    "stake": {
                        "bookmakerFixtureId": "stake-fixture-1",
                        "markets": {
                            "161": {
                                "bookmakerMarketId": "stake-market-161",
                                "marketActive": True,
                                "outcomes": {
                                    "home": {
                                        "selectionId": 161,
                                        "players": [
                                            {
                                                "participantId": 161,
                                                "participantName": "",
                                                "decimalOdds": "1.74",
                                            }
                                        ],
                                    },
                                    "away": {
                                        "selectionId": 162,
                                        "players": {
                                            "162": {
                                                "playerName": "",
                                                "decimalOdds": 2.08,
                                            }
                                        },
                                    },
                                },
                            }
                        },
                    }
                },
            }
        ]
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            payload,
            market_type="h2h",
            bookmaker="stake",
            sport="dota2",
        )

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.provider, "oddspapi")
        self.assertEqual(event.bookmaker, "stake")
        self.assertEqual(event.team_a, "Team Spirit")
        self.assertEqual(event.team_b, "Gaimin Gladiators")
        self.assertEqual(event.odds_a, 1.74)
        self.assertEqual(event.odds_b, 2.08)
        self.assertEqual(event.market_type, "match_winner")
        self.assertEqual(event.tournament, "DreamLeague Season 25")

    def test_direct_v4_fixture_nested_players_maps_by_name_when_ids_do_not_match(
        self,
    ) -> None:
        payload = [
            {
                "fixtureId": "fixture-name-match",
                "participant1Id": 5001,
                "participant2Id": 5002,
                "sportId": 16,
                "tournamentId": 13911,
                "trueStartTime": "2026-06-28T15:00:00Z",
                "participant1Name": "Team Spirit",
                "participant1ShortName": "Spirit",
                "participant2Name": "Gaimin Gladiators",
                "participant2ShortName": "GG",
                "tournamentName": "DreamLeague Season 25",
                "bookmakerOdds": {
                    "stake": {
                        "markets": {
                            "161": {
                                "outcomes": {
                                    "home": {
                                        "selectionId": 9001,
                                        "players": [
                                            {
                                                "participantId": 991,
                                                "selectionId": 901,
                                                "playerName": "Team.Spirit",
                                                "price": "1.74",
                                            }
                                        ],
                                    },
                                    "away": {
                                        "selectionId": 9002,
                                        "players": {
                                            "selection-away": {
                                                "playerId": 992,
                                                "playerName": "Gaimin-Gladiators",
                                                "price": 2.08,
                                            }
                                        },
                                    },
                                },
                            }
                        }
                    }
                },
            }
        ]
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            payload,
            market_type="h2h",
            bookmaker="stake",
            sport="dota2",
        )

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.provider, "oddspapi")
        self.assertEqual(event.bookmaker, "stake")
        self.assertEqual(event.team_a, "Team Spirit")
        self.assertEqual(event.team_b, "Gaimin Gladiators")
        self.assertEqual(event.odds_a, 1.74)
        self.assertEqual(event.odds_b, 2.08)
        self.assertEqual(event.market_type, "match_winner")

    def test_direct_v4_fixture_nested_players_maps_by_preserved_dict_keys(self) -> None:
        payload = [
            {
                "fixtureId": "fixture-key-match",
                "participant1Id": 5001,
                "participant2Id": 5002,
                "sportId": 16,
                "participant1Name": "Team Spirit",
                "participant2Name": "Gaimin Gladiators",
                "bookmakerOdds": {
                    "stake": {
                        "markets": {
                            "161": {
                                "outcomes": {
                                    "5001": {
                                        "selectionId": "0",
                                        "players": {
                                            "0": {
                                                "participantId": 0,
                                                "selectionId": 0,
                                                "playerName": "",
                                                "price": "1.74",
                                            }
                                        },
                                    },
                                    "5002": {
                                        "selectionId": "0",
                                        "players": {
                                            "0": {
                                                "participantId": 0,
                                                "selectionId": 0,
                                                "playerName": "",
                                                "price": "2.08",
                                            }
                                        },
                                    },
                                },
                            }
                        }
                    }
                },
            }
        ]
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            payload,
            market_type="h2h",
            bookmaker="stake",
            sport="dota2",
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].team_a, "Team Spirit")
        self.assertEqual(events[0].team_b, "Gaimin Gladiators")
        self.assertEqual(events[0].odds_a, 1.74)
        self.assertEqual(events[0].odds_b, 2.08)

    def test_direct_v4_fixture_nested_players_maps_by_exchange_meta_or_label(
        self,
    ) -> None:
        payload = [
            {
                "fixtureId": "fixture-meta-match",
                "participant1Id": 5001,
                "participant2Id": 5002,
                "sportId": 16,
                "participant1Name": "Team Spirit",
                "participant2Name": "Gaimin Gladiators",
                "bookmakerOdds": {
                    "stake": {
                        "markets": {
                            "161": {
                                "outcomes": {
                                    "home": {
                                        "selectionId": "0",
                                        "players": {
                                            "0": {
                                                "participantId": 0,
                                                "selectionId": 0,
                                                "playerName": "",
                                                "price": "1.74",
                                                "exchangeMeta": {
                                                    "selectionName": "Team Spirit",
                                                    "huge": {"ignored": "nested"},
                                                },
                                            }
                                        },
                                    },
                                    "away": {
                                        "selectionId": "0",
                                        "label": "Gaimin Gladiators",
                                        "players": {
                                            "0": {
                                                "participantId": 0,
                                                "selectionId": 0,
                                                "playerName": "",
                                                "price": "2.08",
                                            }
                                        },
                                    },
                                },
                            }
                        }
                    }
                },
            }
        ]
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            payload,
            market_type="h2h",
            bookmaker="stake",
            sport="dota2",
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].team_a, "Team Spirit")
        self.assertEqual(events[0].team_b, "Gaimin Gladiators")
        self.assertEqual(events[0].odds_a, 1.74)
        self.assertEqual(events[0].odds_b, 2.08)

    def test_anonymous_market_161_rejects_without_mapping_profile(self) -> None:
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            _stake_dota2_anonymous_market_payload(),
            market_type="h2h",
            bookmaker="stake",
            sport="dota2",
        )

        self.assertEqual(events, [])

    def test_mapping_profile_normalizes_market_161_selection_161_162(self) -> None:
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            _stake_dota2_anonymous_market_payload(),
            market_type="h2h",
            bookmaker="stake",
            sport="dota2",
            mapping_profile=(
                MAPPING_PROFILE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER
            ),
        )

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.provider, "oddspapi")
        self.assertEqual(event.bookmaker, "stake")
        self.assertEqual(event.team_a, "Power Rangers")
        self.assertEqual(event.odds_a, 1.92)
        self.assertEqual(event.team_b, "Yellow Submarine")
        self.assertEqual(event.odds_b, 1.78)
        self.assertEqual(event.market_type, "h2h")
        self.assertEqual(
            event.raw["mapping_source"],
            MAPPING_SOURCE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER,
        )
        self.assertEqual(
            provider.last_debug["mapping_profile"],
            MAPPING_PROFILE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER,
        )
        self.assertEqual(
            provider.last_debug["mapping_sources"],
            [MAPPING_SOURCE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER],
        )

        debug_output = format_provider_debug(
            argparse.Namespace(provider="oddspapi", sport="dota2", bookmaker="stake"),
            provider,
        )
        self.assertIn(
            "mapping_sources="
            f"{MAPPING_SOURCE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER}",
            debug_output,
        )

    def test_dota2_mapping_profile_accepts_group_a_twoway_market(self) -> None:
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            _stake_dota2_anonymous_market_payload(
                market_name="Match Winner",
                tournament_name="European Pro League Group A",
            ),
            market_type="h2h",
            bookmaker="stake",
            sport="dota2",
            mapping_profile=(
                MAPPING_PROFILE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER
            ),
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].team_a, "Power Rangers")
        self.assertEqual(events[0].team_b, "Yellow Submarine")
        self.assertEqual(
            events[0].raw["mapping_source"],
            MAPPING_SOURCE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER,
        )
        self.assertEqual(
            provider.last_debug["mapping_profile_rejection_reasons"],
            {},
        )

    def test_cs2_mapping_profile_normalizes_market_171_selection_171_172(self) -> None:
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            _stake_cs2_anonymous_market_payload(),
            market_type="h2h",
            bookmaker="stake",
            sport="cs2",
            mapping_profile=MAPPING_PROFILE_ODDSPAPI_STAKE_CS2_MARKET171_SELECTION_ORDER,
        )

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.provider, "oddspapi")
        self.assertEqual(event.bookmaker, "stake")
        self.assertEqual(event.team_a, "Pain Academy")
        self.assertEqual(event.odds_a, 1.95)
        self.assertEqual(event.team_b, "Alka Gaming")
        self.assertEqual(event.odds_b, 1.75)
        self.assertEqual(event.market_type, "h2h")
        self.assertEqual(
            event.raw["mapping_source"],
            MAPPING_SOURCE_ODDSPAPI_STAKE_CS2_MARKET171_SELECTION_ORDER,
        )
        self.assertEqual(
            provider.last_debug["mapping_profile"],
            MAPPING_PROFILE_ODDSPAPI_STAKE_CS2_MARKET171_SELECTION_ORDER,
        )
        self.assertEqual(
            provider.last_debug["mapping_sources"],
            [MAPPING_SOURCE_ODDSPAPI_STAKE_CS2_MARKET171_SELECTION_ORDER],
        )

    def test_cs2_mapping_profile_accepts_numeric_sport_id_17(self) -> None:
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            _stake_cs2_anonymous_market_payload(),
            market_type="h2h",
            bookmaker="stake",
            sport="17",
            mapping_profile=MAPPING_PROFILE_ODDSPAPI_STAKE_CS2_MARKET171_SELECTION_ORDER,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].team_a, "Pain Academy")
        self.assertEqual(events[0].team_b, "Alka Gaming")

    def test_lol_mapping_profile_normalizes_g2_top_market_181(
        self,
    ) -> None:
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            _stake_lol_market181_payload(
                participant1_name="G2 Esports",
                participant2_name="TOP Esports",
                prices=("2.05", "1.78"),
            ),
            market_type="h2h",
            bookmaker="stake",
            sport="lol",
            mapping_profile=MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER,
        )

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.provider, "oddspapi")
        self.assertEqual(event.bookmaker, "stake")
        self.assertEqual(event.game, "18")
        self.assertEqual(event.tournament, "Mid-Season Invitational")
        self.assertEqual(event.team_a, "G2 Esports")
        self.assertEqual(event.odds_a, 2.05)
        self.assertEqual(event.team_b, "TOP Esports")
        self.assertEqual(event.odds_b, 1.78)
        self.assertEqual(event.market_type, "h2h")
        self.assertEqual(
            event.raw["mapping_source"],
            MAPPING_SOURCE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER,
        )
        self.assertEqual(
            provider.last_debug["mapping_profile"],
            MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER,
        )
        self.assertEqual(
            provider.last_debug["mapping_sources"],
            [MAPPING_SOURCE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER],
        )
        self.assertEqual(
            provider.last_debug["mapping_profile_rejection_reasons"],
            {},
        )

    def test_lol_mapping_profile_normalizes_lyon_furia_market_181(self) -> None:
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            _stake_lol_market181_payload(
                participant1_name="LYON",
                participant2_name="FURIA Esports",
                prices=("1.40", "3.00"),
            ),
            market_type="h2h",
            bookmaker="stake",
            sport="lol",
            mapping_profile=MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].team_a, "LYON")
        self.assertEqual(events[0].odds_a, 1.40)
        self.assertEqual(events[0].team_b, "FURIA Esports")
        self.assertEqual(events[0].odds_b, 3.00)

    def test_lol_mapping_profile_normalizes_bilibili_t1_market_181(self) -> None:
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            _stake_lol_market181_payload(
                participant1_name="Bilibili Gaming",
                participant2_name="T1 Esports",
                prices=("2.05", "1.78"),
            ),
            market_type="h2h",
            bookmaker="stake",
            sport="lol",
            mapping_profile=MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].team_a, "Bilibili Gaming")
        self.assertEqual(events[0].odds_a, 2.05)
        self.assertEqual(events[0].team_b, "T1 Esports")
        self.assertEqual(events[0].odds_b, 1.78)

    def test_lol_mapping_profile_does_not_map_hanwha_to_underdog_odds(
        self,
    ) -> None:
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            _stake_lol_market181_payload(
                participant1_name="Hanwha Life Esports",
                participant2_name="Team Secret Whales",
                prices=("1.04", "8.00"),
            ),
            market_type="h2h",
            bookmaker="stake",
            sport="lol",
            mapping_profile=MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].team_a, "Hanwha Life Esports")
        self.assertEqual(events[0].odds_a, 1.04)
        self.assertEqual(events[0].team_b, "Team Secret Whales")
        self.assertEqual(events[0].odds_b, 8.00)

    def test_lol_mapping_profile_accepts_league_of_legends_alias(self) -> None:
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            _stake_lol_market181_payload(),
            market_type="h2h",
            bookmaker="stake",
            sport="league_of_legends",
            mapping_profile=MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].team_a, "G2 Esports")
        self.assertEqual(events[0].team_b, "TOP Esports")

    def test_lol_mapping_profile_rejects_non_181_market(self) -> None:
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            _stake_lol_market181_payload(
                market_key="182",
                outcome_ids=("182", "183"),
            ),
            market_type="h2h",
            bookmaker="stake",
            sport="lol",
            mapping_profile=MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER,
        )

        self.assertEqual(events, [])
        self.assertEqual(
            provider.last_debug["mapping_profile_rejection_reasons"],
            {"profile_market_id_mismatch": 1},
        )

    def test_lol_mapping_profile_rejects_market_187_as_not_match_winner(
        self,
    ) -> None:
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            _stake_lol_market181_payload(
                market_key="187",
                outcome_ids=("188", "187"),
                participant1_name="Hanwha Life Esports",
                participant2_name="Team Secret Whales",
                prices=("1.04", "8.00"),
            ),
            market_type="h2h",
            bookmaker="stake",
            sport="lol",
            mapping_profile=MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER,
        )

        self.assertEqual(events, [])
        self.assertEqual(
            provider.last_debug["mapping_profile_rejection_reasons"],
            {"profile_lol_market187_not_match_winner": 1},
        )

    def test_lol_mapping_profile_rejects_three_way_market(self) -> None:
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            _stake_lol_market181_payload(outcome_ids=("181", "182", "183")),
            market_type="h2h",
            bookmaker="stake",
            sport="lol",
            mapping_profile=MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER,
        )

        self.assertEqual(events, [])
        self.assertEqual(
            provider.last_debug["mapping_profile_rejection_reasons"],
            {"profile_outcome_count_not_2": 1},
        )

    def test_lol_mapping_profile_rejects_ambiguous_outcome_mapping(self) -> None:
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            _stake_lol_market181_payload(outcome_ids=("181", "183")),
            market_type="h2h",
            bookmaker="stake",
            sport="lol",
            mapping_profile=MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER,
        )

        self.assertEqual(events, [])
        self.assertEqual(
            provider.last_debug["mapping_profile_rejection_reasons"],
            {"profile_outcome_mapping_ambiguous": 1},
        )

    def test_lol_mapping_profile_rejects_wrong_sport(self) -> None:
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            _stake_lol_market181_payload(sport_id=17),
            market_type="h2h",
            bookmaker="stake",
            sport="cs2",
            mapping_profile=MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER,
        )

        self.assertEqual(events, [])
        self.assertEqual(
            provider.last_debug["mapping_profile_rejection_reasons"],
            {"profile_sport_mismatch": 1},
        )

    def test_lol_mapping_profile_rejects_inactive_market(self) -> None:
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            _stake_lol_market181_payload(market_active=False),
            market_type="h2h",
            bookmaker="stake",
            sport="lol",
            mapping_profile=MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER,
        )

        self.assertEqual(events, [])
        self.assertEqual(
            provider.last_debug["mapping_profile_rejection_reasons"],
            {"profile_market_inactive": 1},
        )

    def test_cs2_market_171_rejects_without_mapping_profile(self) -> None:
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            _stake_cs2_anonymous_market_payload(),
            market_type="h2h",
            bookmaker="stake",
            sport="cs2",
        )

        self.assertEqual(events, [])

    def test_cs2_mapping_profile_rejects_non_171_market(self) -> None:
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            _stake_cs2_anonymous_market_payload(
                market_key="1721",
                outcome_ids=("1721", "1722"),
            ),
            market_type="h2h",
            bookmaker="stake",
            sport="cs2",
            mapping_profile=MAPPING_PROFILE_ODDSPAPI_STAKE_CS2_MARKET171_SELECTION_ORDER,
        )

        self.assertEqual(events, [])

    def test_cs2_mapping_profile_rejects_wrong_sport(self) -> None:
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            _stake_cs2_anonymous_market_payload(sport_id=16),
            market_type="h2h",
            bookmaker="stake",
            sport="dota2",
            mapping_profile=MAPPING_PROFILE_ODDSPAPI_STAKE_CS2_MARKET171_SELECTION_ORDER,
        )

        self.assertEqual(events, [])

    def test_mapping_profile_rejects_market_1649_selection_1649_1650(self) -> None:
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            _stake_dota2_anonymous_market_payload(
                market_key="1649",
                outcome_ids=("1649", "1650"),
            ),
            market_type="h2h",
            bookmaker="stake",
            sport="dota2",
            mapping_profile=(
                MAPPING_PROFILE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER
            ),
        )

        self.assertEqual(events, [])
        self.assertEqual(
            provider.last_debug["mapping_profile_rejection_reasons"],
            {"profile_market_id_mismatch": 1},
        )

    def test_mapping_profile_rejects_missing_participant_names(self) -> None:
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            _stake_dota2_anonymous_market_payload(participant_names=False),
            market_type="h2h",
            bookmaker="stake",
            sport="dota2",
            mapping_profile=(
                MAPPING_PROFILE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER
            ),
        )

        self.assertEqual(events, [])

    def test_mapping_profile_rejects_wrong_outcome_ids(self) -> None:
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            _stake_dota2_anonymous_market_payload(outcome_ids=("161", "163")),
            market_type="h2h",
            bookmaker="stake",
            sport="dota2",
            mapping_profile=(
                MAPPING_PROFILE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER
            ),
        )

        self.assertEqual(events, [])
        self.assertEqual(
            provider.last_debug["mapping_profile_rejection_reasons"],
            {"profile_outcome_mapping_ambiguous": 1},
        )

    def test_mapping_profile_rejects_suspended_or_inactive_market(self) -> None:
        cases = (
            (False, False, "profile_market_inactive"),
            (True, True, "profile_market_suspended"),
        )
        for market_active, suspended, reason in cases:
            with self.subTest(market_active=market_active, suspended=suspended):
                provider = OddsPapiProvider(api_key="offline-test")

                events = provider.normalize_response(
                    _stake_dota2_anonymous_market_payload(
                        market_active=market_active,
                        suspended=suspended,
                    ),
                    market_type="h2h",
                    bookmaker="stake",
                    sport="dota2",
                    mapping_profile=(
                        MAPPING_PROFILE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER
                    ),
                )

                self.assertEqual(events, [])
                self.assertEqual(
                    provider.last_debug["mapping_profile_rejection_reasons"],
                    {reason: 1},
                )

    def test_dota2_mapping_profile_rejects_three_outcome_draw_market(self) -> None:
        cases = (
            (
                "Three-way Winner",
                "profile_dota2_draw_or_three_way",
            ),
            (
                "Match Winner",
                "profile_outcome_count_not_2",
            ),
        )

        for market_name, reason in cases:
            with self.subTest(market_name=market_name):
                provider = OddsPapiProvider(api_key="offline-test")

                events = provider.normalize_response(
                    _stake_dota2_anonymous_market_payload(
                        market_name=market_name,
                        outcome_ids=("161", "162", "draw"),
                    ),
                    market_type="h2h",
                    bookmaker="stake",
                    sport="dota2",
                    mapping_profile=(
                        MAPPING_PROFILE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER
                    ),
                )

                self.assertEqual(events, [])
                self.assertEqual(
                    provider.last_debug["mapping_profile_rejection_reasons"],
                    {reason: 1},
                )

    def test_dota2_mapping_profile_rejects_unsafe_market_labels(self) -> None:
        cases = (
            ("Tie Winner", "DreamLeague", "profile_dota2_draw_or_three_way"),
            ("Game 1 Winner", "DreamLeague", "profile_dota2_map_or_game_winner"),
            ("Game 2 Winner", "DreamLeague", "profile_dota2_map_or_game_winner"),
            ("Exact Maps", "DreamLeague", "profile_dota2_map_or_game_winner"),
            (
                "Best of 2 Winner",
                "DreamLeague",
                "profile_dota2_bo2_or_ambiguous_format",
            ),
            (
                "B02 Winner",
                "DreamLeague",
                "profile_dota2_bo2_or_ambiguous_format",
            ),
            (
                "Match Winner",
                "Esports World Cup",
                "profile_dota2_ewc_or_draw_risk_tournament",
            ),
            ("Kills Handicap", "DreamLeague", "profile_dota2_prop_market"),
            ("Total Kills", "DreamLeague", "profile_dota2_prop_market"),
            ("Player Props", "DreamLeague", "profile_dota2_prop_market"),
            ("Hero Props", "DreamLeague", "profile_dota2_prop_market"),
            ("Outright Winner", "DreamLeague", "profile_dota2_outright_market"),
            ("Tournament Winner", "DreamLeague", "profile_dota2_outright_market"),
            ("Region Winner", "DreamLeague", "profile_dota2_outright_market"),
            ("Champion", "DreamLeague", "profile_dota2_outright_market"),
        )

        for market_name, tournament_name, reason in cases:
            with self.subTest(market_name=market_name):
                provider = OddsPapiProvider(api_key="offline-test")

                events = provider.normalize_response(
                    _stake_dota2_anonymous_market_payload(
                        market_name=market_name,
                        tournament_name=tournament_name,
                    ),
                    market_type="h2h",
                    bookmaker="stake",
                    sport="dota2",
                    mapping_profile=(
                        MAPPING_PROFILE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER
                    ),
                )

                self.assertEqual(events, [])
                self.assertEqual(
                    provider.last_debug["mapping_profile_rejection_reasons"],
                    {reason: 1},
                )

    def test_direct_v4_fixture_rejects_blank_zero_identity_candidates(self) -> None:
        payload = [
            {
                "fixtureId": "fixture-zero-only",
                "participant1Id": 5001,
                "participant2Id": 5002,
                "sportId": 16,
                "participant1Name": "Team Spirit",
                "participant2Name": "Gaimin Gladiators",
                "bookmakerOdds": {
                    "stake": {
                        "markets": {
                            "161": {
                                "outcomes": {
                                    "home": {
                                        "selectionId": "0",
                                        "players": {
                                            "0": {
                                                "participantId": 0,
                                                "selectionId": 0,
                                                "playerName": "",
                                                "price": "1.74",
                                            }
                                        },
                                    },
                                    "away": {
                                        "selectionId": "0",
                                        "players": {
                                            "0": {
                                                "participantId": 0,
                                                "selectionId": 0,
                                                "playerName": "",
                                                "price": "2.08",
                                            }
                                        },
                                    },
                                },
                            }
                        }
                    }
                },
            }
        ]
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            payload,
            market_type="h2h",
            bookmaker="stake",
            sport="dota2",
        )

        self.assertEqual(events, [])

    def test_direct_v4_fixture_rejects_when_candidates_map_to_same_team(self) -> None:
        payload = [
            {
                "fixtureId": "fixture-same-team",
                "participant1Id": 5001,
                "participant2Id": 5002,
                "sportId": 16,
                "participant1Name": "Team Spirit",
                "participant1ShortName": "Spirit",
                "participant2Name": "Gaimin Gladiators",
                "participant2ShortName": "GG",
                "bookmakerOdds": {
                    "stake": {
                        "markets": {
                            "161": {
                                "outcomes": {
                                    "home": {
                                        "selectionId": 9001,
                                        "players": [
                                            {
                                                "playerName": "Team Spirit",
                                                "price": "1.74",
                                            }
                                        ],
                                    },
                                    "away": {
                                        "selectionId": 9002,
                                        "players": [
                                            {
                                                "playerName": "Spirit",
                                                "price": "2.08",
                                            }
                                        ],
                                    },
                                },
                            }
                        }
                    }
                },
            }
        ]
        provider = OddsPapiProvider(api_key="offline-test")

        events = provider.normalize_response(
            payload,
            market_type="h2h",
            bookmaker="stake",
            sport="dota2",
        )

        self.assertEqual(events, [])

    def test_http_error_diagnostic_redacts_url_and_body_preview(self) -> None:
        provider = OddsPapiProvider(api_key="test-key")
        url = (
            "https://api.oddspapi.io/v4/tournaments?"
            "sportId=esport-dota&apiKey=test-key"
        )
        body = (
            b'{"error":"bad request","url":"'
            + url.encode("utf-8")
            + b'","detail":"'
            + (b"x" * 600)
            + b'"}'
        )
        error = HTTPError(url, 400, "Bad Request", hdrs=None, fp=io.BytesIO(body))

        with patch("provider_odds.providers.oddspapi.urlopen", side_effect=error):
            with self.assertRaises(ProviderRequestError) as raised:
                provider._get_json(url)

        message = str(raised.exception)
        self.assertIn(
            "OddsPapi returned HTTP 400 for "
            "https://api.oddspapi.io/v4/tournaments?"
            "sportId=esport-dota&apiKey=***: ",
            message,
        )
        self.assertIn('"error":"bad request"', message)
        self.assertNotIn("test-key", message)
        self.assertLessEqual(
            len(message.rsplit(": ", maxsplit=1)[1]),
            500,
        )

    def test_provider_debug_formatter_outputs_sanitized_snapshot(self) -> None:
        provider = OddsPapiProvider(api_key="test-key")
        provider.last_debug = {
            "resolved_sport_id": "16",
            "bookmaker": "stake",
            "selection_mode": "tournament_keyword",
            "tournaments_url": (
                "https://api.oddspapi.io/v4/tournaments?sportId=16&apiKey=***"
            ),
            "tournaments_count": 1,
            "tournament_samples": [
                {
                    "id": "101",
                    "name": "DreamLeague",
                    "slug": "dreamleague",
                    "category": "Dota 2",
                }
            ],
            "selected_tournament_samples": [
                {
                    "id": "101",
                    "name": "DreamLeague",
                    "slug": "dreamleague",
                    "category": "Dota 2",
                }
            ],
            "selected_tournament_ids": ["101"],
            "odds_by_tournaments_url": (
                "https://api.oddspapi.io/v4/odds-by-tournaments?"
                "tournamentIds=101&bookmakers=stake&language=en&verbosity=3&"
                "oddsFormat=decimal&apiKey=***"
            ),
            "raw_odds_root_type": "dict",
            "raw_odds_top_level_keys": ["data"],
            "raw_odds_first_item_keys": ["data"],
            "raw_odds_zero_sample": {
                "first_item_keys": ["data"],
                "nested_data_count": 1,
                "first_nested_event_keys": ["teams", "bookmakers"],
                "bookmaker_odds_type": "dict",
                "bookmaker_odds_keys": ["stake"],
                "matched_bookmaker_path": "bookmakerOdds.stake",
                "matched_bookmaker_name": "stake",
                "bookmaker_keys": ["key", "markets"],
                "market_container_type": "dict",
                "market_keys": ["marketType", "outcomes"],
                "first_outcome_keys": ["participantId", "decimalOdds"],
                "first_outcome_players_type": "list",
                "first_outcome_players_count": 1,
                "first_nested_player_keys": ["participantId", "decimalOdds"],
                "parsed_outcome_candidate_count": 2,
                "parsed_outcome_candidate_reasons": ["team_a", "missing_price"],
                "parsed_outcome_candidate_samples": [
                    {
                        "name": "Team Spirit",
                        "ids": "5001,9001",
                        "outcome_key": "5001",
                        "player_key": "0",
                        "bookmakerOutcomeId": "bk-home",
                        "participantId": "0",
                        "selectionId": "0",
                        "price": "1.74",
                        "mapped_team": "a",
                        "reason": "team_a",
                        "exchangeMeta": "selectionName=Team Spirit",
                    }
                ],
                "direct_fixture_rejection_reason": "team_outcomes_not_found",
            },
            "normalized_events_count": 0,
            "last_note": "No matching odds rows",
        }
        args = argparse.Namespace(
            provider="oddspapi",
            sport="16",
            bookmaker="stake",
        )

        output = format_provider_debug(args, provider)

        self.assertIn("requested_provider=oddspapi", output)
        self.assertIn("requested_sport_input=16", output)
        self.assertIn("resolved_sportId=16", output)
        self.assertIn("bookmaker=stake", output)
        self.assertIn("request_mode=", output)
        self.assertIn("selection_mode=tournament_keyword", output)
        self.assertIn("tournaments_count=1", output)
        self.assertIn(
            "tournament_sample_1: id=101 name=DreamLeague "
            "slug=dreamleague category=Dota 2",
            output,
        )
        self.assertIn(
            "selected_tournament_sample_1: id=101 name=DreamLeague "
            "slug=dreamleague category=Dota 2",
            output,
        )
        self.assertIn("selected_tournament_ids=101", output)
        self.assertIn("raw_odds_root_type=dict", output)
        self.assertIn("raw_odds_first_item_keys=data", output)
        self.assertIn("raw_odds_top_level_keys=data", output)
        self.assertIn("normalized_events_count=0", output)
        self.assertIn("raw_odds_sample_nested_data_count=1", output)
        self.assertIn(
            "raw_odds_sample_first_nested_event_keys=teams,bookmakers",
            output,
        )
        self.assertIn("raw_odds_sample_bookmaker_odds_type=dict", output)
        self.assertIn("raw_odds_sample_bookmaker_odds_keys=stake", output)
        self.assertIn(
            "raw_odds_sample_matched_bookmaker_path=bookmakerOdds.stake",
            output,
        )
        self.assertIn("raw_odds_sample_matched_bookmaker_name=stake", output)
        self.assertIn("raw_odds_sample_bookmaker_keys=key,markets", output)
        self.assertIn("raw_odds_sample_market_container_type=dict", output)
        self.assertIn("raw_odds_sample_market_keys=marketType,outcomes", output)
        self.assertIn(
            "raw_odds_sample_first_outcome_keys=participantId,decimalOdds",
            output,
        )
        self.assertIn("raw_odds_sample_first_outcome_players_type=list", output)
        self.assertIn("raw_odds_sample_first_outcome_players_count=1", output)
        self.assertIn(
            "raw_odds_sample_first_nested_player_keys=participantId,decimalOdds",
            output,
        )
        self.assertIn("raw_odds_sample_parsed_outcome_candidate_count=2", output)
        self.assertIn(
            "raw_odds_sample_parsed_outcome_candidate_reasons=team_a,missing_price",
            output,
        )
        self.assertIn(
            "raw_odds_sample_candidate_1: name=Team Spirit ids=5001,9001 "
            "outcome_key=5001 player_key=0 bookmakerOutcomeId=bk-home "
            "participantId=0 selectionId=0 price=1.74 mapped_team=a "
            "reason=team_a exchangeMeta=selectionName=Team Spirit",
            output,
        )
        self.assertIn(
            "raw_odds_sample_direct_fixture_rejection_reason="
            "team_outcomes_not_found",
            output,
        )
        self.assertIn("provider_last_note=No matching odds rows", output)
        self.assertNotIn("test-key", output)

    def test_provider_debug_formatter_outputs_fixture_mode_odds_url(self) -> None:
        provider = OddsPapiProvider(api_key="test-key")
        provider.last_debug = {
            "resolved_sport_id": "16",
            "bookmaker": "stake",
            "request_mode": "fixture_id",
            "selection_mode": "fixture_id",
            "odds_url": (
                "https://api.oddspapi.io/v4/odds?fixtureId=fixture-1&"
                "bookmakers=stake&language=en&verbosity=5&"
                "oddsFormat=decimal&apiKey=***"
            ),
            "raw_odds_root_type": "list",
            "raw_odds_top_level_count": 1,
            "raw_odds_first_item_keys": [
                "fixtureId",
                "participant1Name",
                "participant2Name",
            ],
            "raw_odds_top_level_fixture_keys": [
                "fixtureId",
                "participant1Name",
                "participant2Name",
            ],
            "normalized_events_count": 0,
        }
        args = argparse.Namespace(
            provider="oddspapi",
            sport="dota2",
            bookmaker="stake",
        )

        output = format_provider_debug(args, provider)

        self.assertIn("request_mode=fixture_id", output)
        self.assertIn(
            "odds_url=https://api.oddspapi.io/v4/odds?"
            "fixtureId=fixture-1&bookmakers=stake&language=en&verbosity=5&"
            "oddsFormat=decimal&apiKey=***",
            output,
        )
        self.assertIn(
            "raw_odds_top_level_fixture_keys="
            "fixtureId,participant1Name,participant2Name",
            output,
        )
        self.assertIn("normalized_events_count=0", output)
        self.assertNotIn("test-key", output)

    def test_market_debug_formatter_outputs_fixture_markets_and_players(self) -> None:
        payload = [
            {
                "fixtureId": "fixture-1",
                "participant1Id": 244737,
                "participant1Name": "Power Rangers",
                "participant1ShortName": "Power Rangers",
                "participant1Abbr": "PWR",
                "participant2Id": 287351,
                "participant2Name": "Yellow Submarine",
                "participant2ShortName": "Yellow Submarine",
                "participant2Abbr": "YSU",
                "tournamentId": 13911,
                "tournamentName": "DreamLeague",
                "categoryName": "Dota 2",
                "startTime": "2026-06-28T14:30:00Z",
                "trueStartTime": "2026-06-28T15:00:00Z",
                "bookmakerOdds": {
                    "stake": {
                        "markets": {
                            "1649": {
                                "bookmakerMarketId": "bm-1649",
                                "marketActive": True,
                                "suspended": False,
                                "outcomes": {
                                    "1649": {
                                        "selectionId": 1649,
                                        "label": "home",
                                        "players": {
                                            "0": {
                                                "bookmakerOutcomeId": "bo-1",
                                                "playerName": "",
                                                "participantId": 0,
                                                "selectionId": 0,
                                                "price": 1.92,
                                                "exchangeMeta": {
                                                    "selection": "Power Rangers",
                                                    "href": "https://example.test?apiKey=test-key",
                                                    "ignored": {"nested": "value"},
                                                },
                                            }
                                        },
                                    },
                                    "1650": {
                                        "selectionId": 1650,
                                        "label": "away",
                                        "players": {
                                            "0": {
                                                "bookmakerOutcomeId": "bo-2",
                                                "playerName": "",
                                                "participantId": 0,
                                                "selectionId": 0,
                                                "price": 1.78,
                                            }
                                        },
                                    },
                                },
                            },
                            "161": {
                                "bookmakerMarketId": "bm-161",
                                "marketActive": True,
                                "outcomes": {},
                            },
                        }
                    }
                },
            }
        ]
        provider = OddsPapiProvider(api_key="test-key")

        provider.normalize_response(payload, market_type="h2h", bookmaker="stake")
        output = format_market_debug(provider)

        self.assertIn("market_debug:", output)
        self.assertIn("fixture_1: fixtureId=fixture-1", output)
        self.assertIn("tournamentId=13911", output)
        self.assertIn("tournamentName=DreamLeague", output)
        self.assertIn(
            "fixture_1_team1: id=244737 name=Power Rangers "
            "short=Power Rangers abbr=PWR",
            output,
        )
        self.assertIn(
            "fixture_1_team2: id=287351 name=Yellow Submarine "
            "short=Yellow Submarine abbr=YSU",
            output,
        )
        self.assertIn("fixture_1_bookmaker: path=bookmakerOdds.stake name=stake", output)
        self.assertIn(
            "fixture_1_market_1: path=markets.1649 marketId=1649 key=1649 "
            "bookmakerMarketId=bm-1649 marketActive=True suspended=False "
            "outcome_count=2",
            output,
        )
        self.assertIn(
            "fixture_1_market_1_outcome_1: outcome_key=1649 selectionId=1649 "
            "name=home players_type=dict player_key=0 playerName= "
            "participantId=0 playerSelectionId=0 bookmakerOutcomeId=bo-1 "
            "price=1.92 exchangeMeta=selection=Power Rangers;href=https://example.test?apiKey=***",
            output,
        )
        self.assertIn(
            "fixture_1_market_2: path=markets.161 marketId=161 key=161 "
            "bookmakerMarketId=bm-161 marketActive=True",
            output,
        )
        self.assertNotIn("test-key", output)

    def test_dota2_match_winner_fixture_normalizes_to_provider_odds_event(
        self,
    ) -> None:
        fixture_path = (
            Path(__file__).parent / "fixtures" / "oddspapi_dota2_match_winner.json"
        )
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))

        provider = OddsPapiProvider(api_key="offline-test")
        events = provider.normalize_response(
            payload,
            market_type="h2h",
            bookmaker="stake",
            sport="esport-dota",
        )

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertIsInstance(event, ProviderOddsEvent)
        self.assertEqual(event.provider, "oddspapi")
        self.assertEqual(event.bookmaker, "stake")
        self.assertEqual(event.game, "esport-dota")
        self.assertEqual(event.tournament, "DreamLeague Season 25")
        self.assertEqual(
            event.start_time,
            datetime(2026, 6, 28, 15, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(event.team_a, "Team Spirit")
        self.assertEqual(event.team_b, "Gaimin Gladiators")
        self.assertEqual(event.market_type, "match_winner")
        self.assertEqual(event.odds_a, 1.74)
        self.assertEqual(event.odds_b, 2.08)
        self.assertEqual(
            event.event_url,
            "https://example.test/events/oddspapi_evt_001",
        )
        self.assertEqual(event.raw["event"]["id"], "oddspapi_evt_001")


if __name__ == "__main__":
    unittest.main()
