"""Linear and GitHub links for a stream, through the local CLIs, cached."""

import json
import re
import subprocess
import threading
import time

TTL = 300
LINEAR_WORKSPACE = "mercuryo"
_cache = {}
_lock = threading.Lock()


def _run(args, timeout=15):
    res = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip()[:300])
    return res.stdout


def cached(key, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < TTL:
            return hit[1]
    try:
        value = fn()
    except Exception as e:  # a CLI failure degrades to a bare link, never breaks the page
        value = {"error": str(e)[:300]}
    with _lock:
        _cache[key] = (now, value)
    return value


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
