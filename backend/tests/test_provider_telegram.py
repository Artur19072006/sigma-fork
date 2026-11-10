from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from provider_odds.telegram import (
    TELEGRAM_API_BASE_URL,
    DEFAULT_TELEGRAM_REALERT_EDGE_IMPROVEMENT,
    TelegramConfigurationError,
    build_arbitrage_alert_messages,
    format_arbitrage_alert,
    send_arbitrage_alerts,
)


def _opportunity(**overrides: object) -> dict[str, object]:
    opportunity: dict[str, object] = {
        "game": "cs2",
        "start_time": "2026-06-28T16:00:00+00:00",
        "team_a": "Team A",
        "team_b": "Team B",
        "stake_team": "Team A",
        "stake_odds": 1.5,
        "polymarket_team": "Team B",
        "polymarket_raw_odds": 3.0,
        "polymarket_effective_odds": 3.0,
        "edge_percent": 0.0,
        "cancel_risk": False,
        "stake_url": "https://example.test/stake/team-a-team-b",
        "polymarket_url": "https://polymarket.com/event/team-a-team-b",
    }
    opportunity.update(overrides)
    return opportunity


class FakeResponse:
    def read(self) -> bytes:
        return b'{"ok": true}'

    def close(self) -> None:
        return None


def _real_send(
    opportunities: list[dict[str, object]],
    *,
    state_path: Path,
    now: datetime,
    opener: object | None = None,
    ttl_hours: float = 6,
) -> dict[str, object]:
    opener = opener or Mock(return_value=FakeResponse())
    with patch.dict(
        os.environ,
        {
            "TEST_TELEGRAM_TOKEN": "secret-token",
            "TEST_TELEGRAM_CHAT": "12345",
        },
        clear=False,
    ):
        return send_arbitrage_alerts(
            opportunities,
            dry_run=False,
            bot_token_env="TEST_TELEGRAM_TOKEN",
            chat_id_env="TEST_TELEGRAM_CHAT",
            env_file=Path("missing.env"),
            dedup_state_path=state_path,
            dedup_ttl_hours=ttl_hours,
            now_func=lambda: now,
            opener=opener,
        )


