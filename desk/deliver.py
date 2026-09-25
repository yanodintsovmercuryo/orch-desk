"""Deliver the owner's reply to the orchestrator's Orca terminal."""

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime

HANDLE_RE = re.compile(r"\bORCA_TERMINAL_HANDLE=(term_[0-9a-f-]+)")
MAX_LEN = 4000
_log_lock = threading.Lock()


def _run(args, timeout=20):
    res = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f"{args[0]} {args[1]}: {(res.stderr or res.stdout).strip()[:300]}")
    return res.stdout


_agents = {"ts": 0.0, "rows": []}
_agents_lock = threading.Lock()


def agents(max_age=2.0):
    """`claude agents --json`, reused for a couple of seconds across pollers."""
    with _agents_lock:
        if time.time() - _agents["ts"] < max_age:
            return _agents["rows"]
    rows = json.loads(_run(["claude", "agents", "--json"]))
    with _agents_lock:
        _agents.update(ts=time.time(), rows=rows)
    return rows


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


def format_reply(stream, text, images=(), asks=()):
    # One line: a newline typed into the agent prompt would submit early. The task and the
    # question it answers always travel with the reply, so a bare "Принимаю" is never ambiguous.
    body = _one_line(text)
    if images:
        body = (body + " · " if body else "") + "картинки: " + " ".join(images)
    about = "; ".join(f"«{_one_line(a)[:300]}»" for a in asks if _one_line(a))
    head = "[desk]" + (f" {stream}" if stream else "")
    if about:
        head += (" · на " if stream else " на ") + about
    return f"{head}: {body}"


def send(session_id, stream, text, log_path, images=(), asks=()):
    if not text.strip() and not images:
        raise ValueError("empty reply")
    if not stream and not any(_one_line(a) for a in asks):
        raise ValueError("a reply without a task must name the question it answers")
    if len(text) > MAX_LEN:
        raise ValueError(f"reply longer than {MAX_LEN} characters")
    line = format_reply(stream, text, images, asks)
    record = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "stream": stream, "text": line,
              "images": list(images)}
    try:
        target = resolve(session_id)
        record["target"] = target
        out = _run(["orca", "terminal", "send", "--terminal", target["handle"], "--text", line,
                    "--enter", "--wait-submit", "5", "--json"], timeout=30)
        record["receipt"] = json.loads(out)
        sent = ((record["receipt"].get("result") or {}).get("send") or {})
        record["ok"] = bool(record["receipt"].get("ok") and sent.get("accepted"))
        record["stages"] = (sent.get("prompt") or {}).get("stages", [])
    except Exception as e:
        record["ok"], record["error"] = False, str(e)[:500]
    with _log_lock, open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def recent(log_path, limit=50):
    if not os.path.exists(log_path):
        return []
    with open(log_path, encoding="utf-8") as f:
        rows = [json.loads(l) for l in f if l.strip()]
    return rows[-limit:]
