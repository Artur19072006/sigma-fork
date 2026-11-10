from __future__ import annotations

import argparse
import io
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from provider_odds.cli.probe import format_provider_debug, main as probe_main
from provider_odds.models import ProviderOddsEvent
from provider_odds.providers.polymarket import PolymarketProvider


class RecordingPolymarketProvider(PolymarketProvider):
    def __init__(self, responses: list[object]) -> None:
        super().__init__()
        self.responses = responses
        self.urls: list[str] = []

    def _get_json(self, url: str) -> object:
        self.urls.append(url)
        if not self.responses:
            raise AssertionError(f"Unexpected request: {url}")
        return self.responses.pop(0)


def _fixture(name: str) -> object:
    path = Path(__file__).parent / "fixtures" / name
    return json.loads(path.read_text(encoding="utf-8"))


def _dota2_market_payload(
    *,
    question: str,
    event_title: str = "Team Spirit vs Gaimin Gladiators",
    event_slug: str = "team-spirit-vs-gaimin-gladiators-dota2",
    market_slug: str = "team-spirit-vs-gaimin-gladiators-match-winner",
    outcomes: list[str] | None = None,
    prices: list[str] | None = None,
) -> dict[str, object]:
    outcome_names = outcomes or ["Team Spirit", "Gaimin Gladiators"]
    outcome_prices = prices or ["0.57", "0.43"]
    return {
        "events": [
            {
                "id": "pm-event-dota2-safety",
                "title": event_title,
                "slug": event_slug,
                "active": True,
                "closed": False,
                "category": "Dota 2",
                "markets": [
                    {
                        "id": "pm-market-dota2-safety",
                        "conditionId": "pm-cond-dota2-safety",
                        "question": question,
                        "slug": market_slug,
                        "active": True,
                        "closed": False,
                        "outcomes": outcome_names,
                        "outcomePrices": outcome_prices,
                    }
                ],
            }
        ]
    }


