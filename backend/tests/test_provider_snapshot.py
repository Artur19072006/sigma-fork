from __future__ import annotations

import io
import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from provider_odds.cli.snapshot import main as snapshot_main
from provider_odds.models import ProviderOddsEvent
from provider_odds.providers.oddspapi import (
    MAPPING_PROFILE_ODDSPAPI_STAKE_CS2_MARKET171_SELECTION_ORDER,
    MAPPING_PROFILE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER,
)
from provider_odds.snapshot import build_snapshot, load_event_file, run_snapshot_match


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _arbitrage_snapshot() -> dict[str, object]:
    return json.loads((FIXTURE_DIR / "arbitrage_matches.json").read_text(encoding="utf-8"))


class ProviderSnapshotTest(unittest.TestCase):
    def test_offline_snapshot_matches_one_swapped_event(self) -> None:
        left_events = load_event_file(FIXTURE_DIR / "snapshot_stake_events.json")
        right_events = load_event_file(FIXTURE_DIR / "snapshot_polymarket_events.json")

        snapshot = build_snapshot(left_events, right_events)

        self.assertEqual(len(snapshot["matches"]), 1)
        match = snapshot["matches"][0]
        self.assertEqual(match["orientation"], "swapped")
        self.assertEqual(match["left_event"]["provider"], "oddspapi")
        self.assertEqual(match["right_event"]["provider"], "polymarket")
        self.assertEqual(match["start_time_diff_minutes"], 15.0)
        self.assertEqual(match["team_score"], 1.0)

    def test_unmatched_and_summary_counts_are_correct(self) -> None:
        left_events = load_event_file(FIXTURE_DIR / "snapshot_stake_events.json")
        right_events = load_event_file(FIXTURE_DIR / "snapshot_polymarket_events.json")

        snapshot = build_snapshot(left_events, right_events)

        self.assertEqual(
            snapshot["summary"],
            {
                "left_count": 2,
                "right_count": 2,
                "matched_count": 1,
                "unmatched_left_count": 1,
                "unmatched_right_count": 1,
            },
        )
        self.assertEqual(snapshot["unmatched_left"][0]["team_a"], "Team Spirit")
        self.assertEqual(snapshot["unmatched_right"][0]["team_a"], "Natus Vincere")

    def test_cli_table_output_includes_summary_match_and_debug_unmatched(self) -> None:
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            result = snapshot_main(
                [
                    "--left-file",
                    str(FIXTURE_DIR / "snapshot_stake_events.json"),
                    "--right-file",
                    str(FIXTURE_DIR / "snapshot_polymarket_events.json"),
                    "--format",
                    "table",
                    "--debug",
                ]
            )

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("summary:", output)
        self.assertIn("left_count=2", output)
        self.assertIn("right_count=2", output)
        self.assertIn("matched_count=1", output)
        self.assertIn("unmatched_left_count=1", output)
        self.assertIn("unmatched_right_count=1", output)
        self.assertIn("paiN Academy / ALKA", output)
        self.assertIn("ALKA / paiN Academy", output)
        self.assertIn("swapped", output)
        self.assertIn("https://example.test/stake/cs2-paina-alkaa", output)
        self.assertIn("https://polymarket.com/event/cs2-paina-alkaa-2026-06-27", output)
        self.assertIn("unmatched_left_samples:", output)
        self.assertIn("Team Spirit / Gaimin Gladiators", output)
        self.assertIn("unmatched_right_samples:", output)
        self.assertIn("Natus Vincere / Vitality", output)
        self.assertNotIn("arbitrage:", output)
        self.assertNotIn("telegram:", output)

    def test_cli_json_output_is_valid_and_contains_matches(self) -> None:
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            result = snapshot_main(
                [
                    "--left-file",
                    str(FIXTURE_DIR / "snapshot_stake_events.json"),
                    "--right-file",
                    str(FIXTURE_DIR / "snapshot_polymarket_events.json"),
                    "--format",
                    "json",
                ]
            )

        self.assertEqual(result, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["summary"]["matched_count"], 1)
        self.assertEqual(len(payload["matches"]), 1)
        self.assertEqual(payload["matches"][0]["orientation"], "swapped")
        self.assertEqual(payload["unmatched_left"][0]["team_a"], "Team Spirit")
        self.assertEqual(payload["unmatched_right"][0]["team_b"], "Vitality")
        self.assertNotIn("arbitrage", payload)
        self.assertNotIn("telegram", payload)

    def test_cli_table_output_with_arbitrage_includes_opportunity_count(self) -> None:
        with patch(
            "provider_odds.cli.snapshot.run_snapshot_match",
            return_value=_arbitrage_snapshot(),
        ):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                result = snapshot_main(
                    [
                        "--left-file",
                        str(FIXTURE_DIR / "snapshot_stake_events.json"),
                        "--right-file",
                        str(FIXTURE_DIR / "snapshot_polymarket_events.json"),
                        "--format",
                        "table",
                        "--find-arbitrage",
                        "--polymarket-fee-rate",
                        "0",
                    ]
                )

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("summary:", output)
        self.assertIn("matched_count=1", output)
        self.assertIn("arbitrage:", output)
        self.assertIn("opportunity_count=1", output)
        self.assertIn("stake_team", output)
        self.assertIn("pm_eff_odds", output)
        self.assertIn("Team Alpha", output)
        self.assertIn("Team Beta", output)

    def test_cli_json_with_zero_fee_finds_zero_edge_arbitrage(self) -> None:
        with patch(
            "provider_odds.cli.snapshot.run_snapshot_match",
            return_value=_arbitrage_snapshot(),
        ):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                result = snapshot_main(
                    [
                        "--left-file",
                        str(FIXTURE_DIR / "snapshot_stake_events.json"),
                        "--right-file",
                        str(FIXTURE_DIR / "snapshot_polymarket_events.json"),
                        "--format",
                        "json",
                        "--find-arbitrage",
                        "--polymarket-fee-rate",
                        "0",
                    ]
                )

        self.assertEqual(result, 0)
        payload = json.loads(stdout.getvalue())
        self.assertIn("arbitrage", payload)
        self.assertEqual(payload["arbitrage"]["summary"]["opportunity_count"], 1)
        opportunity = payload["arbitrage"]["opportunities"][0]
        self.assertEqual(opportunity["stake_team"], "Team Alpha")
        self.assertEqual(opportunity["polymarket_team"], "Team Beta")
        self.assertAlmostEqual(opportunity["implied_sum"], 1.0)
        self.assertEqual(opportunity["edge_percent"], 0.0)

    def test_cli_json_with_default_fee_can_kill_zero_edge_arbitrage(self) -> None:
        with patch(
            "provider_odds.cli.snapshot.run_snapshot_match",
            return_value=_arbitrage_snapshot(),
        ):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                result = snapshot_main(
                    [
                        "--left-file",
                        str(FIXTURE_DIR / "snapshot_stake_events.json"),
                        "--right-file",
                        str(FIXTURE_DIR / "snapshot_polymarket_events.json"),
                        "--format",
                        "json",
                        "--find-arbitrage",
                    ]
                )

        self.assertEqual(result, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["arbitrage"]["summary"]["opportunity_count"], 0)
        self.assertEqual(payload["arbitrage"]["opportunities"], [])

    def test_cli_telegram_alert_requires_find_arbitrage(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            with self.assertRaises(SystemExit) as cm:
                snapshot_main(["--telegram-alert"])

        self.assertEqual(cm.exception.code, 2)
        self.assertIn(
            "--telegram-alert/--telegram-dry-run requires --find-arbitrage",
            stderr.getvalue(),
        )

    def test_cli_telegram_dry_run_prints_message_without_send(self) -> None:
        with patch(
            "provider_odds.cli.snapshot.run_snapshot_match",
            return_value=_arbitrage_snapshot(),
        ):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                result = snapshot_main(
                    [
                        "--left-file",
                        str(FIXTURE_DIR / "snapshot_stake_events.json"),
                        "--right-file",
                        str(FIXTURE_DIR / "snapshot_polymarket_events.json"),
                        "--format",
                        "table",
                        "--find-arbitrage",
                        "--polymarket-fee-rate",
                        "0",
                        "--telegram-dry-run",
                    ]
                )

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("telegram:", output)
        self.assertIn("dry_run=true", output)
        self.assertIn("message_count=1", output)
        self.assertIn("sent_count=0", output)
        self.assertIn("⚡ CS2 ARB", output)
        self.assertIn("Stake: Team Alpha @ 1.50", output)
        self.assertIn("Poly: Team Beta @ 3.00", output)

    def test_cli_telegram_json_no_opportunities_skips_send(self) -> None:
        with patch(
            "provider_odds.cli.snapshot.run_snapshot_match",
            return_value=_arbitrage_snapshot(),
        ):
            with patch.dict(os.environ, {}, clear=True):
                with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    result = snapshot_main(
                        [
                            "--left-file",
                            str(FIXTURE_DIR / "snapshot_stake_events.json"),
                            "--right-file",
                            str(FIXTURE_DIR / "snapshot_polymarket_events.json"),
                            "--format",
                            "json",
                            "--find-arbitrage",
                            "--telegram-alert",
                            "--telegram-bot-token-env",
                            "TEST_TELEGRAM_TOKEN",
                            "--telegram-chat-id-env",
                            "TEST_TELEGRAM_CHAT",
                        ]
                    )

        self.assertEqual(result, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["arbitrage"]["summary"]["opportunity_count"], 0)
        self.assertEqual(
            payload["telegram"],
            {
                "enabled": True,
                "dry_run": False,
                "message_count": 0,
                "sent_count": 0,
                "skipped_reason": "no_opportunities",
                "dedup_suppressed_count": 0,
                "dedup_sent_new_count": 0,
                "dedup_state_size": 0,
            },
        )

    def test_cli_telegram_json_dry_run_includes_status_block(self) -> None:
        with patch(
            "provider_odds.cli.snapshot.run_snapshot_match",
            return_value=_arbitrage_snapshot(),
        ):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                result = snapshot_main(
                    [
                        "--left-file",
                        str(FIXTURE_DIR / "snapshot_stake_events.json"),
                        "--right-file",
                        str(FIXTURE_DIR / "snapshot_polymarket_events.json"),
                        "--format",
                        "json",
                        "--find-arbitrage",
                        "--polymarket-fee-rate",
                        "0",
                        "--telegram-dry-run",
                    ]
                )

        self.assertEqual(result, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(
            payload["telegram"],
            {
                "enabled": True,
                "dry_run": True,
                "message_count": 1,
                "sent_count": 0,
                "skipped_reason": None,
                "dedup_suppressed_count": 0,
                "dedup_sent_new_count": 0,
                "dedup_state_size": 0,
            },
        )

    def test_cli_telegram_missing_env_in_real_mode_returns_clear_error(self) -> None:
        with patch(
            "provider_odds.cli.snapshot.run_snapshot_match",
            return_value=_arbitrage_snapshot(),
        ):
            with patch.dict(os.environ, {}, clear=True):
                with patch("sys.stderr", new_callable=io.StringIO) as stderr:
                    result = snapshot_main(
                        [
                            "--left-file",
                            str(FIXTURE_DIR / "snapshot_stake_events.json"),
                            "--right-file",
                            str(FIXTURE_DIR / "snapshot_polymarket_events.json"),
                            "--format",
                            "json",
                            "--find-arbitrage",
                            "--polymarket-fee-rate",
                            "0",
                            "--telegram-alert",
                            "--telegram-bot-token-env",
                            "TEST_TELEGRAM_TOKEN",
                            "--telegram-chat-id-env",
                            "TEST_TELEGRAM_CHAT",
                        ]
                    )

        self.assertEqual(result, 2)
        self.assertIn(
            "Missing TEST_TELEGRAM_TOKEN / TEST_TELEGRAM_CHAT",
            stderr.getvalue(),
        )

    def test_missing_cli_inputs_returns_clear_error(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            result = snapshot_main([])

        self.assertEqual(result, 2)
        self.assertIn("Missing --left-provider or --left-file", stderr.getvalue())

    def test_provider_mode_argument_parsing_works_without_live_calls(self) -> None:
        fake_snapshot = {
            "left_events": [],
            "right_events": [],
            "matches": [],
            "unmatched_left": [],
            "unmatched_right": [],
            "summary": {
                "left_count": 0,
                "right_count": 0,
                "matched_count": 0,
                "unmatched_left_count": 0,
                "unmatched_right_count": 0,
            },
        }
        with patch(
            "provider_odds.cli.snapshot.run_snapshot_match",
            return_value=fake_snapshot,
        ) as runner:
            with patch("sys.stdout", new_callable=io.StringIO):
                result = snapshot_main(
                    [
                        "--left-provider",
                        "oddspapi",
                        "--left-sport",
                        "dota2",
                        "--left-bookmaker",
                        "stake",
                        "--left-tournament-id",
                        "101,102",
                        "--left-tournament-id",
                        "103",
                        "--left-mapping-profile",
                        MAPPING_PROFILE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER,
                        "--right-provider",
                        "polymarket",
                        "--right-sport",
                        "cs2",
                        "--right-event-slug",
                        "slug-a,slug-b",
                        "--right-market-slug",
                        "slug-c",
                    ]
                )

        self.assertEqual(result, 0)
        kwargs = runner.call_args.kwargs
        self.assertIsNone(kwargs["left_events"])
        self.assertIsNone(kwargs["right_events"])
        self.assertEqual(kwargs["left_provider"], "oddspapi")
        self.assertEqual(kwargs["left_sport"], "dota2")
        self.assertEqual(kwargs["left_bookmaker"], "stake")
        self.assertEqual(kwargs["left_tournament_id"], "101,102,103")
        self.assertEqual(
            kwargs["left_mapping_profile"],
            MAPPING_PROFILE_ODDSPAPI_STAKE_DOTA2_MARKET161_SELECTION_ORDER,
        )
        self.assertEqual(kwargs["right_provider"], "polymarket")
        self.assertEqual(kwargs["right_sport"], "cs2")
        self.assertEqual(kwargs["right_event_slug"], "slug-a,slug-b")
        self.assertEqual(kwargs["right_market_slug"], "slug-c")

    def test_provider_mode_accepts_cs2_mapping_profile_without_live_calls(self) -> None:
        fake_snapshot = {
            "left_events": [],
            "right_events": [],
            "matches": [],
            "unmatched_left": [],
            "unmatched_right": [],
            "summary": {
                "left_count": 0,
                "right_count": 0,
                "matched_count": 0,
                "unmatched_left_count": 0,
                "unmatched_right_count": 0,
            },
        }
        with patch(
            "provider_odds.cli.snapshot.run_snapshot_match",
            return_value=fake_snapshot,
        ) as runner:
            with patch("sys.stdout", new_callable=io.StringIO):
                result = snapshot_main(
                    [
                        "--left-provider",
                        "oddspapi",
                        "--left-sport",
                        "cs2",
                        "--left-bookmaker",
                        "stake",
                        "--left-mapping-profile",
                        MAPPING_PROFILE_ODDSPAPI_STAKE_CS2_MARKET171_SELECTION_ORDER,
                        "--right-provider",
                        "polymarket",
                        "--right-sport",
                        "cs2",
                        "--right-market-slug",
                        "slug-c",
                    ]
                )

        self.assertEqual(result, 0)
        self.assertEqual(
            runner.call_args.kwargs["left_mapping_profile"],
            MAPPING_PROFILE_ODDSPAPI_STAKE_CS2_MARKET171_SELECTION_ORDER,
        )

    def test_run_snapshot_match_provider_mode_uses_mocked_providers(self) -> None:
        calls: dict[str, list[dict[str, object]]] = {"left": [], "right": []}

        class FakeOddsPapiProvider:
            def fetch_events(self, **kwargs: object) -> list[ProviderOddsEvent]:
                calls["left"].append(kwargs)
                if kwargs.get("tournament_id") == "102":
                    return [
                        ProviderOddsEvent(
                            provider="oddspapi",
                            bookmaker="stake",
                            game="dota2",
                            tournament="DreamLeague",
                            start_time=datetime(
                                2026,
                                6,
                                28,
                                15,
                                0,
                                tzinfo=timezone.utc,
                            ),
                            team_a="Team Spirit",
                            odds_a=1.74,
                            team_b="Gaimin Gladiators",
                            odds_b=2.08,
                            market_type="h2h",
                            event_url="https://example.test/stake-dota2",
                            raw={},
                        )
                    ]
                return [
                    ProviderOddsEvent(
                        provider="oddspapi",
                        bookmaker="stake",
                        game="cs2",
                        tournament="CCT",
                        start_time=datetime(2026, 6, 27, 21, 0, tzinfo=timezone.utc),
                        team_a="paiN Academy",
                        odds_a=2.12,
                        team_b="ALKA",
                        odds_b=1.72,
                        market_type="h2h",
                        event_url="https://example.test/stake",
                        raw={},
                    )
                ]

        class FakePolymarketProvider:
            def fetch_events(self, **kwargs: object) -> list[ProviderOddsEvent]:
                calls["right"].append(kwargs)
                if kwargs.get("event_slug") == "slug-a":
                    return [
                        ProviderOddsEvent(
                            provider="polymarket",
                            bookmaker="polymarket",
                            game="cs2",
                            tournament="BLAST",
                            start_time=datetime(
                                2026,
                                7,
                                2,
                                19,
                                0,
                                tzinfo=timezone.utc,
                            ),
                            team_a="Natus Vincere",
                            odds_a=2.08,
                            team_b="Vitality",
                            odds_b=1.92,
                            market_type="h2h",
                            event_url="https://polymarket.com/event/navi-vitality",
                            raw={},
                        )
                    ]
                return [
                    ProviderOddsEvent(
                        provider="polymarket",
                        bookmaker="polymarket",
                        game="counter-strike",
                        tournament="CCT",
                        start_time=datetime(2026, 6, 27, 21, 15, tzinfo=timezone.utc),
                        team_a="ALKA",
                        odds_a=1.83,
                        team_b="paiN Academy",
                        odds_b=2.20,
                        market_type="h2h",
                        event_url="https://polymarket.com/event/cs2-paina-alkaa",
                        raw={},
                    )
                ]

        snapshot = run_snapshot_match(
            left_provider="oddspapi",
            left_sport="cs2",
            left_bookmaker="stake",
            left_tournament_id="101,102",
            right_provider="polymarket",
            right_sport="cs2",
            right_event_slug="slug-a",
            right_market_slug="slug-b",
            provider_factories={
                "oddspapi": lambda _env: FakeOddsPapiProvider(),
                "polymarket": lambda _env: FakePolymarketProvider(),
            },
        )

        self.assertEqual(snapshot["summary"]["matched_count"], 1)
        self.assertEqual(snapshot["summary"]["unmatched_left_count"], 1)
        self.assertEqual(snapshot["summary"]["unmatched_right_count"], 1)
        self.assertEqual(len(calls["left"]), 2)
        self.assertEqual(calls["left"][0]["tournament_id"], "101")
        self.assertEqual(calls["left"][1]["tournament_id"], "102")
        self.assertEqual(len(calls["right"]), 2)
        self.assertEqual(calls["right"][0]["market_slug"], "slug-b")
        self.assertEqual(calls["right"][1]["event_slug"], "slug-a")


if __name__ == "__main__":
    unittest.main()
