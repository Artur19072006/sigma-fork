from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from dashboard.api.publish_snapshot import publish
from dashboard.api.server import build_server
from dashboard.api.state import DashboardRepository, normalize_runtime_report


DEMO = Path(__file__).resolve().parents[1] / "data" / "demo-state.json"


class DashboardRepositoryTests(unittest.TestCase):
    def test_missing_live_state_uses_explicit_demo_projection(self) -> None:
        repository = DashboardRepository(
            health_path=None,
            state_path=None,
            demo_path=DEMO,
            demo_enabled=True,
        )

        result = repository.snapshot(
            now=datetime(2026, 9, 20, 14, 32, tzinfo=timezone.utc)
        )

        self.assertEqual(result["mode"], "demo")
        self.assertEqual(result["scanner"]["state"], "demo")
        self.assertGreater(len(result["active_opportunities"]), 0)
        self.assertIn("Sample opportunities", result["notice"])

    def test_missing_production_state_is_live_and_honestly_empty(self) -> None:
        result = DashboardRepository(
            health_path=None,
            state_path=None,
            demo_path=DEMO,
        ).snapshot(now=datetime(2026, 9, 20, 14, 32, tzinfo=timezone.utc))

        self.assertEqual(result["mode"], "live")
        self.assertEqual(result["active_opportunities"], [])
        self.assertEqual(result["recent_opportunities"], [])
        self.assertNotIn("Sample", result["notice"])

    def test_live_projection_is_allowlisted_and_stale_health_is_offline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            health = root / "health.json"
            state = root / "state.json"
            now = datetime(2026, 9, 20, 14, 32, tzinfo=timezone.utc)
            health.write_text(
                json.dumps(
                    {
                        "status": "healthy",
                        "ready": True,
                        "cycles": 12,
                        "failures": 1,
                        "consecutive_failures": 0,
                        "last_cycle_at": (now - timedelta(minutes=5)).isoformat(),
                        "TELEGRAM_BOT_TOKEN": "must-never-leak",
                    }
                ),
                encoding="utf-8",
            )
            state.write_text(
                json.dumps(
                    {
                        "coverage": [{"sport": "CS2", "duel_events": 4}],
                        "opportunities": [],
                        "secret": "must-never-leak",
                    }
                ),
                encoding="utf-8",
            )
            repository = DashboardRepository(
                health_path=health,
                state_path=state,
                demo_path=DEMO,
                stale_after_seconds=120,
            )

            result = repository.snapshot(now=now)

            self.assertEqual(result["mode"], "live")
            self.assertEqual(result["scanner"]["state"], "offline")
            self.assertEqual(result["summary"]["duel_event_count"], 4)
            encoded = json.dumps(result)
            self.assertNotIn("must-never-leak", encoded)
            self.assertNotIn("TELEGRAM_BOT_TOKEN", encoded)

    def test_fresh_healthy_runtime_is_online(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = datetime(2026, 9, 20, 14, 32, tzinfo=timezone.utc)
            health = root / "health.json"
            state = root / "state.json"
            health.write_text(
                json.dumps(
                    {
                        "status": "healthy",
                        "ready": True,
                        "cycles": 41,
                        "last_cycle_at": (now - timedelta(seconds=18)).isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            state.write_text('{"coverage":[],"opportunities":[]}', encoding="utf-8")

            result = DashboardRepository(
                health_path=health,
                state_path=state,
                demo_path=DEMO,
            ).snapshot(now=now)

            self.assertTrue(result["scanner"]["online"])
            self.assertEqual(result["scanner"]["cycle_count"], 41)

    def test_runtime_report_normalization_preserves_only_dashboard_fields(self) -> None:
        report = {
            "rows": [
                {"sport": "CS2"},
                {"sport": "League of Legends"},
            ],
            "evaluations": [
                {
                    "opportunities": [
                        {
                            "identity": {
                                "market": {
                                    "match": {
                                        "esport": "CS2",
                                        "tournament": "BLAST Open",
                                        "team_a": "Vitality",
                                        "team_b": "FURIA",
                                        "scheduled_start": "2026-09-20T18:30:00Z",
                                    }
                                },
                                "bookmaker_selection": "FURIA",
                                "polymarket_selection": "Vitality",
                            },
                            "status": "active",
                            "observed_at": "2026-09-20T14:30:00Z",
                            "bookmaker_odds": 2.12,
                            "polymarket_price": 0.458,
                            "edge_percent": 1.42,
                            "polymarket_size": 80,
                            "polymarket_taker_fee": 0.01,
                            "data_age_seconds": 11,
                            "token": "must-never-leak",
                        }
                    ]
                }
            ],
            "polymarket_discovery": [{"sport": "CS2"}],
            "candidate_attempts": [{"accepted": True}],
            "TELEGRAM_BOT_TOKEN": "must-never-leak",
        }

        state = normalize_runtime_report(
            report,
            now=datetime(2026, 9, 20, 14, 32, tzinfo=timezone.utc),
            duration_seconds=8.4,
        )

        self.assertEqual(state["coverage"][0]["duel_events"], 1)
        self.assertEqual(len(state["opportunities"]), 1)
        self.assertEqual(state["opportunities"][0]["duel_side"], "FURIA")
        self.assertEqual(
            state["opportunities"][0]["start_time"],
            "2026-09-20T18:30:00Z",
        )
        self.assertNotIn("must-never-leak", json.dumps(state))

    def test_runtime_report_counts_collector_league_of_legends_rows(self) -> None:
        state = normalize_runtime_report(
            {
                "rows": [{"sport": "league_of_legends"}],
                "evaluations": [{"sport": "league_of_legends", "bindings": []}],
            },
            now=datetime(2026, 9, 24, 17, 0, tzinfo=timezone.utc),
        )

        lol = next(item for item in state["coverage"] if item["sport"] == "League of Legends")
        self.assertEqual(lol["duel_events"], 1)
        self.assertEqual(lol["evaluator_runs"], 1)

    def test_lifecycle_refreshes_active_then_expires_missing_identity(self) -> None:
        def report(observed: str, *, current: bool) -> dict[str, object]:
            opportunity = {
                "identity": {
                    "market": {
                        "match": {
                            "esport": "CS2",
                            "tournament": "BLAST Open",
                            "team_a": "Vitality",
                            "team_b": "FURIA",
                            "scheduled_start": "2026-09-20T18:30:00Z",
                        }
                    },
                    "bookmaker_selection": "FURIA",
                    "polymarket_selection": "Vitality",
                },
                "status": "active",
                "observed_at": observed,
                "bookmaker_odds": 2.12,
                "polymarket_price": 0.458,
                "edge_percent": 1.42,
                "polymarket_size": 80,
                "polymarket_taker_fee": 0.01,
                "data_age_seconds": 11,
            }
            return {
                "rows": [{"sport": "CS2"}],
                "evaluations": [
                    {
                        "sport": "CS2",
                        "current_opportunities": [opportunity] if current else [],
                        "opportunities": [],
                    }
                ],
            }

        first = normalize_runtime_report(
            report("2026-09-20T14:30:00Z", current=True),
            now=datetime(2026, 9, 20, 14, 30, tzinfo=timezone.utc),
        )
        refreshed = normalize_runtime_report(
            report("2026-09-20T14:31:00Z", current=True),
            previous=first,
            now=datetime(2026, 9, 20, 14, 31, tzinfo=timezone.utc),
        )
        expired = normalize_runtime_report(
            report("2026-09-20T14:32:00Z", current=False),
            previous=refreshed,
            now=datetime(2026, 9, 20, 14, 32, tzinfo=timezone.utc),
        )

        self.assertEqual(
            refreshed["opportunities"][0]["first_detected_at"],
            "2026-09-20T14:30:00Z",
        )
        self.assertEqual(
            refreshed["opportunities"][0]["last_seen_at"],
            "2026-09-20T14:31:00Z",
        )
        self.assertEqual(expired["opportunities"][0]["status"], "expired")
        self.assertEqual(
            expired["opportunities"][0]["status_reason"],
            "not_present_in_completed_cycle",
        )

    def test_publisher_is_atomic_bounded_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "state.json"
            publish({"rows": [], "evaluations": []}, output, duration=1.25)

            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["opportunities"], [])
            self.assertEqual(payload["cycle"]["duration_seconds"], 1.25)
            self.assertEqual(list(output.parent.glob(".state.json.*.tmp")), [])
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o640)


class DashboardServerTests(unittest.TestCase):
    def test_non_loopback_bind_is_rejected(self) -> None:
        repository = DashboardRepository(
            health_path=None,
            state_path=None,
            demo_path=DEMO,
        )
        with self.assertRaisesRegex(ValueError, "loopback"):
            build_server(
                host="0.0.0.0",
                port=0,
                repository=repository,
                static_dir=None,
            )


if __name__ == "__main__":
    unittest.main()
