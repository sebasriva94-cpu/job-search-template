"""Save the owner's Applied / Later / Not for me clicks from the job dashboard (approved 2024-12-09).

A small web helper: it serves job-dashboard.html and records each click in dashboard/decisions.json,
which the dashboard, the morning brief and the weekly review all read. It runs on the laptop (started
at login) and can run unchanged on a cloud server later (--host, --origin, JOBSEARCH_ROOT).

Safety: it answers on 127.0.0.1 only unless told otherwise; it only accepts a request addressed to
its own host name (so another web page cannot reach it through DNS tricks); and every click must carry
the secret token written into the dashboard page, so no other site can record a decision.

    python scripts/decision_helper.py [--port 8765] [--host 127.0.0.1] [--origin https://...]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hmac
import http.server
import json
import pathlib
import threading

try:
    from scripts import job_dashboard as dash
    from scripts.sweep_common import project_root
except ModuleNotFoundError:  # Direct execution: scripts/ is sys.path[0].
    import job_dashboard as dash
    from sweep_common import project_root

VALID = {"applied", "later", "skip", "undo"}
MAX_BODY = 4096
FIELDS = ("company", "title", "job_url", "location")
_LOCK = threading.Lock()


def record(root: pathlib.Path, payload: dict, today: dt.date | None = None) -> dict:
    """Store one click (or remove it with "undo") and return the saved entry. Raises ValueError on bad input."""
    decision = str(payload.get("decision") or "")
    key = str(payload.get("key") or "").strip()
    if decision not in VALID or not key or len(key) > 300:
        raise ValueError("bad decision or key")
    today = today or dt.date.today()
    with _LOCK:
        decisions = dash.read_decisions(root)
        if decision == "undo":
            decisions.pop(key, None)
            entry: dict = {}
        else:
            entry = {"decision": decision, "on": today.isoformat(),
                     **{f: str(payload.get(f) or "")[:500] for f in FIELDS}}
            decisions[key] = entry
        dash.atomic_write_text(root / dash.DECISIONS_PATH,
                               json.dumps(decisions, indent=1, ensure_ascii=False, sort_keys=True))
    return entry


def make_handler(root: pathlib.Path, token: str, hosts: set[str], origins: set[str]):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "JobDecisionHelper/1"

        def _host_ok(self) -> bool:
            return (self.headers.get("Host") or "").lower() in hosts

        def _send(self, code: int, body: bytes, kind: str = "application/json") -> None:
            self.send_response(code)
            origin = self.headers.get("Origin")
            if origin in origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: object) -> None:
            self._send(code, json.dumps(obj).encode("utf-8"))

        def do_OPTIONS(self) -> None:  # noqa: N802 (http.server naming)
            self.send_response(204)
            if self.headers.get("Origin") in origins:
                self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
                self.send_header("Access-Control-Allow-Methods", "GET, POST")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._json(403, {"ok": False, "error": "wrong host"})
            path = self.path.split("?", 1)[0]
            if path in ("/", "/job-dashboard.html"):
                try:
                    return self._send(200, (root / dash.HTML_PATH).read_bytes(), "text/html; charset=utf-8")
                except OSError:
                    return self._json(404, {"ok": False, "error": "dashboard not built yet"})
            if path == "/decisions":
                return self._json(200, {k: v.get("decision") for k, v in dash.read_decisions(root).items()})
            if path == "/health":
                return self._json(200, {"ok": True})
            return self._json(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_ok() or (self.headers.get("Origin") and self.headers["Origin"] not in origins):
                return self._json(403, {"ok": False, "error": "not allowed"})
            if self.path.split("?", 1)[0] != "/decide":
                return self._json(404, {"ok": False, "error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_BODY:
                return self._json(413, {"ok": False, "error": "bad size"})
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return self._json(400, {"ok": False, "error": "bad json"})
            if not isinstance(payload, dict) or not hmac.compare_digest(str(payload.get("token") or ""), token):
                return self._json(403, {"ok": False, "error": "bad token"})
            try:
                entry = record(root, payload)
            except ValueError as exc:
                return self._json(400, {"ok": False, "error": str(exc)})
            return self._json(200, {"ok": True, "saved": entry})

        def log_message(self, fmt: str, *args) -> None:  # quiet: runs hidden at login
            pass

    return Handler


def serve(root: pathlib.Path, host: str = "127.0.0.1", port: int = dash.HELPER_PORT,
          extra_origins: tuple[str, ...] = ()) -> http.server.ThreadingHTTPServer:
    token = dash.decision_token(root)
    server = http.server.ThreadingHTTPServer((host, port), http.server.BaseHTTPRequestHandler)
    port = server.server_address[1]  # the real port when 0 asked for any free one (tests)
    hosts = {f"{host}:{port}".lower(), f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {"null", f"http://127.0.0.1:{port}", f"http://localhost:{port}", *extra_origins}
    hosts |= {o.split("://", 1)[-1].lower() for o in extra_origins}  # a cloud host name reaches it as itself
    server.RequestHandlerClass = make_handler(root, token, hosts, origins)
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Save dashboard clicks (Applied / Later / Not for me)")
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on (default: this computer only)")
    parser.add_argument("--port", type=int, default=dash.HELPER_PORT)
    parser.add_argument("--origin", action="append", default=[], help="extra page address allowed to send clicks")
    args = parser.parse_args(argv)
    server = serve(project_root(), args.host, args.port, tuple(args.origin))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
