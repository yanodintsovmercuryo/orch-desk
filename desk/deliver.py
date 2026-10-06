"""Deliver the owner's reply to the orchestrator: through tso, and into its Orca terminal only when tso has no line for it."""

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime

from . import tsoq

HANDLE_RE = re.compile(r"\bORCA_TERMINAL_HANDLE=(term_[0-9a-f-]+)")
MAX_LEN = 4000
_log_lock = threading.Lock()


def _run(args, timeout=20):
    res = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f"{args[0]} {args[1]}: {(res.stderr or res.stdout).strip()[:300]}")
    return res.stdout


_agents = {"ts": 0.0, "rows": [], "pending": False}
_agents_lock = threading.Lock()


def _refresh_agents():
    try:
        rows = json.loads(_run(["claude", "agents", "--json"]))
    except Exception:
        rows = None
    with _agents_lock:
        if rows is not None:
            _agents.update(ts=time.time(), rows=rows)
        _agents["pending"] = False


def agents(max_age=2.0):
    """`claude agents --json` takes seconds; a page reads the last answer and a refresh runs behind it.

    max_age=0 waits for a fresh answer (a send must not target a dead session)."""
    with _agents_lock:
        fresh = time.time() - _agents["ts"] < max_age
        have = bool(_agents["ts"])
        if not fresh and not _agents["pending"]:
            _agents["pending"] = True
            if have and max_age > 0:
                threading.Thread(target=_refresh_agents, daemon=True).start()
                return _agents["rows"]
        elif fresh or (have and max_age > 0):
            return _agents["rows"]
    _refresh_agents()
    with _agents_lock:
        return _agents["rows"]


def session_status(session_id):
    try:
        return next((a.get("status", "") for a in agents() if a.get("sessionId") == session_id), "gone")
    except Exception:
        return ""


def session_process(session_id):
    for agent in agents(max_age=0):
        if agent.get("sessionId") == session_id:
            return agent
    raise RuntimeError(f"session {session_id} is not running")


def terminal_handle(pid):
    # The Orca pane handle lives only in the agent process environment.
    m = HANDLE_RE.search(_run(["ps", "eww", "-o", "command=", "-p", str(pid)]))
    if not m:
        raise RuntimeError(f"process {pid} carries no ORCA_TERMINAL_HANDLE")
    return m.group(1)


def live_terminal(handle):
    out = json.loads(_run(["orca", "terminal", "list", "--json"]))
    for t in (out.get("result") or {}).get("terminals", []):
        if t.get("handle") == handle:
            if not (t.get("connected") and t.get("writable")):
                raise RuntimeError(f"terminal {handle} is not connected and writable")
            return t
    raise RuntimeError(f"terminal {handle} is not live in Orca")


def resolve(session_id):
    agent = session_process(session_id)
    handle = terminal_handle(agent["pid"])
    term = live_terminal(handle)
    return {"handle": handle, "pid": agent["pid"], "status": agent.get("status"), "title": term.get("title")}


def _one_line(text):
    return " / ".join(l.strip() for l in str(text).strip().splitlines() if l.strip())


def format_reply(stream, text, images=(), asks=(), kind="reply"):
    # One line: a newline typed into the agent prompt would submit early. The task and the
    # question it answers always travel with the reply, so a bare "Принимаю" is never ambiguous.
    body = _one_line(text)
    if images:
        body = (body + " · " if body else "") + "картинки: " + " ".join(images)
    about = "; ".join(f"«{_one_line(a)[:300]}»" for a in asks if _one_line(a))
    head = "[desk]" + (f" {stream}" if stream else "")
    if kind == "note":
        return f"{head} · комментарий: {body}"
    if about:
        head += (" · на " if stream else " на ") + about
    return f"{head}: {body}"


def _terminal_send(session_id, line):
    target = resolve(session_id)
    out = _run(["orca", "terminal", "send", "--terminal", target["handle"], "--text", line,
                "--enter", "--wait-submit", "5", "--json"], timeout=30)
    receipt = json.loads(out)
    sent = ((receipt.get("result") or {}).get("send") or {})
    return {"target": target, "receipt": receipt, "ok": bool(receipt.get("ok") and sent.get("accepted")),
            "stages": (sent.get("prompt") or {}).get("stages", [])}


def _deliver(session_id, name, line):
    """tso first (attested, wakes a sleeping line); the terminal only when nobody holds the name under tso."""
    if name:
        via = tsoq.send(name, line)
        if via["ok"] or via["code"] != 5:
            return {"via": "tso", "ok": via["ok"], "tso": via, **({} if via["ok"] else {"error": via["message"]})}
    out = _terminal_send(session_id, line)
    return {"via": "terminal", **out}


def send(session_id, stream, text, log_path, images=(), asks=(), kind="reply", name=None):
    if not text.strip() and not images:
        raise ValueError("пустой ответ")
    if kind == "note" and not stream:
        raise ValueError("комментарий должен относиться к задаче")
    if kind != "note" and not stream and not any(_one_line(a) for a in asks):
        raise ValueError("ответ без задачи должен называть вопрос — обнови страницу")
    if len(text) > MAX_LEN:
        raise ValueError(f"ответ длиннее {MAX_LEN} символов")
    line = format_reply(stream, text, images, asks, kind)
    record = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "stream": stream, "text": line,
              "images": list(images), "kind": kind, "asks": [_one_line(a) for a in asks]}
    try:
        record.update(_deliver(session_id, name, line))
    except Exception as e:
        record["ok"], record["error"] = False, str(e)[:500]
    with _log_lock, open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def poke(session_id, text, name=None):
    """One line to the orchestrator; the receipt says how it went."""
    if os.environ.get("DESK_NO_POKE"):
        return {"ok": False, "error": "DESK_NO_POKE: test instance, nothing sent", "text": text}
    out = _deliver(session_id, name, _one_line(text))
    return {k: out[k] for k in ("via", "ok", "stages", "tso") if k in out}


def log(log_path, record):
    record = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), **record}
    with _log_lock, open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def recent(log_path, limit=50):
    if not os.path.exists(log_path):
        return []
    with open(log_path, encoding="utf-8") as f:
        rows = [json.loads(l) for l in f if l.strip()]
    return rows[-limit:]
