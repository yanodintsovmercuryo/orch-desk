"""The orchestrator's asks to the owner, read from its Claude Code transcript."""

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


def strip_status(text):
    return "\n".join(l for l in text.splitlines() if not STATUS_RE.search(l)).strip()


def last_status(messages):
    """(message text, status line, timestamp) of the newest text ending a turn."""
    for text, ts in messages:
        line = status_of(text)
        if line:
            return text, line, ts
    return "", "", ""


def ask_thread(messages, matches, about=None, updates_limit=3):
    """Where an owner ask began and what was said about it since.

    Walks turn-ending messages back from the newest while their waiting line still
    carries a matching item; the oldest of that run is where the question was put.
    """
    origin, run, misses = None, [], 0
    for text, ts in messages:
        line = status_of(text)
        if not line:
            continue
        if not any(matches(i) for i in owner_items(line)):
            # One turn may fold the ask into "…and the questions above"; two in a row end the run.
            misses += 1
            if misses > 1:
                break
            continue
        misses = 0
        origin = (text, ts)
        run.append((text, ts))
    if not origin:
        return {}
    updates = []
    if about:
        # The run may start in a turn that only carried the ask forward; the question
        # itself is in the oldest turn of the run that talks about the stream.
        relevant = [m for m in run if about(m[0])]
        if relevant:
            origin = relevant[-1]
            run = run[:run.index(origin) + 1]
        for text, ts in run[:-1]:
            paras = about(text)
            if paras:
                updates.append({"ts": ts, "paragraphs": paras})
                if len(updates) >= updates_limit:
                    break
    return {"origin": {"ts": origin[1], "text": strip_status(origin[0])}, "updates": updates}


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


WORD_RE = re.compile(r"[\w-]{4,}", re.UNICODE)


def stems(text):
    # Six-letter stems absorb Russian inflection: «пакетное» and «пакетного» meet.
    return {w.lower()[:6] for w in WORD_RE.findall(text or "") if not w.isdigit()}


def similar(a, b, share=0.5):
    sa, sb = stems(a), stems(b)
    return bool(sa and sb) and len(sa & sb) / min(len(sa), len(sb)) >= share


def paragraphs_matching(message, item):
    """The sections of the message about an ask: a matching paragraph and what follows it up to the
    next bold heading, so a question keeps its options and recommendation."""
    want = stems(item)
    paras = []
    for para in re.split(r"\n\s*\n", message):
        para = "\n".join(l for l in para.strip().splitlines() if not STATUS_RE.search(l)).strip()
        if para:
            paras.append(para)
    out, taken = [], set()
    for i, para in enumerate(paras):
        if i in taken or len(stems(para) & want) < min(2, len(want)):
            continue
        j = i
        while True:
            taken.add(j)
            out.append(paras[j])
            j += 1
            if j >= len(paras) or paras[j].startswith("**"):
                break
    return out


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