class ProviderTelegramTest(unittest.TestCase):
    def test_format_arbitrage_alert_is_compact(self) -> None:
        opportunity = _opportunity()
        message = format_arbitrage_alert(opportunity)

        self.assertEqual(
            message,
            "⚡ CS2 ARB\n\n"
            "Team A vs Team B\n\n"
            "Stake: Team A @ 1.50\n"
            "Poly: Team B @ 3.00",
        )
        self.assertEqual(
            opportunity["polymarket_url"],
            "https://polymarket.com/event/team-a-team-b",
        )
        self.assertNotIn("[V2 ARB FOUND]", message)
        self.assertNotIn("Start:", message)
        self.assertNotIn("Edge after PM fee", message)
        self.assertNotIn("Cancel risk", message)
        self.assertNotIn("https://", message)

    def test_format_arbitrage_alert_uses_effective_polymarket_odds(self) -> None:
        message = format_arbitrage_alert(
            _opportunity(
                polymarket_raw_odds=1.32,
                polymarket_effective_odds=1.306316,
            )
        )

        self.assertIn("Poly: Team B @ 1.31", message)
        self.assertNotIn("Poly: Team B @ 1.32", message)
        self.assertIn("Stake: Team A @ 1.50", message)

    def test_format_arbitrage_alert_cancel_risk_yes(self) -> None:
        message = format_arbitrage_alert(
            _opportunity(
                cancel_risk=True,
                polymarket_raw_odds=1.8,
                polymarket_effective_odds=1.72,
            )
        )

        self.assertEqual(
            message,
            "⚡ CS2 ARB\n\n"
            "Team A vs Team B\n\n"
            "Stake: Team A @ 1.50\n"
            "Poly: Team B @ 1.72\n\n"
            "Cancel risk",
        )
        self.assertNotIn("1.80", message)

    def test_format_arbitrage_alert_cancel_risk_no_string_is_hidden(self) -> None:
        message = format_arbitrage_alert(_opportunity(cancel_risk="no"))

        self.assertNotIn("Cancel risk", message)

    def test_no_opportunities_skips_send(self) -> None:
        opener = Mock()

        result = send_arbitrage_alerts(
            [],
            dry_run=False,
            bot_token_env="TEST_TELEGRAM_TOKEN",
            chat_id_env="TEST_TELEGRAM_CHAT",
            env_file=Path("missing.env"),
            opener=opener,
        )

        self.assertEqual(
            result,
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
        opener.assert_not_called()

    def test_dry_run_builds_message_without_http_call(self) -> None:
        opener = Mock()

        result = send_arbitrage_alerts(
            [_opportunity()],
            dry_run=True,
            bot_token_env="TEST_TELEGRAM_TOKEN",
            chat_id_env="TEST_TELEGRAM_CHAT",
            env_file=Path("missing.env"),
            opener=opener,
        )

        self.assertEqual(result["message_count"], 1)
        self.assertEqual(result["sent_count"], 0)
        self.assertIsNone(result["skipped_reason"])
        self.assertEqual(result["dedup_suppressed_count"], 0)
        self.assertEqual(result["dedup_sent_new_count"], 0)
        self.assertEqual(result["dedup_state_size"], 0)
        opener.assert_not_called()
        self.assertEqual(len(build_arbitrage_alert_messages([_opportunity()])), 1)

    def test_real_mode_uses_telegram_endpoint_without_returning_token(self) -> None:
        sent_requests = []

        class FakeResponse:
            def read(self) -> bytes:
                return b'{"ok": true}'

            def close(self) -> None:
                return None

        def opener(request: object, *, timeout: int) -> FakeResponse:
            sent_requests.append((request, timeout))
            return FakeResponse()

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "telegram_sent_opportunities.json"
            with patch.dict(
                os.environ,
                {
                    "TEST_TELEGRAM_TOKEN": "secret-token",
                    "TEST_TELEGRAM_CHAT": "12345",
                },
                clear=False,
            ):
                result = send_arbitrage_alerts(
                    [_opportunity()],
                    dry_run=False,
                    bot_token_env="TEST_TELEGRAM_TOKEN",
                    chat_id_env="TEST_TELEGRAM_CHAT",
                    env_file=Path("missing.env"),
                    dedup_state_path=state_path,
                    opener=opener,
                )

        self.assertEqual(result["message_count"], 1)
        self.assertEqual(result["sent_count"], 1)
        self.assertNotIn("secret-token", json.dumps(result))
        self.assertEqual(len(sent_requests), 1)
        request, timeout = sent_requests[0]
        self.assertEqual(
            request.full_url,
            f"{TELEGRAM_API_BASE_URL}/botsecret-token/sendMessage",
        )
        self.assertEqual(timeout, 20)
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["chat_id"], "12345")
        self.assertIn("⚡ CS2 ARB", payload["text"])
        self.assertNotIn("Start:", payload["text"])
        self.assertNotIn("Edge after PM fee", payload["text"])
        self.assertNotIn("https://", payload["text"])
        self.assertNotIn("secret-token", payload["text"])

    def test_first_opportunity_sends_and_writes_dedup_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "telegram_sent_opportunities.json"
            opener = Mock(return_value=FakeResponse())

            result = _real_send(
                [_opportunity(edge_percent=1.0)],
                state_path=state_path,
                now=datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc),
                opener=opener,
            )

            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(result["sent_count"], 1)
        self.assertEqual(result["dedup_sent_new_count"], 1)
        self.assertEqual(result["dedup_suppressed_count"], 0)
        self.assertEqual(result["dedup_state_size"], 1)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(len(state["opportunities"]), 1)
        self.assertNotIn("secret-token", json.dumps(state))

    def test_same_opportunity_repeated_within_ttl_is_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "telegram_sent_opportunities.json"
            first_opener = Mock(return_value=FakeResponse())
            second_opener = Mock(return_value=FakeResponse())
            first_time = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
            _real_send(
                [_opportunity(edge_percent=1.0)],
                state_path=state_path,
                now=first_time,
                opener=first_opener,
            )

            result = _real_send(
                [_opportunity(edge_percent=1.0)],
                state_path=state_path,
                now=first_time + timedelta(hours=1),
                opener=second_opener,
            )

        self.assertEqual(result["sent_count"], 0)
        self.assertEqual(result["dedup_suppressed_count"], 1)
        self.assertEqual(result["dedup_sent_new_count"], 0)
        self.assertEqual(result["skipped_reason"], "dedup_suppressed")
        second_opener.assert_not_called()

    def test_same_opportunity_after_ttl_sends_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "telegram_sent_opportunities.json"
            first_time = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
            _real_send(
                [_opportunity(edge_percent=1.0)],
                state_path=state_path,
                now=first_time,
            )
            opener = Mock(return_value=FakeResponse())

            result = _real_send(
                [_opportunity(edge_percent=1.0)],
                state_path=state_path,
                now=first_time + timedelta(hours=7),
                opener=opener,
            )

        self.assertEqual(result["sent_count"], 1)
        self.assertEqual(result["dedup_suppressed_count"], 0)
        self.assertEqual(opener.call_count, 1)

    def test_dry_run_does_not_write_dedup_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "telegram_sent_opportunities.json"
            opener = Mock()

            result = send_arbitrage_alerts(
                [_opportunity()],
                dry_run=True,
                dedup_state_path=state_path,
                opener=opener,
            )

            self.assertFalse(state_path.exists())

        self.assertEqual(result["message_count"], 1)
        self.assertEqual(result["sent_count"], 0)
        opener.assert_not_called()

    def test_different_opportunity_sends(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "telegram_sent_opportunities.json"
            first_time = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
            _real_send(
                [_opportunity(edge_percent=1.0)],
                state_path=state_path,
                now=first_time,
            )
            opener = Mock(return_value=FakeResponse())

            result = _real_send(
                [
                    _opportunity(
                        edge_percent=1.0,
                        team_a="Team C",
                        team_b="Team D",
                        stake_team="Team C",
                        polymarket_team="Team D",
                        polymarket_url="https://polymarket.com/event/team-c-team-d",
                    )
                ],
                state_path=state_path,
                now=first_time + timedelta(hours=1),
                opener=opener,
            )

        self.assertEqual(result["sent_count"], 1)
        self.assertEqual(result["dedup_state_size"], 2)
        self.assertEqual(opener.call_count, 1)

    def test_same_opportunity_with_small_odds_or_edge_change_is_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "telegram_sent_opportunities.json"
            first_time = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
            _real_send(
                [_opportunity(edge_percent=1.0, stake_odds=1.5)],
                state_path=state_path,
                now=first_time,
            )
            opener = Mock(return_value=FakeResponse())

            result = _real_send(
                [
                    _opportunity(
                        edge_percent=1.2,
                        stake_odds=1.55,
                        polymarket_raw_odds=2.95,
                    )
                ],
                state_path=state_path,
                now=first_time + timedelta(hours=1),
                opener=opener,
            )

        self.assertEqual(result["sent_count"], 0)
        self.assertEqual(result["dedup_suppressed_count"], 1)
        opener.assert_not_called()

    def test_same_opportunity_with_large_edge_improvement_sends_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "telegram_sent_opportunities.json"
            first_time = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
            _real_send(
                [_opportunity(edge_percent=1.0)],
                state_path=state_path,
                now=first_time,
            )
            opener = Mock(return_value=FakeResponse())

            result = _real_send(
                [
                    _opportunity(
                        edge_percent=1.0 + DEFAULT_TELEGRAM_REALERT_EDGE_IMPROVEMENT,
                        stake_odds=1.55,
                    )
                ],
                state_path=state_path,
                now=first_time + timedelta(hours=1),
                opener=opener,
            )

        self.assertEqual(result["sent_count"], 1)
        self.assertEqual(result["dedup_suppressed_count"], 0)
        self.assertEqual(result["dedup_sent_new_count"], 1)
        self.assertEqual(opener.call_count, 1)

    def test_missing_env_in_real_mode_raises_clear_error(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(
                    TelegramConfigurationError,
                    "Missing TEST_TELEGRAM_TOKEN / TEST_TELEGRAM_CHAT",
                ):
                    send_arbitrage_alerts(
                        [_opportunity()],
                        dry_run=False,
                        bot_token_env="TEST_TELEGRAM_TOKEN",
                        chat_id_env="TEST_TELEGRAM_CHAT",
                        env_file=Path("missing.env"),
                        dedup_state_path=Path(tmp) / "telegram_sent_opportunities.json",
                        opener=Mock(),
                    )


if __name__ == "__main__":
    unittest.main()
