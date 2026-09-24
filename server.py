#!/usr/bin/env python3
"""orchestrator-desk: a local page over team-skills-orchestrator state."""

import json
import mimetypes
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from desk import deliver, view

HOST = "127.0.0.1"
PORT = int(os.environ.get("DESK_PORT", "8800"))
HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
REPLIES = os.environ.get("DESK_REPLIES_LOG", os.path.join(HERE, "replies.jsonl"))
ALLOWED_HOSTS = {f"{HOST}:{PORT}", f"localhost:{PORT}"}


class Handler(BaseHTTPRequestHandler):
    server_version = "orchestrator-desk"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        # Guards against DNS rebinding: only our own host name reaches the API.
        return self.headers.get("Host", "") in ALLOWED_HOSTS

    def _file(self, path):
        if not path or not os.path.isfile(path):
            return self._send(404, {"error": "not found"})
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "host not allowed"})
        route = self.path.split("?", 1)[0]
        if route in ("/", "/index.html"):
            return self._file(os.path.join(WEB, "index.html"))
        if route == "/api/state":
            try:
                data = view.build()
                data["replies"] = deliver.recent(REPLIES, 20)
                return self._send(200, data)
            except Exception:
                return self._send(500, {"error": traceback.format_exc(limit=5)})
        if route == "/api/health":
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "host not allowed"})
        origin = self.headers.get("Origin", "")
        if origin not in {f"http://{h}" for h in ALLOWED_HOSTS}:
            return self._send(403, {"error": "origin not allowed"})
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._send(415, {"error": "json only"})
        if self.path != "/api/reply":
            return self._send(404, {"error": "not found"})
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 64 * 1024)
            body = json.loads(self.rfile.read(length) or b"{}")
            session_id, stream, text = body.get("session_id", ""), body.get("stream", ""), body.get("text", "")
            orch = next((o for o in view.build_orchestrators() if o["session_id"] and o["session_id"] == session_id), None)
            if not orch:
                return self._send(400, {"error": "unknown orchestrator session"})
            if stream and stream not in orch["streams"]:
                return self._send(400, {"error": "unknown stream"})
            record = deliver.send(session_id, stream, text, REPLIES)
            return self._send(200 if record.get("ok") else 502, record)
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception:
            return self._send(500, {"error": traceback.format_exc(limit=5)})


def main():
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    # Warm the Linear/GitHub cache so the first page load does not wait on the CLIs.
    threading.Thread(target=view.build, daemon=True).start()
    print(f"orchestrator-desk on http://{HOST}:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
