"""`jevloop serve`: a tiny static file server for the dashboard.

Serves the dashboard HTML files alongside ~/.jev-loop/latest.json so
dashboard/index.html and dashboard/wall.html can poll it with a plain
fetch(). No framework, no build step: http.server with two directories
merged via a symlink-free request handler.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import socketserver
import time
from pathlib import Path

LOG_DIR = Path(os.environ.get("JEV_LOOP_HOME", str(Path.home() / ".jev-loop")))
SKILL_DIR = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = SKILL_DIR / "dashboard"


_last_good: dict[str, bytes] = {}


def read_feed(path: Path, attempts: int = 5, wait_s: float = 0.02) -> bytes | None:
    """Read a feed file in one go and close it straight away, so the server
    never holds latest.json open while the loop swaps in a new copy (on
    Windows an open file blocks that swap). A read that hits the swap
    mid-way (PermissionError, or JSON cut short) is retried briefly, then
    falls back to the last good copy served, never an error page."""
    for _ in range(attempts):
        try:
            data = path.read_bytes()
            if path.suffix == ".json":
                json.loads(data)  # a partial write fails here, not in the browser
            _last_good[path.name] = data
            return data
        except FileNotFoundError:
            break
        except (PermissionError, ValueError):
            time.sleep(wait_s)
    return _last_good.get(path.name)


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path not in ("/latest.json", "/log.jsonl"):
            return super().do_GET()
        data = read_feed(LOG_DIR / path.lstrip("/"))
        if data is None:
            data = b'{"ticks": [], "stats": {}}' if path == "/latest.json" else b""
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "application/json" if path == "/latest.json" else "text/plain",
        )
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def translate_path(self, path: str) -> str:
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path in ("/latest.json", "/log.jsonl"):
            return str(LOG_DIR / path.lstrip("/"))
        if path == "/":
            path = "/index.html"
        candidate = DASHBOARD_DIR / path.lstrip("/")
        return str(candidate)

    def log_message(self, format, *args):  # noqa: A002
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jev-loop serve")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    latest = LOG_DIR / "latest.json"
    if not latest.exists():
        latest.write_text('{"ticks": [], "stats": {}}')

    with socketserver.TCPServer(("127.0.0.1", args.port), Handler) as httpd:
        print(f"dashboard: http://127.0.0.1:{args.port}/index.html")
        print(f"dark wall: http://127.0.0.1:{args.port}/wall.html")
        print(f"raw feed:  http://127.0.0.1:{args.port}/latest.json")
        print("Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
