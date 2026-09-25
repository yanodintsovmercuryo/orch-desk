"""Owner asks as records: one JSON file per question, the single source the page, CLI and hook share.

Lifecycle: open -> answered (owner, on the page) -> consumed (orchestrator read it) -> done (acted on);
open -> withdrawn when the question no longer stands.
"""

import fcntl
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime

ROOT = os.path.expanduser(os.environ.get("DESK_HOME", "~/.local/state/desk"))
DIR = os.path.join(ROOT, "asks")
TASK_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
STATUSES = ("open", "answered", "consumed", "done", "withdrawn")


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


@contextmanager
def _locked():
    os.makedirs(DIR, exist_ok=True)
    with open(os.path.join(ROOT, ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _path(ask_id):
    if not re.fullmatch(r"ask-\d+", ask_id or ""):
        raise ValueError(f"не id вопроса: {ask_id!r}")
    return os.path.join(DIR, f"{ask_id}.json")


def _write(ask):
    tmp = _path(ask["id"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ask, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path(ask["id"]))


def get(ask_id):
    try:
        with open(_path(ask_id), encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        raise ValueError(f"вопроса {ask_id} нет")


def all_asks():
    if not os.path.isdir(DIR):
        return []
    out = []
    for name in os.listdir(DIR):
        if name.endswith(".json"):
            try:
                with open(os.path.join(DIR, name), encoding="utf-8") as f:
                    out.append(json.load(f))
            except (OSError, ValueError):
                continue
    return sorted(out, key=lambda a: int(a["id"].split("-")[1]))


def _next_id():
    nums = [int(n[4:-5]) for n in os.listdir(DIR) if re.fullmatch(r"ask-\d+\.json", n)] if os.path.isdir(DIR) else []
    return f"ask-{max(nums, default=0) + 1}"


def create(question, task=None, context="", options=(), recommend=None, why="", links=(), orchestrator=""):
    question = (question or "").strip()
    if not question:
        raise ValueError("нужен текст вопроса")
    if task and not TASK_RE.match(task):
        raise ValueError(f"задача должна быть вида MER-123, а не {task!r}")
    keys = [o["key"] for o in options]
    if recommend and recommend not in keys:
        raise ValueError(f"рекомендация {recommend!r} не среди вариантов {keys}")
    with _locked():
        ask = {"id": _next_id(), "created": now(), "orchestrator": orchestrator, "task": task or None,
               "question": question, "context": context.strip(), "options": list(options),
               "recommend": recommend, "why": why.strip(), "links": [l for l in links if l],
               "status": "open", "answer": None, "history": [{"ts": now(), "status": "open"}]}
        _write(ask)
    return ask


def _move(ask_id, allowed_from, status, **fields):
    with _locked():
        ask = get(ask_id)
        if ask["status"] not in allowed_from:
            raise ValueError(f"{ask_id} в статусе {ask['status']}, а нужно {'/'.join(allowed_from)}")
        ask.update(fields)
        ask["status"] = status
        ask["history"].append({"ts": now(), "status": status, **({"note": fields["note"]} if fields.get("note") else {})})
        _write(ask)
    return ask


def answer(ask_id, text="", option=None, images=()):
    text = (text or "").strip()
    ask = get(ask_id)
    if option and option not in [o["key"] for o in ask["options"]]:
        raise ValueError(f"варианта {option} в вопросе нет")
    if not text and not option:
        raise ValueError("пустой ответ")
    # A second answer before the orchestrator read the first replaces it.
    return _move(ask_id, ("open", "answered"), "answered",
                 answer={"ts": now(), "text": text, "option": option, "images": list(images)})


def consume(ask_id):
    return _move(ask_id, ("answered",), "consumed")


def done(ask_id, note=""):
    return _move(ask_id, ("answered", "consumed"), "done", note=note)


def withdraw(ask_id, note=""):
    return _move(ask_id, ("open", "answered"), "withdrawn", note=note)


def pending_answers():
    return [a for a in all_asks() if a["status"] == "answered"]


def render_answer(ask):
    """How an answer reads to the orchestrator: the question, the chosen option and the owner's words."""
    ans = ask.get("answer") or {}
    opt = next((o for o in ask["options"] if o["key"] == ans.get("option")), None)
    lines = [f"{ask['id']}" + (f" · {ask['task']}" if ask.get("task") else "") + f": {ask['question']}"]
    if opt:
        lines.append(f"Выбор владельца: {opt['key']} — {opt['label']}")
    if ans.get("text"):
        lines.append(f"Слова владельца: {ans['text']}")
    if ans.get("images"):
        lines.append("Картинки: " + " ".join(ans["images"]))
    lines.append(f"Ответ дан {ans.get('ts', '')} на сайте desk — это канал владельца, его слово.")
    return "\n".join(lines)


def visible(recent_minutes=15):
    """What the page shows: live asks, plus closed ones for a short while so the unblock is seen."""
    out = []
    cutoff = datetime.now().astimezone().timestamp() - recent_minutes * 60
    for a in all_asks():
        if a["status"] in ("open", "answered", "consumed"):
            out.append(a)
        elif datetime.fromisoformat(a["history"][-1]["ts"]).timestamp() >= cutoff:
            out.append(a)
    return out
