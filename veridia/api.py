"""Minimal JSON-over-HTTP API (stdlib only, one thread per request).

POST /match                      {"requirement": {...}, "as_of": "YYYY-MM-DD", "limit": 10}
POST /why-not                    {"requirement": {...}, "key": "VM1006"}
POST /sessions/<id>/requirement  {"requirement": {...}}      -> result + diff against previous revision
POST /sessions/<id>/hold         {"key": "VM1006"}
GET  /sessions/<id>/history
GET  /quality        GET /health
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .holds import HoldRejected
from .requirement import FieldError, ValidationError
from .service import NotFound, Service

MAX_BODY = 64 * 1024


def make_server(svc: Service, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep stdout quiet; the audit log is the record
            pass

        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                raise ValidationError([FieldError("body", f"must be at most {MAX_BODY} bytes")])
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                raise ValidationError([FieldError("body", "is not valid JSON")]) from None
            if not isinstance(body, dict):
                raise ValidationError([FieldError("body", "must be a JSON object")])
            return body

        def _route(self, method: str) -> tuple[int, dict]:
            path = self.path.split("?")[0].rstrip("/") or "/"
            if method == "GET":
                if path == "/health":
                    return 200, {"status": "ok", "as_of": svc.cfg.as_of.isoformat(), "dataset_sha256": svc.ds.sha256}
                if path == "/quality":
                    return 200, svc.ds.quality_report()
                m = re.fullmatch(r"/sessions/([^/]+)/history", path)
                if m:
                    return 200, {"history": svc.history(m.group(1))}
                return 404, {"error": {"type": "not_found", "message": "unknown route"}}
            body = self._body()
            if path == "/match":
                return 200, svc.match(body.get("requirement"), as_of=body.get("as_of"), limit=body.get("limit"))
            if path == "/why-not":
                return 200, svc.why_not(body.get("requirement"), str(body.get("key", "")), as_of=body.get("as_of"))
            m = re.fullmatch(r"/sessions/([^/]+)/(requirement|hold)", path)
            if m:
                sid, action = m.groups()
                if action == "requirement":
                    return 200, svc.submit(sid, body.get("requirement"), as_of=body.get("as_of"), limit=body.get("limit"))
                return 200, svc.hold(sid, str(body.get("key", "")))
            return 404, {"error": {"type": "not_found", "message": "unknown route"}}

        def _handle(self, method: str) -> None:
            try:
                code, body = self._route(method)
            except ValidationError as exc:
                code, body = 400, {"error": exc.to_dict()}
            except NotFound as exc:
                code, body = 404, {"error": {"type": "not_found", "message": str(exc)}}
            except HoldRejected as exc:
                code, body = 409, {"error": {"type": "capacity_unavailable", "message": str(exc)}}
            except Exception:  # never leak internals
                code, body = 500, {"error": {"type": "internal_error", "message": "unexpected error"}}
            self._send(code, body)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return ThreadingHTTPServer((host, port), Handler)
