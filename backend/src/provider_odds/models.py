from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class ProviderOddsEvent:
    provider: str
    bookmaker: str
    game: str
    tournament: str | None
    start_time: datetime | None
    team_a: str
    team_b: str
    market_type: str
    odds_a: float
    odds_b: float
    event_url: str | None
    raw: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["start_time"] = self.start_time.isoformat() if self.start_time else None
        return data
