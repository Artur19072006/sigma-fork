"""Atomically publish a sanitized Duel report for the dashboard sidecar.

This command is intentionally separate from the production scanner. A failure
here cannot stop or mutate scanner execution.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Sequence

from dashboard.api.state import (
    DashboardDataError,
    MAX_SOURCE_BYTES,
    normalize_runtime_report,
    read_json,
)


def _read_report(path: Path | None) -> dict[str, object]:
    if path is not None:
        report = read_json(path)
        if report is None:
            raise DashboardDataError("runtime report does not exist")
        return report
    raw = sys.stdin.buffer.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise DashboardDataError("runtime report exceeds size bound")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DashboardDataError("runtime report is not valid JSON") from exc
    if not isinstance(value, dict):
        raise DashboardDataError("runtime report root must be an object")
    return value


def publish(report: dict[str, object], output: Path, *, duration: float | None) -> None:
    previous = read_json(output)
    payload = normalize_runtime_report(
        report,
        previous=previous,
        duration_seconds=duration,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, output)
        if os.name != "nt":
            directory_fd = os.open(output.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration-seconds", type=float)
    arguments = parser.parse_args(argv)
    try:
        publish(
            _read_report(arguments.input),
            arguments.output,
            duration=arguments.duration_seconds,
        )
    except (DashboardDataError, OSError, ValueError) as exc:
        print(f"dashboard snapshot was not published: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
