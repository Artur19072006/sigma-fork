from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from provider_odds.cli.scan import (
    build_polymarket_keyset_url,
    discover_polymarket_page_preseed_events,
    discover_polymarket_preseed_events,
    discover_polymarket_page_slugs,
    discover_polymarket_keyset_events,
    extract_polymarket_page_slug_candidates,
    filter_polymarket_events_by_tournament_quality,
    format_scan_table,
    generate_polymarket_team_searches,
    main as scan_main,
    run_polymarket_candidate_diagnostic,
    run_scan,
    _score_stake_tournament_candidates,
    _score_strict_tournament_candidates,
    _tournament_quality_for_sport,
)
from provider_odds.models import ProviderOddsEvent
from provider_odds.providers.base import ProviderRequestError
from provider_odds.providers.oddspapi import (
    MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER,
)
from provider_odds.providers.polymarket import PolymarketProvider
from provider_odds.snapshot import load_event_file


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _event_from_dict(data: dict[str, object]) -> ProviderOddsEvent:
    start_time = data.get("start_time")
    if isinstance(start_time, str) and start_time.endswith("Z"):
        start_time = f"{start_time[:-1]}+00:00"
    return ProviderOddsEvent(
        provider=str(data["provider"]),
        bookmaker=str(data["bookmaker"]),
        game=str(data["game"]),
        tournament=data.get("tournament") if isinstance(data.get("tournament"), str) else None,
        start_time=(
            datetime.fromisoformat(start_time)
            if isinstance(start_time, str) and start_time
            else None
        ),
        team_a=str(data["team_a"]),
        odds_a=float(data["odds_a"]),
        team_b=str(data["team_b"]),
        odds_b=float(data["odds_b"]),
        market_type=str(data["market_type"]),
        event_url=data.get("event_url") if isinstance(data.get("event_url"), str) else None,
        raw=data.get("raw") if isinstance(data.get("raw"), dict) else {},
    )


def _snapshot_fixture_events() -> tuple[list[ProviderOddsEvent], list[ProviderOddsEvent]]:
    stake = [
        _event_from_dict(event)
        for event in load_event_file(FIXTURE_DIR / "snapshot_stake_events.json")
    ]
    polymarket = [
        _event_from_dict(event)
        for event in load_event_file(FIXTURE_DIR / "snapshot_polymarket_events.json")
    ]
    return stake, polymarket


def _arbitrage_fixture_events() -> tuple[list[ProviderOddsEvent], list[ProviderOddsEvent]]:
    payload = json.loads((FIXTURE_DIR / "arbitrage_matches.json").read_text(encoding="utf-8"))
    match = payload["matches"][0]
    return (
        [_event_from_dict(match["left_event"])],
        [_event_from_dict(match["right_event"])],
    )


def _keyset_payload(events: list[dict[str, object]]) -> dict[str, object]:
    return {"events": events}


def _keyset_payload_with_cursor(
    events: list[dict[str, object]],
    cursor: str,
) -> dict[str, object]:
    payload = _keyset_payload(events)
    payload["after_cursor"] = cursor
    return payload


def _keyset_match_event(
    *,
    sport_title: str,
    slug: str,
    team_a: str,
    team_b: str,
    category: str,
    tournament: str = "Test Series",
    start_time: str = "2026-06-28T16:00:00Z",
) -> dict[str, object]:
    title = f"{sport_title}: {team_a} vs {team_b} (BO3) - {tournament}"
    return {
        "id": f"pm-event-{slug}",
        "title": title,
        "slug": slug,
        "active": True,
        "closed": False,
        "startTime": start_time,
        "category": category,
        "tags": [{"label": category, "slug": category.casefold().replace(" ", "-")}],
        "markets": [
            {
                "id": f"pm-market-{slug}",
                "conditionId": f"pm-cond-{slug}",
                "question": title,
                "slug": slug,
                "active": True,
                "closed": False,
                "outcomes": json.dumps([team_a, team_b]),
                "outcomePrices": json.dumps(["0.50", "0.50"]),
            }
        ],
    }


DOTA2_EPL_PAGE_SLUG = "dota2-team-spirit-vs-gaimin-gladiators-epl-2026-07-10"
DOTA2_EPL_MARKET_SLUG = "dota2-team-spirit-vs-gaimin-gladiators-epl-market"
DOTA2_EWC_BO2_PAGE_SLUG = "dota2-team-falcons-vs-betboom-ewc-bo2-2026-07-10"
DOTA2_HOK_PAGE_SLUG = "hok-bue-tf-2026-07-10"


def _dota2_page_slug_payloads() -> tuple[dict[str, object], dict[str, object]]:
    epl = _keyset_match_event(
        sport_title="Dota 2",
        slug=DOTA2_EPL_PAGE_SLUG,
        team_a="Team Spirit",
        team_b="Gaimin Gladiators",
        category="Dota 2",
        tournament="European Pro League Group A",
        start_time="2026-07-10T16:00:00Z",
    )
    ewc_bo2 = _keyset_match_event(
        sport_title="Dota 2",
        slug=DOTA2_EWC_BO2_PAGE_SLUG,
        team_a="Team Falcons",
        team_b="BetBoom",
        category="Dota 2",
        tournament="Esports World Cup",
        start_time="2026-07-10T18:00:00Z",
    )
    ewc_bo2["title"] = "Dota 2: Team Falcons vs BetBoom BO2 - Esports World Cup"
    ewc_bo2["markets"][0]["question"] = (
        "Dota 2: Team Falcons vs BetBoom Match Winner Threeway Draw BO2"
    )
    hok = _keyset_match_event(
        sport_title="HOK",
        slug=DOTA2_HOK_PAGE_SLUG,
        team_a="BUE",
        team_b="TF",
        category="Honor of Kings",
        tournament="Honor of Kings Invitational",
        start_time="2026-07-10T16:00:00Z",
    )
    return (
        {
            DOTA2_EPL_PAGE_SLUG: _keyset_payload([epl]),
            DOTA2_EWC_BO2_PAGE_SLUG: _keyset_payload([ewc_bo2]),
            DOTA2_HOK_PAGE_SLUG: _keyset_payload([hok]),
        },
        {
            DOTA2_EPL_MARKET_SLUG: _keyset_payload([epl]),
        },
    )


def _stake_event_for_tournament(
    *,
    tournament_id: str,
    tournament: str,
    team_a: str,
    team_b: str,
    game: str = "cs2",
    start_time: str = "2026-06-28T16:00:00Z",
) -> ProviderOddsEvent:
    normalized_start_time = start_time[:-1] + "+00:00" if start_time.endswith("Z") else start_time
    return ProviderOddsEvent(
        provider="oddspapi",
        bookmaker="stake",
        game=game,
        tournament=tournament,
        start_time=datetime.fromisoformat(normalized_start_time),
        team_a=team_a,
        odds_a=1.5,
        team_b=team_b,
        odds_b=2.6,
        market_type="h2h",
        event_url=f"https://example.test/stake/{tournament_id}",
        raw={"event": {"tournamentId": tournament_id}},
    )


def _mixed_esports_keyset_payload() -> dict[str, object]:
    return _keyset_payload(
        [
            _keyset_match_event(
                sport_title="Counter-Strike",
                slug="cs2-yaw-bk-2026-06-28",
                team_a="Yawara eSports",
                team_b="Blitzkrieg",
                category="Counter-Strike",
                tournament="CCT South America",
            ),
            _keyset_match_event(
                sport_title="Overwatch",
                slug="overwatch-alpha-beta-2026-06-28",
                team_a="Overwatch Alpha",
                team_b="Overwatch Beta",
                category="Overwatch",
            ),
            _keyset_match_event(
                sport_title="Rainbow Six Siege",
                slug="rainbow-six-alpha-beta-2026-06-28",
                team_a="R6 Alpha",
                team_b="R6 Beta",
                category="Rainbow Six Siege",
            ),
            _keyset_match_event(
                sport_title="HOK",
                slug="hok-alpha-beta-2026-06-28",
                team_a="HOK Alpha",
                team_b="HOK Beta",
                category="Honor of Kings",
            ),
            _keyset_match_event(
                sport_title="MLP",
                slug="mlp-alpha-beta-2026-06-28",
                team_a="MLP Alpha",
                team_b="MLP Beta",
                category="MLP",
            ),
            _keyset_match_event(
                sport_title="Valorant",
                slug="valorant-alpha-beta-2026-06-28",
                team_a="Valorant Alpha",
                team_b="Valorant Beta",
                category="Valorant",
            ),
        ]
    )


def _cs2_tournament_policy_payload() -> dict[str, object]:
    tournaments = [
        ("CCT South America Series", "yawara", "blitzkrieg"),
        ("CCT Europe Series", "alpha", "beta"),
        ("Super DraculaN", "dracula", "night"),
        ("XSE Pro League 2026", "xse", "pro"),
        ("European Pro League", "epl", "team"),
        ("United21", "united", "twentyone"),
        ("Gamers Club Liga Serie A", "gamers", "club"),
        ("Urban Riga Open", "urban", "riga"),
    ]
    return _keyset_payload(
        [
            _keyset_match_event(
                sport_title="Counter-Strike",
                slug=f"cs2-{left}-{right}-2026-06-28",
                team_a=f"{left.title()} Alpha",
                team_b=f"{right.title()} Beta",
                category="Counter-Strike",
                tournament=tournament,
            )
            for tournament, left, right in tournaments
        ]
    )


def _lol_tournament_policy_payload() -> dict[str, object]:
    tournaments = [
        ("MSI", "t1", "geng"),
        ("Worlds", "blg", "g2"),
        ("Esports World Cup", "fnatic", "flyquest"),
        ("LEC", "karmine", "mad"),
        ("LCK", "hanwha", "kt"),
        ("LPL", "top", "jdg"),
        ("LCS", "cloud9", "liquid"),
        ("LTA", "furia", "pain"),
        ("Prime League 1st Division", "berlin", "mouz"),
        ("Prime League Pro Division", "rossmann", "big"),
        ("Prime League 1. Liga", "ewe", "nno"),
        ("Techniker Prime League 1st Division", "s04", "uol"),
        ("Prime League 2nd Division", "lower", "two"),
        ("Prime League 3rd Division", "lower", "three"),
        ("Academy League", "academy", "team"),
        ("LCK Challengers", "challenger", "team"),
        ("NACL", "nacl", "team"),
        ("EMEA Masters", "emea", "masters"),
        ("Worlds Open Qualifier", "qualifier", "team"),
        ("Promotion Relegation", "promo", "team"),
        ("Random Regional Cup", "regional", "cup"),
        ("Prime League", "prime", "generic"),
    ]
    return _keyset_payload(
        [
            _keyset_match_event(
                sport_title="League of Legends",
                slug=f"league-of-legends-{left}-{right}-2026-07-10",
                team_a=f"{left.title()} Alpha",
                team_b=f"{right.title()} Beta",
                category="League of Legends",
                tournament=tournament,
                start_time="2026-07-10T16:00:00Z",
            )
            for tournament, left, right in tournaments
        ]
    )


def _cct_xse_keyset_payload() -> dict[str, object]:
    return _keyset_payload(
        [
            _keyset_match_event(
                sport_title="Counter-Strike",
                slug="cs2-yawara-blitzkrieg-2026-06-28",
                team_a="Yawara eSports",
                team_b="Blitzkrieg",
                category="Counter-Strike",
                tournament="CCT South America Series",
                start_time="2026-06-28T16:00:00Z",
            ),
            _keyset_match_event(
                sport_title="Counter-Strike",
                slug="cs2-xse-alpha-beta-2026-06-28",
                team_a="XSE Alpha",
                team_b="Pro Beta",
                category="Counter-Strike",
                tournament="XSE Pro League",
                start_time="2026-06-28T18:00:00Z",
            ),
        ]
    )


class FakeOddsPapiProvider:
    def __init__(self, events: list[ProviderOddsEvent]) -> None:
        self.events = events
        self.calls: list[dict[str, object]] = []

    def fetch_events(self, **kwargs: object) -> list[ProviderOddsEvent]:
        self.calls.append(kwargs)
        return list(self.events)


class TournamentAwareOddsPapiProvider:
    def __init__(
        self,
        events_by_tournament_id: dict[str, list[ProviderOddsEvent]],
        errors_by_tournament_id: dict[str, Exception | list[Exception]] | None = None,
    ) -> None:
        self.events_by_tournament_id = events_by_tournament_id
        self.errors_by_tournament_id = errors_by_tournament_id or {}
        self.calls: list[dict[str, object]] = []

    def fetch_events(self, **kwargs: object) -> list[ProviderOddsEvent]:
        self.calls.append(kwargs)
        tournament_id = str(kwargs.get("tournament_id"))
        error = self.errors_by_tournament_id.get(tournament_id)
        if isinstance(error, list):
            if error:
                raise error.pop(0)
        elif error:
            raise error
        return list(self.events_by_tournament_id.get(tournament_id, []))


class AutoDiscoverOddsPapiProvider(TournamentAwareOddsPapiProvider):
    def __init__(
        self,
        tournaments: list[dict[str, str]],
        events_by_tournament_id: dict[str, list[ProviderOddsEvent]],
        errors_by_tournament_id: dict[str, Exception | list[Exception]] | None = None,
    ) -> None:
        super().__init__(events_by_tournament_id, errors_by_tournament_id)
        self.tournaments = tournaments
        self.tournament_candidate_calls: list[dict[str, object]] = []

    def fetch_tournament_candidates(self, **kwargs: object) -> list[dict[str, str]]:
        self.tournament_candidate_calls.append(kwargs)
        return list(self.tournaments)


class CacheAwareOddsPapiProvider(TournamentAwareOddsPapiProvider):
    def __init__(
        self,
        tournaments: list[dict[str, str]],
        events_by_tournament_id: dict[str, list[ProviderOddsEvent]],
        errors_by_tournament_id: dict[str, Exception | list[Exception]] | None = None,
    ) -> None:
        super().__init__(events_by_tournament_id, errors_by_tournament_id)
        self.tournaments = tournaments
        self.tournament_list_calls: list[dict[str, object]] = []
        self.batch_calls: list[dict[str, object]] = []

    def fetch_tournament_list(self, **kwargs: object) -> list[dict[str, str]]:
        self.tournament_list_calls.append(kwargs)
        return list(self.tournaments)

    def fetch_events_for_tournaments(self, **kwargs: object) -> list[ProviderOddsEvent]:
        self.batch_calls.append(kwargs)
        events: list[ProviderOddsEvent] = []
        for tournament_id in [str(item) for item in kwargs.get("tournament_ids", [])]:
            error = self.errors_by_tournament_id.get(tournament_id)
            if isinstance(error, list):
                if error:
                    raise error.pop(0)
            elif error:
                raise error
            events.extend(self.events_by_tournament_id.get(tournament_id, []))
        return events


class KeywordFilteringAutoDiscoverOddsPapiProvider(AutoDiscoverOddsPapiProvider):
    def fetch_tournament_candidates(self, **kwargs: object) -> list[dict[str, str]]:
        self.tournament_candidate_calls.append(kwargs)
        raw_keywords = kwargs.get("tournament_keywords") or []
        keywords = (
            [raw_keywords]
            if isinstance(raw_keywords, str)
            else list(raw_keywords)
            if isinstance(raw_keywords, (list, tuple, set))
            else []
        )
        if not keywords:
            return list(self.tournaments)
        normalized_keywords = [
            " ".join(str(keyword).casefold().split())
            for keyword in keywords
            if str(keyword).strip()
        ]
        return [
            tournament
            for tournament in self.tournaments
            if any(
                keyword in _test_tournament_search_text(tournament)
                for keyword in normalized_keywords
            )
        ]


class CandidateErrorAutoDiscoverOddsPapiProvider(AutoDiscoverOddsPapiProvider):
    def __init__(
        self,
        tournaments: list[dict[str, str]],
        events_by_tournament_id: dict[str, list[ProviderOddsEvent]],
        candidate_errors: list[Exception],
    ) -> None:
        super().__init__(tournaments, events_by_tournament_id)
        self.candidate_errors = candidate_errors

    def fetch_tournament_candidates(self, **kwargs: object) -> list[dict[str, str]]:
        self.tournament_candidate_calls.append(kwargs)
        if self.candidate_errors:
            raise self.candidate_errors.pop(0)
        return list(self.tournaments)


def _test_tournament_search_text(tournament: dict[str, str]) -> str:
    return " ".join(
        str(tournament.get(key, "")).casefold()
        for key in ("name", "slug", "category", "categoryName", "categorySlug")
    )


def _write_test_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _active_tournament_cache(
    entries: dict[str, tuple[str, str]],
    *,
    sport: str = "cs2",
) -> dict[str, object]:
    return {
        sport: {
            "stake": {
                bucket.casefold(): {
                    "tournament_id": int(tournament_id),
                    "tournament_name": tournament_name,
                    "last_verified_at": "2999-01-01T00:00:00Z",
                    "fixture_count": 1,
                    "source": "verified_team_overlap",
                    "aliases": [bucket, tournament_name],
                }
                for bucket, (tournament_id, tournament_name) in entries.items()
            }
        }
    }


def _tournament_list_cache(
    candidates: list[dict[str, str]],
    *,
    sport: str = "cs2",
    sport_id: str = "17",
    fetched_at: str = "2999-01-01T00:00:00Z",
) -> dict[str, object]:
    return {
        "sports": {
            sport: {
                "sport": sport,
                "sport_id": sport_id,
                "bookmaker": "stake",
                "source": "oddspapi",
                "fetched_at": fetched_at,
                "candidates": candidates,
            }
        }
    }


class FakePolymarketProvider:
    def __init__(self, events: list[ProviderOddsEvent]) -> None:
        self.events = events
        self.calls: list[dict[str, object]] = []

    def fetch_events(self, **kwargs: object) -> list[ProviderOddsEvent]:
        self.calls.append(kwargs)
        return list(self.events)


class SearchAwarePolymarketProvider:
    def __init__(self, events_by_search: dict[str, list[ProviderOddsEvent]]) -> None:
        self.events_by_search = events_by_search
        self.calls: list[dict[str, object]] = []

    def fetch_events(self, **kwargs: object) -> list[ProviderOddsEvent]:
        self.calls.append(kwargs)
        return list(self.events_by_search.get(str(kwargs.get("search")), []))


class PayloadSearchPolymarketProvider(PolymarketProvider):
    def __init__(
        self,
        payloads_by_search: dict[str, object] | None = None,
        *,
        payloads_by_event_slug: dict[str, object] | None = None,
        payloads_by_market_slug: dict[str, object] | None = None,
    ) -> None:
        super().__init__()
        self.payloads_by_search = payloads_by_search or {}
        self.payloads_by_event_slug = payloads_by_event_slug or {}
        self.payloads_by_market_slug = payloads_by_market_slug or {}
        self.calls: list[dict[str, object]] = []

    def fetch_events(self, **kwargs: object) -> list[ProviderOddsEvent]:
        self.calls.append(kwargs)
        if kwargs.get("event_slug"):
            payload = self.payloads_by_event_slug.get(
                str(kwargs.get("event_slug")),
                {"events": []},
            )
            source = "test_event_slug"
        elif kwargs.get("market_slug"):
            payload = self.payloads_by_market_slug.get(
                str(kwargs.get("market_slug")),
                {"events": []},
            )
            source = "test_market_slug"
        else:
            payload = self.payloads_by_search.get(
                str(kwargs.get("search")),
                {"events": []},
            )
            source = "test_search"
        return self.normalize_response(
            payload,
            sport=str(kwargs.get("sport") or ""),
            market_type=str(kwargs.get("market_type") or "h2h"),
            max_events=int(kwargs.get("max_events") or 100),
            source=source,
        )


class KeysetAwarePolymarketProvider(PolymarketProvider):
    def __init__(self, events: list[ProviderOddsEvent] | None = None) -> None:
        super().__init__()
        self.events = events or []
        self.calls: list[dict[str, object]] = []

    def fetch_events(self, **kwargs: object) -> list[ProviderOddsEvent]:
        self.calls.append(kwargs)
        return list(self.events)


