"""Epic progress from Linear: the parents of the streams' tracker cards, counted by their children.

The orchestrator knows nothing about epics; this is a reading of Linear, labelled as such.
Everything is stale-while-revalidate over the local `linear` CLI, so a page never waits on it.
"""

import json
import os
import subprocess
import threading
import time
from datetime import datetime

from . import asks, links

PARENT_TTL = 3600  # a card's parent rarely changes
CHILDREN_TTL = 300
DEAD_STATES = ("Canceled", "Duplicate")
_cache = {}
_pending = set()
_lock = threading.Lock()


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
    def fetch():
        out = _run(["linear", "issues", "list", "--parent", key, "--include-archived", "--limit", "250"])
        rows = out.get("nodes", out) if isinstance(out, dict) else out
        return {"rows": [{"id": r.get("identifier"), "title": r.get("title", ""), "state": (r.get("state") or {}).get("name", ""),
                          "completed": r.get("completedAt") or "", "url": r.get("url", "")} for r in rows]}
    return _cached(("children", key), CHILDREN_TTL, fetch)


def _count(rows, since):
    live = [r for r in rows if r["state"] not in DEAD_STATES]
    done = [r for r in live if r["state"] == "Done"]
    today = [r for r in done if r["completed"] and _local(r["completed"]) >= since]
    return {"total": len(live), "done": len(done), "today": len(today)}


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
        c, kids = card(e), children(e)
        if not kids or kids.get("error") or not c:
            out.append({"id": e, "title": (c or {}).get("title", ""), "url": (c or {}).get("url", ""), "loading": True})
            continue
        rows = kids["rows"]
        staged = any(r["id"] in seen_stages for r in rows)
        item = {"id": e, "title": c.get("title", ""), "url": c.get("url", ""), "staged": staged}
        if staged:
            agg, stage_list, loading = {"total": 0, "done": 0, "today": 0}, [], False
            for r in rows:
                if r["state"] in DEAD_STATES:
                    continue
                sub = children(r["id"])
                if not sub or sub.get("error"):
                    loading = True
                    stage_list.append({"id": r["id"], "title": r["title"], "url": r["url"], "loading": True})
                    continue
                n = _count(sub["rows"], since)
                for k in agg:
                    agg[k] += n[k]
                stage_list.append({"id": r["id"], "title": r["title"], "url": r["url"], **n})
            item.update(agg, stages=stage_list, loading=loading)
        else:
            item.update(_count(rows, since))
        out.append(item)
    out.sort(key=lambda x: x["id"])
    return out
