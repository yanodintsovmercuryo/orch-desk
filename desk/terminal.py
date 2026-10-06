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


HELD_HEAD = "Held message from another session"
DASH_RULE = re.compile(r"^\s*[╌┄┈]{8,}")


def parse_held(lines):
    """The dialog that holds a cross-session message: who sent it, what it says, and the two choices (arrow keys, no digits)."""
    head = next((i for i, l in enumerate(lines) if HELD_HEAD in l), None)
    if head is None:
        return None
    block = lines[head:]
    sender = next((m.group(1) for l in block for m in [re.search(r"peer claims name: ([^)]+)\)", l)] if m), "")
    rules = [i for i, l in enumerate(block) if DASH_RULE.match(l)]
    body, truncated = [], False
    if len(rules) >= 2:
        for l in block[rules[0] + 1:rules[1]]:
            text = l.strip().lstrip("│").strip()
            if re.match(r"…?\[\d+ lines?, \d+ chars total", text):
                truncated = True
            elif text:
                body.append(text)
    options, selected = [], 0
    for l in block[(rules[-1] + 1 if rules else 1):]:
        if not l.strip() or RULE_RE.match(l) or l.strip().startswith(("Esc", "Enter")):
            continue
        if l.lstrip().startswith("❯"):
            selected = len(options)
        label = l.strip().lstrip("❯").strip()
        if label.startswith("Deny"):
            label, hint = "Не доставлять", "Сессия не получит сообщение, отправителю ответят, что оно отклонено."
        elif label.startswith("Deliver"):
            label, hint = "Доставить сообщение", "Сессия получит сообщение целиком, включая скрытую часть."
        else:
            hint = ""
        options.append({"key": str(len(options) + 1), "label": label, "hint": hint})
    return {"kind": "held", "title": "Сообщение от другой сессии ждёт разрешения", "sender": sender, "body": body,
            "truncated": truncated, "options": options, "selected": selected, "question": ["Сообщение от другой сессии ждёт разрешения"]}


def parse_prompt(lines):
    """The prompt block below the last rule of the screen: its question lines and numbered options."""
    held = parse_held(lines)
    if held:
        return held
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
    question = [q for q in question if q]
    return {"kind": "choice", "title": question[0] if question else "", "body": question[1:], "question": question, "options": options}


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
    if prompt.get("kind") == "held":
        # No digits in this dialog: move the cursor with the arrow keys, then confirm.
        delta = int(key) - 1 - prompt["selected"]
        for _ in range(abs(delta)):
            _run(["orca", "terminal", "send", "--terminal", handle, "--text", "\x1b[B" if delta > 0 else "\x1b[A", "--json"])
            time.sleep(0.2)
        _run(["orca", "terminal", "send", "--terminal", handle, "--text", "", "--enter", "--json"])
        with _lock:
            _cache.clear()
        return {"ok": True, "picked": keys[key], "text": None}
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
