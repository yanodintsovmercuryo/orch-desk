"""Linear and GitHub links for a stream, through the local CLIs, cached."""

import json
import os
import re
import subprocess
import threading
import time

TTL = 300
LINEAR_WORKSPACE = "mercuryo"
_cache = {}
_pending = set()
_lock = threading.Lock()
_slots = threading.Semaphore(6)
CACHE_FILE = os.path.join(os.path.expanduser("~/.local/state/desk"), "links-cache.json")


def _load_cache():
    """Stale links beat blank ones after a restart; every entry refreshes in the background anyway."""
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            for row in json.load(f):
                _cache[tuple(row["key"])] = (row["ts"], row["value"])
    except (OSError, ValueError, KeyError, TypeError):
        pass


def _save_cache():
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([{"key": list(k), "ts": v[0], "value": v[1]} for k, v in _cache.items()
                       if not (v[1] or {}).get("error")], f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass


_load_cache()


def _run(args, timeout=15):
    res = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip()[:300])
    return res.stdout


def _refresh(key, fn):
    with _slots:
        try:
            value = fn()
        except Exception as e:  # a CLI failure degrades to a bare link, never breaks the page
            value = {"error": str(e)[:300]}
    with _lock:
        _cache[key] = (time.time(), value)
        _pending.discard(key)
        if not _pending:
            _save_cache()


def cached(key, fn):
    """Stale-while-revalidate: never waits on a CLI; a miss returns {} until the fetch lands."""
    with _lock:
        hit = _cache.get(key)
        fresh = hit and time.time() - hit[0] < TTL
        if not fresh and key not in _pending:
            _pending.add(key)
            threading.Thread(target=_refresh, args=(key, fn), daemon=True).start()
        return dict(hit[1]) if hit else {}


def linear(key):
    def fetch():
        out = json.loads(_run(["linear", "issues", "read", key, "--compact",
                               "--fields", "identifier,title,url,state.name"]))
        return {"url": out.get("url"), "title": out.get("title"), "state": (out.get("state") or {}).get("name")}

    info = cached(("linear", key), fetch)
    info.setdefault("url", f"https://linear.app/{LINEAR_WORKSPACE}/issue/{key}")
    return info


def repo_slug(repository):
    """owner/name from the worktree's origin, else from an Orca path …/workspaces/<repo>/<branch>."""
    def fetch():
        url = _run(["git", "-C", repository, "remote", "get-url", "origin"]).strip()
        m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
        if not m:
            raise RuntimeError(f"not a GitHub remote: {url}")
        return {"slug": m.group(1)}

    info = cached(("slug", repository), fetch)
    if info.get("slug"):
        return info["slug"]
    m = re.search(r"/workspaces/([^/]+)/[^/]+/?$", repository or "")
    return f"MercuryoPro/{m.group(1)}" if m else ""


def pull_request(repository, branch, journal_prs):
    slug = repo_slug(repository)
    if not slug:
        return {}

    def fetch():
        rows = json.loads(_run(["gh", "pr", "list", "-R", slug, "--head", branch, "--state", "all",
                                "--json", "number,url,state,title", "--limit", "5"]))
        if not rows:
            raise RuntimeError("no PR for the branch")
        return rows[0]

    info = cached(("pr", slug, branch), fetch) if branch else {}
    if not info.get("url") and journal_prs:
        n = max(journal_prs, key=int)
        info = {"number": int(n), "url": f"https://github.com/{slug}/pull/{n}", **info}
    return info
