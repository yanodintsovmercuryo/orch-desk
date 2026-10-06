"""A question about a task, asked in a new Orca window of the task's own worktree (a fresh read-only Claude session)."""

import json
import os
import shlex
import subprocess
import time

DIR = os.path.join(os.environ.get("DESK_ROOT") or os.path.expanduser("~/.local/state/desk"), "questions")
MAX_LEN = 4000


def prompt_text(stream, repo, text):
    return (f"Вопрос владельца по задаче {stream}. Рабочее дерево этой сессии — её worktree ({repo}); ничего не меняй, только читай код, "
            f"журнал и состояние и отвечай по существу.\n\n{text.strip()}")


def command(file_path):
    """The shell line typed into the new terminal: plan mode so the session cannot edit, the question from a file."""
    return f"claude --permission-mode plan \"$(cat {shlex.quote(file_path)})\""


def open_window(stream, repo, text, run=subprocess.run):
    if not text.strip() or len(text) > MAX_LEN:
        raise ValueError("пустой или слишком длинный вопрос")
    if not repo or not os.path.isdir(repo):
        raise ValueError("у задачи нет рабочего дерева")
    os.makedirs(DIR, exist_ok=True)
    path = os.path.join(DIR, f"{stream}-{int(time.time())}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(prompt_text(stream, repo, text))
    res = run(["orca", "terminal", "create", "--worktree", f"path:{repo}", "--title", f"{stream} · вопрос",
               "--command", command(path), "--focus", "--json"], capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip()[:300])
    return {"ok": True, "result": json.loads(res.stdout or "{}").get("result", {})}
