"""Deliver the owner's reply to the orchestrator's Orca terminal."""

import json
import os
import re
import subprocess
import threading
import time

HANDLE_RE = re.compile(r"\bORCA_TERMINAL_HANDLE=(term_[0-9a-f-]+)")
MAX_LEN = 4000
_log_lock = threading.Lock()


def _run(args, timeout=20):
    res = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f"{args[0]} {args[1]}: {(res.stderr or res.stdout).strip()[:300]}")
    return res.stdout


def session_process(session_id):
    for agent in json.loads(_run(["claude", "agents", "--json"])):
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


def format_reply(stream, text, images=()):
    # One line: a newline typed into the agent prompt would submit early.
    body = " / ".join(l.strip() for l in text.strip().splitlines() if l.strip())
    if images:
        body = (body + " · " if body else "") + "картинки: " + " ".join(images)
    prefix = f"[desk] {stream}: " if stream else "[desk] "
    return prefix + body


def send(session_id, stream, text, log_path, images=()):
    if not text.strip() and not images:
        raise ValueError("empty reply")
    if len(text) > MAX_LEN:
        raise ValueError(f"reply longer than {MAX_LEN} characters")
    line = format_reply(stream, text, images)
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "stream": stream, "text": line, "images": list(images)}
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
