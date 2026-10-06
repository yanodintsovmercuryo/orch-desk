"""A read-only advisor per task: a headless Claude session in the task's worktree, one thread each."""

import json
import os
import subprocess
import threading
import time
from datetime import datetime


DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "advisor")
MODELS = {"sonnet": "sonnet", "opus": "opus"}
READ_GIT = ["log", "diff", "show", "status", "branch", "rev-parse", "blame", "ls-files", "merge-base"]
ALLOWED = ["Read", "Grep", "Glob"] + [f"Bash(git {c}:*)" for c in READ_GIT]
RULES = """Ты — советчик владельца по одной задаче. Владелец задаёт вопросы, чтобы понять задачу, решение или вопрос оркестратора.
Правила этой сессии:
- Только чтение. Не меняй файлы, не коммить, не запускай тесты и сборки, не пиши другим сессиям.
- Рабочий каталог уже worktree задачи: запускай git без -C (это исключение из общего правила про git -C); разрешены только читающие команды git.
- Отвечай по-русски, коротко и по делу; сначала ответ, потом обоснование.
- Файлы — полным абсолютным путём со строкой (/полный/путь/file.go:42), PR и задачи — ссылками.
- Если чего-то не знаешь или это решение владельца/оркестратора — так и скажи, не выдумывай."""
_runs = {}
_lock = threading.Lock()


def _path(stream):
    return os.path.join(DIR, f"{stream}.json")


def load(stream):
    try:
        with open(_path(stream), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"session_id": "", "cwd": "", "messages": []}


def _save(stream, data):
    os.makedirs(DIR, exist_ok=True)
    tmp = _path(stream) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path(stream))


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _git(cwd, *args):
    try:
        res = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=15)
        return res.stdout.strip() if res.returncode == 0 else ""
    except Exception:
        return ""


def workdir(header):
    """The task's worktree; for a closed task whose worktree is gone, the primary checkout."""
    repo = header.get("repository", "")
    if repo and os.path.isdir(repo):
        return repo
    name = os.path.basename(os.path.dirname(repo.rstrip("/"))) if "/workspaces/" in repo else ""
    main = os.path.expanduser(f"~/go/src/github.com/MercuryoPro/{name}") if name else ""
    return main if main and os.path.isdir(main) else os.path.expanduser("~/go/src/github.com/MercuryoPro")


def context(stream_row, stream_dir):
    """The first message's preamble: what the task is and where it stands."""
    h, lin, pr = stream_row["header"], stream_row["links"].get("linear") or {}, stream_row["links"].get("pr") or {}
    cwd = workdir(h)
    base = _git(cwd, "merge-base", "HEAD", "origin/main") or "origin/main"
    parts = [
        f"Задача {stream_row['id']}: {lin.get('title', '')} — {lin.get('url', '')} (статус в Linear: {lin.get('state', '?')})",
        f"Ветка: {h.get('branch', '?')}; worktree: {cwd}; PR: {pr.get('url', 'ещё не открыт')}",
        f"Бриф потока: {os.path.join(stream_dir, 'brief.md')}; карточка: {os.path.join(stream_dir, 'card.md')}; "
        f"журнал: {os.path.join(stream_dir, 'journal.md')}",
    ]
    events = stream_row.get("events", [])[-15:]
    if events:
        parts.append("Последние события журнала:\n" + "\n".join(
            f"- {e['ts']} {e.get('checkpoint') or 'note'} {e.get('direction', '')}: {e['text']}" for e in events))
    asks = stream_row.get("owner_asks") or []
    origin = (stream_row.get("ask") or {}).get("origin") or {}
    if asks:
        parts.append("Оркестратор сейчас спрашивает владельца: " + "; ".join(asks))
    if origin.get("text"):
        parts.append("Сообщение оркестратора с этим вопросом:\n" + origin["text"])
    log = _git(cwd, "log", "--oneline", f"{base}..HEAD", "-n", "30")
    stat = _git(cwd, "diff", "--stat", f"{base}...HEAD")
    if log:
        parts.append("Коммиты ветки:\n" + log)
    if stat:
        parts.append("Изменённые файлы:\n" + "\n".join(stat.splitlines()[-25:]))
    return cwd, "\n\n".join(parts)


def status(stream):
    data = load(stream)
    with _lock:
        run = _runs.get(stream)
        live = dict(run) if run else None
    return {"messages": data["messages"], "model": data.get("model", "sonnet"), "running": live}


def ask(stream, question, model, stream_row, stream_dir, fresh=False):
    question = (question or "").strip()
    if not question:
        raise ValueError("пустой вопрос")
    model = model if model in MODELS else "sonnet"
    with _lock:
        if stream in _runs and _runs[stream].get("state") == "running":
            raise ValueError("советчик ещё отвечает на прошлый вопрос")
        data = {"session_id": "", "cwd": "", "messages": []} if fresh else load(stream)
        if data["session_id"]:
            cwd, prompt = data["cwd"], question
        else:
            cwd, preamble = context(stream_row, stream_dir)
            prompt = preamble + "\n\nВопрос владельца:\n" + question
        data["messages"].append({"role": "user", "text": question, "ts": _now(), "model": model})
        data["model"], data["cwd"] = model, cwd
        _save(stream, data)
        _runs[stream] = {"state": "running", "text": "", "tools": [], "started": time.time()}
    threading.Thread(target=_run, args=(stream, cwd, prompt, model, data["session_id"]), daemon=True).start()
    return status(stream)


def _run(stream, cwd, prompt, model, session_id):
    args = ["claude", "-p", prompt, "--model", MODELS[model], "--output-format", "stream-json", "--verbose",
            "--include-partial-messages", "--tools", "Read,Grep,Glob,Bash", "--permission-mode", "dontAsk",
            "--allowedTools", *ALLOWED, "--append-system-prompt", RULES,
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
    if session_id:
        args += ["--resume", session_id]
    result, new_sid, error = "", session_id, ""
    try:
        proc = subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            with _lock:
                run = _runs.get(stream)
                if not run:
                    continue
                if ev.get("type") == "stream_event":
                    delta = ((ev.get("event") or {}).get("delta") or {})
                    if delta.get("type") == "text_delta":
                        run["text"] += delta.get("text", "")
                elif ev.get("type") == "assistant":
                    for c in (ev.get("message") or {}).get("content") or []:
                        if c.get("type") == "tool_use":
                            inp = c.get("input") or {}
                            what = inp.get("file_path") or inp.get("pattern") or inp.get("command") or ""
                            run["tools"].append(f"{c.get('name')}: {str(what)[:120]}")
                            run["text"] += "\n\n"
                elif ev.get("type") == "result":
                    result, new_sid = ev.get("result") or "", ev.get("session_id") or new_sid
                    if ev.get("is_error"):
                        error = result or "ошибка агента"
        proc.wait(timeout=600)
    except Exception as e:
        error = str(e)[:300]
    with _lock:
        run = _runs.get(stream) or {}
        data = load(stream)
        data["session_id"] = new_sid
        data["messages"].append({"role": "advisor", "text": result or run.get("text", "").strip() or error,
                                 "ts": _now(), "tools": run.get("tools", []), "error": error})
        _save(stream, data)
        _runs.pop(stream, None)


def reset(stream):
    with _lock:
        if stream in _runs:
            raise ValueError("советчик ещё отвечает")
        _save(stream, {"session_id": "", "cwd": "", "messages": []})
    return status(stream)