class PolymarketProviderTest(unittest.TestCase):
    def test_sport_aliases_build_gamma_search_urls(self) -> None:
        cases = [
            ("dota2", "dota2", "Dota+2"),
            ("dota-2", "dota2", "Dota+2"),
            ("dota 2", "dota2", "Dota+2"),
            ("cs2", "cs2", "Counter-Strike"),
            ("counter-strike", "cs2", "Counter-Strike"),
            ("counter strike", "cs2", "Counter-Strike"),
            ("lol", "league_of_legends", "League+of+Legends"),
            ("league-of-legends", "league_of_legends", "League+of+Legends"),
            ("league of legends", "league_of_legends", "League+of+Legends"),
        ]

        for alias, resolved, query in cases:
            with self.subTest(alias=alias):
                provider = RecordingPolymarketProvider(responses=[{"data": []}])

                events = provider.fetch_events(sport=alias, max_events=250)

                self.assertEqual(events, [])
                self.assertEqual(provider.last_debug["resolved_sport"], resolved)
                self.assertEqual(
                    provider.urls,
                    [
                        "https://gamma-api.polymarket.com/events?"
                        f"active=true&closed=false&limit=100&search={query}"
                    ],
                )
                self.assertEqual(provider.last_debug["max_events"], 100)

    def test_valid_dota2_match_winner_normalizes_and_rejects_unsafe_markets(
        self,
    ) -> None:
        payload = _fixture("polymarket_dota2_markets.json")
        provider = PolymarketProvider()

        events = provider.normalize_response(payload, sport="dota 2")

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertIsInstance(event, ProviderOddsEvent)
        self.assertEqual(event.provider, "polymarket")
        self.assertEqual(event.bookmaker, "polymarket")
        self.assertEqual(event.game, "dota2")
        self.assertEqual(event.tournament, "DreamLeague Season 25")
        self.assertEqual(
            event.start_time,
            datetime(2026, 6, 29, 16, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(event.team_a, "Team Spirit")
        self.assertEqual(event.team_b, "Gaimin Gladiators")
        self.assertEqual(event.market_type, "h2h")
        self.assertAlmostEqual(event.odds_a, 1 / 0.57)
        self.assertAlmostEqual(event.odds_b, 1 / 0.43)
        self.assertEqual(
            event.event_url,
            "https://polymarket.com/event/"
            "team-spirit-vs-gaimin-gladiators-dota2-dreamleague",
        )
        self.assertEqual(event.raw["market_id"], "pm-market-dota2-h2h-001")
        self.assertEqual(event.raw["condition_id"], "pm-cond-dota2-h2h-001")
        self.assertEqual(event.raw["original_prices"], [0.57, 0.43])
        self.assertEqual(event.raw["source_fields"]["game"], "dota2")
        self.assertEqual(
            provider.last_debug["reject_reason_counts"],
            {
                "closed": 1,
                "inactive": 1,
                "invalid_price": 1,
                "map_or_game_winner": 1,
            },
        )
        self.assertEqual(provider.last_debug["fetched_count"], 5)
        self.assertEqual(provider.last_debug["normalized_count"], 1)

    def test_best_ask_is_normalized_as_actionable_buy_price(self) -> None:
        payload = {
            "events": [
                {
                    "id": "pm-event-blg-hle",
                    "title": (
                        "League of Legends: Bilibili Gaming vs "
                        "Hanwha Life Esports"
                    ),
                    "slug": "lol-bilibili-gaming-vs-hanwha-life-esports",
                    "active": True,
                    "closed": False,
                    "category": "League of Legends",
                    "markets": [
                        {
                            "id": "pm-market-blg-hle",
                            "conditionId": "pm-cond-blg-hle",
                            "question": (
                                "League of Legends: Bilibili Gaming vs "
                                "Hanwha Life Esports"
                            ),
                            "slug": "lol-bilibili-gaming-vs-hanwha-life-esports",
                            "active": True,
                            "closed": False,
                            "outcomes": json.dumps(
                                ["Bilibili Gaming", "Hanwha Life Esports"]
                            ),
                            "outcomePrices": json.dumps(["0.385", "0.615"]),
                            "bestAsk": "0.39",
                            "lastTradePrice": "0.39",
                        }
                    ],
                }
            ]
        }
        provider = PolymarketProvider()

        events = provider.normalize_response(payload, sport="lol")

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertAlmostEqual(event.odds_a, 1 / 0.39)
        self.assertAlmostEqual(event.odds_b, 1 / 0.615)
        self.assertEqual(event.raw["original_prices"], [0.385, 0.615])
        self.assertEqual(event.raw["executable_buy_prices"], [0.39, None])
        self.assertEqual(event.raw["price_sources"], ["bestAsk", "outcomePrices"])
        self.assertEqual(event.raw["polymarket_actionable"], [True, False])
        self.assertEqual(
            provider.last_debug["normalized_samples"][0]["price_source"],
            "bestAsk/outcomePrices",
        )
        self.assertEqual(
            provider.last_debug["normalized_samples"][0][
                "executable_buy_prices"
            ],
            "0.39/",
        )

    def test_dota2_rejects_draw_tie_and_three_way_markets(self) -> None:
        cases = (
            "Dota 2: Team Spirit vs Gaimin Gladiators draw winner",
            "Dota 2: Team Spirit vs Gaimin Gladiators tie winner",
            "Dota 2: Team Spirit vs Gaimin Gladiators three-way winner",
            "Dota 2: Team Spirit vs Gaimin Gladiators 3-way winner",
        )

        for question in cases:
            with self.subTest(question=question):
                provider = PolymarketProvider()

                events = provider.normalize_response(
                    _dota2_market_payload(question=question),
                    sport="dota2",
                )

                self.assertEqual(events, [])
                self.assertEqual(
                    provider.last_debug["reject_reason_counts"],
                    {"draw_or_three_way": 1},
                )
                sample = provider.last_debug["rejected_samples"][0]
                self.assertEqual(sample["unsafe_flags"], "draw_or_three_way")
                self.assertTrue(sample["draw_or_three_way_matched_text"])

    def test_dota2_rejects_bo2_or_ambiguous_format_markets(self) -> None:
        cases = (
            "BO2",
            "B02",
            "Best of 2",
            "Best-of-2",
        )

        for best_of in cases:
            with self.subTest(best_of=best_of):
                provider = PolymarketProvider()

                events = provider.normalize_response(
                    _dota2_market_payload(
                        question=(
                            "Dota 2: Team Spirit vs Gaimin Gladiators "
                            "match winner"
                        ),
                        event_title=(
                            "Team Spirit vs Gaimin Gladiators "
                            f"{best_of}"
                        ),
                    ),
                    sport="dota2",
                )

                self.assertEqual(events, [])
                self.assertEqual(
                    provider.last_debug["reject_reason_counts"],
                    {"bo2_or_ambiguous_format": 1},
                )

    def test_dota2_rejects_ewc_even_when_market_is_two_way(self) -> None:
        provider = PolymarketProvider()

        events = provider.normalize_response(
            _dota2_market_payload(
                question="Dota 2: Team Falcons vs BetBoom match winner",
                event_title="Dota 2: Team Falcons vs BetBoom - Esports World Cup",
                event_slug="dota2-team-falcons-vs-betboom-ewc-2026-07-10",
                outcomes=["Team Falcons", "BetBoom"],
            ),
            sport="dota2",
        )

        self.assertEqual(events, [])
        self.assertEqual(
            provider.last_debug["reject_reason_counts"],
            {"ewc_or_draw_risk_tournament": 1},
        )

    def test_dota2_rejects_map_and_game_winner_markets(self) -> None:
        cases = (
            "Dota 2: Team Spirit vs Gaimin Gladiators Map 1 Winner",
            "Dota 2: Team Spirit vs Gaimin Gladiators Game 1 Winner",
            "Dota 2: Team Spirit vs Gaimin Gladiators Game 2 Winner",
            "Dota 2: Team Spirit vs Gaimin Gladiators game winner",
            "Dota 2: Team Spirit vs Gaimin Gladiators exact maps",
        )

        for question in cases:
            with self.subTest(question=question):
                provider = PolymarketProvider()

                events = provider.normalize_response(
                    _dota2_market_payload(question=question),
                    sport="dota2",
                )

                self.assertEqual(events, [])
                self.assertEqual(
                    provider.last_debug["reject_reason_counts"],
                    {"map_or_game_winner": 1},
                )

    def test_dota2_rejects_handicaps_totals_and_props(self) -> None:
        cases = (
            "Dota 2: Team Spirit vs Gaimin Gladiators handicap",
            "Dota 2: Team Spirit vs Gaimin Gladiators spread",
            "Dota 2: Team Spirit vs Gaimin Gladiators total kills",
            "Dota 2: Team Spirit vs Gaimin Gladiators over/under kills",
            "Dota 2: Team Spirit vs Gaimin Gladiators player props",
            "Dota 2: Team Spirit vs Gaimin Gladiators hero props",
        )

        for question in cases:
            with self.subTest(question=question):
                provider = PolymarketProvider()

                events = provider.normalize_response(
                    _dota2_market_payload(question=question),
                    sport="dota2",
                )

                self.assertEqual(events, [])
                self.assertEqual(
                    provider.last_debug["reject_reason_counts"],
                    {"prop_market": 1},
                )

    def test_dota2_rejects_outrights_futures_and_champion_markets(self) -> None:
        cases = (
            "Dota 2: DreamLeague outright winner",
            "Dota 2: DreamLeague futures winner",
            "Dota 2: DreamLeague tournament winner",
            "Dota 2: DreamLeague champion",
            "Dota 2: Europe region winner",
        )

        for question in cases:
            with self.subTest(question=question):
                provider = PolymarketProvider()

                events = provider.normalize_response(
                    _dota2_market_payload(question=question),
                    sport="dota2",
                )

                self.assertEqual(events, [])
                self.assertEqual(
                    provider.last_debug["reject_reason_counts"],
                    {"outright_market": 1},
                )

    def test_clean_dota2_bo3_or_bo5_series_winner_survives(self) -> None:
        cases = (
            (
                "Dota 2: Team Spirit vs Gaimin Gladiators BO3 Match Winner",
                "Team Spirit vs Gaimin Gladiators BO3 - DreamLeague",
                "BO3",
            ),
            (
                "Dota 2: Team Spirit vs Gaimin Gladiators B03 Match Winner",
                "Team Spirit vs Gaimin Gladiators B03 - DreamLeague",
                "B03",
            ),
            (
                "Dota 2: Team Spirit vs Gaimin Gladiators BO5 Series Winner",
                "Team Spirit vs Gaimin Gladiators BO5 - DreamLeague",
                "BO5",
            ),
            (
                "Dota 2: Team Spirit vs Gaimin Gladiators B05 Series Winner",
                "Team Spirit vs Gaimin Gladiators B05 - DreamLeague",
                "B05",
            ),
            (
                "Dota 2: Team Spirit vs Gaimin Gladiators Best of 3 Match Winner",
                "Team Spirit vs Gaimin Gladiators Best of 3 - DreamLeague",
                "BO3",
            ),
            (
                "Dota 2: Team Spirit vs Gaimin Gladiators Best-of-5 Series Winner",
                "Team Spirit vs Gaimin Gladiators Best-of-5 - DreamLeague",
                "BO5",
            ),
        )
        for question, event_title, detected_best_of in cases:
            with self.subTest(question=question):
                provider = PolymarketProvider()

                events = provider.normalize_response(
                    _dota2_market_payload(
                        question=question,
                        event_title=event_title,
                    ),
                    sport="dota2",
                )

                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].game, "dota2")
                self.assertEqual(events[0].team_a, "Team Spirit")
                self.assertEqual(events[0].team_b, "Gaimin Gladiators")
                self.assertEqual(
                    events[0].raw["detected_best_of"],
                    detected_best_of,
                )
                self.assertEqual(
                    provider.last_debug["normalized_samples"][0][
                        "detected_best_of"
                    ],
                    detected_best_of,
                )
                self.assertEqual(provider.last_debug["reject_reason_counts"], {})

    def test_clean_dota2_bo3_group_match_winner_survives(self) -> None:
        cases = (
            ("BO3", "European Pro League Group A", "BO3"),
            ("B03", "European Pro League Group B", "B03"),
            ("Best of 3", "European Pro League group stage", "BO3"),
        )
        for best_of, tournament, detected_best_of in cases:
            with self.subTest(best_of=best_of, tournament=tournament):
                provider = PolymarketProvider()

                events = provider.normalize_response(
                    _dota2_market_payload(
                        question=(
                            f"Dota 2: Spirit Academy vs PuckChamp ({best_of}) "
                            f"- {tournament}"
                        ),
                        event_title=(
                            f"Dota 2: Spirit Academy vs PuckChamp ({best_of}) "
                            f"- {tournament}"
                        ),
                        event_slug="dota2-spirit1-pckcp-2026-06-29",
                        outcomes=["Spirit Academy", "PuckChamp"],
                    ),
                    sport="dota2",
                )

                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].team_a, "Spirit Academy")
                self.assertEqual(events[0].team_b, "PuckChamp")
                self.assertEqual(events[0].raw["detected_best_of"], detected_best_of)
                self.assertEqual(provider.last_debug["reject_reason_counts"], {})

    def test_exact_dota2_epl_group_a_bo3_market_is_not_draw_or_three_way(
        self,
    ) -> None:
        title = (
            "Dota 2: Spirit Academy vs PuckChamp (BO3) - "
            "European Pro League Group A"
        )
        provider = PolymarketProvider()

        events = provider.normalize_response(
            _dota2_market_payload(
                question=title,
                event_title=title,
                event_slug="dota2-spirit1-pckcp-2026-06-29",
                outcomes=["Spirit Academy", "PuckChamp"],
            ),
            sport="dota2",
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].team_a, "Spirit Academy")
        self.assertEqual(events[0].team_b, "PuckChamp")
        self.assertEqual(events[0].raw["detected_best_of"], "BO3")
        self.assertEqual(provider.last_debug["reject_reason_counts"], {})
        self.assertEqual(
            provider.last_debug["normalized_samples"][0]["outcome_count"],
            "2",
        )

    def test_exact_dota2_epl_group_a_bo3_generic_outcomes_are_not_draw_risk(
        self,
    ) -> None:
        title = (
            "Dota 2: Spirit Academy vs PuckChamp (BO3) - "
            "European Pro League Group A"
        )
        provider = PolymarketProvider()

        events = provider.normalize_response(
            _dota2_market_payload(
                question=title,
                event_title=title,
                event_slug="dota2-spirit1-pckcp-2026-06-29",
                outcomes=["Yes", "No"],
            ),
            sport="dota2",
        )

        self.assertEqual(events, [])
        self.assertEqual(
            provider.last_debug["reject_reason_counts"],
            {"generic_outcome": 1},
        )
        sample = provider.last_debug["rejected_samples"][0]
        self.assertEqual(sample["reason"], "generic_outcome")
        self.assertEqual(sample["detected_best_of"], "BO3")
        self.assertEqual(sample["outcome_count"], "2")
        self.assertEqual(sample["outcome_names"], "Yes|No")
        self.assertEqual(sample["unsafe_flags"], "")
        self.assertEqual(sample["draw_or_three_way_matched_text"], "")

    def test_market_response_shape_uses_embedded_event_context(self) -> None:
        payload = _fixture("polymarket_cs2_market_response.json")
        provider = PolymarketProvider()

        events = provider.normalize_response(payload, sport="counter strike")

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.game, "cs2")
        self.assertEqual(event.tournament, "BLAST Premier")
        self.assertEqual(
            event.start_time,
            datetime(2026, 7, 2, 19, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(event.team_a, "Natus Vincere")
        self.assertEqual(event.team_b, "Vitality")
        self.assertAlmostEqual(event.odds_a, 1 / 0.48)
        self.assertAlmostEqual(event.odds_b, 1 / 0.52)
        self.assertEqual(
            event.event_url,
            "https://polymarket.com/event/natus-vincere-vs-vitality-cs2-blast",
        )

    def test_event_slug_query_keeps_only_bo3_match_winner(self) -> None:
        payload = _fixture("polymarket_cs2_event_slug_markets.json")
        provider = RecordingPolymarketProvider(responses=[payload])

        events = provider.fetch_events(
            sport="cs2",
            event_slug="cs2-paina-alkaa-2026-06-27",
            search="ignored broad search",
        )

        self.assertEqual(
            provider.urls,
            [
                "https://gamma-api.polymarket.com/events?"
                "slug=cs2-paina-alkaa-2026-06-27"
            ],
        )
        self.assertNotIn("/events/slug/", provider.urls[0])
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.provider, "polymarket")
        self.assertEqual(event.bookmaker, "polymarket")
        self.assertEqual(event.game, "cs2")
        self.assertEqual(
            event.tournament,
            "Counter-Strike: paiN Academy vs ALKA (BO3) - "
            "CCT South America Series 3 Group Stage",
        )
        self.assertEqual(
            event.start_time,
            datetime(2026, 6, 27, 21, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(event.team_a, "paiN Academy")
        self.assertEqual(event.team_b, "ALKA")
        self.assertEqual(event.market_type, "h2h")
        self.assertAlmostEqual(event.odds_a, 1 / 0.455)
        self.assertAlmostEqual(event.odds_b, 1 / 0.545)
        self.assertAlmostEqual(event.odds_a, 2.20, places=2)
        self.assertAlmostEqual(event.odds_b, 1.83, places=2)
        self.assertEqual(
            event.event_url,
            "https://polymarket.com/event/cs2-paina-alkaa-2026-06-27",
        )
        self.assertEqual(event.raw["source"], "gamma_query_slug")
        self.assertEqual(event.raw["event_id"], "pm-event-cs2-paina-alkaa")
        self.assertEqual(event.raw["market_id"], "pm-market-cs2-match")
        self.assertEqual(event.raw["slug"], "cs2-paina-alkaa-2026-06-27")
        self.assertEqual(
            event.raw["match_winner_source"],
            "polymarket_parent_team_vs_team_market",
        )
        self.assertEqual(
            event.raw["question"],
            "Counter-Strike: paiN Academy vs ALKA (BO3) - "
            "CCT South America Series 3 Group Stage",
        )
        self.assertEqual(event.raw["original_prices"], [0.455, 0.545])
        self.assertEqual(
            event.raw["active"],
            {"event": True, "market": True},
        )
        self.assertEqual(
            event.raw["closed"],
            {"event": False, "market": False},
        )
        self.assertEqual(
            provider.last_debug["reject_reason_counts"],
            {"map_or_game_winner": 4, "prop_market": 4},
        )
        self.assertEqual(
            provider.last_debug["normalized_samples"][0]["source"],
            "gamma_query_slug",
        )
        self.assertEqual(
            provider.last_debug["normalized_samples"][0]["match_winner_source"],
            "polymarket_parent_team_vs_team_market",
        )
        self.assertEqual(
            provider.last_debug["normalized_samples"][0]["original_prices"],
            "0.455/0.545",
        )
        self.assertEqual(provider.last_debug["request_mode"], "event_slug")
        self.assertEqual(provider.last_debug["fetched_count"], 9)
        self.assertEqual(provider.last_debug["normalized_count"], 1)

    def test_market_slug_query_normalizes_direct_h2h_market(self) -> None:
        payload = _fixture("polymarket_cs2_market_slug_direct.json")
        provider = RecordingPolymarketProvider(responses=[payload])

        events = provider.fetch_events(
            sport="counter-strike",
            event_slug="ignored-event-slug",
            market_slug="cs2-paina-alkaa-2026-06-27",
        )

        self.assertEqual(
            provider.urls,
            [
                "https://gamma-api.polymarket.com/markets?"
                "slug=cs2-paina-alkaa-2026-06-27"
            ],
        )
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.game, "cs2")
        self.assertEqual(event.tournament, "Counter-Strike")
        self.assertEqual(event.team_a, "paiN Academy")
        self.assertEqual(event.team_b, "ALKA")
        self.assertAlmostEqual(event.odds_a, 1 / 0.455)
        self.assertAlmostEqual(event.odds_b, 1 / 0.545)
        self.assertEqual(
            event.event_url,
            "https://polymarket.com/event/cs2-paina-alkaa-2026-06-27",
        )
        self.assertEqual(event.raw["source"], "gamma_query_slug")
        self.assertEqual(provider.last_debug["request_mode"], "market_slug")
        self.assertEqual(provider.last_debug["fetched_count"], 1)
        self.assertEqual(provider.last_debug["normalized_count"], 1)
        self.assertEqual(provider.last_debug["reject_reason_counts"], {})

    def test_non_team_binary_market_is_rejected(self) -> None:
        payload = {
            "events": [
                {
                    "id": "pm-event-prop-001",
                    "title": "Team Spirit vs Gaimin Gladiators",
                    "category": "Dota 2",
                    "active": True,
                    "closed": False,
                    "markets": [
                        {
                            "id": "pm-market-prop-001",
                            "question": "Will Team Spirit win this match?",
                            "active": True,
                            "closed": False,
                            "outcomes": ["Yes", "No"],
                            "outcomePrices": ["0.62", "0.38"],
                        }
                    ],
                }
            ]
        }
        provider = PolymarketProvider()

        events = provider.normalize_response(payload, sport="dota2")

        self.assertEqual(events, [])
        self.assertEqual(
            provider.last_debug["reject_reason_counts"],
            {"generic_outcome": 1},
        )

    def test_explicit_non_esports_market_is_rejected(self) -> None:
        payload = {
            "events": [
                {
                    "id": "pm-event-nba-001",
                    "title": "Lakers vs Celtics",
                    "category": "NBA",
                    "active": True,
                    "closed": False,
                    "markets": [
                        {
                            "id": "pm-market-nba-001",
                            "question": "NBA: Lakers vs Celtics match winner",
                            "active": True,
                            "closed": False,
                            "outcomes": ["Lakers", "Celtics"],
                            "outcomePrices": ["0.52", "0.48"],
                        }
                    ],
                }
            ]
        }
        provider = PolymarketProvider()

        events = provider.normalize_response(payload, sport="dota2")

        self.assertEqual(events, [])
        self.assertEqual(
            provider.last_debug["reject_reason_counts"],
            {"unsupported_sport": 1},
        )

    def test_fetch_events_uses_optional_search_and_hard_cap(self) -> None:
        payload = _fixture("polymarket_dota2_markets.json")
        provider = RecordingPolymarketProvider(responses=[payload])

        events = provider.fetch_events(
            sport="dota2",
            market_type="h2h",
            search="DreamLeague",
            max_events=500,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(
            provider.urls,
            [
                "https://gamma-api.polymarket.com/events?"
                "active=true&closed=false&limit=100&search=Dota+2+DreamLeague"
            ],
        )
        self.assertEqual(
            provider.last_debug["request_url"],
            "https://gamma-api.polymarket.com/events?"
            "active=true&closed=false&limit=100&search=Dota+2+DreamLeague",
        )
        self.assertEqual(provider.last_debug["max_events"], 100)

    def test_cli_table_output_works_for_polymarket_fixture(self) -> None:
        fixture_path = (
            Path(__file__).parent / "fixtures" / "polymarket_dota2_markets.json"
        )

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            result = probe_main(
                [
                    "--provider",
                    "polymarket",
                    "--sport",
                    "dota2",
                    "--market-type",
                    "h2h",
                    "--fixture",
                    str(fixture_path),
                ]
            )

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("provider", output)
        self.assertIn("polymarket", output)
        self.assertIn("Team Spirit", output)
        self.assertIn("Gaimin Gladiators", output)
        self.assertIn("h2h", output)
        self.assertIn("1.75439", output)
        self.assertIn("2.32558", output)

    def test_cli_table_output_works_for_event_slug_fixture(self) -> None:
        fixture_path = (
            Path(__file__).parent
            / "fixtures"
            / "polymarket_cs2_event_slug_markets.json"
        )

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            result = probe_main(
                [
                    "--provider",
                    "polymarket",
                    "--sport",
                    "cs2",
                    "--event-slug",
                    "cs2-paina-alkaa-2026-06-27",
                    "--fixture",
                    str(fixture_path),
                ]
            )

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("polymarket", output)
        self.assertIn("paiN Academy", output)
        self.assertIn("ALKA", output)
        self.assertIn("2.1978", output)
        self.assertIn("1.83486", output)

    def test_cli_table_output_works_for_market_slug_fixture(self) -> None:
        fixture_path = (
            Path(__file__).parent
            / "fixtures"
            / "polymarket_cs2_market_slug_direct.json"
        )

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            result = probe_main(
                [
                    "--provider",
                    "polymarket",
                    "--sport",
                    "cs2",
                    "--market-slug",
                    "cs2-paina-alkaa-2026-06-27",
                    "--fixture",
                    str(fixture_path),
                ]
            )

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("polymarket", output)
        self.assertIn("paiN Academy", output)
        self.assertIn("ALKA", output)
        self.assertIn("2.1978", output)
        self.assertIn("1.83486", output)

    def test_debug_output_is_compact_and_sanitized(self) -> None:
        payload = _fixture("polymarket_dota2_markets.json")
        provider = RecordingPolymarketProvider(responses=[payload])
        provider.fetch_events(sport="dota2", search="DreamLeague")
        args = argparse.Namespace(
            provider="polymarket",
            sport="dota2",
            market_type="h2h",
            search="DreamLeague",
            max_events=20,
        )

        output = format_provider_debug(args, provider)

        self.assertIn("requested_provider=polymarket", output)
        self.assertIn("resolved_sport=dota2", output)
        self.assertIn(
            "request_url=https://gamma-api.polymarket.com/events?"
            "active=true&closed=false&limit=20&search=Dota+2+DreamLeague",
            output,
        )
        self.assertIn("fetched_count=5", output)
        self.assertIn("normalized_count=1", output)
        self.assertIn(
            "reject_reason_counts=closed:1,inactive:1,"
            "invalid_price:1,map_or_game_winner:1",
            output,
        )
        self.assertIn(
            "normalized_sample_1: sport=dota2 "
            "teams=Team Spirit vs Gaimin Gladiators",
            output,
        )
        self.assertIn(
            "rejected_sample_1: reason=map_or_game_winner",
            output,
        )
        self.assertLessEqual(len(output.splitlines()), 20)
        self.assertNotIn("apiKey", output)
        self.assertNotIn("token=", output)

    def test_slug_debug_output_is_compact_and_sanitized(self) -> None:
        payload = _fixture("polymarket_cs2_event_slug_markets.json")
        provider = RecordingPolymarketProvider(responses=[payload])
        provider.fetch_events(
            sport="cs2",
            event_slug="cs2-paina-alkaa-2026-06-27",
        )
        args = argparse.Namespace(
            provider="polymarket",
            sport="cs2",
            market_type="h2h",
            search=None,
            max_events=20,
        )

        output = format_provider_debug(args, provider)

        self.assertIn("request_mode=event_slug", output)
        self.assertIn(
            "request_url=https://gamma-api.polymarket.com/events?"
            "slug=cs2-paina-alkaa-2026-06-27",
            output,
        )
        self.assertIn("fetched_count=9", output)
        self.assertIn("normalized_count=1", output)
        self.assertIn(
            "reject_reason_counts=map_or_game_winner:4,prop_market:4",
            output,
        )
        self.assertIn(
            "normalized_sample_1: sport=cs2 teams=paiN Academy vs ALKA",
            output,
        )
        self.assertIn("rejected_sample_1: reason=map_or_game_winner", output)
        self.assertLessEqual(len(output.splitlines()), 20)
        self.assertNotIn("/events/slug/", output)
        self.assertNotIn("apiKey", output)
        self.assertNotIn("token=", output)


if __name__ == "__main__":
    unittest.main()
