from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from provider_odds.arbitrage import (
    ArbitrageConfig,
    calculate_arbitrage,
    polymarket_effective_decimal_odds,
)
from provider_odds.cli.arbitrage import main as arbitrage_main


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _match(
    *,
    orientation: str = "same",
    stake_left: bool = True,
    stake_odds: tuple[object, object] = (1.5, 2.6),
    polymarket_odds: tuple[object, object] = (1.4, 3.0),
    polymarket_teams: tuple[str, str] | None = None,
    polymarket_executable: bool = True,
) -> dict[str, object]:
    stake_event = {
        "provider": "oddspapi",
        "bookmaker": "stake",
        "game": "cs2",
        "tournament": "CCT",
        "start_time": "2026-06-28T16:00:00+00:00",
        "team_a": "Team Alpha",
        "odds_a": stake_odds[0],
        "team_b": "Team Beta",
        "odds_b": stake_odds[1],
        "market_type": "h2h",
        "event_url": "https://example.test/stake/team-alpha-team-beta",
        "raw": {
            "fixture_id": "stake-fixture-1",
        },
    }
    if polymarket_teams is None:
        polymarket_teams = (
            ("Team Beta", "Team Alpha")
            if orientation == "swapped"
            else ("Team Alpha", "Team Beta")
        )
    executable_prices = (
        [_price_from_odds(odds) for odds in polymarket_odds]
        if polymarket_executable
        else [None, None]
    )
    executable_sources = [
        "bestAsk" if price is not None else "" for price in executable_prices
    ]
    polymarket_event = {
        "provider": "polymarket",
        "bookmaker": "polymarket",
        "game": "cs2",
        "tournament": "CCT",
        "start_time": "2026-06-28T16:00:00+00:00",
        "team_a": polymarket_teams[0],
        "odds_a": polymarket_odds[0],
        "team_b": polymarket_teams[1],
        "odds_b": polymarket_odds[1],
        "market_type": "h2h",
        "event_url": "https://polymarket.com/event/team-alpha-team-beta",
        "raw": {
            "market_id": "pm-market-1",
            "original_prices": [_price_from_odds(odds) for odds in polymarket_odds],
            "executable_buy_prices": executable_prices,
            "executable_price_sources": executable_sources,
            "price_sources": executable_sources,
            "polymarket_actionable": [
                price is not None for price in executable_prices
            ],
        },
    }
    return {
        "left_event": stake_event if stake_left else polymarket_event,
        "right_event": polymarket_event if stake_left else stake_event,
        "orientation": orientation,
        "score": 1.0,
        "team_score": 1.0,
        "start_time_diff_minutes": 0.0,
    }


def _price_from_odds(value: object) -> float | None:
    try:
        odds = float(value)
    except (TypeError, ValueError):
        return None
    if odds <= 1:
        return None
    return 1 / odds


