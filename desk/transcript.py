"""The orchestrator's turn-ending status line and its owner items, read from its Claude Code transcript."""

import glob
import json
import os
import re
import threading

TAIL_BYTES = 16 * 1024 * 1024
STATUS_RE = re.compile(r"(?:^|\s·\s)(?:waiting|in flight|closed):")
OWNER_RE = re.compile(r"^(?:ты|you|owner|владелец)\s*(?:—|-|:)\s*", re.IGNORECASE)
STREAM_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
PR_RE = re.compile(r"#(\d+)\b")


def find_transcript(registry, projects=os.path.expanduser("~/.claude/projects")):
    path = registry.get("transcript", "")
    if path and os.path.isfile(path):
        return path
    sid = registry.get("session_id", "")
    if not sid:
        return ""
    hits = glob.glob(os.path.join(projects, "*", f"{sid}.jsonl"))
    return max(hits, key=os.path.getmtime) if hits else ""


def _tail_lines(path, size=TAIL_BYTES):
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        f.seek(max(0, end - size))
        data = f.read()
    lines = data.split(b"\n")
    if end > size:
        lines = lines[1:]
    return lines


def _texts(entry):
    if entry.get("type") != "assistant":
        return []
    content = (entry.get("message") or {}).get("content") or []
    if isinstance(content, str):
        return [content]
    return [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]


_memo = {}
_memo_lock = threading.Lock()


def recent_messages(path, limit=400):
    """Assistant texts, newest first, as (text, timestamp); reparsed only when the file changes."""
    st = os.stat(path)
    key = (path, st.st_size, st.st_mtime_ns, limit)
    with _memo_lock:
        if _memo.get("key") == key:
            return _memo["value"]
    out = []
    for raw in reversed(_tail_lines(path)):
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        for text in reversed(_texts(entry)):
            if text.strip():
                out.append((text.strip(), entry.get("timestamp", "")))
        if len(out) >= limit:
            break
    with _memo_lock:
        _memo.update(key=key, value=out)
    return out


def status_of(text):
    for line in reversed(text.splitlines()):
        if STATUS_RE.search(line):
            return line.strip()
    return ""


def last_status(messages):
    """(message text, status line, timestamp) of the newest text ending a turn."""
    for text, ts in messages:
        line = status_of(text)
        if line:
            return text, line, ts
    return "", "", ""


def owner_items(status_line):
    items = []
    for seg in status_line.split(" · "):
        seg = seg.strip()
        if not seg.startswith("waiting:"):
            continue
        rest = seg[len("waiting:"):].strip()
        m = OWNER_RE.match(rest)
        if not m:
            continue
        # The closing clock ("; 14:08") is part of the line's format, not an ask.
        items += [i.strip() for i in rest[m.end():].split(";") if i.strip() and not re.fullmatch(r"\d{1,2}:\d{2}", i.strip())]
    return items


def refs(text):
    return set(STREAM_RE.findall(text)), set(PR_RE.findall(text))


WORD_RE = re.compile(r"[\w-]{4,}", re.UNICODE)


def stems(text):
    # Six-letter stems absorb Russian inflection: «пакетное» and «пакетного» meet.
    return {w.lower()[:6] for w in WORD_RE.findall(text or "") if not w.isdigit()}


def similar(a, b, share=0.5):
    sa, sb = stems(a), stems(b)
    return bool(sa and sb) and len(sa & sb) / min(len(sa), len(sb)) >= share
