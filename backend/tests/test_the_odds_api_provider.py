from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from provider_odds.models import ProviderOddsEvent
from provider_odds.providers.the_odds_api import TheOddsApiProvider


class TheOddsApiProviderTest(unittest.TestCase):
    def test_h2h_fixture_normalizes_to_provider_odds_event(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "the_odds_api_h2h.json"
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))

        provider = TheOddsApiProvider(api_key="offline-test")
        events = provider.normalize_response(payload)

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertIsInstance(event, ProviderOddsEvent)
        self.assertEqual(event.provider, "the_odds_api")
        self.assertEqual(event.bookmaker, "DraftKings")
        self.assertEqual(event.game, "League of Legends")
        self.assertIsNone(event.tournament)
        self.assertEqual(
            event.start_time,
            datetime(2026, 6, 27, 18, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(event.team_a, "Blue Side")
        self.assertEqual(event.team_b, "Red Side")
        self.assertEqual(event.market_type, "h2h")
        self.assertEqual(event.odds_a, 1.82)
        self.assertEqual(event.odds_b, 2.04)
        self.assertIsNone(event.event_url)
        self.assertEqual(event.raw["event"]["id"], "evt_001")


if __name__ == "__main__":
    unittest.main()
