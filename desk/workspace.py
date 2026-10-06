"""A stream's local contour: `task workspace:info` and `task app:up` in its worktree."""

import json
import os
import subprocess
import threading
import time

TTL = 30
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
_cache = {}
_pending = set()
_starting = {}
_lock = threading.Lock()


def has_contour(repo):
    """Only repositories whose Taskfile declares workspace:info have a contour."""
    path = os.path.join(repo or "", "Taskfile.yml")
    if not os.path.isfile(path):
        return False
    with open(path, encoding="utf-8") as f:
        return "workspace:info:" in f.read()


def _fetch(repo):
    res = subprocess.run(["task", "--silent", "-d", repo, "workspace:info", "FORMAT=json"],
                         capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        return {"error": (res.stderr or res.stdout).strip()[-400:]}
    try:
        return {"info": json.loads(res.stdout)}
    except ValueError:
        return {"error": "workspace:info returned no JSON: " + res.stdout.strip()[-200:]}


def _refresh(repo):
    value = _fetch(repo)
    with _lock:
        _cache[repo] = (time.time(), value)
        _pending.discard(repo)


def _starting_state(repo):
    proc, log, started = _starting.get(repo, (None, "", 0))
    if not proc:
        return None
    code = proc.poll()
    return {"running": code is None, "exit_code": code, "log": log, "started": started}


def status(repo, force=False):
    """The cached info; a stale or missing entry refreshes in the background, force waits for it."""
    if not has_contour(repo):
        return {"available": False}
    if force:
        _refresh(repo)
    with _lock:
        hit = _cache.get(repo)
        if not hit or time.time() - hit[0] >= TTL:
            if repo not in _pending:
                _pending.add(repo)
                threading.Thread(target=_refresh, args=(repo,), daemon=True).start()
        out = {"available": True, "loading": not hit, **(dict(hit[1]) if hit else {})}
        if hit:
            out["checked"] = hit[0]
        start = _starting_state(repo)
    if start:
        out["app_up"] = start
    return out


USER_ENV_NU = os.path.expanduser("~/Library/Application Support/nushell/env.nu")


def _app_up_command(repo):
    # app:up needs the owner's GITHUB_TOKEN, which lives only in the nushell env; run
    # through it instead of copying the secret into the launchd agent.
    if os.path.isfile(USER_ENV_NU):
        return ["nu", "--env-config", USER_ENV_NU, "-c", "^task -d $env.DESK_REPO app:up"]
    return ["task", "-d", repo, "app:up"]


def app_up(repo, stream):
    """Starts `task app:up` detached; a second click while one runs is refused."""
    if not has_contour(repo):
        raise ValueError("this worktree has no local contour")
    with _lock:
        cur = _starting_state(repo)
        if cur and cur["running"]:
            raise ValueError("app:up is already running")
        os.makedirs(LOG_DIR, exist_ok=True)
        log = os.path.join(LOG_DIR, f"{stream}-app-up.log")
        with open(log, "w", encoding="utf-8") as f:
            proc = subprocess.Popen(_app_up_command(repo), stdout=f, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True,
                                    env={**os.environ, "DESK_REPO": repo})
        _starting[repo] = (proc, log, time.time())
        _cache.pop(repo, None)

    def watch():
        proc.wait()
        _refresh(repo)

    threading.Thread(target=watch, daemon=True).start()
    return _starting_state(repo)


def log_tail(path, lines=30):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except OSError:
        return ""
