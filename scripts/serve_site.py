"""Loopback-only, uncached static preview with enough backlog for browser checks."""

from __future__ import annotations

import argparse
import contextlib
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class PreviewHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


class PreviewServer(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "site"
    with PreviewServer(
        ("127.0.0.1", args.port), partial(PreviewHandler, directory=str(root))
    ) as server:
        print(f"Local: http://127.0.0.1:{args.port}", flush=True)
        with contextlib.suppress(KeyboardInterrupt):
            server.serve_forever()
