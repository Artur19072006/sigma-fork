"""Loopback-only read-only dashboard API and static-file server."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
from typing import Sequence
from urllib.parse import unquote, urlsplit

from dashboard.api.state import DashboardDataError, DashboardRepository


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEMO = ROOT / "data" / "demo-state.json"


class DashboardHandler(BaseHTTPRequestHandler):
    repository: DashboardRepository
    static_dir: Path | None = None
    server_version = "realtime-v3-dashboard/1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        path = urlsplit(self.path).path
        if path in {"/api/dashboard", "/api/health"}:
            self._api_response(health_only=path == "/api/health")
            return
        self._static_response(path)

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler contract
        path = urlsplit(self.path).path
        if path in {"/api/dashboard", "/api/health"}:
            self._api_response(health_only=path == "/api/health", head=True)
            return
        self._static_response(path, head=True)

    def do_POST(self) -> None:  # noqa: N802 - explicit read-only rejection
        self.send_error(HTTPStatus.METHOD_NOT_ALLOWED, "dashboard is read-only")

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

    def _api_response(self, *, health_only: bool, head: bool = False) -> None:
        try:
            payload = self.repository.snapshot()
            if health_only:
                payload = {
                    "generated_at": payload["generated_at"],
                    "mode": payload["mode"],
                    "scanner": payload["scanner"],
                }
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            status = HTTPStatus.OK
        except DashboardDataError:
            encoded = b'{"status":"unavailable","detail":"safe dashboard data could not be read"}'
            status = HTTPStatus.SERVICE_UNAVAILABLE
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        if not head:
            self.wfile.write(encoded)

    def _static_response(self, raw_path: str, *, head: bool = False) -> None:
        if self.static_dir is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        requested = unquote(raw_path).lstrip("/") or "index.html"
        candidate = (self.static_dir / requested).resolve()
        root = self.static_dir.resolve()
        if root not in candidate.parents and candidate != root:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file():
            candidate = root / "index.html"
        try:
            content = candidate.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header(
            "Cache-Control",
            "no-cache" if candidate.name == "index.html" else "public, max-age=31536000, immutable",
        )
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if not head:
            self.wfile.write(content)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )

    def log_message(self, format: str, *args: object) -> None:
        # Keep logs bounded and never include query strings.
        safe_path = urlsplit(self.path).path[:200]
        print(f"dashboard {self.command} {safe_path} {args[1] if len(args) > 1 else '-'}")


def build_server(
    *,
    host: str,
    port: int,
    repository: DashboardRepository,
    static_dir: Path | None,
) -> ThreadingHTTPServer:
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError("dashboard server must bind to a loopback address")
    handler = type(
        "ConfiguredDashboardHandler",
        (DashboardHandler,),
        {"repository": repository, "static_dir": static_dir},
    )
    return ThreadingHTTPServer((host, port), handler)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4310)
    parser.add_argument(
        "--health-file",
        type=Path,
        default=Path(os.environ.get("REALTIME_V3_DASHBOARD_HEALTH_FILE", "/run/realtime-v3/health.json")),
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=(
            Path(os.environ["REALTIME_V3_DASHBOARD_STATE_FILE"])
            if os.environ.get("REALTIME_V3_DASHBOARD_STATE_FILE")
            else None
        ),
    )
    parser.add_argument("--demo-file", type=Path, default=DEFAULT_DEMO)
    parser.add_argument(
        "--demo",
        action="store_true",
        default=os.environ.get("SIGMA_FORK_DEMO") == "1",
        help="use clearly labelled sample opportunities when no state file exists",
    )
    parser.add_argument("--static-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--stale-after-seconds", type=float, default=120)
    arguments = parser.parse_args(argv)
    static_dir = arguments.static_dir.resolve() if arguments.static_dir.is_dir() else None
    repository = DashboardRepository(
        health_path=arguments.health_file,
        state_path=arguments.state_file,
        demo_path=arguments.demo_file,
        demo_enabled=arguments.demo,
        stale_after_seconds=arguments.stale_after_seconds,
    )
    server = build_server(
        host=arguments.host,
        port=arguments.port,
        repository=repository,
        static_dir=static_dir,
    )
    print(f"realtime-v3 dashboard listening on http://{arguments.host}:{arguments.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
