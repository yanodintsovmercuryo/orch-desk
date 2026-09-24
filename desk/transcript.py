"""The orchestrator's asks to the owner, read from its Claude Code transcript."""

import glob
import json
import os
import re

TAIL_BYTES = 4 * 1024 * 1024
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


def recent_messages(path, limit=80):
    """Assistant texts, newest first, as (text, timestamp)."""
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
                    return out
    return out


def last_status(messages):
    """(message text, status line, timestamp) of the newest text ending a turn."""
    for text, ts in messages:
        for line in reversed(text.splitlines()):
            if STATUS_RE.search(line):
                return text, line.strip(), ts
    return "", "", ""


def request_for(messages, stream_id, pr_numbers, limit=3):
    """Paragraphs about the stream from the newest messages naming it, newest first."""
    out = []
    for text, ts in messages:
        paras = paragraphs_about(text, stream_id, pr_numbers)
        if paras:
            out.append({"ts": ts, "paragraphs": paras})
            if len(out) >= limit:
                break
    return out


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
        items += [i.strip() for i in rest[m.end():].split(";") if i.strip()]
    return items


def refs(text):
    return set(STREAM_RE.findall(text)), set(PR_RE.findall(text))


def paragraphs_about(message, stream_id, pr_numbers):
    """Paragraphs of the message naming the stream or one of its PRs, status line excluded."""
    pr_res = [re.compile(rf"#{n}\b") for n in pr_numbers]
    out = []
    for para in re.split(r"\n\s*\n", message):
        para = "\n".join(l for l in para.strip().splitlines() if not STATUS_RE.search(l)).strip()
        if not para:
            continue
        about = lambda s: re.search(rf"\b{re.escape(stream_id)}\b", s) or any(r.search(s) for r in pr_res)
        if not about(para):
            continue
        lines = para.splitlines()
        if len(lines) > 2 and all(l.lstrip().startswith("|") for l in lines):
            para = "\n".join(lines[:2] + [l for l in lines[2:] if about(l)])
        out.append(para)
    return out
