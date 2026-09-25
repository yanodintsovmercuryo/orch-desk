#!/usr/bin/env python3
"""orchestrator-desk: a local page over team-skills-orchestrator state."""

import json
import mimetypes
import re
import os
import sys
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from desk import advisor, asks, deliver, links, shots, source, state, supervisor, terminal, uploads, view, workspace

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
                data = view.build()
                data["replies"] = deliver.recent(REPLIES, 20)
                data["ui_version"] = int(os.path.getmtime(os.path.join(WEB, "index.html")))
                data["desk_asks"] = asks.visible()
                data["supervisor"] = {"config": supervisor.config(), "events": supervisor.events(12)}
                return self._send(200, data)
            except Exception:
                return self._send(500, {"error": traceback.format_exc(limit=5)})
        if route == "/api/advisor":
            query = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            stream = (query.get("stream") or [""])[0]
            if not stream.replace("-", "").isalnum():
                return self._send(400, {"error": "unknown stream"})
            return self._send(200, advisor.status(stream))
        if route == "/api/source":
            query = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            try:
                return self._send(200, source.read((query.get("path") or [""])[0], (query.get("line") or ["0"])[0]))
            except ValueError as e:
                return self._send(404, {"error": str(e)})
        if route in ("/api/shots", "/api/file"):
            query = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            path = (query.get("path") or [""])[0]
            try:
                if route == "/api/shots":
                    return self._send(200, shots.listing(path))
                data, ctype = shots.image(path)
                return self._send(200, data, ctype)
            except ValueError as e:
                return self._send(404, {"error": str(e)})
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
            if self.path in ("/api/workspace/refresh", "/api/workspace/up"):
                stream = body.get("stream", "")
                repo = view.stream_repo(stream) if stream.replace("-", "").isalnum() else ""
                if not repo:
                    return self._send(400, {"error": "unknown stream"})
                if self.path.endswith("/up"):
                    workspace.app_up(repo, stream)
                return self._send(200, workspace.status(repo, force=self.path.endswith("/refresh")))
            if self.path in ("/api/advisor", "/api/advisor/reset"):
                stream = body.get("stream", "")
                row = next((r for r in view.build()["streams"] if r["id"] == stream), None)
                if not row:
                    return self._send(400, {"error": "unknown stream"})
                if self.path.endswith("/reset"):
                    return self._send(200, advisor.reset(stream))
                sdir = os.path.join(state.state_root(), row["orchestrator"], "streams", stream)
                return self._send(200, advisor.ask(stream, body.get("question", ""), body.get("model", "sonnet"),
                                                   row, sdir, fresh=bool(body.get("fresh"))))
            if self.path == "/api/supervisor/config":
                return self._send(200, supervisor.set_config(body))
            if self.path == "/api/asks/answer":
                ask = asks.answer(body.get("id", ""), body.get("text", ""), body.get("option") or None,
                                  uploads.checked(body.get("images") or []))
                return self._send(200, {"ask": ask, "nudge": _nudge_answer(ask)})
            if self.path == "/api/zed":
                return self._send(200, source.open_in_zed(body.get("path", ""), int(body.get("line") or 0)))
            if self.path == "/api/reveal":
                return self._send(200, shots.reveal(body.get("path", "")))
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
            if kind == "note":
                return self._send_record(deliver.send(session_id, stream, text, REPLIES, images, kind="note"))
            named = [a for a in (body.get("asks") or []) if isinstance(a, str)][:5]
            if stream and not named:
                named = view.current_asks(orch["name"], stream)
            if not stream and not named:
                # An older page sends no question; with one open question there is no ambiguity.
                general = view.general_asks(orch["name"])
                if len(general) != 1:
                    return self._send(400, {"error": "не понятно, на какой вопрос ответ — обнови страницу"})
                named = general
            return self._send_record(deliver.send(session_id, stream, text, REPLIES, images, named))
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception:
            return self._send(500, {"error": traceback.format_exc(limit=5)})


def _nudge_answer(ask):
    """The answer lives in the record; the orchestrator only gets a pointer to read it."""
    orch = next((o for o in view.build_orchestrators() if o["name"] == ask.get("orchestrator")), None) \
        or next(iter(view.build_orchestrators()), None)
    if not orch or not orch["session_id"]:
        return {"ok": False, "error": "оркестратор не найден — ответ сохранён, надсмотрщик напомнит"}
    text = (f"[desk] ответ владельца на {ask['id']}" + (f" ({ask['task']})" if ask.get("task") else "")
            + ": прочитай `desk answers` и действуй по нему; после выполнения `desk done " + ask["id"] + "`.")
    try:
        record = deliver.poke(orch["session_id"], text)
    except Exception as e:
        record = {"ok": False, "error": str(e)[:300]}
    deliver.log(REPLIES, {"stream": ask.get("task") or "", "kind": "answer", "ok": record.get("ok"),
                          "text": f"{ask['id']}: " + (ask["answer"].get("option") or "") + " " + ask["answer"].get("text", "")})
    return record


def main():
    uploads.prune()
    supervisor.start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    # Warm the Linear/GitHub cache so the first page load does not wait on the CLIs.
    threading.Thread(target=view.build, daemon=True).start()
    print(f"orchestrator-desk on http://{HOST}:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
