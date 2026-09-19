"""Local-only call-centre route fixture for Asterisk IVR verification.

This deliberately does not use the application database.  It lets a local
Asterisk/AGI test prove the important distinction between an unknown digit
and a configured department with no active staff: every requested digit is
returned as a valid department with zero targets and an AI no-answer action.

Run only on a development host::

    python3 scripts/ivr_route_fixture_server.py

The server binds to 127.0.0.1 by default and records dial-attempt POSTs only
in memory/stdout.  It never logs request headers (which may contain the
internal API key).
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


class RouteFixtureHandler(BaseHTTPRequestHandler):
    server_version = "SunwayIVRFixture/1.0"

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        prefix = "/api/callcentre/route/"
        if not path.startswith(prefix):
            self._reply(404, {"detail": "Not found"})
            return
        digit = path.removeprefix(prefix)
        if len(digit) != 1 or digit not in "12345678":
            self._reply(404, {"detail": "No enabled department for this digit"})
            return
        self._reply(
            200,
            {
                "department_id": "00000000-0000-0000-0000-000000000000",
                "department_name": f"IVR verification {digit}",
                "dtmf_digit": digit,
                "ring_timeout_seconds": 1,
                "no_answer_action": "ai",
                "targets": [],
                "is_reachable": False,
            },
        )

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if urlparse(self.path).path != "/api/callcentre/route/attempts":
            self._reply(404, {"detail": "Not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        self._reply(202, {})

    def log_message(self, message: str, *args: object) -> None:
        # BaseHTTPRequestHandler's access line contains method/path/status but
        # not request headers, so INTERNAL_API_KEY is never printed.
        print(f"IVR fixture: {message % args}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        parser.error("the IVR route fixture may bind only to localhost")
    server = ThreadingHTTPServer((args.host, args.port), RouteFixtureHandler)
    print(f"IVR route fixture listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