class ProviderScanTest(unittest.TestCase):
    def test_extracts_slug_from_polymarket_sports_page_link(self) -> None:
        html = (FIXTURE_DIR / "polymarket_page_sports_links.html").read_text(
            encoding="utf-8"
        )

        candidates = extract_polymarket_page_slug_candidates(html, sport="cs2")

        self.assertEqual(candidates["event_slugs"], ["cs2-yaw-bk-2026-06-28"])
        self.assertEqual(candidates["market_slugs"], [])

    def test_extracts_slug_from_polymarket_event_link_and_embedded_json(self) -> None:
        html = (FIXTURE_DIR / "polymarket_page_event_links.html").read_text(
            encoding="utf-8"
        )

        candidates = extract_polymarket_page_slug_candidates(html, sport="cs2")

        self.assertEqual(candidates["event_slugs"], ["cs2-yaw-bk-2026-06-28"])
        self.assertEqual(candidates["market_slugs"], ["cs2-yaw-bk-market"])

    def test_page_discovery_dedupes_duplicate_slugs(self) -> None:
        html = (FIXTURE_DIR / "polymarket_page_sports_links.html").read_text(
            encoding="utf-8"
        )

        discovery = discover_polymarket_page_slugs(
            ["https://polymarket.com/uk/sports/cs2/cct-south-america"],
            sport="cs2",
            fetcher=lambda _url: html,
        )

        self.assertEqual(discovery["event_slugs"], ["cs2-yaw-bk-2026-06-28"])
        self.assertEqual(discovery["candidate_slugs"], ["cs2-yaw-bk-2026-06-28"])
        self.assertEqual(discovery["fetch_error_count"], 0)

    def test_extracts_dota2_slugs_from_polymarket_esports_page_fixture(
        self,
    ) -> None:
        html = (FIXTURE_DIR / "polymarket_dota2_page.html").read_text(
            encoding="utf-8"
        )

        candidates = extract_polymarket_page_slug_candidates(html, sport="dota2")

        self.assertEqual(
            candidates["event_slugs"],
            [
                DOTA2_EPL_PAGE_SLUG,
                DOTA2_EWC_BO2_PAGE_SLUG,
                DOTA2_HOK_PAGE_SLUG,
            ],
        )
        self.assertEqual(candidates["market_slugs"], [DOTA2_EPL_MARKET_SLUG])

    def test_dota2_page_preseed_fetches_slugs_and_applies_safety_filters(
        self,
    ) -> None:
        html = (FIXTURE_DIR / "polymarket_dota2_page.html").read_text(
            encoding="utf-8"
        )
        event_payloads, market_payloads = _dota2_page_slug_payloads()
        provider = PayloadSearchPolymarketProvider(
            payloads_by_event_slug=event_payloads,
            payloads_by_market_slug=market_payloads,
        )

        discovery = discover_polymarket_page_preseed_events(
            provider,
            sport="dota2",
            page_urls=["https://polymarket.com/esports/dota-2"],
            max_events=100,
            fetcher=lambda _url: html,
        )

        self.assertTrue(discovery["enabled"])
        self.assertEqual(discovery["candidate_slugs"], [
            DOTA2_EPL_PAGE_SLUG,
            DOTA2_EWC_BO2_PAGE_SLUG,
            DOTA2_HOK_PAGE_SLUG,
            DOTA2_EPL_MARKET_SLUG,
        ])
        self.assertEqual(discovery["event_count_raw"], 4)
        self.assertEqual(discovery["event_count_after_safety_filter"], 2)
        self.assertEqual(discovery["event_count_after_sport_filter"], 1)
        self.assertEqual(
            discovery["reject_reason_counts"],
            {"ewc_or_draw_risk_tournament": 1},
        )
        self.assertEqual(len(discovery["rejected_samples"]), 1)
        rejected_sample = discovery["rejected_samples"][0]
        self.assertEqual(rejected_sample["reason"], "ewc_or_draw_risk_tournament")
        self.assertIn(DOTA2_EWC_BO2_PAGE_SLUG, rejected_sample["slug"])
        self.assertIn("Dota 2", rejected_sample["category"])
        self.assertIn("event=true", rejected_sample["active"])
        self.assertIn("event=false", rejected_sample["closed"])
        self.assertEqual(discovery["events"][0].team_a, "Team Spirit")
        self.assertIn("European Pro League", discovery["events"][0].tournament or "")

    def test_dota2_page_preseed_rejected_samples_are_capped(self) -> None:
        payloads: dict[str, object] = {}
        links: list[str] = []
        for index in range(15):
            slug = f"dota2-spirit-gladiators-map-{index}-2026-07-10"
            event = _keyset_match_event(
                sport_title="Dota 2",
                slug=slug,
                team_a="Team Spirit",
                team_b="Gaimin Gladiators",
                category="Dota 2",
                tournament="European Pro League",
            )
            event["markets"][0]["question"] = (
                "Dota 2: Team Spirit vs Gaimin Gladiators Map 1 Winner"
            )
            payloads[slug] = _keyset_payload([event])
            links.append(f'<a href="/esports/dota-2/{slug}">market</a>')

        discovery = discover_polymarket_page_preseed_events(
            PayloadSearchPolymarketProvider(payloads_by_event_slug=payloads),
            sport="dota2",
            page_urls=["https://polymarket.com/esports/dota-2"],
            max_events=100,
            fetcher=lambda _url: "".join(links),
        )

        self.assertEqual(discovery["event_count_raw"], 15)
        self.assertEqual(discovery["event_count_after_safety_filter"], 0)
        self.assertEqual(
            discovery["reject_reason_counts"],
            {"map_or_game_winner": 15},
        )
        self.assertEqual(len(discovery["rejected_samples"]), 10)

    def test_page_discovery_filters_candidates_to_stake_team_slugs(self) -> None:
        html = (FIXTURE_DIR / "polymarket_page_mixed_links.html").read_text(
            encoding="utf-8"
        )
        stake_events = [
            ProviderOddsEvent(
                provider="oddspapi",
                bookmaker="stake",
                game="cs2",
                tournament="CCT",
                start_time=None,
                team_a="Yawara eSports",
                odds_a=1.5,
                team_b="Blitzkrieg",
                odds_b=2.6,
                market_type="h2h",
                event_url=None,
                raw={},
            )
        ]

        discovery = discover_polymarket_page_slugs(
            ["https://polymarket.com/uk/sports/cs2/cct-south-america"],
            sport="cs2",
            stake_events=stake_events,
            fetcher=lambda _url: html,
        )

        self.assertEqual(discovery["event_slugs"], ["cs2-yaw-bk-2026-06-28"])
        self.assertEqual(discovery["candidate_slugs"], ["cs2-yaw-bk-2026-06-28"])
        self.assertEqual(len(discovery["raw_candidate_slugs"]), 2)
        self.assertEqual(
            discovery["rejected_slugs"],
            ["nocries-signs-for-a-pro-cs2-organization-by-june-30"],
        )

    def test_page_discovery_rejects_unrelated_slug_without_crashing(self) -> None:
        stake_events = [
            ProviderOddsEvent(
                provider="oddspapi",
                bookmaker="stake",
                game="cs2",
                tournament="CCT",
                start_time=None,
                team_a="Yawara eSports",
                odds_a=1.5,
                team_b="Blitzkrieg",
                odds_b=2.6,
                market_type="h2h",
                event_url=None,
                raw={},
            )
        ]

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_tournament_id="101",
            polymarket_page_url=["https://polymarket.com/uk/sports/cs2/cct"],
            providers={
                "oddspapi": FakeOddsPapiProvider(stake_events),
                "polymarket": FakePolymarketProvider([]),
            },
            page_fetcher=(
                lambda _url: '<a href="/event/'
                'nocries-signs-for-a-pro-cs2-organization-by-june-30">NoCries</a>'
            ),
        )

        self.assertEqual(result["summary"]["polymarket_count"], 0)
        self.assertEqual(
            result["debug"]["polymarket_page_candidate_slug_count_raw"],
            1,
        )
        self.assertEqual(
            result["debug"]["polymarket_page_candidate_slug_count_kept"],
            0,
        )
        self.assertEqual(result["debug"]["polymarket_page_rejected_slug_count"], 1)
        self.assertEqual(
            result["debug"]["polymarket_page_rejected_slugs_sample"],
            ["nocries-signs-for-a-pro-cs2-organization-by-june-30"],
        )

    def test_keyset_url_generation_includes_tags_window_limit_and_locale(
        self,
    ) -> None:
        now = datetime(2026, 6, 28, 11, 21, 44, 842000, tzinfo=timezone.utc)

        url = build_polymarket_keyset_url(
            tag_ids=["100639", "1"],
            window_hours=24,
            limit=50,
            locale="uk",
            now=now,
        )

        parsed = urlsplit(url)
        params = parse_qs(parsed.query)
        self.assertEqual(
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
            "https://gamma-api.polymarket.com/events/keyset",
        )
        self.assertEqual(params["tag_id"], ["100639", "1"])
        self.assertEqual(params["active"], ["true"])
        self.assertEqual(params["closed"], ["false"])
        self.assertEqual(params["order"], ["startTime"])
        self.assertEqual(params["ascending"], ["true"])
        self.assertEqual(params["limit"], ["50"])
        self.assertEqual(params["start_time_min"], ["2026-06-28T11:21:44.842Z"])
        self.assertEqual(params["start_time_max"], ["2026-06-29T11:21:44.842Z"])
        self.assertEqual(params["locale"], ["uk"])
        self.assertNotIn("live", params)

    def test_keyset_discovery_normalizes_parent_and_rejects_map_markets(
        self,
    ) -> None:
        payload = json.loads(
            (FIXTURE_DIR / "polymarket_keyset_page_1.json").read_text(
                encoding="utf-8"
            )
        )
        provider = KeysetAwarePolymarketProvider()

        discovery = discover_polymarket_keyset_events(
            provider,
            sport="cs2",
            keyset_urls=["https://gamma-api.polymarket.com/events/keyset"],
            max_pages=1,
            fetcher=lambda _url: payload,
        )

        self.assertEqual(len(discovery["events"]), 1)
        event = discovery["events"][0]
        self.assertEqual(event.provider, "polymarket")
        self.assertEqual(event.game, "cs2")
        self.assertEqual(event.team_a, "Yawara eSports")
        self.assertEqual(event.team_b, "Blitzkrieg")
        self.assertEqual(event.market_type, "h2h")
        self.assertEqual(event.raw["source"], "gamma_keyset")
        self.assertEqual(event.raw["market_id"], "pm-market-cs2-yaw-bk-match")
        self.assertEqual(discovery["page_count"], 1)
        self.assertEqual(discovery["event_count_raw"], 1)
        self.assertEqual(discovery["rejected_count"], 2)
        self.assertIn("cs2-yaw-bk-2026-06-28", discovery["sample_slugs"])
        self.assertEqual(
            provider.last_debug["reject_reason_counts"],
            {"map_or_game_winner": 2},
        )

    def test_keyset_discovery_paginates_and_dedupes_across_pages(self) -> None:
        page_1 = json.loads(
            (FIXTURE_DIR / "polymarket_keyset_page_1.json").read_text(
                encoding="utf-8"
            )
        )
        page_2 = json.loads(
            (FIXTURE_DIR / "polymarket_keyset_page_2.json").read_text(
                encoding="utf-8"
            )
        )
        page_2_with_duplicate = {
            "events": [page_1["events"][0], *page_2["events"]]
        }
        responses = [page_1, page_2_with_duplicate]
        urls: list[str] = []

        def fetcher(url: str) -> object:
            urls.append(url)
            return responses.pop(0)

        discovery = discover_polymarket_keyset_events(
            KeysetAwarePolymarketProvider(),
            sport="cs2",
            keyset_urls=["https://gamma-api.polymarket.com/events/keyset?tag_id=100639"],
            max_pages=3,
            fetcher=fetcher,
        )

        self.assertEqual(len(discovery["events"]), 2)
        self.assertEqual(discovery["page_count"], 2)
        self.assertEqual(discovery["event_count_raw"], 3)
        self.assertTrue(discovery["next_cursor_seen"])
        self.assertIn("after_cursor=cursor-page-2", urls[1])
        self.assertFalse(discovery["adaptive_enabled"])

    def test_keyset_adaptive_continues_when_pages_have_new_target_markets(
        self,
    ) -> None:
        responses = [
            _keyset_payload_with_cursor(
                [
                    _keyset_match_event(
                        sport_title="League of Legends",
                        slug="league-of-legends-t1-vs-geng-lck-2026-07-10",
                        team_a="T1",
                        team_b="Gen.G",
                        category="League of Legends",
                        tournament="LCK",
                    )
                ],
                "cursor-page-2",
            ),
            _keyset_payload_with_cursor(
                [
                    _keyset_match_event(
                        sport_title="League of Legends",
                        slug="league-of-legends-g2-vs-fnc-lec-2026-07-10",
                        team_a="G2",
                        team_b="Fnatic",
                        category="League of Legends",
                        tournament="LEC",
                    )
                ],
                "cursor-page-3",
            ),
            _keyset_payload(
                [
                    _keyset_match_event(
                        sport_title="Counter-Strike",
                        slug="cs2-alpha-vs-beta-2026-07-10",
                        team_a="Alpha",
                        team_b="Beta",
                        category="Counter-Strike",
                    )
                ]
            ),
        ]

        discovery = discover_polymarket_keyset_events(
            KeysetAwarePolymarketProvider(),
            sport="lol",
            keyset_urls=["https://gamma-api.polymarket.com/events/keyset"],
            max_pages=5,
            adaptive=True,
            empty_target_pages_stop=1,
            fetcher=lambda _url: responses.pop(0),
        )

        self.assertEqual(len(discovery["events"]), 2)
        self.assertEqual(discovery["page_count"], 3)
        self.assertEqual(discovery["adaptive_pages_fetched"], 3)
        self.assertEqual(discovery["adaptive_stop_reason"], "no_next_cursor")
        self.assertEqual(
            [page["new_target_markets"] for page in discovery["page_details"]],
            [1, 1, 0],
        )

    def test_keyset_adaptive_stops_after_empty_target_page_streak(self) -> None:
        responses = [
            _keyset_payload_with_cursor(
                [
                    _keyset_match_event(
                        sport_title="League of Legends",
                        slug="league-of-legends-t1-vs-geng-lck-2026-07-10",
                        team_a="T1",
                        team_b="Gen.G",
                        category="League of Legends",
                        tournament="LCK",
                    )
                ],
                "cursor-page-2",
            ),
            _keyset_payload_with_cursor(
                [
                    _keyset_match_event(
                        sport_title="Counter-Strike",
                        slug="cs2-alpha-vs-beta-2026-07-10",
                        team_a="Alpha",
                        team_b="Beta",
                        category="Counter-Strike",
                    )
                ],
                "cursor-page-3",
            ),
            _keyset_payload_with_cursor(
                [
                    _keyset_match_event(
                        sport_title="Dota 2",
                        slug="dota2-radiant-vs-dire-2026-07-10",
                        team_a="Radiant",
                        team_b="Dire",
                        category="Dota 2",
                    )
                ],
                "cursor-page-4",
            ),
            _keyset_payload(
                [
                    _keyset_match_event(
                        sport_title="League of Legends",
                        slug="league-of-legends-g2-vs-fnc-lec-2026-07-10",
                        team_a="G2",
                        team_b="Fnatic",
                        category="League of Legends",
                        tournament="LEC",
                    )
                ]
            ),
        ]

        discovery = discover_polymarket_keyset_events(
            KeysetAwarePolymarketProvider(),
            sport="lol",
            keyset_urls=["https://gamma-api.polymarket.com/events/keyset"],
            max_pages=5,
            adaptive=True,
            empty_target_pages_stop=2,
            fetcher=lambda _url: responses.pop(0),
        )

        self.assertEqual(len(discovery["events"]), 1)
        self.assertEqual(discovery["page_count"], 3)
        self.assertEqual(
            discovery["adaptive_stop_reason"],
            "empty_target_page_streak",
        )
        self.assertEqual(discovery["empty_target_pages_streak"], 2)
        self.assertEqual(
            [
                page["empty_target_pages_streak"]
                for page in discovery["page_details"]
            ],
            [0, 1, 2],
        )

    def test_keyset_adaptive_respects_hard_max_page_cap(self) -> None:
        responses = [
            _keyset_payload_with_cursor(
                [
                    _keyset_match_event(
                        sport_title="League of Legends",
                        slug="league-of-legends-t1-vs-geng-lck-2026-07-10",
                        team_a="T1",
                        team_b="Gen.G",
                        category="League of Legends",
                        tournament="LCK",
                    )
                ],
                "cursor-page-2",
            ),
            _keyset_payload_with_cursor(
                [
                    _keyset_match_event(
                        sport_title="League of Legends",
                        slug="league-of-legends-g2-vs-fnc-lec-2026-07-10",
                        team_a="G2",
                        team_b="Fnatic",
                        category="League of Legends",
                        tournament="LEC",
                    )
                ],
                "cursor-page-3",
            ),
            _keyset_payload(
                [
                    _keyset_match_event(
                        sport_title="League of Legends",
                        slug="league-of-legends-blg-vs-tes-lpl-2026-07-10",
                        team_a="BLG",
                        team_b="TES",
                        category="League of Legends",
                        tournament="LPL",
                    )
                ]
            ),
        ]

        discovery = discover_polymarket_keyset_events(
            KeysetAwarePolymarketProvider(),
            sport="lol",
            keyset_urls=["https://gamma-api.polymarket.com/events/keyset"],
            max_pages=2,
            adaptive=True,
            empty_target_pages_stop=2,
            fetcher=lambda _url: responses.pop(0),
        )

        self.assertEqual(len(discovery["events"]), 2)
        self.assertEqual(discovery["page_count"], 2)
        self.assertEqual(discovery["adaptive_stop_reason"], "max_pages")
        self.assertTrue(discovery["max_pages_stopped_early"])
        self.assertEqual(len(responses), 1)

    def test_scan_table_includes_adaptive_keyset_diagnostics(self) -> None:
        output = format_scan_table(
            {
                "summary": {
                    "stake_count": 0,
                    "polymarket_count": 0,
                    "matched_count": 0,
                    "opportunity_count": 0,
                    "telegram_sent_count": 0,
                },
                "matches": [],
                "debug": {
                    "polymarket_keyset_adaptive_enabled": True,
                    "polymarket_keyset_adaptive_pages_fetched": 3,
                    "polymarket_keyset_adaptive_stop_reason": (
                        "empty_target_page_streak"
                    ),
                    "polymarket_keyset_empty_target_pages_streak": 2,
                    "polymarket_keyset_page_details": [
                        {
                            "page": 1,
                            "raw_event_count": 100,
                            "sport_kept_count": 3,
                            "new_target_markets": 2,
                            "empty_target_pages_streak": 0,
                        }
                    ],
                },
            },
            debug=True,
        )

        self.assertIn("polymarket_keyset_adaptive_enabled=true", output)
        self.assertIn("polymarket_keyset_adaptive_pages_fetched=3", output)
        self.assertIn(
            "polymarket_keyset_adaptive_stop_reason=empty_target_page_streak",
            output,
        )
        self.assertIn(
            "polymarket_keyset_page page=1 raw=100 sport_kept=3 "
            "new_target_markets=2 empty_target_pages_streak=0",
            output,
        )

    def test_lol_debug_includes_compact_unmatched_event_samples(self) -> None:
        start_time = datetime(2026, 7, 10, 16, 0, tzinfo=timezone.utc)
        stake_events = [
            ProviderOddsEvent(
                provider="oddspapi",
                bookmaker="stake",
                game="lol",
                tournament="Mid-Season Invitational",
                start_time=start_time,
                team_a="T1",
                odds_a=1.75,
                team_b="Gen.G",
                odds_b=2.05,
                market_type="h2h",
                event_url=None,
                raw={
                    "event": {"tournamentId": "2527"},
                    "market": {"marketId": "181", "active": True},
                    "mapping_source": (
                        MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER
                    ),
                },
            ),
            ProviderOddsEvent(
                provider="oddspapi",
                bookmaker="stake",
                game="lol",
                tournament="Mid-Season Invitational",
                start_time=datetime(2026, 7, 11, 16, 0, tzinfo=timezone.utc),
                team_a="Only Stake A",
                odds_a=1.9,
                team_b="Only Stake B",
                odds_b=1.9,
                market_type="h2h",
                event_url=None,
                raw={
                    "event": {"tournamentId": "2527"},
                    "market": {"marketId": "181", "active": True},
                    "mapping_source": (
                        MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER
                    ),
                },
            ),
        ]
        polymarket_events = [
            ProviderOddsEvent(
                provider="polymarket",
                bookmaker="polymarket",
                game="lol",
                tournament="Mid-Season Invitational",
                start_time=start_time,
                team_a="T1",
                odds_a=1.8,
                team_b="Gen.G",
                odds_b=2.0,
                market_type="h2h",
                event_url="https://polymarket.com/event/lol-t1-geng-2026-07-10",
                raw={
                    "event_slug": "lol-t1-geng-2026-07-10",
                    "market_id": "pm-market-t1-geng",
                    "active": {"market": True},
                },
            ),
            ProviderOddsEvent(
                provider="polymarket",
                bookmaker="polymarket",
                game="lol",
                tournament="Mid-Season Invitational",
                start_time=datetime(2026, 7, 12, 16, 0, tzinfo=timezone.utc),
                team_a="Only Poly A",
                odds_a=1.6,
                team_b="Only Poly B",
                odds_b=2.4,
                market_type="h2h",
                event_url="https://polymarket.com/event/lol-only-poly-2026-07-12",
                raw={
                    "event_slug": "lol-only-poly-2026-07-12",
                    "market_id": "pm-market-only-poly",
                    "active": {"market": True},
                },
            ),
        ]

        result = run_scan(
            sport="lol",
            bookmaker="stake",
            stake_tournament_id="2527",
            stake_request_delay_seconds=0,
            polymarket_search=["lol"],
            providers={
                "oddspapi": FakeOddsPapiProvider(stake_events),
                "polymarket": FakePolymarketProvider(polymarket_events),
            },
        )

        self.assertEqual(result["summary"]["matched_count"], 1)
        self.assertEqual(len(result["debug"]["stake_events_sample"]), 2)
        self.assertEqual(len(result["debug"]["polymarket_events_sample"]), 2)
        self.assertEqual(len(result["debug"]["unmatched_stake_sample"]), 1)
        self.assertEqual(len(result["debug"]["unmatched_polymarket_sample"]), 1)
        self.assertIn(
            "teams=Only Stake A vs Only Stake B",
            result["debug"]["unmatched_stake_sample"][0],
        )
        self.assertIn(
            "team_keys=only stake a vs only stake b",
            result["debug"]["unmatched_stake_sample"][0],
        )
        self.assertIn(
            "tournament_id=2527",
            result["debug"]["unmatched_stake_sample"][0],
        )
        self.assertIn(
            "market_id=181",
            result["debug"]["unmatched_stake_sample"][0],
        )
        self.assertIn(
            "mapping=oddspapi_stake_lol_market181_selection_order",
            result["debug"]["unmatched_stake_sample"][0],
        )
        self.assertIn(
            "slug=lol-only-poly-2026-07-12",
            result["debug"]["unmatched_polymarket_sample"][0],
        )

        output = format_scan_table(result, debug=True)

        self.assertIn("stake_events_sample=", output)
        self.assertIn("polymarket_events_sample=", output)
        self.assertIn("unmatched_stake_sample=", output)
        self.assertIn("unmatched_polymarket_sample=", output)
        self.assertIn("market_active=True", output)

    def test_keyset_sport_filter_cs2_keeps_counter_strike_and_rejects_others(
        self,
    ) -> None:
        discovery = discover_polymarket_keyset_events(
            KeysetAwarePolymarketProvider(),
            sport="cs2",
            keyset_urls=["https://gamma-api.polymarket.com/events/keyset"],
            max_pages=1,
            fetcher=lambda _url: _mixed_esports_keyset_payload(),
        )

        self.assertEqual(len(discovery["events"]), 1)
        self.assertEqual(discovery["events"][0].team_a, "Yawara eSports")
        self.assertEqual(discovery["event_count_before_sport_filter"], 6)
        self.assertEqual(discovery["event_count_after_sport_filter"], 1)
        self.assertEqual(discovery["sport_filter_rejected_count"], 5)
        rejected_text = " ".join(discovery["sport_filter_rejected_sample"])
        self.assertIn("Overwatch", rejected_text)
        self.assertIn("Rainbow Six Siege", rejected_text)
        self.assertIn("HOK", rejected_text)
        self.assertIn("MLP", rejected_text)
        self.assertIn(
            "cs2-yaw-bk-2026-06-28",
            " ".join(discovery["sport_filter_kept_sample"]),
        )

    def test_keyset_sport_filter_dota2_keeps_dota2_event(self) -> None:
        payload = _keyset_payload(
            [
                _keyset_match_event(
                    sport_title="Dota 2",
                    slug="dota2-spirit-gladiators-2026-06-28",
                    team_a="Team Spirit",
                    team_b="Gaimin Gladiators",
                    category="Dota 2",
                ),
                _keyset_match_event(
                    sport_title="Overwatch",
                    slug="overwatch-alpha-beta-2026-06-28",
                    team_a="Overwatch Alpha",
                    team_b="Overwatch Beta",
                    category="Overwatch",
                ),
            ]
        )

        discovery = discover_polymarket_keyset_events(
            KeysetAwarePolymarketProvider(),
            sport="dota2",
            keyset_urls=["https://gamma-api.polymarket.com/events/keyset"],
            max_pages=1,
            fetcher=lambda _url: payload,
        )

        self.assertEqual(len(discovery["events"]), 1)
        self.assertEqual(discovery["events"][0].team_a, "Team Spirit")
        self.assertEqual(discovery["sport_filter_rejected_count"], 1)

    def test_dota2_preseed_search_filters_hok_and_unsafe_markets(self) -> None:
        valid = _keyset_match_event(
            sport_title="Dota 2",
            slug="dota2-spirit-gladiators-epl-2026-07-10",
            team_a="Team Spirit",
            team_b="Gaimin Gladiators",
            category="Dota 2",
            tournament="European Pro League",
            start_time="2026-07-10T16:00:00Z",
        )
        hok = _keyset_match_event(
            sport_title="HOK",
            slug="hok-bue-tf-2026-07-10",
            team_a="BUE",
            team_b="TF",
            category="Honor of Kings",
            tournament="Honor of Kings Invitational",
            start_time="2026-07-10T16:00:00Z",
        )
        map_winner = _keyset_match_event(
            sport_title="Dota 2",
            slug="dota2-spirit-gladiators-map-1-2026-07-10",
            team_a="Team Spirit",
            team_b="Gaimin Gladiators",
            category="Dota 2",
            tournament="European Pro League",
            start_time="2026-07-10T16:00:00Z",
        )
        map_winner["markets"][0]["question"] = (
            "Dota 2: Team Spirit vs Gaimin Gladiators Map 1 Winner"
        )
        draw_winner = _keyset_match_event(
            sport_title="Dota 2",
            slug="dota2-spirit-gladiators-draw-2026-07-10",
            team_a="Team Spirit",
            team_b="Gaimin Gladiators",
            category="Dota 2",
            tournament="European Pro League",
            start_time="2026-07-10T16:00:00Z",
        )
        draw_winner["markets"][0]["question"] = (
            "Dota 2: Team Spirit vs Gaimin Gladiators draw winner"
        )

        discovery = discover_polymarket_preseed_events(
            PayloadSearchPolymarketProvider(
                {
                    "Dota 2": _keyset_payload(
                        [valid, hok, map_winner, draw_winner]
                    )
                }
            ),
            sport="dota2",
            searches=["Dota 2"],
            max_events=100,
        )

        self.assertTrue(discovery["enabled"])
        self.assertEqual(discovery["event_count_raw"], 4)
        self.assertEqual(discovery["event_count_after_safety_filter"], 2)
        self.assertEqual(discovery["event_count_after_sport_filter"], 1)
        self.assertEqual(discovery["sport_filter_rejected_count"], 1)
        self.assertEqual(discovery["events"][0].team_a, "Team Spirit")
        self.assertEqual(
            discovery["reject_reason_counts"],
            {"draw_or_three_way": 1, "map_or_game_winner": 1},
        )
        rejected_samples = discovery["rejected_samples"]
        self.assertEqual(len(rejected_samples), 2)
        self.assertEqual(rejected_samples[0]["reason"], "map_or_game_winner")
        self.assertIn("dota2-spirit-gladiators-map-1", rejected_samples[0]["slug"])
        self.assertIn("Dota 2", rejected_samples[0]["category"])
        self.assertIn("event=true", rejected_samples[0]["active"])
        self.assertIn("event=false", rejected_samples[0]["closed"])
        rejected_text = " ".join(discovery["sport_filter_rejected_sample"])
        self.assertIn("hok-bue-tf", rejected_text)

    def test_dota2_preseed_rejected_samples_are_capped(self) -> None:
        payloads: dict[str, object] = {}
        for search_index in range(3):
            events: list[dict[str, object]] = []
            for event_index in range(5):
                event = _keyset_match_event(
                    sport_title="Dota 2",
                    slug=(
                        "dota2-spirit-gladiators-map-"
                        f"{search_index}-{event_index}"
                    ),
                    team_a="Team Spirit",
                    team_b="Gaimin Gladiators",
                    category="Dota 2",
                    tournament="European Pro League",
                )
                event["markets"][0]["question"] = (
                    "Dota 2: Team Spirit vs Gaimin Gladiators Map 1 Winner"
                )
                events.append(event)
            payloads[f"Dota {search_index}"] = _keyset_payload(events)

        discovery = discover_polymarket_preseed_events(
            PayloadSearchPolymarketProvider(payloads),
            sport="dota2",
            searches=["Dota 0", "Dota 1", "Dota 2"],
            max_events=100,
        )

        self.assertEqual(discovery["event_count_raw"], 15)
        self.assertEqual(discovery["event_count_after_safety_filter"], 0)
        self.assertEqual(
            discovery["reject_reason_counts"],
            {"map_or_game_winner": 15},
        )
        self.assertEqual(len(discovery["rejected_samples"]), 10)

    def test_scan_table_includes_preseed_rejection_diagnostics(self) -> None:
        output = format_scan_table(
            {
                "summary": {
                    "stake_count": 0,
                    "polymarket_count": 0,
                    "matched_count": 0,
                    "opportunity_count": 0,
                    "telegram_sent_count": 0,
                },
                "matches": [],
                "debug": {
                    "polymarket_preseed_reject_reason_counts": {
                        "map_or_game_winner": 2,
                        "draw_or_three_way": 1,
                    },
                    "polymarket_preseed_rejected_samples": [
                        {
                            "reason": "map_or_game_winner",
                            "slug": "dota2-spirit-gladiators-map-1",
                            "event_title": "Dota 2: Team Spirit vs Gaimin",
                            "question": "Dota 2 Map 1 Winner",
                            "active": "event=true,market=true",
                            "closed": "event=false,market=false",
                            "sport_guess": "Dota 2",
                            "category": "Dota 2",
                            "tags": "Dota 2",
                        }
                    ],
                },
            },
            debug=True,
        )

        self.assertIn(
            "polymarket_preseed_reject_reason_counts="
            "map_or_game_winner=2,draw_or_three_way=1",
            output,
        )
        self.assertIn(
            "polymarket_preseed_rejected_samples="
            "reason:map_or_game_winner|slug:dota2-spirit-gladiators-map-1",
            output,
        )
        self.assertNotIn("{'reason'", output)

    def test_scan_table_includes_page_preseed_rejection_diagnostics(self) -> None:
        output = format_scan_table(
            {
                "summary": {
                    "stake_count": 0,
                    "polymarket_count": 0,
                    "matched_count": 0,
                    "opportunity_count": 0,
                    "telegram_sent_count": 0,
                },
                "matches": [],
                "debug": {
                    "polymarket_page_preseed_reject_reason_counts": {
                        "map_or_game_winner": 2,
                        "draw_or_three_way": 1,
                    },
                    "polymarket_page_preseed_rejected_samples": [
                        {
                            "reason": "draw_or_three_way",
                            "slug": DOTA2_EWC_BO2_PAGE_SLUG,
                            "event_title": "Dota 2: Team Falcons vs BetBoom",
                            "question": "Dota 2 Match Winner Threeway Draw",
                            "active": "event=true,market=true",
                            "closed": "event=false,market=false",
                            "sport_guess": "Dota 2",
                            "tags": "Dota 2",
                        }
                    ],
                },
            },
            debug=True,
        )

        self.assertIn(
            "polymarket_page_preseed_reject_reason_counts="
            "map_or_game_winner=2,draw_or_three_way=1",
            output,
        )
        self.assertIn(
            "polymarket_page_preseed_rejected_samples="
            "reason:draw_or_three_way|slug:" + DOTA2_EWC_BO2_PAGE_SLUG,
            output,
        )
        self.assertNotIn("{'reason'", output)

    def test_scan_table_includes_dota_tournament_policy_diagnostics(self) -> None:
        output = format_scan_table(
            {
                "summary": {
                    "stake_count": 0,
                    "polymarket_count": 0,
                    "matched_count": 0,
                    "opportunity_count": 0,
                    "telegram_sent_count": 0,
                },
                "matches": [],
                "debug": {
                    "dota_tournament_policy_rejected_count": 2,
                    "dota_tournament_policy_rejected_sample": [
                        {
                            "reason": "ewc_or_draw_risk_tournament",
                            "slug": DOTA2_EWC_BO2_PAGE_SLUG,
                            "event_title": "Dota 2: Falcons vs BetBoom",
                            "question": "Dota 2 Match Winner",
                            "category": "Dota 2",
                        }
                    ],
                    "dota_unsafe_tournament_reasons": {
                        "EWC": 1,
                        "BO2": 1,
                    },
                },
            },
            debug=True,
        )

        self.assertIn("dota_tournament_policy_rejected_count=2", output)
        self.assertIn(
            "dota_tournament_policy_rejected_sample="
            "reason:ewc_or_draw_risk_tournament|slug:"
            + DOTA2_EWC_BO2_PAGE_SLUG,
            output,
        )
        self.assertIn("dota_unsafe_tournament_reasons=EWC=1,BO2=1", output)

    def test_keyset_sport_filter_lol_keeps_league_of_legends_event(self) -> None:
        payload = _keyset_payload(
            [
                _keyset_match_event(
                    sport_title="League of Legends",
                    slug="league-of-legends-t1-geng-2026-06-28",
                    team_a="T1",
                    team_b="Gen.G",
                    category="League of Legends",
                ),
                _keyset_match_event(
                    sport_title="Overwatch",
                    slug="overwatch-alpha-beta-2026-06-28",
                    team_a="Overwatch Alpha",
                    team_b="Overwatch Beta",
                    category="Overwatch",
                ),
            ]
        )

        discovery = discover_polymarket_keyset_events(
            KeysetAwarePolymarketProvider(),
            sport="lol",
            keyset_urls=["https://gamma-api.polymarket.com/events/keyset"],
            max_pages=1,
            fetcher=lambda _url: payload,
        )

        self.assertEqual(len(discovery["events"]), 1)
        self.assertEqual(discovery["events"][0].team_a, "T1")
        self.assertEqual(discovery["sport_filter_rejected_count"], 1)

    def test_lol_msi_keyset_keeps_match_winner_and_rejects_props_futures(
        self,
    ) -> None:
        event = _keyset_match_event(
            sport_title="League of Legends",
            slug="league-of-legends-t1-vs-geng-msi-2026-07-10",
            team_a="T1",
            team_b="Gen.G",
            category="League of Legends",
            tournament="Mid-Season Invitational",
            start_time="2026-07-10T16:00:00Z",
        )
        markets = event["markets"]
        assert isinstance(markets, list)
        markets.extend(
            [
                {
                    "id": "pm-market-lol-msi-map-1",
                    "conditionId": "pm-cond-lol-msi-map-1",
                    "question": (
                        "League of Legends: T1 vs Gen.G Map 1 Winner - "
                        "Mid-Season Invitational"
                    ),
                    "slug": "league-of-legends-t1-vs-geng-map-1-msi",
                    "active": True,
                    "closed": False,
                    "outcomes": json.dumps(["T1", "Gen.G"]),
                    "outcomePrices": json.dumps(["0.50", "0.50"]),
                },
                {
                    "id": "pm-market-lol-msi-future",
                    "conditionId": "pm-cond-lol-msi-future",
                    "question": "Who will win MSI?",
                    "slug": "league-of-legends-msi-winner-2026",
                    "active": True,
                    "closed": False,
                    "outcomes": json.dumps(["T1", "Gen.G"]),
                    "outcomePrices": json.dumps(["0.50", "0.50"]),
                },
            ]
        )
        provider = KeysetAwarePolymarketProvider()

        discovery = discover_polymarket_keyset_events(
            provider,
            sport="lol",
            keyset_urls=["https://gamma-api.polymarket.com/events/keyset?tag_id=100639"],
            max_pages=1,
            fetcher=lambda _url: _keyset_payload([event]),
        )

        self.assertEqual(len(discovery["events"]), 1)
        self.assertEqual(discovery["events"][0].team_a, "T1")
        self.assertEqual(discovery["events"][0].team_b, "Gen.G")
        self.assertEqual(
            provider.last_debug["reject_reason_counts"],
            {"map_or_game_winner": 1, "outright_market": 1},
        )

    def test_polymarket_lol_candidate_diagnostic_reports_kept_and_rejected_reasons(
        self,
    ) -> None:
        event = _keyset_match_event(
            sport_title="League of Legends",
            slug="league-of-legends-t1-vs-geng-msi-2026-07-10",
            team_a="T1",
            team_b="Gen.G",
            category="League of Legends",
            tournament="Mid-Season Invitational",
            start_time="2026-07-10T16:00:00Z",
        )
        markets = event["markets"]
        assert isinstance(markets, list)
        markets.append(
            {
                "id": "pm-market-lol-msi-map-1",
                "conditionId": "pm-cond-lol-msi-map-1",
                "question": "League of Legends: T1 vs Gen.G Map 1 Winner - MSI",
                "slug": "league-of-legends-t1-vs-geng-map-1-msi",
                "active": True,
                "closed": False,
                "outcomes": json.dumps(["T1", "Gen.G"]),
                "outcomePrices": json.dumps(["0.50", "0.50"]),
            }
        )

        result = run_polymarket_candidate_diagnostic(
            sport="lol",
            polymarket_keyset_url=[
                "https://gamma-api.polymarket.com/events/keyset?tag_id=100639"
            ],
            polymarket_keyset_max_pages=1,
            providers={"polymarket": KeysetAwarePolymarketProvider()},
            keyset_fetcher=lambda _url: _keyset_payload([event]),
        )

        self.assertEqual(result["summary"]["raw_event_count"], 1)
        self.assertEqual(result["summary"]["normalized_event_count"], 1)
        self.assertEqual(result["summary"]["kept_event_count"], 1)
        self.assertEqual(result["summary"]["raw_msi_signal_count"], 1)
        self.assertEqual(result["keyset"]["tag_ids_used"], ["100639"])
        self.assertEqual(result["keyset"]["raw_event_count_by_page"], [1])
        self.assertEqual(
            result["rejected_reason_counts"],
            {"map_or_game_winner": 1},
        )
        kept = result["kept_event_samples"][0]
        self.assertEqual(kept["event_title"], event["title"])
        self.assertEqual(kept["slug"], event["slug"])
        self.assertEqual(kept["bucket"], "MSI")
        rejected = result["rejected_event_samples"][0]
        self.assertEqual(rejected["reason"], "map_or_game_winner")
        self.assertIn("Map 1 Winner", rejected["market_title"])

    def test_polymarket_lol_candidate_diagnostic_does_not_reject_msi_match_winner(
        self,
    ) -> None:
        event = _keyset_match_event(
            sport_title="League of Legends",
            slug="league-of-legends-bilibili-vs-t1-msi-2026-07-12",
            team_a="Bilibili Gaming",
            team_b="T1",
            category="League of Legends",
            tournament="Mid-Season Invitational",
            start_time="2026-07-12T16:00:00Z",
        )

        result = run_polymarket_candidate_diagnostic(
            sport="lol",
            polymarket_keyset_url=[
                "https://gamma-api.polymarket.com/events/keyset?tag_id=100639"
            ],
            polymarket_keyset_max_pages=1,
            providers={"polymarket": KeysetAwarePolymarketProvider()},
            keyset_fetcher=lambda _url: _keyset_payload([event]),
        )

        self.assertEqual(result["summary"]["kept_event_count"], 1)
        self.assertEqual(result["rejected_reason_counts"], {})
        self.assertEqual(result["rejected_event_samples"], [])
        kept = result["kept_event_samples"][0]
        self.assertEqual(kept["outcomes"], "Bilibili Gaming|T1")
        self.assertEqual(kept["bucket"], "MSI")

    def test_scan_keyset_results_match_fake_stake_event(self) -> None:
        payload = json.loads(
            (FIXTURE_DIR / "polymarket_keyset_page_1.json").read_text(
                encoding="utf-8"
            )
        )
        stake_events = [
            ProviderOddsEvent(
                provider="oddspapi",
                bookmaker="stake",
                game="cs2",
                tournament="CCT South America",
                start_time=datetime(2026, 6, 28, 16, 0, tzinfo=timezone.utc),
                team_a="Yawara eSports",
                odds_a=1.5,
                team_b="Blitzkrieg",
                odds_b=2.6,
                market_type="h2h",
                event_url="https://example.test/stake/yaw-bk",
                raw={},
            )
        ]

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_tournament_id="101",
            polymarket_keyset_url=[
                "https://gamma-api.polymarket.com/events/keyset?tag_id=100639"
            ],
            polymarket_keyset_max_pages=1,
            providers={
                "oddspapi": FakeOddsPapiProvider(stake_events),
                "polymarket": KeysetAwarePolymarketProvider(),
            },
            keyset_fetcher=lambda _url: payload,
        )

        self.assertEqual(result["summary"]["polymarket_count"], 1)
        self.assertEqual(result["summary"]["matched_count"], 1)
        self.assertEqual(result["debug"]["polymarket_keyset_url_count"], 1)
        self.assertEqual(result["debug"]["polymarket_keyset_page_count"], 1)
        self.assertEqual(result["debug"]["polymarket_keyset_event_count_raw"], 1)
        self.assertEqual(result["debug"]["polymarket_keyset_normalized_count"], 1)
        self.assertEqual(result["debug"]["polymarket_keyset_rejected_count"], 2)

    def test_scan_uses_sport_filtered_keyset_events_for_counts_hints_and_matching(
        self,
    ) -> None:
        stake_events = [
            ProviderOddsEvent(
                provider="oddspapi",
                bookmaker="stake",
                game="cs2",
                tournament="CCT South America",
                start_time=datetime(2026, 6, 28, 16, 0, tzinfo=timezone.utc),
                team_a="Yawara eSports",
                odds_a=1.5,
                team_b="Blitzkrieg",
                odds_b=2.6,
                market_type="h2h",
                event_url="https://example.test/stake/yaw-bk",
                raw={},
            )
        ]

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_tournament_id="101",
            polymarket_keyset_url=[
                "https://gamma-api.polymarket.com/events/keyset?tag_id=100639"
            ],
            polymarket_keyset_max_pages=1,
            providers={
                "oddspapi": FakeOddsPapiProvider(stake_events),
                "polymarket": KeysetAwarePolymarketProvider(),
            },
            keyset_fetcher=lambda _url: _mixed_esports_keyset_payload(),
        )

        self.assertEqual(result["summary"]["polymarket_count"], 1)
        self.assertEqual(result["summary"]["matched_count"], 1)
        self.assertEqual(
            result["debug"]["polymarket_keyset_event_count_before_sport_filter"],
            6,
        )
        self.assertEqual(
            result["debug"]["polymarket_keyset_event_count_after_sport_filter"],
            1,
        )
        self.assertEqual(
            result["debug"]["polymarket_keyset_sport_filter_rejected_count"],
            5,
        )
        event_urls = ",".join(
            event["event_url"] for event in result["polymarket_events"]
        )
        self.assertIn("cs2-yaw-bk-2026-06-28", event_urls)
        self.assertNotIn("overwatch", event_urls.casefold())
        self.assertNotIn("rainbow", event_urls.casefold())
        self.assertNotIn("hok", event_urls.casefold())
        self.assertNotIn("mlp", event_urls.casefold())

    def test_cs2_tournament_quality_filter_keeps_default_allowed_buckets(
        self,
    ) -> None:
        discovery = discover_polymarket_keyset_events(
            KeysetAwarePolymarketProvider(),
            sport="cs2",
            keyset_urls=["https://gamma-api.polymarket.com/events/keyset"],
            max_pages=1,
            fetcher=lambda _url: _cs2_tournament_policy_payload(),
        )

        result = filter_polymarket_events_by_tournament_quality(
            discovery["events"],
            sport="cs2",
            min_quality="b",
            allow_unknown=False,
            include_low_tier=False,
        )

        kept_buckets = {
            item.split("|", 1)[0] for item in result["kept_sample"]
        }
        self.assertIn("CCT South America Series", kept_buckets)
        self.assertIn("CCT Europe Series", kept_buckets)
        self.assertIn("Super DraculaN", kept_buckets)
        self.assertIn("XSE Pro League 2026", kept_buckets)
        self.assertEqual(result["kept_count"], 4)

    def test_cs2_tournament_quality_filter_rejects_default_low_tier_buckets(
        self,
    ) -> None:
        discovery = discover_polymarket_keyset_events(
            KeysetAwarePolymarketProvider(),
            sport="cs2",
            keyset_urls=["https://gamma-api.polymarket.com/events/keyset"],
            max_pages=1,
            fetcher=lambda _url: _cs2_tournament_policy_payload(),
        )

        result = filter_polymarket_events_by_tournament_quality(
            discovery["events"],
            sport="cs2",
            min_quality="b",
            allow_unknown=False,
            include_low_tier=False,
        )

        rejected_buckets = {
            item.split("|", 1)[0] for item in result["rejected_sample"]
        }
        self.assertIn("European Pro League", rejected_buckets)
        self.assertIn("United21", rejected_buckets)
        self.assertIn("Gamers Club Liga Serie A", rejected_buckets)
        self.assertIn("Urban Riga Open", rejected_buckets)
        self.assertEqual(result["rejected_count"], 4)

    def test_cs2_tournament_quality_filter_includes_low_tier_when_enabled(
        self,
    ) -> None:
        discovery = discover_polymarket_keyset_events(
            KeysetAwarePolymarketProvider(),
            sport="cs2",
            keyset_urls=["https://gamma-api.polymarket.com/events/keyset"],
            max_pages=1,
            fetcher=lambda _url: _cs2_tournament_policy_payload(),
        )

        result = filter_polymarket_events_by_tournament_quality(
            discovery["events"],
            sport="cs2",
            min_quality="b",
            allow_unknown=False,
            include_low_tier=True,
        )

        self.assertEqual(result["kept_count"], 8)
        self.assertEqual(result["rejected_count"], 0)
        self.assertEqual(
            result["by_bucket"]["European Pro League"]["reason"],
            "low_tier_included",
        )

    def test_lol_msi_quality_aliases_infer_s(self) -> None:
        for tournament in ("MSI", "Mid-Season Invitational", "Mid Season Invitational"):
            self.assertEqual(
                _tournament_quality_for_sport(tournament, sport="lol"),
                "s",
            )

    def test_lol_prime_league_quality_aliases_infer_b(self) -> None:
        for tournament in (
            "Prime League Pro Division",
            "Prime League 1st Division",
            "Prime League 1. Liga",
            "Techniker Prime League 1st Division",
        ):
            self.assertEqual(
                _tournament_quality_for_sport(tournament, sport="lol"),
                "b",
            )

    def test_lol_tournament_quality_filter_allows_high_quality_buckets(
        self,
    ) -> None:
        discovery = discover_polymarket_keyset_events(
            KeysetAwarePolymarketProvider(),
            sport="lol",
            keyset_urls=["https://gamma-api.polymarket.com/events/keyset"],
            max_pages=1,
            fetcher=lambda _url: _lol_tournament_policy_payload(),
        )

        result = filter_polymarket_events_by_tournament_quality(
            discovery["events"],
            sport="lol",
            min_quality="b",
            allow_unknown=True,
            include_low_tier=True,
        )

        kept_buckets = {item.split("|", 1)[0] for item in result["kept_sample"]}
        self.assertIn("MSI", kept_buckets)
        self.assertIn("Worlds", kept_buckets)
        self.assertIn("Esports World Cup", kept_buckets)
        self.assertIn("LEC", kept_buckets)
        self.assertIn("LCK", kept_buckets)
        self.assertIn("LPL", kept_buckets)
        self.assertIn("LCS", kept_buckets)
        self.assertIn("LTA", kept_buckets)
        self.assertIn("Prime League 1st Division", kept_buckets)
        self.assertEqual(result["kept_count"], 12)

    def test_lol_tournament_quality_filter_rejects_lower_and_minor_buckets(
        self,
    ) -> None:
        discovery = discover_polymarket_keyset_events(
            KeysetAwarePolymarketProvider(),
            sport="lol",
            keyset_urls=["https://gamma-api.polymarket.com/events/keyset"],
            max_pages=1,
            fetcher=lambda _url: _lol_tournament_policy_payload(),
        )

        result = filter_polymarket_events_by_tournament_quality(
            discovery["events"],
            sport="lol",
            min_quality="b",
            allow_unknown=True,
            include_low_tier=True,
        )

        rejected = result["by_bucket"]
        self.assertEqual(
            rejected["Prime League 2nd Division"]["reason"],
            "lol_blocked_prime_league_lower_division",
        )
        self.assertEqual(
            rejected["Prime League 3rd Division"]["reason"],
            "lol_blocked_prime_league_lower_division",
        )
        self.assertEqual(
            rejected["Academy League"]["reason"],
            "lol_blocked_academy",
        )
        self.assertEqual(
            rejected["LCK Challengers"]["reason"],
            "lol_blocked_challengers",
        )
        self.assertEqual(rejected["NACL"]["reason"], "lol_blocked_nacl")
        self.assertEqual(
            rejected["EMEA Masters"]["reason"],
            "lol_blocked_emea_masters",
        )
        self.assertEqual(
            rejected["Worlds Open Qualifier"]["reason"],
            "lol_blocked_qualifier",
        )
        self.assertEqual(
            rejected["Promotion Relegation"]["reason"],
            "lol_blocked_promotion",
        )
        self.assertEqual(
            rejected["Random Regional Cup"]["reason"],
            "lol_blocked_regional_cup",
        )
        self.assertEqual(
            rejected["Prime League"]["reason"],
            "lol_prime_league_subdivision_required",
        )
        self.assertEqual(result["rejected_count"], 10)

    def test_dota2_preseed_searches_create_buckets_before_stake_discovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            stake = CacheAwareOddsPapiProvider(
                [
                    {
                        "id": "38029",
                        "name": "European Pro League",
                        "category": "Dota 2",
                    }
                ],
                {
                    "38029": [
                        _stake_event_for_tournament(
                            tournament_id="38029",
                            tournament="European Pro League",
                            team_a="Team Spirit",
                            team_b="Gaimin Gladiators",
                            game="dota2",
                            start_time="2026-07-10T16:00:00Z",
                        )
                    ]
                },
            )
            polymarket = PayloadSearchPolymarketProvider(
                {
                    "European Pro League Dota 2": _keyset_payload(
                        [
                            _keyset_match_event(
                                sport_title="Dota 2",
                                slug="dota2-spirit-gladiators-epl-2026-07-10",
                                team_a="Team Spirit",
                                team_b="Gaimin Gladiators",
                                category="Dota 2",
                                tournament="European Pro League",
                                start_time="2026-07-10T16:00:00Z",
                            )
                        ]
                    )
                }
            )

            result = run_scan(
                sport="dota2",
                bookmaker="stake",
                stake_auto_discover_tournaments=True,
                stake_request_delay_seconds=0,
                active_tournament_cache_path=cache_dir / "active.json",
                tournament_list_cache_path=cache_dir / "tournaments.json",
                polymarket_preseed_search=["European Pro League Dota 2"],
                polymarket_keyset_url=["https://gamma-api.polymarket.com/events/keyset"],
                polymarket_keyset_max_pages=1,
                max_oddspapi_requests_per_run=4,
                providers={
                    "oddspapi": stake,
                    "polymarket": polymarket,
                },
                keyset_fetcher=lambda _url: _keyset_payload(
                    [
                        _keyset_match_event(
                            sport_title="HOK",
                            slug="hok-bue-tf-2026-07-10",
                            team_a="BUE",
                            team_b="TF",
                            category="Honor of Kings",
                        )
                    ]
                ),
                stake_sleep=lambda _seconds: None,
            )

        self.assertEqual(result["debug"]["polymarket_keyset_event_count_after_sport_filter"], 0)
        self.assertEqual(result["debug"]["polymarket_preseed_enabled"], True)
        self.assertEqual(
            result["debug"]["polymarket_preseed_searches"],
            ["European Pro League Dota 2"],
        )
        self.assertEqual(result["debug"]["polymarket_preseed_event_count_after_sport_filter"], 1)
        self.assertEqual(
            result["debug"]["polymarket_tournament_bucket_sources"],
            {"European Pro League": "search"},
        )
        self.assertEqual(result["debug"]["stake_tournament_selected_ids"], ["38029"])
        self.assertEqual(stake.tournament_list_calls[0]["sport"], "dota2")
        self.assertEqual(stake.batch_calls[0]["tournament_ids"], ["38029"])
        self.assertEqual(result["summary"]["stake_count"], 1)
        self.assertEqual(result["summary"]["polymarket_count"], 1)
        self.assertEqual(result["summary"]["matched_count"], 1)

    def test_dota2_preseed_does_not_accept_tournament_name_without_team_overlap(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            stake = CacheAwareOddsPapiProvider(
                [
                    {
                        "id": "38029",
                        "name": "European Pro League",
                        "category": "Dota 2",
                    }
                ],
                {
                    "38029": [
                        _stake_event_for_tournament(
                            tournament_id="38029",
                            tournament="European Pro League",
                            team_a="Unrelated Alpha",
                            team_b="Unrelated Beta",
                            game="dota2",
                            start_time="2026-07-10T16:00:00Z",
                        )
                    ]
                },
            )
            polymarket = PayloadSearchPolymarketProvider(
                {
                    "European Pro League Dota 2": _keyset_payload(
                        [
                            _keyset_match_event(
                                sport_title="Dota 2",
                                slug="dota2-spirit-gladiators-epl-2026-07-10",
                                team_a="Team Spirit",
                                team_b="Gaimin Gladiators",
                                category="Dota 2",
                                tournament="European Pro League",
                                start_time="2026-07-10T16:00:00Z",
                            )
                        ]
                    )
                }
            )

            result = run_scan(
                sport="dota2",
                bookmaker="stake",
                stake_auto_discover_tournaments=True,
                stake_request_delay_seconds=0,
                active_tournament_cache_path=cache_dir / "active.json",
                tournament_list_cache_path=cache_dir / "tournaments.json",
                polymarket_preseed_search=["European Pro League Dota 2"],
                max_oddspapi_requests_per_run=4,
                providers={
                    "oddspapi": stake,
                    "polymarket": polymarket,
                },
                stake_sleep=lambda _seconds: None,
            )

        self.assertEqual(
            result["debug"]["polymarket_tournament_bucket_sources"],
            {"European Pro League": "search"},
        )
        self.assertEqual(stake.batch_calls[0]["tournament_ids"], ["38029"])
        self.assertEqual(result["debug"]["stake_tournament_selected_ids"], [])
        self.assertEqual(result["summary"]["stake_count"], 0)
        self.assertEqual(result["summary"]["matched_count"], 0)
        self.assertEqual(
            result["debug"]["stake_auto_stop_reason"],
            "unresolved_tournament_buckets",
        )

    def test_dota2_page_preseed_creates_buckets_before_stake_discovery(
        self,
    ) -> None:
        html = (FIXTURE_DIR / "polymarket_dota2_page.html").read_text(
            encoding="utf-8"
        )
        event_payloads, market_payloads = _dota2_page_slug_payloads()
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            stake = CacheAwareOddsPapiProvider(
                [
                    {
                        "id": "38029",
                        "name": "European Pro League",
                        "category": "Dota 2",
                    },
                    {
                        "id": "47329",
                        "name": "Esports World Cup",
                        "category": "Dota 2",
                    },
                ],
                {
                    "38029": [
                        _stake_event_for_tournament(
                            tournament_id="38029",
                            tournament="European Pro League",
                            team_a="Team Spirit",
                            team_b="Gaimin Gladiators",
                            game="dota2",
                            start_time="2026-07-10T16:00:00Z",
                        )
                    ],
                    "47329": [
                        _stake_event_for_tournament(
                            tournament_id="47329",
                            tournament="Esports World Cup",
                            team_a="Team Falcons",
                            team_b="BetBoom",
                            game="dota2",
                            start_time="2026-07-10T18:00:00Z",
                        )
                    ],
                },
            )
            polymarket = PayloadSearchPolymarketProvider(
                payloads_by_event_slug=event_payloads,
                payloads_by_market_slug=market_payloads,
            )

            result = run_scan(
                sport="dota2",
                bookmaker="stake",
                stake_auto_discover_tournaments=True,
                stake_request_delay_seconds=0,
                active_tournament_cache_path=cache_dir / "active.json",
                tournament_list_cache_path=cache_dir / "tournaments.json",
                polymarket_page_preseed_url=[
                    "https://polymarket.com/esports/dota-2"
                ],
                max_oddspapi_requests_per_run=4,
                providers={
                    "oddspapi": stake,
                    "polymarket": polymarket,
                },
                page_fetcher=lambda _url: html,
                stake_sleep=lambda _seconds: None,
            )

        self.assertEqual(result["debug"]["polymarket_page_preseed_enabled"], True)
        self.assertEqual(result["debug"]["polymarket_page_preseed_candidate_slug_count"], 4)
        self.assertEqual(result["debug"]["polymarket_page_preseed_event_count_raw"], 4)
        self.assertEqual(
            result["debug"]["polymarket_page_preseed_event_count_after_safety_filter"],
            2,
        )
        self.assertEqual(
            result["debug"]["polymarket_page_preseed_event_count_after_sport_filter"],
            1,
        )
        self.assertEqual(
            result["debug"]["polymarket_tournament_bucket_sources"],
            {"European Pro League": "page"},
        )
        self.assertEqual(stake.tournament_list_calls[0]["sport"], "dota2")
        self.assertEqual(stake.batch_calls[0]["tournament_ids"], ["38029"])
        self.assertEqual(result["debug"]["stake_tournament_attempted_names"], ["European Pro League"])
        self.assertEqual(result["debug"]["stake_tournament_selected_names"], ["European Pro League"])
        self.assertEqual(result["debug"]["stake_tournament_selected_ids"], ["38029"])
        self.assertEqual(result["summary"]["stake_count"], 1)
        self.assertEqual(result["summary"]["polymarket_count"], 1)
        self.assertEqual(result["summary"]["matched_count"], 1)
        self.assertEqual(
            polymarket.last_debug["normalized_samples"][0]["detected_best_of"],
            "BO3",
        )
        self.assertFalse(
            any(call.get("search") == "dota2" for call in polymarket.calls)
        )

    def test_dota2_page_preseed_does_not_accept_tournament_name_without_team_overlap(
        self,
    ) -> None:
        html = (FIXTURE_DIR / "polymarket_dota2_page.html").read_text(
            encoding="utf-8"
        )
        event_payloads, market_payloads = _dota2_page_slug_payloads()
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            stake = CacheAwareOddsPapiProvider(
                [
                    {
                        "id": "38029",
                        "name": "European Pro League",
                        "category": "Dota 2",
                    }
                ],
                {
                    "38029": [
                        _stake_event_for_tournament(
                            tournament_id="38029",
                            tournament="European Pro League",
                            team_a="Unrelated Alpha",
                            team_b="Unrelated Beta",
                            game="dota2",
                            start_time="2026-07-10T16:00:00Z",
                        )
                    ]
                },
            )
            polymarket = PayloadSearchPolymarketProvider(
                payloads_by_event_slug=event_payloads,
                payloads_by_market_slug=market_payloads,
            )

            result = run_scan(
                sport="dota2",
                bookmaker="stake",
                stake_auto_discover_tournaments=True,
                stake_request_delay_seconds=0,
                active_tournament_cache_path=cache_dir / "active.json",
                tournament_list_cache_path=cache_dir / "tournaments.json",
                polymarket_page_preseed_url=[
                    "https://polymarket.com/uk/esports/dota-2"
                ],
                max_oddspapi_requests_per_run=4,
                providers={
                    "oddspapi": stake,
                    "polymarket": polymarket,
                },
                page_fetcher=lambda _url: html,
                stake_sleep=lambda _seconds: None,
            )

        self.assertEqual(
            result["debug"]["polymarket_tournament_bucket_sources"],
            {"European Pro League": "page"},
        )
        self.assertEqual(stake.batch_calls[0]["tournament_ids"], ["38029"])
        self.assertEqual(result["debug"]["stake_tournament_selected_ids"], [])
        self.assertEqual(result["summary"]["stake_count"], 0)
        self.assertEqual(result["summary"]["matched_count"], 0)
        self.assertEqual(
            result["debug"]["stake_auto_stop_reason"],
            "unresolved_tournament_buckets",
        )

    def test_dota2_page_preseed_stops_without_stake_when_only_unsafe_markets(
        self,
    ) -> None:
        ewc_slug = "dota2-team-falcons-vs-betboom-ewc-2026-07-10"
        map_slug = "dota2-team-spirit-vs-gaimin-gladiators-map-1-2026-07-10"
        html = (
            f'<a href="/esports/dota-2/{ewc_slug}">Falcons vs BetBoom</a>'
            f'<a href="/esports/dota-2/{map_slug}">Spirit vs Gladiators</a>'
        )
        ewc_event = _keyset_match_event(
            sport_title="Dota 2",
            slug=ewc_slug,
            team_a="Team Falcons",
            team_b="BetBoom",
            category="Dota 2",
            tournament="Esports World Cup",
            start_time="2026-07-10T18:00:00Z",
        )
        ewc_event["title"] = "Dota 2: Team Falcons vs BetBoom - Esports World Cup"
        ewc_event["markets"][0]["question"] = (
            "Dota 2: Team Falcons vs BetBoom Match Winner"
        )
        map_event = _keyset_match_event(
            sport_title="Dota 2",
            slug=map_slug,
            team_a="Team Spirit",
            team_b="Gaimin Gladiators",
            category="Dota 2",
            tournament="European Pro League",
            start_time="2026-07-10T16:00:00Z",
        )
        map_event["markets"][0]["question"] = (
            "Dota 2: Team Spirit vs Gaimin Gladiators Game 1 Winner"
        )

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            stake = CacheAwareOddsPapiProvider(
                [
                    {
                        "id": "38029",
                        "name": "European Pro League",
                        "category": "Dota 2",
                    },
                    {
                        "id": "47329",
                        "name": "Esports World Cup",
                        "category": "Dota 2",
                    },
                ],
                {},
            )
            polymarket = PayloadSearchPolymarketProvider(
                payloads_by_event_slug={
                    ewc_slug: _keyset_payload([ewc_event]),
                    map_slug: _keyset_payload([map_event]),
                }
            )

            result = run_scan(
                sport="dota2",
                bookmaker="stake",
                stake_auto_discover_tournaments=True,
                stake_request_delay_seconds=0,
                active_tournament_cache_path=cache_dir / "active.json",
                tournament_list_cache_path=cache_dir / "tournaments.json",
                polymarket_page_preseed_url=[
                    "https://polymarket.com/esports/dota-2"
                ],
                max_oddspapi_requests_per_run=4,
                providers={
                    "oddspapi": stake,
                    "polymarket": polymarket,
                },
                page_fetcher=lambda _url: html,
                stake_sleep=lambda _seconds: None,
            )

        self.assertEqual(result["summary"]["polymarket_count"], 0)
        self.assertEqual(result["summary"]["stake_count"], 0)
        self.assertEqual(result["summary"]["matched_count"], 0)
        self.assertEqual(result["debug"]["stake_auto_stop_reason"], "no_tournament_buckets")
        self.assertEqual(result["debug"]["oddspapi_total_request_count"], 0)
        self.assertEqual(result["debug"]["polymarket_tournament_bucket_count"], 0)
        self.assertEqual(result["debug"]["polymarket_tournament_bucket_sources"], {})
        self.assertEqual(
            result["debug"]["polymarket_page_preseed_reject_reason_counts"],
            {
                "ewc_or_draw_risk_tournament": 1,
                "map_or_game_winner": 1,
            },
        )
        self.assertEqual(
            result["debug"]["polymarket_page_preseed_event_count_after_safety_filter"],
            0,
        )
        self.assertEqual(result["debug"]["dota_tournament_policy_rejected_count"], 1)
        self.assertEqual(result["debug"]["dota_unsafe_tournament_reasons"], {"EWC": 1})
        self.assertEqual(stake.tournament_list_calls, [])
        self.assertEqual(stake.batch_calls, [])

    def test_active_cache_uses_cct_and_xse_without_tournament_discovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            active_cache_path = cache_dir / "active.json"
            tournament_list_cache_path = cache_dir / "tournaments.json"
            _write_test_json(
                active_cache_path,
                _active_tournament_cache(
                    {
                        "CCT South America Series": (
                            "51042",
                            "CCT South American Series",
                        ),
                        "XSE Pro League": ("51296", "XSE Pro League"),
                    }
                ),
            )
            stake = CacheAwareOddsPapiProvider(
                [],
                {
                    "51042": [
                        _stake_event_for_tournament(
                            tournament_id="51042",
                            tournament="CCT South American Series",
                            team_a="Yawara eSports",
                            team_b="Blitzkrieg",
                        )
                    ],
                    "51296": [
                        _stake_event_for_tournament(
                            tournament_id="51296",
                            tournament="XSE Pro League",
                            team_a="XSE Alpha",
                            team_b="Pro Beta",
                            start_time="2026-06-28T18:00:00Z",
                        )
                    ],
                },
            )

            result = run_scan(
                sport="cs2",
                bookmaker="stake",
                stake_auto_discover_tournaments=True,
                stake_request_delay_seconds=0,
                allow_unknown_tournaments=True,
                active_tournament_cache_path=active_cache_path,
                tournament_list_cache_path=tournament_list_cache_path,
                polymarket_keyset_url=["https://gamma-api.polymarket.com/events/keyset"],
                polymarket_keyset_max_pages=1,
                providers={
                    "oddspapi": stake,
                    "polymarket": KeysetAwarePolymarketProvider(),
                },
                keyset_fetcher=lambda _url: _cct_xse_keyset_payload(),
                stake_sleep=lambda _seconds: None,
            )

        self.assertEqual(stake.tournament_list_calls, [])
        self.assertEqual(stake.batch_calls[0]["tournament_ids"], ["51042", "51296"])
        self.assertEqual(result["debug"]["active_tournament_cache_hit_count"], 2)
        self.assertEqual(result["debug"]["active_tournament_cache_miss_count"], 0)
        self.assertEqual(
            result["debug"]["stake_tournament_selected_ids"],
            ["51042", "51296"],
        )
        self.assertEqual(result["summary"]["stake_count"], 2)
        self.assertEqual(result["summary"]["matched_count"], 2)

    def test_lol_active_cache_is_separate_from_cs2_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            active_cache_path = cache_dir / "active.json"
            tournament_list_cache_path = cache_dir / "tournaments.json"
            _write_test_json(
                active_cache_path,
                _active_tournament_cache({"LCK": ("99999", "LCK")}, sport="cs2"),
            )
            stake = CacheAwareOddsPapiProvider(
                [{"id": "18001", "name": "LCK", "category": "League of Legends"}],
                {
                    "18001": [
                        _stake_event_for_tournament(
                            tournament_id="18001",
                            tournament="LCK",
                            team_a="Hanwha Life",
                            team_b="KT Rolster",
                            game="lol",
                            start_time="2026-07-10T16:00:00Z",
                        )
                    ]
                },
            )

            result = run_scan(
                sport="league-of-legends",
                bookmaker="stake",
                stake_auto_discover_tournaments=True,
                stake_request_delay_seconds=0,
                lol_tournament_scope="all",
                active_tournament_cache_path=active_cache_path,
                tournament_list_cache_path=tournament_list_cache_path,
                polymarket_keyset_url=["https://gamma-api.polymarket.com/events/keyset"],
                polymarket_keyset_max_pages=1,
                providers={
                    "oddspapi": stake,
                    "polymarket": KeysetAwarePolymarketProvider(),
                },
                keyset_fetcher=lambda _url: _keyset_payload(
                    [
                        _keyset_match_event(
                            sport_title="League of Legends",
                            slug="league-of-legends-hanwha-kt-2026-07-10",
                            team_a="Hanwha Life",
                            team_b="KT Rolster",
                            category="League of Legends",
                            tournament="LCK",
                            start_time="2026-07-10T16:00:00Z",
                        )
                    ]
                ),
                stake_sleep=lambda _seconds: None,
            )
            updated_cache = json.loads(active_cache_path.read_text(encoding="utf-8"))

        self.assertEqual(result["summary"]["matched_count"], 1)
        self.assertEqual(result["debug"]["active_tournament_cache_hit_count"], 0)
        self.assertEqual(result["debug"]["active_tournament_cache_misses"], ["LCK"])
        self.assertEqual(result["debug"]["stake_tournament_selected_ids"], ["18001"])
        self.assertEqual(stake.tournament_list_calls[0]["sport"], "league-of-legends")
        self.assertEqual(stake.batch_calls[0]["sport"], "league-of-legends")
        self.assertIn("cs2", updated_cache)
        self.assertIn("lol", updated_cache)
        self.assertEqual(updated_cache["cs2"]["stake"]["lck"]["tournament_id"], 99999)
        self.assertEqual(updated_cache["lol"]["stake"]["lck"]["tournament_id"], 18001)

    def test_lol_default_scope_discovers_multiple_buckets_with_market181_profile(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            stake = CacheAwareOddsPapiProvider(
                [
                    {
                        "id": "81001",
                        "name": "Mid-Season Invitational",
                        "category": "League of Legends",
                    },
                    {
                        "id": "18001",
                        "name": "LCK",
                        "category": "League of Legends",
                    },
                ],
                {
                    "81001": [
                        _stake_event_for_tournament(
                            tournament_id="81001",
                            tournament="Mid-Season Invitational",
                            team_a="T1",
                            team_b="Gen.G",
                            game="lol",
                            start_time="2026-07-10T16:00:00Z",
                        )
                    ],
                    "18001": [
                        _stake_event_for_tournament(
                            tournament_id="18001",
                            tournament="LCK",
                            team_a="Hanwha Life",
                            team_b="KT Rolster",
                            game="lol",
                            start_time="2026-07-10T18:00:00Z",
                        )
                    ],
                },
            )

            result = run_scan(
                sport="lol",
                bookmaker="stake",
                stake_auto_discover_tournaments=True,
                stake_auto_max_tournaments=4,
                stake_request_delay_seconds=0,
                active_tournament_cache_path=cache_dir / "active.json",
                tournament_list_cache_path=cache_dir / "tournaments.json",
                max_discovery_candidates_per_bucket=2,
                max_oddspapi_requests_per_run=4,
                polymarket_keyset_url=["https://gamma-api.polymarket.com/events/keyset"],
                polymarket_keyset_max_pages=1,
                providers={
                    "oddspapi": stake,
                    "polymarket": KeysetAwarePolymarketProvider(),
                },
                keyset_fetcher=lambda _url: _keyset_payload(
                    [
                        _keyset_match_event(
                            sport_title="League of Legends",
                            slug="league-of-legends-t1-vs-geng-msi-2026-07-10",
                            team_a="T1",
                            team_b="Gen.G",
                            category="League of Legends",
                            tournament="MSI",
                            start_time="2026-07-10T16:00:00Z",
                        ),
                        _keyset_match_event(
                            sport_title="League of Legends",
                            slug="league-of-legends-hanwha-vs-kt-lck-2026-07-10",
                            team_a="Hanwha Life",
                            team_b="KT Rolster",
                            category="League of Legends",
                            tournament="LCK",
                            start_time="2026-07-10T18:00:00Z",
                        ),
                    ]
                ),
                stake_sleep=lambda _seconds: None,
            )

        self.assertEqual(result["debug"]["lol_tournament_scope"], "all")
        self.assertEqual(result["debug"]["polymarket_keyset_scope_rejected_count"], 0)
        self.assertEqual(
            result["debug"]["stake_tournament_selected_ids"],
            ["81001", "18001"],
        )
        self.assertEqual(result["summary"]["matched_count"], 2)
        self.assertEqual(
            result["debug"]["polymarket_team_pair_count_by_bucket"],
            {"MSI": 1, "LCK": 1},
        )
        self.assertEqual(
            result["debug"]["selected_tournament_ids_by_bucket"],
            {"MSI": ["81001"], "LCK": ["18001"]},
        )
        self.assertTrue(stake.batch_calls)
        self.assertTrue(
            all(
                call["mapping_profile"]
                == MAPPING_PROFILE_ODDSPAPI_STAKE_LOL_MARKET181_SELECTION_ORDER
                for call in stake.batch_calls
            )
        )

    def test_cached_tournament_list_avoids_fresh_tournament_list_call(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            tournament_list_cache_path = cache_dir / "tournaments.json"
            active_cache_path = cache_dir / "active.json"
            _write_test_json(
                tournament_list_cache_path,
                _tournament_list_cache(
                    [
                        {
                            "id": "51042",
                            "name": "CCT South American Series",
                            "slug": "cct-south-american-series",
                        }
                    ]
                ),
            )
            stake = CacheAwareOddsPapiProvider(
                [{"id": "99999", "name": "Should Not Be Fetched"}],
                {
                    "51042": [
                        _stake_event_for_tournament(
                            tournament_id="51042",
                            tournament="CCT South American Series",
                            team_a="Yawara eSports",
                            team_b="Blitzkrieg",
                        )
                    ]
                },
            )

            result = run_scan(
                sport="cs2",
                bookmaker="stake",
                stake_auto_discover_tournaments=True,
                stake_request_delay_seconds=0,
                active_tournament_cache_path=active_cache_path,
                tournament_list_cache_path=tournament_list_cache_path,
                polymarket_keyset_url=["https://gamma-api.polymarket.com/events/keyset"],
                polymarket_keyset_max_pages=1,
                providers={
                    "oddspapi": stake,
                    "polymarket": KeysetAwarePolymarketProvider(),
                },
                keyset_fetcher=lambda _url: _keyset_payload(
                    [
                        _keyset_match_event(
                            sport_title="Counter-Strike",
                            slug="cs2-yawara-blitzkrieg-2026-06-28",
                            team_a="Yawara eSports",
                            team_b="Blitzkrieg",
                            category="Counter-Strike",
                            tournament="CCT South America Series",
                        )
                    ]
                ),
                stake_sleep=lambda _seconds: None,
            )

        self.assertEqual(stake.tournament_list_calls, [])
        self.assertTrue(result["debug"]["oddspapi_tournament_list_cache_hit"])
        self.assertEqual(result["debug"]["oddspapi_tournament_list_request_count"], 0)
        self.assertEqual(stake.batch_calls[0]["tournament_ids"], ["51042"])

    def test_stale_tournament_list_cache_refreshes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            tournament_list_cache_path = cache_dir / "tournaments.json"
            active_cache_path = cache_dir / "active.json"
            _write_test_json(
                tournament_list_cache_path,
                _tournament_list_cache([], fetched_at="2000-01-01T00:00:00Z"),
            )
            stake = CacheAwareOddsPapiProvider(
                [{"id": "51042", "name": "CCT South American Series"}],
                {
                    "51042": [
                        _stake_event_for_tournament(
                            tournament_id="51042",
                            tournament="CCT South American Series",
                            team_a="Yawara eSports",
                            team_b="Blitzkrieg",
                        )
                    ]
                },
            )

            result = run_scan(
                sport="cs2",
                bookmaker="stake",
                stake_auto_discover_tournaments=True,
                stake_request_delay_seconds=0,
                active_tournament_cache_path=active_cache_path,
                tournament_list_cache_path=tournament_list_cache_path,
                polymarket_keyset_url=["https://gamma-api.polymarket.com/events/keyset"],
                polymarket_keyset_max_pages=1,
                providers={
                    "oddspapi": stake,
                    "polymarket": KeysetAwarePolymarketProvider(),
                },
                keyset_fetcher=lambda _url: _keyset_payload(
                    [
                        _keyset_match_event(
                            sport_title="Counter-Strike",
                            slug="cs2-yawara-blitzkrieg-2026-06-28",
                            team_a="Yawara eSports",
                            team_b="Blitzkrieg",
                            category="Counter-Strike",
                            tournament="CCT South America Series",
                        )
                    ]
                ),
                stake_sleep=lambda _seconds: None,
            )

        self.assertEqual(len(stake.tournament_list_calls), 1)
        self.assertEqual(result["debug"]["oddspapi_tournament_list_request_count"], 1)

    def test_strict_tournament_matching_examples(self) -> None:
        self.assertEqual(
            _score_strict_tournament_candidates(
                "CCT South America Series",
                [{"id": "51042", "name": "CCT South American Series"}],
            )[0]["id"],
            "51042",
        )
        self.assertEqual(
            _score_strict_tournament_candidates(
                "CCT South America Series",
                [{"id": "999", "name": "South America Cup"}],
            ),
            [],
        )
        self.assertEqual(
            _score_strict_tournament_candidates(
                "XSE Pro League",
                [{"id": "51296", "name": "XSE Pro League"}],
            )[0]["id"],
            "51296",
        )
        self.assertEqual(
            _score_strict_tournament_candidates(
                "XSE Pro League",
                [{"id": "1", "name": "Gluck SixSeven"}],
            ),
            [],
        )

    def test_lol_msi_scope_resolves_mid_season_invitational_by_team_overlap(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            stake = CacheAwareOddsPapiProvider(
                [
                    {
                        "id": "81001",
                        "name": "Mid-Season Invitational",
                        "category": "League of Legends",
                    },
                    {
                        "id": "19002",
                        "name": "Prime League 1st Division",
                        "category": "League of Legends",
                    },
                ],
                {
                    "81001": [
                        _stake_event_for_tournament(
                            tournament_id="81001",
                            tournament="Mid-Season Invitational",
                            team_a="T1",
                            team_b="Gen.G",
                            game="lol",
                            start_time="2026-07-10T16:00:00Z",
                        )
                    ],
                    "19002": [
                        _stake_event_for_tournament(
                            tournament_id="19002",
                            tournament="Prime League 1st Division",
                            team_a="Berlin Alpha",
                            team_b="Mouz Beta",
                            game="lol",
                            start_time="2026-07-10T18:00:00Z",
                        )
                    ],
                },
            )

            result = run_scan(
                sport="lol",
                bookmaker="stake",
                stake_auto_discover_tournaments=True,
                stake_auto_max_tournaments=2,
                stake_request_delay_seconds=0,
                lol_tournament_scope="msi",
                active_tournament_cache_path=cache_dir / "active.json",
                tournament_list_cache_path=cache_dir / "tournaments.json",
                max_discovery_candidates_per_bucket=2,
                max_oddspapi_requests_per_run=3,
                polymarket_keyset_url=["https://gamma-api.polymarket.com/events/keyset"],
                polymarket_keyset_max_pages=1,
                providers={
                    "oddspapi": stake,
                    "polymarket": KeysetAwarePolymarketProvider(),
                },
                keyset_fetcher=lambda _url: _keyset_payload(
                    [
                        _keyset_match_event(
                            sport_title="League of Legends",
                            slug="league-of-legends-t1-vs-geng-msi-2026-07-10",
                            team_a="T1",
                            team_b="Gen.G",
                            category="League of Legends",
                            tournament="MSI",
                            start_time="2026-07-10T16:00:00Z",
                        ),
                        _keyset_match_event(
                            sport_title="League of Legends",
                            slug="league-of-legends-berlin-vs-mouz-2026-07-10",
                            team_a="Berlin Alpha",
                            team_b="Mouz Beta",
                            category="League of Legends",
                            tournament="Prime League 1st Division",
                            start_time="2026-07-10T18:00:00Z",
                        ),
                    ]
                ),
                stake_sleep=lambda _seconds: None,
            )

        self.assertEqual(result["debug"]["lol_tournament_scope"], "msi")
        self.assertEqual(result["debug"]["stake_tournament_selected_ids"], ["81001"])
        self.assertEqual(stake.batch_calls[0]["tournament_ids"], ["81001"])
        self.assertEqual(result["summary"]["matched_count"], 1)
        self.assertEqual(
            result["debug"]["selected_tournament_ids_by_bucket"],
            {"MSI": ["81001"]},
        )
        self.assertEqual(result["debug"]["polymarket_keyset_scope_rejected_count"], 1)
        self.assertIn(
            "Prime League 1st Division",
            " ".join(result["debug"]["polymarket_keyset_scope_rejected_sample"]),
        )
        overlap = result["debug"]["lol_verified_team_overlap"]["MSI"]
        self.assertEqual(overlap[0]["id"], "81001")
        self.assertEqual(overlap[0]["verified_match_count"], 1)

    def test_lol_explicit_msi_scope_does_not_fetch_prime_league(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            stake = CacheAwareOddsPapiProvider(
                [{"id": "19002", "name": "Prime League 1st Division"}],
                {
                    "19002": [
                        _stake_event_for_tournament(
                            tournament_id="19002",
                            tournament="Prime League 1st Division",
                            team_a="Berlin Alpha",
                            team_b="Mouz Beta",
                            game="lol",
                            start_time="2026-07-10T16:00:00Z",
                        )
                    ]
                },
            )

            result = run_scan(
                sport="lol",
                bookmaker="stake",
                stake_auto_discover_tournaments=True,
                stake_auto_max_tournaments=2,
                stake_request_delay_seconds=0,
                lol_tournament_scope="msi",
                active_tournament_cache_path=cache_dir / "active.json",
                tournament_list_cache_path=cache_dir / "tournaments.json",
                polymarket_keyset_url=["https://gamma-api.polymarket.com/events/keyset"],
                polymarket_keyset_max_pages=1,
                providers={
                    "oddspapi": stake,
                    "polymarket": KeysetAwarePolymarketProvider(),
                },
                keyset_fetcher=lambda _url: _keyset_payload(
                    [
                        _keyset_match_event(
                            sport_title="League of Legends",
                            slug="league-of-legends-berlin-mouz-2026-07-10",
                            team_a="Berlin Alpha",
                            team_b="Mouz Beta",
                            category="League of Legends",
                            tournament="Prime League 1st Division",
                            start_time="2026-07-10T16:00:00Z",
                        )
                    ]
                ),
                stake_sleep=lambda _seconds: None,
            )

        self.assertEqual(result["debug"]["lol_tournament_scope"], "msi")
        self.assertEqual(stake.tournament_list_calls, [])
        self.assertEqual(stake.batch_calls, [])
        self.assertEqual(result["debug"]["stake_tournament_selected_ids"], [])
        self.assertEqual(result["debug"]["polymarket_keyset_scope_rejected_count"], 1)
        self.assertEqual(result["summary"]["matched_count"], 0)

    def test_lol_prime_league_strict_matching_rejects_prime_pokal(self) -> None:
        self.assertEqual(
            _score_strict_tournament_candidates(
                "Prime League 1st Division",
                [{"id": "19000", "name": "Prime Pokal"}],
                sport="lol",
            ),
            [],
        )

    def test_lol_prime_league_candidate_scoring_requires_strong_evidence(
        self,
    ) -> None:
        scored = _score_stake_tournament_candidates(
            [
                {"id": "19000", "name": "Prime Pokal"},
                {"id": "19001", "name": "Prime League"},
                {"id": "19002", "name": "Prime League 1st Division"},
                {"id": "19003", "name": "Prime League 1. Liga"},
                {"id": "19004", "name": "Techniker Prime League 1st Division"},
                {"id": "19005", "name": "Prime League Pro Division"},
            ],
            hints=[
                {
                    "source_hint": "Prime League 1st Division",
                    "tokens": {"prime", "league", "1st", "division"},
                }
            ],
            sport="league-of-legends",
        )
        by_id = {item["id"]: item for item in scored}

        self.assertEqual(float(by_id["19000"]["score"]), 0.0)
        self.assertEqual(
            by_id["19000"]["reason"],
            "rejected_prime_pokal_not_prime_league",
        )
        for candidate_id in ("19001", "19002", "19003", "19004", "19005"):
            self.assertGreater(float(by_id[candidate_id]["score"]), 0.0)

    def test_lol_prime_league_scan_does_not_fetch_prime_pokal(self) -> None:
        stake = AutoDiscoverOddsPapiProvider(
            [
                {"id": "19000", "name": "Prime Pokal"},
                {"id": "19001", "name": "Prime League"},
            ],
            {
                "19000": [
                    _stake_event_for_tournament(
                        tournament_id="19000",
                        tournament="Prime Pokal",
                        team_a="Berlin Alpha",
                        team_b="Mouz Beta",
                        game="lol",
                    )
                ],
                "19001": [
                    _stake_event_for_tournament(
                        tournament_id="19001",
                        tournament="Prime League",
                        team_a="Berlin Alpha",
                        team_b="Mouz Beta",
                        game="lol",
                    )
                ],
            },
        )

        result = run_scan(
            sport="lol",
            bookmaker="stake",
            stake_auto_discover_tournaments=True,
            stake_auto_max_tournaments=2,
            stake_request_delay_seconds=0,
            lol_tournament_scope="all",
            polymarket_keyset_url=["https://gamma-api.polymarket.com/events/keyset"],
            polymarket_keyset_max_pages=1,
            providers={
                "oddspapi": stake,
                "polymarket": KeysetAwarePolymarketProvider(),
            },
            keyset_fetcher=lambda _url: _keyset_payload(
                [
                    _keyset_match_event(
                        sport_title="League of Legends",
                        slug="league-of-legends-berlin-mouz-2026-07-10",
                        team_a="Berlin Alpha",
                        team_b="Mouz Beta",
                        category="League of Legends",
                        tournament="Prime League 1st Division",
                    )
                ]
            ),
            stake_sleep=lambda _seconds: None,
        )

        fetched_tournament_ids = {
            str(call.get("tournament_id")) for call in stake.calls
        }
        self.assertNotIn("19000", fetched_tournament_ids)
        self.assertIn("19001", fetched_tournament_ids)
        self.assertEqual(result["summary"]["matched_count"], 1)
        score_breakdown = result["debug"]["stake_candidate_score_breakdown_by_bucket"][
            "Prime League 1st Division"
        ]
        prime_pokal = next(item for item in score_breakdown if item["id"] == "19000")
        self.assertEqual(
            prime_pokal["reason"],
            "rejected_prime_pokal_not_prime_league",
        )

    def test_lol_prime_league_cache_resolver_verifies_team_probe_and_caches(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            active_cache_path = cache_dir / "active.json"
            tournament_list_cache_path = cache_dir / "tournaments.json"
            stake = CacheAwareOddsPapiProvider(
                [
                    {"id": "19000", "name": "Prime Pokal"},
                    {
                        "id": "19050",
                        "name": "Prime League Pro Division",
                        "slug": (
                            "kaufland-hangry-knights-unicorns-of-love-sexy-edition"
                        ),
                        "category": "League of Legends",
                    },
                ],
                {
                    "19000": [
                        _stake_event_for_tournament(
                            tournament_id="19000",
                            tournament="Prime Pokal",
                            team_a="Eintracht Frankfurt",
                            team_b="Team Orange Gaming",
                            game="lol",
                            start_time="2026-07-10T16:00:00Z",
                        )
                    ],
                    "19050": [
                        _stake_event_for_tournament(
                            tournament_id="19050",
                            tournament="Prime League Pro Division",
                            team_a="Eintracht Frankfurt",
                            team_b="Team Orange Gaming",
                            game="lol",
                            start_time="2026-07-10T16:00:00Z",
                        ),
                        _stake_event_for_tournament(
                            tournament_id="19050",
                            tournament="Prime League Pro Division",
                            team_a="Kaufland Hangry Knights",
                            team_b="Eintracht Spandau",
                            game="lol",
                            start_time="2026-07-10T18:00:00Z",
                        ),
                    ],
                },
            )

            result = run_scan(
                sport="lol",
                bookmaker="stake",
                stake_auto_discover_tournaments=True,
                stake_auto_max_tournaments=2,
                stake_request_delay_seconds=0,
                lol_tournament_scope="all",
                active_tournament_cache_path=active_cache_path,
                tournament_list_cache_path=tournament_list_cache_path,
                max_discovery_candidates_per_bucket=2,
                max_oddspapi_requests_per_run=2,
                polymarket_keyset_url=["https://gamma-api.polymarket.com/events/keyset"],
                polymarket_keyset_max_pages=1,
                providers={
                    "oddspapi": stake,
                    "polymarket": KeysetAwarePolymarketProvider(),
                },
                keyset_fetcher=lambda _url: _keyset_payload(
                    [
                        _keyset_match_event(
                            sport_title="League of Legends",
                            slug=(
                                "league-of-legends-eintracht-frankfurt-vs-"
                                "team-orange-gaming-2026-07-10"
                            ),
                            team_a="Eintracht Frankfurt",
                            team_b="Team Orange Gaming",
                            category="League of Legends",
                            tournament="Prime League 1st Division",
                            start_time="2026-07-10T16:00:00Z",
                        ),
                        _keyset_match_event(
                            sport_title="League of Legends",
                            slug=(
                                "league-of-legends-kaufland-hangry-knights-vs-"
                                "eintracht-spandau-2026-07-10"
                            ),
                            team_a="Kaufland Hangry Knights",
                            team_b="Eintracht Spandau",
                            category="League of Legends",
                            tournament="Prime League 1st Division",
                            start_time="2026-07-10T18:00:00Z",
                        ),
                    ]
                ),
                stake_sleep=lambda _seconds: None,
            )
            updated_cache = json.loads(active_cache_path.read_text(encoding="utf-8"))

        self.assertEqual(stake.batch_calls[0]["tournament_ids"], ["19050"])
        self.assertEqual(result["debug"]["stake_tournament_selected_ids"], ["19050"])
        self.assertGreaterEqual(result["summary"]["matched_count"], 1)
        self.assertEqual(result["debug"]["active_tournament_cache_updated"], True)
        self.assertIn("lol", updated_cache)
        self.assertNotIn("cs2", updated_cache)
        self.assertEqual(
            updated_cache["lol"]["stake"]["prime league 1st division"][
                "tournament_id"
            ],
            19050,
        )
        self.assertEqual(
            updated_cache["lol"]["stake"]["prime league 1st division"][
                "tournament_name"
            ],
            "Prime League Pro Division",
        )
        rejected = result["debug"]["rejected_lol_candidate_reasons"][
            "Prime League 1st Division"
        ]
        self.assertEqual(rejected[0]["id"], "19000")
        self.assertEqual(
            rejected[0]["reason"],
            "rejected_prime_pokal_not_prime_league",
        )
        overlap = result["debug"]["lol_verified_team_overlap"][
            "Prime League 1st Division"
        ]
        self.assertEqual(overlap[0]["id"], "19050")
        self.assertEqual(overlap[0]["verified_match_count"], 2)

    def test_lol_prime_league_pro_division_requires_team_overlap_before_caching(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            active_cache_path = cache_dir / "active.json"
            stake = CacheAwareOddsPapiProvider(
                [{"id": "19050", "name": "Prime League Pro Division"}],
                {
                    "19050": [
                        _stake_event_for_tournament(
                            tournament_id="19050",
                            tournament="Prime League Pro Division",
                            team_a="Unrelated Alpha",
                            team_b="Unrelated Beta",
                            game="lol",
                            start_time="2026-07-10T16:00:00Z",
                        )
                    ]
                },
            )

            result = run_scan(
                sport="lol",
                bookmaker="stake",
                stake_auto_discover_tournaments=True,
                stake_auto_max_tournaments=2,
                stake_request_delay_seconds=0,
                lol_tournament_scope="all",
                active_tournament_cache_path=active_cache_path,
                tournament_list_cache_path=cache_dir / "tournaments.json",
                max_discovery_candidates_per_bucket=2,
                max_oddspapi_requests_per_run=3,
                polymarket_keyset_url=["https://gamma-api.polymarket.com/events/keyset"],
                polymarket_keyset_max_pages=1,
                providers={
                    "oddspapi": stake,
                    "polymarket": KeysetAwarePolymarketProvider(),
                },
                keyset_fetcher=lambda _url: _keyset_payload(
                    [
                        _keyset_match_event(
                            sport_title="League of Legends",
                            slug="league-of-legends-berlin-mouz-2026-07-10",
                            team_a="Berlin Alpha",
                            team_b="Mouz Beta",
                            category="League of Legends",
                            tournament="Prime League 1st Division",
                            start_time="2026-07-10T16:00:00Z",
                        )
                    ]
                ),
                stake_sleep=lambda _seconds: None,
            )

        self.assertEqual(stake.batch_calls[0]["tournament_ids"], ["19050"])
        self.assertEqual(result["debug"]["stake_tournament_selected_ids"], [])
        self.assertEqual(result["debug"]["active_tournament_cache_updated"], False)
        self.assertFalse(active_cache_path.exists())

    def test_lol_team_probe_respects_request_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            stake = CacheAwareOddsPapiProvider(
                [
                    {"id": "19000", "name": "Prime Pokal"},
                    {
                        "id": "19050",
                        "name": "Rift Sunday",
                        "slug": "rossmann-centaurs-big",
                        "category": "League of Legends",
                    },
                ],
                {
                    "19050": [
                        _stake_event_for_tournament(
                            tournament_id="19050",
                            tournament="Rift Sunday",
                            team_a="ROSSMANN Centaurs",
                            team_b="BIG",
                            game="lol",
                            start_time="2026-07-10T18:00:00Z",
                        )
                    ]
                },
            )

            result = run_scan(
                sport="lol",
                bookmaker="stake",
                stake_auto_discover_tournaments=True,
                stake_auto_max_tournaments=2,
                stake_request_delay_seconds=0,
                lol_tournament_scope="all",
                active_tournament_cache_path=cache_dir / "active.json",
                tournament_list_cache_path=cache_dir / "tournaments.json",
                max_discovery_candidates_per_bucket=2,
                max_oddspapi_requests_per_run=1,
                polymarket_keyset_url=["https://gamma-api.polymarket.com/events/keyset"],
                polymarket_keyset_max_pages=1,
                providers={
                    "oddspapi": stake,
                    "polymarket": KeysetAwarePolymarketProvider(),
                },
                keyset_fetcher=lambda _url: _keyset_payload(
                    [
                        _keyset_match_event(
                            sport_title="League of Legends",
                            slug="league-of-legends-rossmann-centaurs-vs-big-2026-07-10",
                            team_a="ROSSMANN Centaurs",
                            team_b="BIG",
                            category="League of Legends",
                            tournament="Prime League 1st Division",
                            start_time="2026-07-10T18:00:00Z",
                        )
                    ]
                ),
                stake_sleep=lambda _seconds: None,
            )

        self.assertEqual(len(stake.tournament_list_calls), 1)
        self.assertEqual(stake.batch_calls, [])
        self.assertEqual(result["debug"]["stake_tournament_selected_ids"], [])
        self.assertEqual(result["debug"]["stake_auto_stop_reason"], "request_budget_reached")
        self.assertEqual(
            result["debug"]["discovery_skipped_due_to_budget"],
            ["Prime League 1st Division"],
        )

    def test_cache_miss_verifies_top_candidates_in_one_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            tournament_list_cache_path = cache_dir / "tournaments.json"
            active_cache_path = cache_dir / "active.json"
            _write_test_json(
                tournament_list_cache_path,
                _tournament_list_cache(
                    [
                        {"id": "51042", "name": "CCT South American Series"},
                        {"id": "51296", "name": "XSE Pro League"},
                    ]
                ),
            )
            stake = CacheAwareOddsPapiProvider(
                [],
                {
                    "51042": [
                        _stake_event_for_tournament(
                            tournament_id="51042",
                            tournament="CCT South American Series",
                            team_a="Yawara eSports",
                            team_b="Blitzkrieg",
                        )
                    ],
                    "51296": [
                        _stake_event_for_tournament(
                            tournament_id="51296",
                            tournament="XSE Pro League",
                            team_a="XSE Alpha",
                            team_b="Pro Beta",
                            start_time="2026-06-28T18:00:00Z",
                        )
                    ],
                },
            )

            result = run_scan(
                sport="cs2",
                bookmaker="stake",
                stake_auto_discover_tournaments=True,
                stake_request_delay_seconds=0,
                allow_unknown_tournaments=True,
                active_tournament_cache_path=active_cache_path,
                tournament_list_cache_path=tournament_list_cache_path,
                polymarket_keyset_url=["https://gamma-api.polymarket.com/events/keyset"],
                polymarket_keyset_max_pages=1,
                providers={
                    "oddspapi": stake,
                    "polymarket": KeysetAwarePolymarketProvider(),
                },
                keyset_fetcher=lambda _url: _cct_xse_keyset_payload(),
                stake_sleep=lambda _seconds: None,
            )

        self.assertEqual(stake.batch_calls[0]["tournament_ids"], ["51042", "51296"])
        self.assertEqual(result["debug"]["active_tournament_cache_miss_count"], 2)
        self.assertEqual(
            result["debug"]["stake_tournament_selected_ids"],
            ["51042", "51296"],
        )

    def test_max_selected_tournament_ids_per_sport_is_respected(self) -> None:
        payload = _keyset_payload(
            [
                *_cct_xse_keyset_payload()["events"],
                _keyset_match_event(
                    sport_title="Counter-Strike",
                    slug="cs2-dracula-night-2026-06-28",
                    team_a="Dracula Alpha",
                    team_b="Night Beta",
                    category="Counter-Strike",
                    tournament="Super DraculaN",
                    start_time="2026-06-28T20:00:00Z",
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            active_cache_path = cache_dir / "active.json"
            tournament_list_cache_path = cache_dir / "tournaments.json"
            _write_test_json(
                active_cache_path,
                _active_tournament_cache(
                    {
                        "CCT South America Series": (
                            "51042",
                            "CCT South American Series",
                        ),
                        "XSE Pro League": ("51296", "XSE Pro League"),
                        "Super DraculaN": ("62000", "Super DraculaN"),
                    }
                ),
            )
            stake = CacheAwareOddsPapiProvider(
                [],
                {
                    "51042": [
                        _stake_event_for_tournament(
                            tournament_id="51042",
                            tournament="CCT South American Series",
                            team_a="Yawara eSports",
                            team_b="Blitzkrieg",
                        )
                    ],
                    "51296": [
                        _stake_event_for_tournament(
                            tournament_id="51296",
                            tournament="XSE Pro League",
                            team_a="XSE Alpha",
                            team_b="Pro Beta",
                            start_time="2026-06-28T18:00:00Z",
                        )
                    ],
                    "62000": [
                        _stake_event_for_tournament(
                            tournament_id="62000",
                            tournament="Super DraculaN",
                            team_a="Dracula Alpha",
                            team_b="Night Beta",
                            start_time="2026-06-28T20:00:00Z",
                        )
                    ],
                },
            )

            result = run_scan(
                sport="cs2",
                bookmaker="stake",
                stake_auto_discover_tournaments=True,
                stake_request_delay_seconds=0,
                allow_unknown_tournaments=True,
                active_tournament_cache_path=active_cache_path,
                tournament_list_cache_path=tournament_list_cache_path,
                max_selected_tournament_ids_per_sport=2,
                polymarket_keyset_url=["https://gamma-api.polymarket.com/events/keyset"],
                polymarket_keyset_max_pages=1,
                providers={
                    "oddspapi": stake,
                    "polymarket": KeysetAwarePolymarketProvider(),
                },
                keyset_fetcher=lambda _url: payload,
                stake_sleep=lambda _seconds: None,
            )

        self.assertEqual(result["debug"]["stake_tournament_selected_ids"], ["51042", "51296"])
        self.assertEqual(stake.batch_calls[0]["tournament_ids"], ["51042", "51296"])

    def test_active_cache_failure_marks_entry_stale_without_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            active_cache_path = cache_dir / "active.json"
            tournament_list_cache_path = cache_dir / "tournaments.json"
            _write_test_json(
                active_cache_path,
                _active_tournament_cache(
                    {
                        "CCT South America Series": (
                            "51042",
                            "CCT South American Series",
                        )
                    }
                ),
            )
            stake = CacheAwareOddsPapiProvider(
                [],
                {
                    "51042": [
                        _stake_event_for_tournament(
                            tournament_id="51042",
                            tournament="CCT South American Series",
                            team_a="Other Alpha",
                            team_b="Other Beta",
                        )
                    ]
                },
            )

            result = run_scan(
                sport="cs2",
                bookmaker="stake",
                stake_auto_discover_tournaments=True,
                stake_request_delay_seconds=0,
                active_tournament_cache_path=active_cache_path,
                tournament_list_cache_path=tournament_list_cache_path,
                polymarket_keyset_url=["https://gamma-api.polymarket.com/events/keyset"],
                polymarket_keyset_max_pages=1,
                providers={
                    "oddspapi": stake,
                    "polymarket": KeysetAwarePolymarketProvider(),
                },
                keyset_fetcher=lambda _url: _keyset_payload(
                    [
                        _keyset_match_event(
                            sport_title="Counter-Strike",
                            slug="cs2-yawara-blitzkrieg-2026-06-28",
                            team_a="Yawara eSports",
                            team_b="Blitzkrieg",
                            category="Counter-Strike",
                            tournament="CCT South America Series",
                        )
                    ]
                ),
                stake_sleep=lambda _seconds: None,
            )
            updated = json.loads(active_cache_path.read_text(encoding="utf-8"))

        entry = updated["cs2"]["stake"]["cct south america series"]
        self.assertEqual(result["summary"]["matched_count"], 0)
        self.assertEqual(
            result["debug"]["active_tournament_cache_failures"],
            [
                {
                    "bucket": "CCT South America Series",
                    "tournament_id": "51042",
                    "reason": "no_team_overlap",
                }
            ],
        )
        self.assertEqual(entry["tournament_id"], 51042)
        self.assertEqual(entry["fail_count"], 1)
        self.assertEqual(entry["last_failure_reason"], "no_team_overlap")
        self.assertTrue(entry["stale"])

    def test_max_discovery_candidates_per_bucket_is_respected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            tournament_list_cache_path = cache_dir / "tournaments.json"
            active_cache_path = cache_dir / "active.json"
            _write_test_json(
                tournament_list_cache_path,
                _tournament_list_cache(
                    [
                        {"id": "51126", "name": "CCT South America Series"},
                        {"id": "51042", "name": "CCT South American Series"},
                    ]
                ),
            )
            stake = CacheAwareOddsPapiProvider(
                [],
                {
                    "51126": [],
                    "51042": [
                        _stake_event_for_tournament(
                            tournament_id="51042",
                            tournament="CCT South American Series",
                            team_a="Yawara eSports",
                            team_b="Blitzkrieg",
                        )
                    ],
                },
            )

            run_scan(
                sport="cs2",
                bookmaker="stake",
                stake_auto_discover_tournaments=True,
                stake_request_delay_seconds=0,
                active_tournament_cache_path=active_cache_path,
                tournament_list_cache_path=tournament_list_cache_path,
                max_discovery_candidates_per_bucket=1,
                polymarket_keyset_url=["https://gamma-api.polymarket.com/events/keyset"],
                polymarket_keyset_max_pages=1,
                providers={
                    "oddspapi": stake,
                    "polymarket": KeysetAwarePolymarketProvider(),
                },
                keyset_fetcher=lambda _url: _keyset_payload(
                    [
                        _keyset_match_event(
                            sport_title="Counter-Strike",
                            slug="cs2-yawara-blitzkrieg-2026-06-28",
                            team_a="Yawara eSports",
                            team_b="Blitzkrieg",
                            category="Counter-Strike",
                            tournament="CCT South America Series",
                        )
                    ]
                ),
                stake_sleep=lambda _seconds: None,
            )

        self.assertEqual(len(stake.batch_calls[0]["tournament_ids"]), 1)

    def test_cache_only_mode_does_not_discover_missing_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            stake = CacheAwareOddsPapiProvider(
                [{"id": "51042", "name": "CCT South American Series"}],
                {},
            )

            result = run_scan(
                sport="cs2",
                bookmaker="stake",
                stake_auto_discover_tournaments=True,
                stake_request_delay_seconds=0,
                active_tournament_cache_path=cache_dir / "active.json",
                tournament_list_cache_path=cache_dir / "tournaments.json",
                tournament_resolution_mode="cache-only",
                polymarket_keyset_url=["https://gamma-api.polymarket.com/events/keyset"],
                polymarket_keyset_max_pages=1,
                providers={
                    "oddspapi": stake,
                    "polymarket": KeysetAwarePolymarketProvider(),
                },
                keyset_fetcher=lambda _url: _keyset_payload(
                    [
                        _keyset_match_event(
                            sport_title="Counter-Strike",
                            slug="cs2-yawara-blitzkrieg-2026-06-28",
                            team_a="Yawara eSports",
                            team_b="Blitzkrieg",
                            category="Counter-Strike",
                            tournament="CCT South America Series",
                        )
                    ]
                ),
                stake_sleep=lambda _seconds: None,
            )

        self.assertEqual(stake.tournament_list_calls, [])
        self.assertEqual(stake.batch_calls, [])
        self.assertEqual(
            result["debug"]["discovery_skipped_due_to_mode"],
            ["CCT South America Series"],
        )

    def test_normal_mode_uses_cached_tournament_list_for_missing_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            tournament_list_cache_path = cache_dir / "tournaments.json"
            _write_test_json(
                tournament_list_cache_path,
                _tournament_list_cache(
                    [{"id": "51296", "name": "XSE Pro League"}]
                ),
            )
            stake = CacheAwareOddsPapiProvider(
                [],
                {
                    "51296": [
                        _stake_event_for_tournament(
                            tournament_id="51296",
                            tournament="XSE Pro League",
                            team_a="XSE Alpha",
                            team_b="Pro Beta",
                            start_time="2026-06-28T18:00:00Z",
                        )
                    ]
                },
            )

            result = run_scan(
                sport="cs2",
                bookmaker="stake",
                stake_auto_discover_tournaments=True,
                stake_request_delay_seconds=0,
                allow_unknown_tournaments=True,
                active_tournament_cache_path=cache_dir / "active.json",
                tournament_list_cache_path=tournament_list_cache_path,
                tournament_resolution_mode="normal",
                polymarket_keyset_url=["https://gamma-api.polymarket.com/events/keyset"],
                polymarket_keyset_max_pages=1,
                providers={
                    "oddspapi": stake,
                    "polymarket": KeysetAwarePolymarketProvider(),
                },
                keyset_fetcher=lambda _url: _keyset_payload(
                    [
                        _keyset_match_event(
                            sport_title="Counter-Strike",
                            slug="cs2-xse-alpha-beta-2026-06-28",
                            team_a="XSE Alpha",
                            team_b="Pro Beta",
                            category="Counter-Strike",
                            tournament="XSE Pro League",
                            start_time="2026-06-28T18:00:00Z",
                        )
                    ]
                ),
                stake_sleep=lambda _seconds: None,
            )

        self.assertEqual(stake.tournament_list_calls, [])
        self.assertTrue(result["debug"]["oddspapi_tournament_list_cache_hit"])
        self.assertEqual(result["debug"]["stake_tournament_selected_ids"], ["51296"])

    def test_refresh_mode_refreshes_tournament_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            tournament_list_cache_path = cache_dir / "tournaments.json"
            _write_test_json(
                tournament_list_cache_path,
                _tournament_list_cache(
                    [{"id": "99999", "name": "Wrong Cached Tournament"}]
                ),
            )
            stake = CacheAwareOddsPapiProvider(
                [{"id": "51042", "name": "CCT South American Series"}],
                {
                    "51042": [
                        _stake_event_for_tournament(
                            tournament_id="51042",
                            tournament="CCT South American Series",
                            team_a="Yawara eSports",
                            team_b="Blitzkrieg",
                        )
                    ]
                },
            )

            result = run_scan(
                sport="cs2",
                bookmaker="stake",
                stake_auto_discover_tournaments=True,
                stake_request_delay_seconds=0,
                active_tournament_cache_path=cache_dir / "active.json",
                tournament_list_cache_path=tournament_list_cache_path,
                tournament_resolution_mode="refresh",
                polymarket_keyset_url=["https://gamma-api.polymarket.com/events/keyset"],
                polymarket_keyset_max_pages=1,
                providers={
                    "oddspapi": stake,
                    "polymarket": KeysetAwarePolymarketProvider(),
                },
                keyset_fetcher=lambda _url: _keyset_payload(
                    [
                        _keyset_match_event(
                            sport_title="Counter-Strike",
                            slug="cs2-yawara-blitzkrieg-2026-06-28",
                            team_a="Yawara eSports",
                            team_b="Blitzkrieg",
                            category="Counter-Strike",
                            tournament="CCT South America Series",
                        )
                    ]
                ),
                stake_sleep=lambda _seconds: None,
            )

        self.assertEqual(len(stake.tournament_list_calls), 1)
        self.assertEqual(result["debug"]["stake_tournament_selected_ids"], ["51042"])

    def test_request_budget_prevents_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            stake = CacheAwareOddsPapiProvider(
                [{"id": "51042", "name": "CCT South American Series"}],
                {},
            )

            result = run_scan(
                sport="cs2",
                bookmaker="stake",
                stake_auto_discover_tournaments=True,
                stake_request_delay_seconds=0,
                active_tournament_cache_path=cache_dir / "active.json",
                tournament_list_cache_path=cache_dir / "tournaments.json",
                max_oddspapi_requests_per_run=0,
                polymarket_keyset_url=["https://gamma-api.polymarket.com/events/keyset"],
                polymarket_keyset_max_pages=1,
                providers={
                    "oddspapi": stake,
                    "polymarket": KeysetAwarePolymarketProvider(),
                },
                keyset_fetcher=lambda _url: _keyset_payload(
                    [
                        _keyset_match_event(
                            sport_title="Counter-Strike",
                            slug="cs2-yawara-blitzkrieg-2026-06-28",
                            team_a="Yawara eSports",
                            team_b="Blitzkrieg",
                            category="Counter-Strike",
                            tournament="CCT South America Series",
                        )
                    ]
                ),
                stake_sleep=lambda _seconds: None,
            )

        self.assertEqual(stake.tournament_list_calls, [])
        self.assertEqual(result["debug"]["stake_auto_stop_reason"], "request_budget_reached")
        self.assertEqual(
            result["debug"]["discovery_skipped_due_to_budget"],
            ["CCT South America Series"],
        )

    def test_auto_discovery_selects_matching_stake_tournament(self) -> None:
        payload = json.loads(
            (FIXTURE_DIR / "polymarket_keyset_page_1.json").read_text(
                encoding="utf-8"
            )
        )
        stake_events = [
            ProviderOddsEvent(
                provider="oddspapi",
                bookmaker="stake",
                game="cs2",
                tournament="CCT South American Series",
                start_time=datetime(2026, 6, 28, 16, 0, tzinfo=timezone.utc),
                team_a="Yawara eSports",
                odds_a=1.5,
                team_b="Blitzkrieg",
                odds_b=2.6,
                market_type="h2h",
                event_url="https://example.test/stake/yaw-bk",
                raw={},
            )
        ]
        stake = AutoDiscoverOddsPapiProvider(
            [
                {
                    "id": "51042",
                    "name": "CCT South American Series",
                    "slug": "cct-south-american-series",
                    "category": "Counter-Strike",
                },
                {
                    "id": "32873",
                    "name": "BLAST Premier",
                    "slug": "blast-premier",
                    "category": "Counter-Strike",
                },
            ],
            {"51042": stake_events},
        )

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_auto_discover_tournaments=True,
            stake_request_delay_seconds=0,
            polymarket_keyset_url=[
                "https://gamma-api.polymarket.com/events/keyset?tag_id=100639"
            ],
            polymarket_keyset_max_pages=1,
            providers={
                "oddspapi": stake,
                "polymarket": KeysetAwarePolymarketProvider(),
            },
            keyset_fetcher=lambda _url: payload,
            stake_sleep=lambda _seconds: None,
        )

        self.assertEqual(stake.tournament_candidate_calls[0]["sport"], "cs2")
        self.assertEqual([call["tournament_id"] for call in stake.calls], ["51042"])
        self.assertEqual(result["summary"]["stake_count"], 1)
        self.assertEqual(result["summary"]["matched_count"], 1)
        self.assertEqual(result["debug"]["stake_auto_discovery_enabled"], True)
        self.assertEqual(result["debug"]["stake_tournament_candidate_count"], 2)
        self.assertEqual(result["debug"]["stake_tournament_selected_ids"], ["51042"])
        self.assertEqual(
            result["debug"]["stake_tournament_selected_names"],
            ["CCT South American Series"],
        )
        self.assertEqual(result["debug"]["stake_tournament_rejected_count"], 1)

    def test_auto_discovery_uses_sport_filtered_keyset_hints_for_51042(self) -> None:
        stake_events = [
            ProviderOddsEvent(
                provider="oddspapi",
                bookmaker="stake",
                game="cs2",
                tournament="CCT South American Series",
                start_time=datetime(2026, 6, 28, 16, 0, tzinfo=timezone.utc),
                team_a="Yawara eSports",
                odds_a=1.5,
                team_b="Blitzkrieg",
                odds_b=2.6,
                market_type="h2h",
                event_url="https://example.test/stake/yaw-bk",
                raw={},
            )
        ]
        stake = AutoDiscoverOddsPapiProvider(
            [
                {
                    "id": "51043",
                    "name": "CCT South American Series Regional",
                    "slug": "cct-south-american-series-regional",
                    "category": "Counter-Strike",
                },
                {
                    "id": "51042",
                    "name": "CCT South American Series",
                    "slug": "cct-south-american-series",
                    "category": "Counter-Strike",
                },
            ],
            {"51042": stake_events},
            {
                "51043": ProviderRequestError(
                    "OddsPapi returned HTTP 404 for /v4/odds: "
                    '{"code":"FIXTURE_NOT_FOUND"}'
                )
            },
        )

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_auto_discover_tournaments=True,
            stake_request_delay_seconds=0,
            polymarket_keyset_url=[
                "https://gamma-api.polymarket.com/events/keyset?tag_id=100639"
            ],
            polymarket_keyset_max_pages=1,
            providers={
                "oddspapi": stake,
                "polymarket": KeysetAwarePolymarketProvider(),
            },
            keyset_fetcher=lambda _url: _mixed_esports_keyset_payload(),
            stake_sleep=lambda _seconds: None,
        )

        self.assertEqual(result["debug"]["stake_tournament_selected_ids"], ["51042"])
        self.assertEqual(result["summary"]["stake_count"], 1)
        self.assertEqual(result["summary"]["polymarket_count"], 1)
        self.assertEqual(result["summary"]["matched_count"], 1)
        self.assertEqual(result["debug"]["stake_tournament_known_51042_seen"], True)
        hints = " ".join(result["debug"]["polymarket_tournament_hints_sample"])
        self.assertIn("Yawara eSports vs Blitzkrieg", hints)
        self.assertNotIn("Overwatch", hints)
        self.assertNotIn("Rainbow Six", hints)
        self.assertNotIn("HOK", hints)
        self.assertNotIn("MLP", hints)

    def test_bucket_aware_resolver_attempts_multiple_kept_buckets(self) -> None:
        payload = _keyset_payload(
            [
                _keyset_match_event(
                    sport_title="Counter-Strike",
                    slug="cs2-yaw-bk-2026-06-28",
                    team_a="Yawara eSports",
                    team_b="Blitzkrieg",
                    category="Counter-Strike",
                    tournament="CCT South America Series",
                    start_time="2026-06-28T16:00:00Z",
                ),
                _keyset_match_event(
                    sport_title="Counter-Strike",
                    slug="cs2-dracula-night-2026-06-28",
                    team_a="Dracula Alpha",
                    team_b="Night Beta",
                    category="Counter-Strike",
                    tournament="Super DraculaN",
                    start_time="2026-06-28T18:00:00Z",
                ),
            ]
        )
        stake_events_by_tournament = {
            "51042": [
                ProviderOddsEvent(
                    provider="oddspapi",
                    bookmaker="stake",
                    game="cs2",
                    tournament="CCT South American Series",
                    start_time=datetime(2026, 6, 28, 16, 0, tzinfo=timezone.utc),
                    team_a="Yawara eSports",
                    odds_a=1.5,
                    team_b="Blitzkrieg",
                    odds_b=2.6,
                    market_type="h2h",
                    event_url="https://example.test/stake/yaw-bk",
                    raw={},
                )
            ],
            "62000": [
                ProviderOddsEvent(
                    provider="oddspapi",
                    bookmaker="stake",
                    game="cs2",
                    tournament="Super DraculaN",
                    start_time=datetime(2026, 6, 28, 18, 0, tzinfo=timezone.utc),
                    team_a="Dracula Alpha",
                    odds_a=1.7,
                    team_b="Night Beta",
                    odds_b=2.2,
                    market_type="h2h",
                    event_url="https://example.test/stake/dracula-night",
                    raw={},
                )
            ],
        }
        stake = AutoDiscoverOddsPapiProvider(
            [
                {
                    "id": "51042",
                    "name": "CCT South American Series",
                    "slug": "cct-south-american-series",
                    "category": "Counter-Strike",
                },
                {
                    "id": "62000",
                    "name": "Super DraculaN",
                    "slug": "super-draculan",
                    "category": "Counter-Strike",
                },
            ],
            stake_events_by_tournament,
        )

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_auto_discover_tournaments=True,
            stake_auto_max_tournaments=2,
            stake_request_delay_seconds=0,
            polymarket_keyset_url=[
                "https://gamma-api.polymarket.com/events/keyset?tag_id=100639"
            ],
            polymarket_keyset_max_pages=1,
            providers={
                "oddspapi": stake,
                "polymarket": KeysetAwarePolymarketProvider(),
            },
            keyset_fetcher=lambda _url: payload,
            stake_sleep=lambda _seconds: None,
        )

        self.assertEqual(
            result["debug"]["stake_attempted_ids_by_bucket"],
            {
                "CCT South America Series": ["51042"],
                "Super DraculaN": ["62000"],
            },
        )
        self.assertEqual(
            result["debug"]["stake_selected_ids_by_bucket"],
            {
                "CCT South America Series": ["51042"],
                "Super DraculaN": ["62000"],
            },
        )
        self.assertEqual(result["summary"]["stake_count"], 2)
        self.assertEqual(result["summary"]["matched_count"], 2)
        self.assertEqual(result["debug"]["stake_missing_bucket_names"], [])

    def test_bucket_scoring_prefers_core_tournament_token_over_generic_overlap(
        self,
    ) -> None:
        payload = _keyset_payload(
            [
                _keyset_match_event(
                    sport_title="Counter-Strike",
                    slug="cs2-metanoia-guara-2026-06-28",
                    team_a="Metanoia Wolves",
                    team_b="Guara eSports",
                    category="Counter-Strike",
                    tournament="CCT South America Series",
                    start_time="2026-06-28T16:00:00Z",
                ),
                _keyset_match_event(
                    sport_title="Counter-Strike",
                    slug="cs2-thunder-pick-2026-06-28",
                    team_a="Thunder Alpha",
                    team_b="Pick Beta",
                    category="Counter-Strike",
                    tournament="Thunderpick World Championship South American Series",
                    start_time="2026-06-28T18:00:00Z",
                ),
            ]
        )
        stake = AutoDiscoverOddsPapiProvider(
            [
                {
                    "id": "51042",
                    "name": "CCT South American Series",
                    "slug": "cct-south-american-series",
                    "category": "Counter-Strike",
                },
                {
                    "id": "62000",
                    "name": "Thunderpick World Championship South American Series",
                    "slug": "thunderpick-world-championship-south-american-series",
                    "category": "Counter-Strike",
                },
            ],
            {
                "51042": [
                    ProviderOddsEvent(
                        provider="oddspapi",
                        bookmaker="stake",
                        game="cs2",
                        tournament="CCT South American Series",
                        start_time=datetime(2026, 6, 28, 16, 0, tzinfo=timezone.utc),
                        team_a="Metanoia Wolves",
                        odds_a=1.7,
                        team_b="Guara eSports",
                        odds_b=2.1,
                        market_type="h2h",
                        event_url="https://example.test/stake/metanoia-guara",
                        raw={},
                    )
                ],
                "62000": [
                    ProviderOddsEvent(
                        provider="oddspapi",
                        bookmaker="stake",
                        game="cs2",
                        tournament="Thunderpick World Championship South American Series",
                        start_time=datetime(2026, 6, 28, 18, 0, tzinfo=timezone.utc),
                        team_a="Thunder Alpha",
                        odds_a=1.8,
                        team_b="Pick Beta",
                        odds_b=2.0,
                        market_type="h2h",
                        event_url="https://example.test/stake/thunder-pick",
                        raw={},
                    )
                ],
            },
        )

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_auto_discover_tournaments=True,
            stake_auto_max_tournaments=2,
            stake_request_delay_seconds=0,
            polymarket_keyset_url=[
                "https://gamma-api.polymarket.com/events/keyset?tag_id=100639"
            ],
            polymarket_keyset_max_pages=1,
            providers={
                "oddspapi": stake,
                "polymarket": KeysetAwarePolymarketProvider(),
            },
            keyset_fetcher=lambda _url: payload,
            stake_sleep=lambda _seconds: None,
        )

        score_by_bucket = result["debug"][
            "stake_candidate_score_breakdown_by_bucket"
        ]
        cct_scores = score_by_bucket["CCT South America Series"]
        thunder_scores = score_by_bucket[
            "Thunderpick World Championship Regional Series"
        ]
        self.assertEqual(cct_scores[0]["id"], "51042")
        self.assertEqual(thunder_scores[0]["id"], "62000")

        thunder_cct_score = next(
            item for item in thunder_scores if item["id"] == "51042"
        )
        self.assertLess(float(thunder_cct_score["score"]), 0.5)
        self.assertEqual(thunder_cct_score["core"], "")
        self.assertIn("america", thunder_cct_score["generic"])
        self.assertNotIn(
            "51042",
            result["debug"]["stake_attempted_ids_by_bucket"][
                "Thunderpick World Championship Regional Series"
            ],
        )
        self.assertEqual(
            result["debug"]["stake_attempted_ids_by_bucket"],
            {
                "CCT South America Series": ["51042"],
                "Thunderpick World Championship Regional Series": ["62000"],
            },
        )
        self.assertEqual(result["summary"]["stake_count"], 2)
        self.assertEqual(result["summary"]["matched_count"], 2)

    def test_bucket_resolver_selects_cct_candidate_with_matching_team_fixtures(
        self,
    ) -> None:
        payload = _keyset_payload(
            [
                _keyset_match_event(
                    sport_title="Counter-Strike",
                    slug="cs2-metanoia-guara-2026-06-28",
                    team_a="Metanoia Wolves",
                    team_b="Guara eSports",
                    category="Counter-Strike",
                    tournament="CCT South America Series",
                    start_time="2026-06-28T16:00:00Z",
                ),
                _keyset_match_event(
                    sport_title="Counter-Strike",
                    slug="cs2-mibr-patins-2026-06-28",
                    team_a="MIBR Academy",
                    team_b="Patins Da Ferrari",
                    category="Counter-Strike",
                    tournament="CCT South America Series",
                    start_time="2026-06-28T19:00:00Z",
                ),
            ]
        )
        stake = KeywordFilteringAutoDiscoverOddsPapiProvider(
            [
                {
                    "id": "51126",
                    "name": "CCT South American Series Regional",
                    "slug": "cct-south-american-series-regional",
                    "category": "Counter-Strike",
                },
                {
                    "id": "51042",
                    "name": "CCT South American Series",
                    "slug": "cct-south-american-series",
                    "category": "Counter-Strike",
                },
                {
                    "id": "62000",
                    "name": "Thunderpick World Championship South American Series",
                    "slug": "thunderpick-world-championship-south-american-series",
                    "category": "Counter-Strike",
                },
            ],
            {
                "51042": [
                    ProviderOddsEvent(
                        provider="oddspapi",
                        bookmaker="stake",
                        game="cs2",
                        tournament="CCT South American Series",
                        start_time=datetime(2026, 6, 28, 16, 0, tzinfo=timezone.utc),
                        team_a="Metanoia Wolves",
                        odds_a=1.6,
                        team_b="Guara eSports",
                        odds_b=2.3,
                        market_type="h2h",
                        event_url="https://example.test/stake/metanoia-guara",
                        raw={},
                    ),
                    ProviderOddsEvent(
                        provider="oddspapi",
                        bookmaker="stake",
                        game="cs2",
                        tournament="CCT South American Series",
                        start_time=datetime(2026, 6, 28, 19, 0, tzinfo=timezone.utc),
                        team_a="MIBR Academy",
                        odds_a=1.8,
                        team_b="Patins Da Ferrari",
                        odds_b=2.0,
                        market_type="h2h",
                        event_url="https://example.test/stake/mibr-patins",
                        raw={},
                    ),
                ]
            },
            {
                "51126": ProviderRequestError(
                    "OddsPapi returned HTTP 404 for /v4/odds: "
                    '{"code":"FIXTURE_NOT_FOUND"}'
                )
            },
        )

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_auto_discover_tournaments=True,
            stake_auto_max_tournaments=2,
            stake_request_delay_seconds=0,
            polymarket_keyset_url=[
                "https://gamma-api.polymarket.com/events/keyset?tag_id=100639"
            ],
            polymarket_keyset_max_pages=1,
            providers={
                "oddspapi": stake,
                "polymarket": KeysetAwarePolymarketProvider(),
            },
            keyset_fetcher=lambda _url: payload,
            stake_sleep=lambda _seconds: None,
        )

        self.assertEqual(
            result["debug"]["stake_attempted_ids_by_bucket"],
            {"CCT South America Series": ["51126", "51042"]},
        )
        self.assertEqual(
            result["debug"]["stake_selected_ids_by_bucket"],
            {"CCT South America Series": ["51042"]},
        )
        self.assertEqual(result["debug"]["stake_tournament_selected_ids"], ["51042"])
        self.assertEqual(result["summary"]["stake_count"], 2)
        self.assertEqual(result["summary"]["matched_count"], 2)
        self.assertEqual(
            result["debug"]["stake_tournament_verified_match_count"]["51042"],
            2,
        )
        self.assertEqual(
            result["debug"]["polymarket_team_pair_count_by_bucket"],
            {"CCT South America Series": 2},
        )
        self.assertEqual(
            result["debug"]["polymarket_team_pairs_by_bucket"],
            {
                "CCT South America Series": [
                    "Metanoia Wolves vs Guara eSports",
                    "MIBR Academy vs Patins Da Ferrari",
                ]
            },
        )
        cct_keywords = result["debug"]["stake_candidate_fetch_keywords_by_bucket"][
            "CCT South America Series"
        ]
        self.assertIn("CCT South America", cct_keywords)
        self.assertIn("CCT South American", cct_keywords)
        self.assertIn("CCT South America Series", cct_keywords)
        self.assertIn("CCT South American Series", cct_keywords)

    def test_candidate_discovery_rate_limit_preserves_team_pairs_and_stop_reason(
        self,
    ) -> None:
        payload = _keyset_payload(
            [
                _keyset_match_event(
                    sport_title="Counter-Strike",
                    slug="cs2-metanoia-guara-2026-06-28",
                    team_a="Metanoia Wolves",
                    team_b="Guara eSports",
                    category="Counter-Strike",
                    tournament="CCT South America Series",
                    start_time="2026-06-28T16:00:00Z",
                ),
            ]
        )
        stake = CandidateErrorAutoDiscoverOddsPapiProvider(
            [],
            {},
            [
                ProviderRequestError(
                    "OddsPapi returned HTTP 429 for /v4/tournaments: "
                    '{"code":"RATE_LIMITED","retryAfter":0}'
                ),
                ProviderRequestError(
                    "OddsPapi returned HTTP 429 for /v4/tournaments: "
                    '{"code":"RATE_LIMITED","retryAfter":0}'
                ),
            ],
        )

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_auto_discover_tournaments=True,
            stake_request_delay_seconds=0,
            stake_rate_limit_retries=1,
            polymarket_keyset_url=[
                "https://gamma-api.polymarket.com/events/keyset?tag_id=100639"
            ],
            polymarket_keyset_max_pages=1,
            providers={
                "oddspapi": stake,
                "polymarket": KeysetAwarePolymarketProvider(),
            },
            keyset_fetcher=lambda _url: payload,
            stake_sleep=lambda _seconds: None,
        )

        self.assertEqual(result["debug"]["stake_auto_stop_reason"], "rate_limited")
        self.assertNotEqual(
            result["debug"]["stake_auto_stop_reason"],
            "no_polymarket_team_pairs",
        )
        self.assertEqual(result["debug"]["stake_rate_limit_count"], 2)
        self.assertEqual(result["debug"]["stake_tournament_requested_count"], 0)
        self.assertEqual(
            result["debug"]["polymarket_team_pair_count_by_bucket"],
            {"CCT South America Series": 1},
        )
        self.assertEqual(
            result["debug"]["polymarket_team_pairs_by_bucket"],
            {"CCT South America Series": ["Metanoia Wolves vs Guara eSports"]},
        )
        self.assertEqual(stake.calls, [])

    def test_zero_candidates_with_valid_team_pairs_reports_no_candidates(
        self,
    ) -> None:
        payload = _keyset_payload(
            [
                _keyset_match_event(
                    sport_title="Counter-Strike",
                    slug="cs2-mibr-patins-2026-06-28",
                    team_a="MIBR Academy",
                    team_b="Patins Da Ferrari",
                    category="Counter-Strike",
                    tournament="CCT South America Series",
                    start_time="2026-06-28T19:00:00Z",
                ),
            ]
        )
        stake = KeywordFilteringAutoDiscoverOddsPapiProvider([], {})

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_auto_discover_tournaments=True,
            stake_request_delay_seconds=0,
            polymarket_keyset_url=[
                "https://gamma-api.polymarket.com/events/keyset?tag_id=100639"
            ],
            polymarket_keyset_max_pages=1,
            providers={
                "oddspapi": stake,
                "polymarket": KeysetAwarePolymarketProvider(),
            },
            keyset_fetcher=lambda _url: payload,
            stake_sleep=lambda _seconds: None,
        )

        self.assertEqual(
            result["debug"]["stake_auto_stop_reason"],
            "no_stake_tournament_candidates",
        )
        self.assertEqual(result["debug"]["stake_tournament_candidate_count"], 0)
        self.assertEqual(result["debug"]["stake_tournament_requested_count"], 0)
        self.assertEqual(
            result["debug"]["polymarket_team_pair_count_by_bucket"],
            {"CCT South America Series": 1},
        )
        self.assertEqual(
            result["debug"]["polymarket_team_pairs_by_bucket"],
            {"CCT South America Series": ["MIBR Academy vs Patins Da Ferrari"]},
        )

    def test_auto_discovery_penalizes_wrong_regions_and_requires_verified_match(
        self,
    ) -> None:
        payload = json.loads(
            (FIXTURE_DIR / "polymarket_keyset_page_1.json").read_text(
                encoding="utf-8"
            )
        )
        matching_stake_events = [
            ProviderOddsEvent(
                provider="oddspapi",
                bookmaker="stake",
                game="cs2",
                tournament="CCT South American Series",
                start_time=datetime(2026, 6, 28, 16, 0, tzinfo=timezone.utc),
                team_a="Yawara eSports",
                odds_a=1.5,
                team_b="Blitzkrieg",
                odds_b=2.6,
                market_type="h2h",
                event_url="https://example.test/stake/yaw-bk",
                raw={},
            )
        ]
        unrelated_stake_events = [
            ProviderOddsEvent(
                provider="oddspapi",
                bookmaker="stake",
                game="cs2",
                tournament="CCT South America Regional Series",
                start_time=datetime(2026, 6, 28, 16, 0, tzinfo=timezone.utc),
                team_a="Natus Vincere",
                odds_a=1.6,
                team_b="Vitality",
                odds_b=2.4,
                market_type="h2h",
                event_url="https://example.test/stake/nav-vitality",
                raw={},
            )
        ]
        stake = AutoDiscoverOddsPapiProvider(
            [
                {
                    "id": "51043",
                    "name": "CCT South American Series Regional",
                    "slug": "cct-south-american-series-regional",
                    "category": "Counter-Strike",
                },
                {
                    "id": "32873",
                    "name": "CCT North America Series",
                    "slug": "cct-north-america-series",
                    "category": "Counter-Strike",
                },
                {
                    "id": "32874",
                    "name": "CCT Europe Series",
                    "slug": "cct-europe-series",
                    "category": "Counter-Strike",
                },
                {
                    "id": "32875",
                    "name": "CCT Asia Series",
                    "slug": "cct-asia-series",
                    "category": "Counter-Strike",
                },
                {
                    "id": "51042",
                    "name": "CCT South American Series",
                    "slug": "cct-south-american-series",
                    "category": "Counter-Strike",
                },
            ],
            {
                "51043": unrelated_stake_events,
                "51042": matching_stake_events,
            },
        )

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_auto_discover_tournaments=True,
            stake_request_delay_seconds=0,
            polymarket_keyset_url=[
                "https://gamma-api.polymarket.com/events/keyset?tag_id=100639"
            ],
            polymarket_keyset_max_pages=1,
            providers={
                "oddspapi": stake,
                "polymarket": KeysetAwarePolymarketProvider(),
            },
            keyset_fetcher=lambda _url: payload,
            stake_sleep=lambda _seconds: None,
        )

        self.assertEqual(
            [call["tournament_id"] for call in stake.calls],
            ["51043", "51042"],
        )
        self.assertEqual(result["debug"]["stake_tournament_selected_ids"], ["51042"])
        self.assertEqual(result["debug"]["stake_tournament_selected_count"], 1)
        self.assertEqual(
            result["debug"]["stake_tournament_attempted_ids"],
            ["51043", "51042"],
        )
        self.assertEqual(
            result["debug"]["stake_tournament_verified_match_count"],
            {"51043": 0, "51042": 1},
        )
        self.assertEqual(
            [item["result"] for item in result["debug"]["stake_tournament_attempt_results"]],
            ["no_team_match", "selected"],
        )
        self.assertEqual(
            result["debug"]["stake_auto_stop_reason"],
            "all_polymarket_events_matched",
        )
        self.assertIn(
            "Yawara eSports vs Blitzkrieg",
            result["debug"]["polymarket_tournament_hints_sample"][0],
        )

        sample_by_id = {
            item["id"]: item
            for item in result["debug"]["stake_tournament_selection_sample"]
        }
        self.assertGreaterEqual(sample_by_id["51042"]["score"], 0.5)
        self.assertIn("cct", sample_by_id["51042"]["matched_tokens"])
        self.assertIn("south", sample_by_id["51042"]["matched_tokens"])
        self.assertIn("america", sample_by_id["51042"]["matched_tokens"])
        self.assertIn("north", sample_by_id["32873"]["rejected_tokens"])
        self.assertIn("europe", sample_by_id["32874"]["rejected_tokens"])
        self.assertIn("asia", sample_by_id["32875"]["rejected_tokens"])
        self.assertEqual(result["debug"]["stake_tournament_known_51042_seen"], True)
        self.assertEqual(
            result["debug"]["stake_tournament_known_51042_name"],
            "CCT South American Series",
        )

    def test_auto_discovery_skips_fixture_not_found_candidate(self) -> None:
        payload = json.loads(
            (FIXTURE_DIR / "polymarket_keyset_page_1.json").read_text(
                encoding="utf-8"
            )
        )
        stake_events = [
            ProviderOddsEvent(
                provider="oddspapi",
                bookmaker="stake",
                game="cs2",
                tournament="CCT South American Series",
                start_time=datetime(2026, 6, 28, 16, 0, tzinfo=timezone.utc),
                team_a="Yawara eSports",
                odds_a=1.5,
                team_b="Blitzkrieg",
                odds_b=2.6,
                market_type="h2h",
                event_url="https://example.test/stake/yaw-bk",
                raw={},
            )
        ]
        stake = AutoDiscoverOddsPapiProvider(
            [
                {
                    "id": "51043",
                    "name": "CCT South American Series Regional",
                    "slug": "cct-south-american-series-regional",
                    "category": "Counter-Strike",
                },
                {
                    "id": "51042",
                    "name": "CCT South American Series",
                    "slug": "cct-south-american-series",
                    "category": "Counter-Strike",
                },
            ],
            {"51042": stake_events},
            {
                "51043": ProviderRequestError(
                    "OddsPapi returned HTTP 404 for /v4/odds: "
                    '{"code":"FIXTURE_NOT_FOUND"}'
                )
            },
        )

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_auto_discover_tournaments=True,
            stake_request_delay_seconds=0,
            polymarket_keyset_url=[
                "https://gamma-api.polymarket.com/events/keyset?tag_id=100639"
            ],
            polymarket_keyset_max_pages=1,
            providers={
                "oddspapi": stake,
                "polymarket": KeysetAwarePolymarketProvider(),
            },
            keyset_fetcher=lambda _url: payload,
            stake_sleep=lambda _seconds: None,
        )

        self.assertEqual(
            [call["tournament_id"] for call in stake.calls],
            ["51043", "51042"],
        )
        self.assertEqual(result["debug"]["stake_tournament_selected_ids"], ["51042"])
        self.assertEqual(result["debug"]["stake_skipped_tournament_ids"], ["51043"])
        self.assertEqual(
            result["debug"]["stake_skipped_tournament_reasons"],
            {"51043": "fixture_not_found"},
        )
        self.assertEqual(
            [item["result"] for item in result["debug"]["stake_tournament_attempt_results"]],
            ["fixture_not_found", "selected"],
        )
        self.assertEqual(result["summary"]["matched_count"], 1)

    def test_auto_discovery_continues_after_failed_candidates_within_budget(
        self,
    ) -> None:
        payload = json.loads(
            (FIXTURE_DIR / "polymarket_keyset_page_1.json").read_text(
                encoding="utf-8"
            )
        )
        stake_events = [
            ProviderOddsEvent(
                provider="oddspapi",
                bookmaker="stake",
                game="cs2",
                tournament="CCT South American Series",
                start_time=datetime(2026, 6, 28, 16, 0, tzinfo=timezone.utc),
                team_a="Yawara eSports",
                odds_a=1.5,
                team_b="Blitzkrieg",
                odds_b=2.6,
                market_type="h2h",
                event_url="https://example.test/stake/yaw-bk",
                raw={},
            )
        ]
        failed_ids = ["61004", "61003", "61002", "61001"]
        stake = AutoDiscoverOddsPapiProvider(
            [
                {
                    "id": item,
                    "name": f"CCT South American Series Archive {item}",
                    "slug": f"cct-south-american-series-archive-{item}",
                    "category": "Counter-Strike",
                }
                for item in failed_ids
            ]
            + [
                {
                    "id": "51042",
                    "name": "CCT South American Series",
                    "slug": "cct-south-american-series",
                    "category": "Counter-Strike",
                }
            ],
            {"51042": stake_events},
            {
                item: ProviderRequestError(
                    "OddsPapi returned HTTP 404 for /v4/odds: "
                    '{"code":"FIXTURE_NOT_FOUND"}'
                )
                for item in failed_ids
            },
        )

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_auto_discover_tournaments=True,
            stake_auto_max_tournaments=5,
            stake_request_delay_seconds=0,
            polymarket_keyset_url=[
                "https://gamma-api.polymarket.com/events/keyset?tag_id=100639"
            ],
            polymarket_keyset_max_pages=1,
            providers={
                "oddspapi": stake,
                "polymarket": KeysetAwarePolymarketProvider(),
            },
            keyset_fetcher=lambda _url: payload,
            stake_sleep=lambda _seconds: None,
        )

        self.assertEqual(
            [call["tournament_id"] for call in stake.calls],
            [*failed_ids, "51042"],
        )
        self.assertEqual(
            result["debug"]["stake_tournament_attempted_ids"],
            [*failed_ids, "51042"],
        )
        self.assertEqual(result["debug"]["stake_tournament_selected_ids"], ["51042"])
        self.assertEqual(result["debug"]["stake_tournament_selected_count"], 1)
        self.assertEqual(result["summary"]["stake_count"], 1)
        self.assertEqual(result["summary"]["matched_count"], 1)
        self.assertEqual(
            [item["result"] for item in result["debug"]["stake_tournament_attempt_results"]],
            [
                "fixture_not_found",
                "fixture_not_found",
                "fixture_not_found",
                "fixture_not_found",
                "selected",
            ],
        )

    def test_auto_discovery_rate_limit_stops_cleanly(self) -> None:
        payload = json.loads(
            (FIXTURE_DIR / "polymarket_keyset_page_1.json").read_text(
                encoding="utf-8"
            )
        )
        stake = AutoDiscoverOddsPapiProvider(
            [
                {
                    "id": "51042",
                    "name": "CCT South American Series",
                    "slug": "cct-south-american-series",
                    "category": "Counter-Strike",
                }
            ],
            {},
            {
                "51042": [
                    ProviderRequestError(
                        "OddsPapi returned HTTP 429 for /v4/odds: "
                        '{"code":"RATE_LIMITED","retryAfter":0}'
                    ),
                    ProviderRequestError(
                        "OddsPapi returned HTTP 429 for /v4/odds: "
                        '{"code":"RATE_LIMITED","retryAfter":0}'
                    ),
                ]
            },
        )

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_auto_discover_tournaments=True,
            stake_request_delay_seconds=0,
            stake_rate_limit_retries=1,
            polymarket_keyset_url=[
                "https://gamma-api.polymarket.com/events/keyset?tag_id=100639"
            ],
            polymarket_keyset_max_pages=1,
            providers={
                "oddspapi": stake,
                "polymarket": KeysetAwarePolymarketProvider(),
            },
            keyset_fetcher=lambda _url: payload,
            stake_sleep=lambda _seconds: None,
        )

        self.assertEqual(
            [call["tournament_id"] for call in stake.calls],
            ["51042", "51042"],
        )
        self.assertEqual(result["debug"]["stake_auto_stop_reason"], "rate_limited")
        self.assertEqual(result["debug"]["stake_stopped_reason"], "rate_limited")
        self.assertEqual(result["debug"]["stake_rate_limit_count"], 2)
        self.assertEqual(result["summary"]["stake_count"], 0)

    def test_manual_tournament_ids_merge_with_auto_discovered_ids(self) -> None:
        payload = json.loads(
            (FIXTURE_DIR / "polymarket_keyset_page_1.json").read_text(
                encoding="utf-8"
            )
        )
        stake_events = [
            ProviderOddsEvent(
                provider="oddspapi",
                bookmaker="stake",
                game="cs2",
                tournament="CCT South American Series",
                start_time=datetime(2026, 6, 28, 16, 0, tzinfo=timezone.utc),
                team_a="Yawara eSports",
                odds_a=1.5,
                team_b="Blitzkrieg",
                odds_b=2.6,
                market_type="h2h",
                event_url="https://example.test/stake/yaw-bk",
                raw={},
            )
        ]
        stake = AutoDiscoverOddsPapiProvider(
            [
                {
                    "id": "51042",
                    "name": "CCT South American Series",
                    "slug": "cct-south-american-series",
                    "category": "Counter-Strike",
                }
            ],
            {"51042": stake_events},
        )

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_tournament_id="51042",
            stake_auto_discover_tournaments=True,
            stake_request_delay_seconds=0,
            polymarket_keyset_url=[
                "https://gamma-api.polymarket.com/events/keyset?tag_id=100639"
            ],
            polymarket_keyset_max_pages=1,
            providers={
                "oddspapi": stake,
                "polymarket": KeysetAwarePolymarketProvider(),
            },
            keyset_fetcher=lambda _url: payload,
            stake_sleep=lambda _seconds: None,
        )

        self.assertEqual([call["tournament_id"] for call in stake.calls], ["51042"])
        self.assertEqual(result["debug"]["stake_tournament_selected_ids"], ["51042"])
        self.assertEqual(result["summary"]["stake_requested_tournament_count"], 1)

    def test_generated_polymarket_queries_include_safe_team_pair_variants(self) -> None:
        stake_events = [
            ProviderOddsEvent(
                provider="oddspapi",
                bookmaker="stake",
                game="cs2",
                tournament="CCT",
                start_time=None,
                team_a="Yawara eSports",
                odds_a=1.5,
                team_b="Blitzkrieg",
                odds_b=2.6,
                market_type="h2h",
                event_url=None,
                raw={},
            ),
            ProviderOddsEvent(
                provider="oddspapi",
                bookmaker="stake",
                game="cs2",
                tournament="CCT",
                start_time=None,
                team_a="Mibr Academy",
                odds_a=1.7,
                team_b="Patins Da Ferrari",
                odds_b=2.2,
                market_type="h2h",
                event_url=None,
                raw={},
            ),
        ]

        searches = generate_polymarket_team_searches(stake_events)

        self.assertIn("Yawara eSports Blitzkrieg", searches)
        self.assertIn("Yawara Blitzkrieg", searches)
        self.assertIn("Mibr Academy Patins Da Ferrari", searches)
        self.assertIn("Mibr Patins Da Ferrari", searches)
        self.assertNotIn("Yawara BK", searches)

    def test_generated_polymarket_queries_dedupe_case_insensitively(self) -> None:
        stake_events = [
            ProviderOddsEvent(
                provider="oddspapi",
                bookmaker="stake",
                game="cs2",
                tournament="CCT",
                start_time=None,
                team_a="Yawara eSports",
                odds_a=1.5,
                team_b="Blitzkrieg",
                odds_b=2.6,
                market_type="h2h",
                event_url=None,
                raw={},
            ),
            ProviderOddsEvent(
                provider="oddspapi",
                bookmaker="stake",
                game="cs2",
                tournament="CCT",
                start_time=None,
                team_a="yawara esports",
                odds_a=1.5,
                team_b="blitzkrieg",
                odds_b=2.6,
                market_type="h2h",
                event_url=None,
                raw={},
            ),
        ]

        searches = generate_polymarket_team_searches(stake_events)

        self.assertEqual(
            [search.casefold() for search in searches].count(
                "yawara esports blitzkrieg"
            ),
            1,
        )
        self.assertEqual(
            [search.casefold() for search in searches].count("yawara blitzkrieg"),
            1,
        )

    def test_offline_scan_finds_one_match_from_fixtures(self) -> None:
        stake_events, polymarket_events = _snapshot_fixture_events()
        stake = FakeOddsPapiProvider(stake_events)
        polymarket = FakePolymarketProvider(polymarket_events)

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_tournament_id="101",
            polymarket_search=["cs2"],
            providers={"oddspapi": stake, "polymarket": polymarket},
        )

        self.assertEqual(result["summary"]["stake_count"], 2)
        self.assertEqual(result["summary"]["polymarket_count"], 2)
        self.assertEqual(result["summary"]["matched_count"], 1)
        self.assertEqual(stake.calls[0]["tournament_id"], "101")
        self.assertEqual(polymarket.calls[0]["search"], "cs2")
        self.assertNotIn("arbitrage", result)
        self.assertNotIn("unmatched_stake_sample", result["debug"])
        self.assertNotIn("unmatched_polymarket_sample", result["debug"])

    def test_scan_skips_fixture_not_found_tournament_and_uses_remaining_events(
        self,
    ) -> None:
        stake_events, polymarket_events = _arbitrage_fixture_events()
        stake = TournamentAwareOddsPapiProvider(
            {"51042": stake_events},
            {
                "32873": ProviderRequestError(
                    "OddsPapi returned HTTP 404 for /v4/odds: "
                    '{"code":"FIXTURE_NOT_FOUND"}'
                )
            },
        )

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_tournament_id="32873,51042",
            stake_request_delay_seconds=0,
            polymarket_search=["cs2"],
            find_arbitrage=True,
            polymarket_fee_rate=0,
            providers={
                "oddspapi": stake,
                "polymarket": FakePolymarketProvider(polymarket_events),
            },
            stake_sleep=lambda _seconds: None,
        )

        self.assertEqual(
            [call["tournament_id"] for call in stake.calls],
            ["32873", "51042"],
        )
        self.assertEqual(result["summary"]["stake_count"], 1)
        self.assertEqual(result["summary"]["matched_count"], 1)
        self.assertEqual(result["summary"]["opportunity_count"], 1)
        self.assertEqual(result["summary"]["stake_requested_tournament_count"], 2)
        self.assertEqual(result["summary"]["stake_skipped_tournament_count"], 1)
        self.assertEqual(result["summary"]["stake_error_count"], 1)
        self.assertEqual(
            result["debug"]["stake_skipped_tournament_ids"],
            ["32873"],
        )
        self.assertEqual(
            result["debug"]["stake_skipped_tournament_reasons"],
            {"32873": "fixture_not_found"},
        )

    def test_scan_retries_once_on_stake_rate_limit_with_retry_after(self) -> None:
        stake_events, polymarket_events = _arbitrage_fixture_events()
        stake = TournamentAwareOddsPapiProvider(
            {"51042": stake_events},
            {
                "51042": [
                    ProviderRequestError(
                        "OddsPapi returned HTTP 429 for /v4/odds: "
                        '{"code":"RATE_LIMITED","retryAfter":0}'
                    )
                ]
            },
        )
        sleeps: list[float] = []

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_tournament_id="51042",
            stake_request_delay_seconds=0,
            stake_rate_limit_retries=1,
            polymarket_search=["cs2"],
            providers={
                "oddspapi": stake,
                "polymarket": FakePolymarketProvider(polymarket_events),
            },
            stake_sleep=sleeps.append,
        )

        self.assertEqual(
            [call["tournament_id"] for call in stake.calls],
            ["51042", "51042"],
        )
        self.assertEqual(result["debug"]["stake_rate_limit_count"], 1)
        self.assertEqual(result["summary"]["stake_count"], 1)
        self.assertEqual(result["summary"]["matched_count"], 1)
        self.assertEqual(sleeps, [0.25])

    def test_persistent_stake_rate_limit_stops_scan_cleanly(self) -> None:
        stake_events, polymarket_events = _arbitrage_fixture_events()
        stake = TournamentAwareOddsPapiProvider(
            {"51042": stake_events},
            {
                "51042": [
                    ProviderRequestError(
                        "OddsPapi returned HTTP 429 for /v4/odds: "
                        '{"code":"RATE_LIMITED","retryAfter":0}'
                    ),
                    ProviderRequestError(
                        "OddsPapi returned HTTP 429 for /v4/odds: "
                        '{"code":"RATE_LIMITED","retryAfter":0}'
                    ),
                ]
            },
        )

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_tournament_id="51042",
            stake_request_delay_seconds=0,
            stake_rate_limit_retries=1,
            polymarket_search=["cs2"],
            providers={
                "oddspapi": stake,
                "polymarket": FakePolymarketProvider(polymarket_events),
            },
            stake_sleep=lambda _seconds: None,
        )

        self.assertEqual(
            [call["tournament_id"] for call in stake.calls],
            ["51042", "51042"],
        )
        self.assertEqual(result["summary"]["stake_count"], 0)
        self.assertEqual(result["summary"]["matched_count"], 0)
        self.assertEqual(result["debug"]["stake_rate_limit_count"], 2)
        self.assertEqual(result["debug"]["stake_stopped_reason"], "rate_limited")
        self.assertEqual(result["summary"]["stake_error_count"], 1)

    def test_scan_keeps_auth_like_stake_errors_fatal(self) -> None:
        stake_events, polymarket_events = _arbitrage_fixture_events()
        stake = TournamentAwareOddsPapiProvider(
            {"51042": stake_events},
            {
                "32873": ProviderRequestError(
                    "OddsPapi returned HTTP 401 for /v4/odds: INVALID_API_KEY"
                )
            },
        )

        with self.assertRaises(ProviderRequestError) as raised:
            run_scan(
                sport="cs2",
                bookmaker="stake",
                stake_tournament_id="32873,51042",
                stake_request_delay_seconds=0,
                polymarket_search=["cs2"],
                providers={
                    "oddspapi": stake,
                    "polymarket": FakePolymarketProvider(polymarket_events),
                },
                stake_sleep=lambda _seconds: None,
            )

        self.assertIn("HTTP 401", str(raised.exception))
        self.assertEqual(
            [call["tournament_id"] for call in stake.calls],
            ["32873"],
        )

    def test_scan_with_generated_searches_calls_polymarket_for_team_queries(self) -> None:
        stake_events, polymarket_events = _arbitrage_fixture_events()
        polymarket = FakePolymarketProvider(polymarket_events)

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_tournament_id="101",
            polymarket_search_from_stake_teams=True,
            providers={
                "oddspapi": FakeOddsPapiProvider(stake_events),
                "polymarket": polymarket,
            },
        )

        searches = [call["search"] for call in polymarket.calls]
        self.assertIn("Team Alpha Team Beta", searches)
        self.assertEqual(
            result["debug"]["polymarket_generated_search_count"],
            len(searches),
        )
        self.assertEqual(result["summary"]["polymarket_count"], 1)

    def test_generated_results_merge_with_user_search_results(self) -> None:
        stake_events, polymarket_events = _arbitrage_fixture_events()
        polymarket = SearchAwarePolymarketProvider(
            {
                "manual cs2": [polymarket_events[0]],
                "Team Alpha Team Beta": [polymarket_events[0]],
            }
        )

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_tournament_id="101",
            polymarket_search=["manual cs2"],
            polymarket_search_from_stake_teams=True,
            providers={
                "oddspapi": FakeOddsPapiProvider(stake_events),
                "polymarket": polymarket,
            },
        )

        searches = [call["search"] for call in polymarket.calls]
        self.assertEqual(searches[0], "manual cs2")
        self.assertIn("Team Alpha Team Beta", searches)
        self.assertEqual(result["debug"]["polymarket_user_search_count"], 1)
        self.assertEqual(result["summary"]["polymarket_count"], 1)

    def test_scan_with_page_url_calls_polymarket_provider_for_extracted_slugs(
        self,
    ) -> None:
        stake_events, polymarket_events = _arbitrage_fixture_events()
        html = (
            '<a href="/event/cs2-team-alpha-team-beta-2026-06-28">'
            "Team Alpha vs Team Beta</a>"
            '<script>{"marketSlug":"cs2-team-alpha-team-beta-market"}</script>'
        )
        polymarket = FakePolymarketProvider(polymarket_events)

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_tournament_id="101",
            polymarket_page_url=["https://polymarket.com/uk/sports/cs2/cct"],
            providers={
                "oddspapi": FakeOddsPapiProvider(stake_events),
                "polymarket": polymarket,
            },
            page_fetcher=lambda _url: html,
        )

        event_slug_calls = [
            call for call in polymarket.calls if call.get("event_slug")
        ]
        market_slug_calls = [
            call for call in polymarket.calls if call.get("market_slug")
        ]
        self.assertEqual(
            event_slug_calls[0]["event_slug"],
            "cs2-team-alpha-team-beta-2026-06-28",
        )
        self.assertEqual(
            market_slug_calls[0]["market_slug"],
            "cs2-team-alpha-team-beta-market",
        )
        self.assertEqual(result["debug"]["polymarket_page_url_count"], 1)
        self.assertEqual(result["debug"]["polymarket_page_candidate_slug_count"], 2)
        self.assertEqual(result["summary"]["polymarket_count"], 1)

    def test_page_url_with_no_candidates_does_not_crash(self) -> None:
        stake_events, _polymarket_events = _arbitrage_fixture_events()

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_tournament_id="101",
            polymarket_page_url=["https://polymarket.com/uk/sports/cs2/empty"],
            providers={
                "oddspapi": FakeOddsPapiProvider(stake_events),
                "polymarket": FakePolymarketProvider([]),
            },
            page_fetcher=lambda _url: "<html><body>No markets here</body></html>",
        )

        self.assertEqual(result["summary"]["polymarket_count"], 0)
        self.assertEqual(result["debug"]["polymarket_page_url_count"], 1)
        self.assertEqual(result["debug"]["polymarket_page_candidate_slug_count"], 0)
        self.assertEqual(result["debug"]["polymarket_page_no_candidate_count"], 1)

    def test_scan_with_arbitrage_includes_summary(self) -> None:
        stake_events, polymarket_events = _arbitrage_fixture_events()

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_tournament_id="101",
            polymarket_search=["cs2"],
            find_arbitrage=True,
            polymarket_fee_rate=0,
            providers={
                "oddspapi": FakeOddsPapiProvider(stake_events),
                "polymarket": FakePolymarketProvider(polymarket_events),
            },
        )

        self.assertEqual(result["summary"]["matched_count"], 1)
        self.assertEqual(result["summary"]["opportunity_count"], 1)
        self.assertIn("arbitrage", result)
        self.assertEqual(result["arbitrage"]["summary"]["opportunity_count"], 1)

    def test_scan_debug_includes_polymarket_price_sources(self) -> None:
        stake_events, polymarket_events = _arbitrage_fixture_events()
        stake_data = stake_events[0].to_dict()
        stake_data["odds_a"] = 1.6
        polymarket_data = polymarket_events[0].to_dict()
        polymarket_data["odds_b"] = 1 / 0.34
        polymarket_data["raw"] = {
            "market_id": "pm-market-1",
            "slug": "team-alpha-team-beta-market",
            "original_outcomes": ["Team Alpha", "Team Beta"],
            "original_prices": [0.714285714, 0.333333333],
            "selected_prices": [0.714285714, 0.34],
            "price_sources": ["outcomePrices", "bestAsk"],
            "executable_buy_prices": [None, 0.34],
            "executable_price_sources": ["", "bestAsk"],
            "polymarket_actionable": [False, True],
            "market": {
                "id": "pm-market-1",
                "slug": "team-alpha-team-beta-market",
                "outcomes": json.dumps(["Team Alpha", "Team Beta"]),
                "outcomePrices": json.dumps(["0.714285714", "0.333333333"]),
                "bestBid": "0.32",
                "bestAsk": "0.34",
                "lastTradePrice": "0.33",
                "clobTokenIds": json.dumps(["clob-alpha", "clob-beta"]),
                "tokens": [
                    {
                        "outcome": "Team Alpha",
                        "token_id": "tok-alpha",
                        "price": "0.71",
                    },
                    {
                        "outcome": "Team Beta",
                        "token_id": "tok-beta",
                        "price": "0.34",
                        "bestAsk": "0.34",
                    },
                ],
            },
        }

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_tournament_id="101",
            polymarket_search=["cs2"],
            find_arbitrage=True,
            polymarket_fee_rate=0.03,
            providers={
                "oddspapi": FakeOddsPapiProvider([_event_from_dict(stake_data)]),
                "polymarket": FakePolymarketProvider(
                    [_event_from_dict(polymarket_data)]
                ),
            },
        )

        self.assertEqual(result["summary"]["opportunity_count"], 1)
        price_sample = result["debug"]["polymarket_price_sample"][0]
        self.assertIn("slug=team-alpha-team-beta-market", price_sample)
        self.assertIn("market_id=pm-market-1", price_sample)
        self.assertIn("outcomes=Team Alpha|Team Beta", price_sample)
        self.assertIn("odds=1.4/2.941176", price_sample)
        self.assertIn("implied_prices=0.714286/0.34", price_sample)
        self.assertIn("price_source=outcomePrices/bestAsk", price_sample)
        self.assertIn("executable_buy_prices=/0.34", price_sample)
        self.assertIn("outcomePrices=0.714285714/0.333333333", price_sample)
        self.assertIn("bestAsk=0.34", price_sample)
        self.assertIn("lastTradePrice=0.33", price_sample)
        self.assertIn("clobTokenIds=clob-alpha/clob-beta", price_sample)
        self.assertIn("token_id:tok-beta", price_sample)

        opportunity_debug = result["debug"]["opportunity_price_debug"][0]
        self.assertIn("polymarket_team=Team Beta", opportunity_debug)
        self.assertIn("pm_raw_odds=2.941176", opportunity_debug)
        self.assertIn("pm_eff_odds=2.882941", opportunity_debug)
        self.assertIn("pm_raw_price=0.34", opportunity_debug)
        self.assertIn("executable_buy_price=0.34", opportunity_debug)
        self.assertIn("pm_fee_per_share=0.006868", opportunity_debug)
        self.assertIn("pm_all_in_price=0.346868", opportunity_debug)
        self.assertIn("pm_price_source=bestAsk", opportunity_debug)
        self.assertIn("outcomePrices[1]=0.333333333", opportunity_debug)
        self.assertIn("clobTokenIds[1]=clob-beta", opportunity_debug)
        self.assertIn("tokens[1]=", opportunity_debug)
        self.assertIn("token_id:tok-beta", opportunity_debug)

        output = format_scan_table(result, debug=True)

        self.assertIn("polymarket_price_sample=", output)
        self.assertIn("opportunity_price_debug=", output)

    def test_blg_hle_style_best_ask_fee_blocks_false_opportunity(self) -> None:
        start_time = datetime(2026, 7, 10, 16, 0, tzinfo=timezone.utc)
        stake_event = ProviderOddsEvent(
            provider="oddspapi",
            bookmaker="stake",
            game="lol",
            tournament="Mid-Season Invitational",
            start_time=start_time,
            team_a="Bilibili Gaming",
            odds_a=2.3,
            team_b="Hanwha Life Esports",
            odds_b=1.65,
            market_type="h2h",
            event_url="https://example.test/stake/blg-hle",
            raw={"fixture_id": "stake-blg-hle"},
        )
        polymarket_event = ProviderOddsEvent(
            provider="polymarket",
            bookmaker="polymarket",
            game="lol",
            tournament="Mid-Season Invitational",
            start_time=start_time,
            team_a="Bilibili Gaming",
            odds_a=1 / 0.39,
            team_b="Hanwha Life Esports",
            odds_b=1 / 0.615,
            market_type="h2h",
            event_url="https://polymarket.com/event/blg-hle",
            raw={
                "market_id": "pm-market-blg-hle",
                "slug": "blg-hle",
                "original_outcomes": [
                    "Bilibili Gaming",
                    "Hanwha Life Esports",
                ],
                "original_prices": [0.385, 0.615],
                "selected_prices": [0.39, 0.615],
                "price_sources": ["bestAsk", "outcomePrices"],
                "executable_buy_prices": [0.39, None],
                "executable_price_sources": ["bestAsk", ""],
                "polymarket_actionable": [True, False],
                "market": {
                    "id": "pm-market-blg-hle",
                    "slug": "blg-hle",
                    "outcomes": json.dumps(
                        ["Bilibili Gaming", "Hanwha Life Esports"]
                    ),
                    "outcomePrices": json.dumps(["0.385", "0.615"]),
                    "bestAsk": "0.39",
                    "lastTradePrice": "0.39",
                },
            },
        )

        result = run_scan(
            sport="lol",
            bookmaker="stake",
            stake_tournament_id="2527",
            polymarket_search=["lol"],
            find_arbitrage=True,
            polymarket_fee_rate=0.03,
            providers={
                "oddspapi": FakeOddsPapiProvider([stake_event]),
                "polymarket": FakePolymarketProvider([polymarket_event]),
            },
        )

        self.assertEqual(result["summary"]["matched_count"], 1)
        self.assertEqual(result["summary"]["opportunity_count"], 0)
        self.assertEqual(result["arbitrage"]["opportunities"], [])
        self.assertEqual(
            result["arbitrage"]["summary"]["opportunity_count"],
            0,
        )
        self.assertEqual(
            result["debug"]["opportunity_price_debug"],
            [],
        )
        reason_counts = result["arbitrage"].get("debug", {}).get(
            "reason_counts",
            {},
        )
        self.assertEqual(reason_counts["no_arbitrage"], 1)
        self.assertEqual(reason_counts["non_executable_polymarket_price"], 1)

    def test_scan_without_find_arbitrage_does_not_include_arbitrage_block(self) -> None:
        stake_events, polymarket_events = _arbitrage_fixture_events()

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_tournament_id="101",
            polymarket_search=["cs2"],
            providers={
                "oddspapi": FakeOddsPapiProvider(stake_events),
                "polymarket": FakePolymarketProvider(polymarket_events),
            },
        )

        self.assertEqual(result["summary"]["matched_count"], 1)
        self.assertNotIn("arbitrage", result)

    def test_telegram_dry_run_with_no_opportunities_sends_zero_messages(self) -> None:
        stake_events, polymarket_events = _snapshot_fixture_events()

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_tournament_id="101",
            polymarket_search=["cs2"],
            find_arbitrage=True,
            telegram_alert=True,
            telegram_dry_run=True,
            providers={
                "oddspapi": FakeOddsPapiProvider(stake_events),
                "polymarket": FakePolymarketProvider(polymarket_events),
            },
        )

        self.assertEqual(result["summary"]["opportunity_count"], 0)
        self.assertEqual(result["telegram"]["message_count"], 0)
        self.assertEqual(result["telegram"]["sent_count"], 0)
        self.assertEqual(result["telegram"]["skipped_reason"], "no_opportunities")

    def test_telegram_dry_run_with_opportunity_creates_message_preview(self) -> None:
        stake_events, polymarket_events = _arbitrage_fixture_events()
        with patch(
            "provider_odds.cli.scan.OddsPapiProvider.from_env",
            return_value=FakeOddsPapiProvider(stake_events),
        ):
            with patch(
                "provider_odds.cli.scan.PolymarketProvider.from_env",
                return_value=FakePolymarketProvider(polymarket_events),
            ):
                with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    result = scan_main(
                        [
                            "--sport",
                            "cs2",
                            "--bookmaker",
                            "stake",
                            "--stake-tournament-id",
                            "101",
                            "--polymarket-search",
                            "cs2",
                            "--find-arbitrage",
                            "--polymarket-fee-rate",
                            "0",
                            "--telegram-alert",
                            "--telegram-dry-run",
                            "--format",
                            "table",
                        ]
                    )

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("scan summary:", output)
        self.assertIn("opportunity_count=1", output)
        self.assertIn("telegram:", output)
        self.assertIn("message_count=1", output)
        self.assertIn("⚡ CS2 ARB", output)

    def test_telegram_alert_without_find_arbitrage_gives_clear_error(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            with self.assertRaises(SystemExit) as cm:
                scan_main(
                    [
                        "--sport",
                        "cs2",
                        "--stake-tournament-id",
                        "101",
                        "--telegram-alert",
                    ]
                )

        self.assertEqual(cm.exception.code, 2)
        self.assertIn(
            "--telegram-alert/--telegram-dry-run requires --find-arbitrage",
            stderr.getvalue(),
        )

    def test_json_output_includes_expected_blocks(self) -> None:
        stake_events, polymarket_events = _arbitrage_fixture_events()
        with patch(
            "provider_odds.cli.scan.OddsPapiProvider.from_env",
            return_value=FakeOddsPapiProvider(stake_events),
        ):
            with patch(
                "provider_odds.cli.scan.PolymarketProvider.from_env",
                return_value=FakePolymarketProvider(polymarket_events),
            ):
                with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    result = scan_main(
                        [
                            "--sport",
                            "cs2",
                            "--stake-tournament-id",
                            "101",
                            "--polymarket-search",
                            "cs2",
                            "--find-arbitrage",
                            "--polymarket-fee-rate",
                            "0",
                            "--telegram-dry-run",
                            "--format",
                            "json",
                        ]
                    )

        self.assertEqual(result, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["summary"]["stake_count"], 1)
        self.assertEqual(payload["summary"]["polymarket_count"], 1)
        self.assertEqual(payload["summary"]["matched_count"], 1)
        self.assertEqual(payload["summary"]["opportunity_count"], 1)
        self.assertEqual(len(payload["stake_events"]), 1)
        self.assertEqual(len(payload["polymarket_events"]), 1)
        self.assertEqual(len(payload["matches"]), 1)
        self.assertEqual(payload["arbitrage"]["summary"]["opportunity_count"], 1)
        self.assertEqual(payload["telegram"]["message_count"], 1)

    def test_repeated_polymarket_searches_are_deduped(self) -> None:
        stake_events, polymarket_events = _snapshot_fixture_events()
        polymarket = FakePolymarketProvider(polymarket_events)

        result = run_scan(
            sport="cs2",
            bookmaker="stake",
            stake_tournament_id="101",
            polymarket_search=["cs2", "counter-strike"],
            providers={
                "oddspapi": FakeOddsPapiProvider(stake_events),
                "polymarket": polymarket,
            },
        )

        self.assertEqual(len(polymarket.calls), 2)
        self.assertEqual(result["summary"]["polymarket_count"], 2)

    def test_debug_output_includes_polymarket_search_counts(self) -> None:
        stake_events, polymarket_events = _arbitrage_fixture_events()
        with patch(
            "provider_odds.cli.scan.OddsPapiProvider.from_env",
            return_value=FakeOddsPapiProvider(stake_events),
        ):
            with patch(
                "provider_odds.cli.scan.PolymarketProvider.from_env",
                return_value=FakePolymarketProvider(polymarket_events),
            ):
                with patch(
                    "provider_odds.cli.scan.fetch_polymarket_page_html",
                    side_effect=OSError("offline"),
                ):
                    with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                        result = scan_main(
                            [
                                "--sport",
                                "cs2",
                                "--stake-tournament-id",
                                "101",
                                "--polymarket-page-url",
                                "https://polymarket.com/uk/sports/cs2/cct",
                                "--polymarket-search",
                                "manual cs2",
                                "--polymarket-search-from-stake-teams",
                                "--format",
                                "table",
                                "--debug",
                            ]
                        )

        self.assertEqual(result, 0)
        output = stdout.getvalue()
        self.assertIn("stake_requested_tournament_count=1", output)
        self.assertIn("stake_skipped_tournament_count=0", output)
        self.assertIn("stake_error_count=0", output)
        self.assertIn("polymarket_user_search_count=1", output)
        self.assertIn("polymarket_generated_search_count=1", output)
        self.assertIn(
            "polymarket_generated_searches_sample=Team Alpha Team Beta",
            output,
        )
        self.assertIn("polymarket_page_url_count=1", output)
        self.assertIn("polymarket_page_fetch_error_count=1", output)
        self.assertIn("polymarket_keyset_url_count=0", output)
        self.assertIn("polymarket_keyset_next_cursor_seen=false", output)
        self.assertIn("polymarket_count=1", output)


if __name__ == "__main__":
    unittest.main()
