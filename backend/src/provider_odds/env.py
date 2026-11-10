from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> dict[str, str]:
    """Load simple KEY=VALUE lines into os.environ without external dependencies."""
    env_path = Path(path)
    loaded: dict[str, str] = {}
    if not env_path.exists():
        return loaded

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue

        loaded[key] = value
        os.environ.setdefault(key, value)

    return loaded