class ProviderArbitrageTest(unittest.TestCase):
    def test_positive_arbitrage_detects_stake_team_a_polymarket_team_b(self) -> None:
        result = calculate_arbitrage([_match()])

        opportunities = result["opportunities"]
        self.assertEqual(result["summary"]["opportunity_count"], 1)
        self.assertEqual(len(opportunities), 1)
        opportunity = opportunities[0]
        self.assertEqual(opportunity["stake_team"], "Team Alpha")
        self.assertEqual(opportunity["stake_odds"], 1.5)
        self.assertEqual(opportunity["polymarket_team"], "Team Beta")
        self.assertEqual(opportunity["polymarket_raw_odds"], 3.0)
        self.assertLessEqual(opportunity["implied_sum"], 1.0)
        self.assertEqual(opportunity["source_ids"]["stake_fixture_id"], "stake-fixture-1")
        self.assertEqual(opportunity["source_ids"]["polymarket_market_id"], "pm-market-1")

    def test_zero_edge_arbitrage_counts_with_zero_fee(self) -> None:
        result = calculate_arbitrage(
            [_match()],
            config=ArbitrageConfig(polymarket_fee_rate=0.0),
        )

        opportunity = result["opportunities"][0]
        self.assertAlmostEqual(opportunity["implied_sum"], 1.0)
        self.assertEqual(opportunity["edge_percent"], 0.0)

    def test_no_arbitrage_outputs_no_opportunities(self) -> None:
        result = calculate_arbitrage(
            [
                _match(
                    stake_odds=(1.5, 1.8),
                    polymarket_odds=(1.7, 2.5),
                )
            ]
        )

        self.assertEqual(result["opportunities"], [])
        self.assertEqual(result["summary"]["opportunity_count"], 0)
        self.assertEqual(result["debug"]["reason_counts"]["no_arbitrage"], 2)

    def test_cancel_risk_uses_polymarket_raw_odds_threshold(self) -> None:
        result = calculate_arbitrage(
            [
                _match(stake_odds=(3.0, 1.2), polymarket_odds=(1.2, 1.8)),
                _match(stake_odds=(2.1, 1.2), polymarket_odds=(1.2, 2.1)),
            ]
        )

        cancel_risks = [
            opportunity["cancel_risk"] for opportunity in result["opportunities"]
        ]
        self.assertEqual(cancel_risks, [True, False])

    def test_swapped_orientation_maps_polymarket_odds_to_stake_team_order(self) -> None:
        result = calculate_arbitrage(
            [
                _match(
                    orientation="swapped",
                    polymarket_odds=(3.0, 1.4),
                )
            ]
        )

        opportunities = result["opportunities"]
        self.assertEqual(len(opportunities), 1)
        self.assertEqual(opportunities[0]["stake_team"], "Team Alpha")
        self.assertEqual(opportunities[0]["polymarket_team"], "Team Beta")
        self.assertEqual(opportunities[0]["polymarket_raw_odds"], 3.0)

    def test_swapped_orientation_also_works_when_stake_is_right_event(self) -> None:
        result = calculate_arbitrage(
            [
                _match(
                    orientation="swapped",
                    stake_left=False,
                    polymarket_odds=(3.0, 1.4),
                )
            ]
        )

        opportunities = result["opportunities"]
        self.assertEqual(len(opportunities), 1)
        self.assertEqual(opportunities[0]["stake_team"], "Team Alpha")
        self.assertEqual(opportunities[0]["polymarket_team"], "Team Beta")

    def test_polymarket_fee_adjustment_reduces_effective_odds(self) -> None:
        raw_odds = 3.0

        no_fee = polymarket_effective_decimal_odds(
            raw_odds,
            config=ArbitrageConfig(polymarket_fee_rate=0.0),
        )
        with_fee = polymarket_effective_decimal_odds(
            raw_odds,
            config=ArbitrageConfig(polymarket_fee_rate=0.02),
        )

        self.assertEqual(no_fee, raw_odds)
        self.assertLess(with_fee, raw_odds)
        self.assertAlmostEqual(with_fee, 2.96)

    def test_invalid_or_missing_odds_skip_safely_with_debug_reason(self) -> None:
        stake_result = calculate_arbitrage(
            [_match(stake_odds=("bad", None))]
        )
        polymarket_result = calculate_arbitrage(
            [_match(polymarket_odds=(None, "bad"))]
        )

        self.assertEqual(stake_result["opportunities"], [])
        self.assertEqual(
            stake_result["debug"]["reason_counts"]["invalid_stake_odds"],
            2,
        )
        self.assertEqual(polymarket_result["opportunities"], [])
        self.assertEqual(
            polymarket_result["debug"]["reason_counts"][
                "non_executable_polymarket_price"
            ],
            2,
        )

    def test_outcome_prices_without_executable_ask_do_not_create_opportunity(
        self,
    ) -> None:
        result = calculate_arbitrage(
            [_match(polymarket_executable=False)]
        )

        self.assertEqual(result["opportunities"], [])
        self.assertEqual(result["summary"]["opportunity_count"], 0)
        self.assertEqual(
            result["debug"]["reason_counts"]["non_executable_polymarket_price"],
            2,
        )

    def test_cli_table_output_works_from_snapshot_like_fixture(self) -> None:
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            result = arbitrage_main(
                [
                    "--matches-file",
                    str(FIXTURE_DIR / "arbitrage_matches.json"),
                    "--format",
                    "table",
                    "--debug",
                ]
            )

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("game", output)
        self.assertIn("stake_team", output)
        self.assertIn("pm_eff_odds", output)
        self.assertIn("Team Alpha", output)
        self.assertIn("Team Beta", output)
        self.assertIn("cancel_risk", output)
        self.assertIn("opportunity_count=1", output)

    def test_cli_json_output_works_from_snapshot_like_fixture(self) -> None:
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            result = arbitrage_main(
                [
                    "--matches-file",
                    str(FIXTURE_DIR / "arbitrage_matches.json"),
                    "--format",
                    "json",
                ]
            )

        self.assertEqual(result, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["summary"]["opportunity_count"], 1)
        self.assertEqual(payload["opportunities"][0]["stake_team"], "Team Alpha")


if __name__ == "__main__":
    unittest.main()
