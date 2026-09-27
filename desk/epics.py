"""Epic progress from Linear: the parents of the streams' tracker cards, counted by their children.

The orchestrator knows nothing about epics; this is a reading of Linear, labelled as such.
Everything is stale-while-revalidate over the local `linear` CLI, so a page never waits on it.
"""

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime

from . import asks, links

PARENT_TTL = 3600  # a card's parent rarely changes
CHILDREN_TTL = 300
DEAD_STATES = ("Canceled", "Duplicate")
STARTED_STATES = ("In Progress", "In Review")
_cache = {}
_pending = set()
_lock = threading.Lock()
CACHE_FILE = os.path.join(asks.ROOT, "epics-cache.json")


def _load_cache():
    """Yesterday's answers are better than a blank panel after a restart; they refresh in the background."""
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            for k, (ts, value) in json.load(f).items():
                kind, key = k.split(":", 1)
                _cache[(kind, key)] = (ts, value)
    except (OSError, ValueError):
        pass


def _save_cache():
    try:
        os.makedirs(asks.ROOT, exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({f"{k[0]}:{k[1]}": v for k, v in _cache.items() if not (v[1] or {}).get("error")}, f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass


_load_cache()


def _run(args):
    res = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip()[:300])
    return json.loads(res.stdout or "{}")


def _cached(key, ttl, fn):
    with _lock:
        hit = _cache.get(key)
        fresh = hit and time.time() - hit[0] < ttl
        if not fresh and key not in _pending:
            _pending.add(key)
            threading.Thread(target=_refresh, args=(key, fn), daemon=True).start()
        return hit[1] if hit else None


def _refresh(key, fn):
    with links._slots:
        try:
            value = fn()
        except Exception as e:
            value = {"error": str(e)[:300]}
    with _lock:
        _cache[key] = (time.time(), value)
        _pending.discard(key)
        if not _pending:
            _save_cache()


def card(key):
    """identifier, title, url and parent of one card; None until fetched."""
    def fetch():
        out = _run(["linear", "issues", "read", key, "--compact",
                    "--fields", "identifier,title,url,parent.identifier,parent.title"])
        parent = out.get("parent") or {}
        return {"id": out.get("identifier", key), "title": out.get("title", ""), "url": out.get("url", ""),
                "parent": parent.get("identifier", "")}
    return _cached(("card", key), PARENT_TTL, fetch)


def children(key):
    """Direct children with their state; `kids` says which of them have children of their own."""
    def fetch():
        out = _run(["linear", "issues", "list", "--parent", key, "--include-archived", "--limit", "250"])
        rows = out.get("nodes", out) if isinstance(out, dict) else out
        return {"rows": [{"id": r.get("identifier"), "title": r.get("title", ""), "state": (r.get("state") or {}).get("name", ""),
                          "completed": r.get("completedAt") or "", "url": r.get("url", ""),
                          "kids": bool(((r.get("children") or {}).get("nodes")) or [])} for r in rows]}
    return _cached(("children", key), CHILDREN_TTL, fetch)


def subtree(key):
    """Every descendant, as Linear counts sub-issues; None while any level is still loading."""
    top = children(key)
    if not top or top.get("error"):
        return None
    rows = list(top["rows"])
    for r in top["rows"]:
        if r["kids"]:
            below = subtree(r["id"])
            if below is None:
                return None
            rows.extend(below)
    return rows


def _count(rows, since):
    """Every sub-issue in the tree, as Linear counts them, minus the canceled and duplicate ones."""
    live = [r for r in rows if r["state"] not in DEAD_STATES]
    done = [r for r in live if r["state"] == "Done"]
    started = [r for r in live if r["state"] in STARTED_STATES]
    today = [r for r in done if r["completed"] and _local(r["completed"]) >= since]
    return {"total": len(live), "done": len(done), "dead": len(rows) - len(live),
            "started": len(started), "today": len(today)}


def _local(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()


def _ancestry(tracker):
    """The chain tracker → parent → … as far as the cache knows; stops at a missing parent."""
    chain, key = [], tracker
    for _ in range(4):
        c = card(key)
        if c is None:
            return None  # not fetched yet: no guess about the top
        if c.get("error") or not c.get("parent"):
            return chain
        chain.append(c["parent"])
        key = c["parent"]
    return chain


def _stage_no(title):
    """The stage number a roadmap card leads with ("[notifier] 12 · …"); 999 when it has none."""
    m = re.search(r"(?:^|\]\s*)(\d+)\s*·", title)
    return int(m.group(1)) if m else 999


def configured():
    cfg = asks.ROOT and os.path.join(asks.ROOT, "config.json")
    try:
        with open(cfg, encoding="utf-8") as f:
            return list(json.load(f).get("epics") or [])
    except (OSError, ValueError):
        return []


def summary(streams):
    """Epics found from the streams' tracker cards (or config.json `epics`), each with counts and stages."""
    since = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    epics, stages = {}, {}
    for e in configured():
        epics[e] = set()
    for s in streams:
        for t in (s.get("header", {}).get("tracker") or s.get("id", "")).replace(",", " ").split():
            chain = _ancestry(t)
            if not chain:
                continue
            top = chain[-1]
            epics.setdefault(top, set()).update(chain[:-1])
    out = []
    for e, seen_stages in epics.items():
        c, kids, rows = card(e), children(e), subtree(e)
        if rows is None or not c:
            out.append({"id": e, "title": (c or {}).get("title", ""), "url": (c or {}).get("url", ""), "loading": True})
            continue
        item = {"id": e, "title": c.get("title", ""), "url": c.get("url", ""), **_count(rows, since)}
        if any(r["id"] in seen_stages for r in kids["rows"]):
            stage_list = []
            for r in kids["rows"]:
                if r["state"] in DEAD_STATES:
                    continue
                below = subtree(r["id"]) if r["kids"] else []
                st = {"id": r["id"], "title": r["title"], "url": r["url"]}
                stage_list.append({**st, "loading": True} if below is None else {**st, **_count(below, since)})
            stage_list.sort(key=lambda st: (_stage_no(st["title"]), st["id"]))
            item.update(staged=True, stages=stage_list)
        out.append(item)
    out.sort(key=lambda x: x["id"])
    return out
