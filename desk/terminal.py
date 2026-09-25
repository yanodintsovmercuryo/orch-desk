"""A stream session stopped on its own prompt: read its screen, answer through Orca."""

import json
import re
import subprocess
import threading
import time

from . import deliver

TTL = 2.0
_cache = {}
_lock = threading.Lock()
OPTION_RE = re.compile(r"^\s*(?:❯\s*)?(\d)\.\s+(.+?)\s*$")
RULE_RE = re.compile(r"^\s*[─━]{8,}")


def _run(args, timeout=20):
    res = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f"{args[0]} {args[1]}: {(res.stderr or res.stdout).strip()[:300]}")
    return res.stdout


def session_for(repository):
    """The live Claude session working in a stream's worktree."""
    if not repository:
        return None
    for a in deliver.agents():
        if a.get("cwd") == repository and a.get("kind") != "background":
            return a
    return None


def screen(pid):
    handle = deliver.terminal_handle(pid)
    out = json.loads(_run(["orca", "terminal", "read", "--terminal", handle, "--json"]))
    term = (out.get("result") or {}).get("terminal") or {}
    lines = term.get("tail") or term.get("lines") or term.get("text") or []
    if isinstance(lines, str):
        lines = lines.splitlines()
    return handle, [l if isinstance(l, str) else str(l) for l in lines]


def parse_prompt(lines):
    """The prompt block below the last rule of the screen: its question lines and numbered options."""
    rules = [i for i, l in enumerate(lines) if RULE_RE.match(l)]
    # The prompt sits between the last-but-one and last rules; the footer follows the last.
    start = rules[-3] + 1 if len(rules) >= 3 else (rules[0] + 1 if rules else 0)
    block = lines[start:]
    question, options = [], []
    for l in block:
        if RULE_RE.match(l):
            continue
        m = OPTION_RE.match(l)
        if m:
            options.append({"key": m.group(1), "label": m.group(2), "hint": ""})
        elif options and l.startswith("     ") and l.strip() and not l.strip().startswith(("Enter", "│")):
            options[-1]["hint"] = (options[-1]["hint"] + " " + l.strip()).strip()
        elif l.strip().startswith("│"):
            question.append(l.strip().lstrip("│").strip())
        elif l.strip().startswith("☐"):
            question.insert(0, l.strip().lstrip("☐").strip())
    return {"question": [q for q in question if q], "options": options}


def waiting_prompt(repository):
    """For a stream whose session is `waiting`: the parsed prompt and the raw screen, cached briefly."""
    agent = session_for(repository)
    if not agent or agent.get("status") != "waiting":
        return None
    key = (agent["sessionId"], agent.get("pid"))
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < TTL:
            return hit[1]
    try:
        handle, lines = screen(agent["pid"])
        value = {"session_id": agent["sessionId"], **parse_prompt(lines), "screen": "\n".join(lines[-40:])}
    except Exception as e:
        value = {"session_id": agent["sessionId"], "error": str(e)[:300], "question": [], "options": []}
    with _lock:
        _cache[key] = (time.time(), value)
    return value


def answer(repository, key=None, text=None):
    """Picks a numbered option, or picks the free-text option and types the text."""
    agent = session_for(repository)
    if not agent or agent.get("status") != "waiting":
        raise ValueError("сессия задачи сейчас не ждёт ввода")
    handle, lines = screen(agent["pid"])
    prompt = parse_prompt(lines)
    keys = {o["key"]: o["label"] for o in prompt["options"]}
    if text is not None:
        key = next((k for k, v in keys.items() if v.lower().startswith("type something")), None)
        if not key:
            raise ValueError("в этом вопросе нет варианта для своего текста")
    if key not in keys:
        raise ValueError("такого варианта на экране нет")
    _run(["orca", "terminal", "send", "--terminal", handle, "--text", key, "--json"])
    time.sleep(0.6)
    if text is not None:
        body = " / ".join(l.strip() for l in text.strip().splitlines() if l.strip())
        _run(["orca", "terminal", "send", "--terminal", handle, "--text", body, "--enter", "--json"])
    else:
        _, after = screen(agent["pid"])
        # A digit may only move the cursor; confirm when the same menu is still on screen.
        if [o["label"] for o in parse_prompt(after)["options"]] == [o["label"] for o in prompt["options"]]:
            _run(["orca", "terminal", "send", "--terminal", handle, "--text", "", "--enter", "--json"])
    with _lock:
        _cache.clear()
    return {"ok": True, "picked": keys[key], "text": text}
