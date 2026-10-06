#!/usr/bin/env python3
"""orchestrator-desk: epics and running tasks on one page, over the orchestrator's files and tso."""

import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from desk import deliver, epics, links, terminal, tsoq, uploads, view

HOST = "127.0.0.1"
PORT = int(os.environ.get("DESK_PORT", "8800"))
HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
REPLIES = os.environ.get("DESK_REPLIES_LOG", os.path.join(HERE, "replies.jsonl"))
ALLOWED_HOSTS = {f"{HOST}:{PORT}", f"localhost:{PORT}"}


_snap = {"data": None, "at": 0.0, "busy": False}
_snap_lock = threading.Lock()


def _compute_state():
    data = view.build()
    data["replies"] = deliver.recent(REPLIES, 20)
    data["ui_version"] = int(os.path.getmtime(os.path.join(WEB, "index.html")))
    data["epics"] = epics.summary(data["streams"])
    return data


def _state():
    """One build at a time; a page opened under load gets the last snapshot instead of piling up more builds."""
    with _snap_lock:
        fresh = _snap["data"] is not None and time.time() - _snap["at"] < 4
        if fresh or (_snap["busy"] and _snap["data"] is not None):
            return _snap["data"]
        if _snap["busy"]:
            wait = True
        else:
            _snap["busy"], wait = True, False
    if wait:
        for _ in range(240):
            time.sleep(0.5)
            if _snap["data"] is not None:
                return _snap["data"]
    try:
        data = _compute_state()
    except Exception:
        with _snap_lock:
            _snap["busy"] = False
        raise
    with _snap_lock:
        _snap.update(data=data, at=time.time(), busy=False)
    return data


def _drop_snapshot():
    with _snap_lock:
        _snap["at"] = 0.0


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
                         "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(data)

    def _send_record(self, record):
        return self._send(200 if record.get("ok") else 502, record)

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
                return self._send(200, _state())
            except Exception:
                return self._send(500, {"error": traceback.format_exc(limit=5)})
        if route.startswith("/api/question/"):
            try:
                return self._send(200, tsoq.question(route.rsplit("/", 1)[1]))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                return self._send(404, {"error": str(e)[:300]})
        if route == "/api/issues":
            # Titles for task ids mentioned in text; served from the Linear cache, never blocking.
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            ids = [i for i in query.replace("ids=", "").split(",") if re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", i)][:60]
            return self._send(200, {i: links.linear(i) for i in ids})
        if route == "/api/health":
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "host not allowed"})
        origin = self.headers.get("Origin", "")
        if origin not in {f"http://{h}" for h in ALLOWED_HOSTS}:
            return self._send(403, {"error": "origin not allowed"})
        ctype = self.headers.get("Content-Type", "")
        if self.path == "/api/upload":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > uploads.MAX_BYTES:
                    return self._send(413, {"error": "image too large"})
                return self._send(200, {"path": uploads.save(self.rfile.read(length), ctype)})
            except ValueError as e:
                return self._send(400, {"error": str(e)})
        if not ctype.startswith("application/json"):
            return self._send(415, {"error": "json only"})
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 64 * 1024)
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/answer":
                # One call answers one question: tsod records the answer and delivers it to the line that holds the name.
                result = tsoq.answer(body.get("id", ""), option=str(body.get("option") or ""), text=str(body.get("text") or "").strip())
                deliver.log(REPLIES, {"stream": body.get("stream", ""), "kind": "answer", "ok": result["ok"],
                                      "text": f"{body.get('id')}: {body.get('option') or ''} {body.get('text') or ''}".strip()})
                _drop_snapshot()
                return self._send(200 if result["ok"] else 502, result)
            if self.path == "/api/terminal":
                stream = body.get("stream", "")
                repo = view.stream_repo(stream) if stream.replace("-", "").isalnum() else ""
                if not repo:
                    return self._send(400, {"error": "unknown stream"})
                key, text = body.get("key"), body.get("text")
                if text is not None and (not str(text).strip() or len(str(text)) > deliver.MAX_LEN):
                    return self._send(400, {"error": "пустой или слишком длинный текст"})
                result = terminal.answer(repo, key=str(key) if key else None, text=text)
                deliver.log(REPLIES, {"stream": stream, "kind": "terminal", "ok": True,
                                      "text": f"[терминал] {stream}: «{result['picked']}»" + (f" · {text}" if text else "")})
                return self._send(200, result)
            if self.path != "/api/reply":
                return self._send(404, {"error": "not found"})
            session_id, stream, text = body.get("session_id", ""), body.get("stream", ""), body.get("text", "")
            orch = next((o for o in view.build_orchestrators() if o["session_id"] and o["session_id"] == session_id), None)
            if not orch:
                return self._send(400, {"error": "unknown orchestrator session"})
            if stream and stream not in orch["streams"]:
                return self._send(400, {"error": "unknown stream"})
            images = uploads.checked(body.get("images") or [])
            kind = "note" if body.get("kind") == "note" else "reply"
            return self._send_record(deliver.send(session_id, stream, text, REPLIES, images, kind=kind, name=orch["name"]))
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception:
            return self._send(500, {"error": traceback.format_exc(limit=5)})


def main():
    uploads.prune()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    # Warm the Linear/GitHub cache so the first page load does not wait on the CLIs.
    threading.Thread(target=view.build, daemon=True).start()
    print(f"orchestrator-desk on http://{HOST}:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
