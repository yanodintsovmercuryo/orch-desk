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

from . import links

ROOT = os.environ.get("DESK_ROOT") or os.path.expanduser("~/.local/state/desk")

PARENT_TTL = 21600  # a card's parent rarely changes
CHILDREN_TTL = 900
DEAD_STATES = ("Canceled", "Duplicate")
STARTED_STATES = ("In Progress", "In Review")
_cache = {}
_pending = set()
_lock = threading.Lock()
CACHE_FILE = os.path.join(ROOT, "epics-cache.json")


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
        os.makedirs(ROOT, exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({f"{k[0]}:{k[1]}": v for k, v in _cache.items() if not (isinstance(v[1], dict) and v[1].get("error"))}, f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass


_load_cache()


def _run(args):
    res = subprocess.run(args, capture_output=True, text=True, timeout=90)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip()[:300])
    return json.loads(res.stdout or "{}")


RETRY_AFTER = 30  # a failed refresh is tried again this many seconds later, not after a full ttl


def _cached(key, ttl, fn):
    with _lock:
        hit = _cache.get(key)
        fresh = hit and time.time() - hit[0] < ttl
        if not fresh and key not in _pending and not links.paused():
            _pending.add(key)
            threading.Thread(target=_refresh, args=(key, fn, ttl), daemon=True).start()
        return hit[1] if hit else None


def _refresh(key, fn, ttl=CHILDREN_TTL):
    """Fetch first, replace after: the answer being served is dropped only when a better one is in hand."""
    with links._slots:
        try:
            if links.paused():  # queued before the limit hit: do not add to it
                raise RuntimeError("linear paused")
            value, failed = fn(), False
        except Exception as e:
            value, failed = {"error": str(e)[:300]}, True
            links.note_failure(str(e))
    with _lock:
        prev = _cache.get(key)
        if failed and prev and not (prev[1] or {}).get("error"):
            # Linear was slow or refused: the last good answer stays on the page and is retried soon.
            _cache[key] = (time.time() - ttl + RETRY_AFTER, prev[1])
        else:
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


def _count(rows, since, active=frozenset()):
    """Every sub-issue in the tree, as Linear counts them, minus the canceled and duplicate ones.

    `started` is what runs now: cards whose stream is open on the desk."""
    # A card with children is an epic, not a task: only leaves are counted.
    live = [r for r in rows if r["state"] not in DEAD_STATES and not r.get("kids")]
    done = [r for r in live if r["state"] == "Done"]
    # Only a card with an open stream runs: Linear keeps partly fixed cards In Progress with nobody on them.
    working = {r["id"] for r in live if r["id"] in active and r["state"] != "Done"}
    today = [r for r in done if r["completed"] and _local(r["completed"]) >= since]
    return {"total": len(live), "done": len(done), "dead": sum(1 for r in rows if r["state"] in DEAD_STATES and not r.get("kids")),
            "started": len(working), "today": len(today)}


def _local(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()


def _ancestry(tracker):
    """The chain tracker → parent → … as far as the cache knows; stops at a missing parent."""
    chain, key = [], tracker
    for _ in range(4):
        c = card(key)
        if c is None:
            return None  # not fetched yet: no guess about the top
        if c.get("error"):
            return None  # unknown is not "no parent": a guess here would put a stage at the top
        if not c.get("parent"):
            return chain
        chain.append(c["parent"])
        key = c["parent"]
    return chain


def _stage_no(title):
    """The stage number a roadmap card leads with ("[notifier] 12 · …"); 999 when it has none."""
    m = re.search(r"(?:^|\]\s*)(\d+)\s*·", title)
    return int(m.group(1)) if m else 999


def configured():
    cfg = ROOT and os.path.join(ROOT, "config.json")
    try:
        with open(cfg, encoding="utf-8") as f:
            return list(json.load(f).get("epics") or [])
    except (OSError, ValueError):
        return []


TRACKER_RE = re.compile(r"[A-Z][A-Z0-9]+-\d+")


def _trackers(stream):
    """Card ids a stream works under; a header's free text ("MER-3690-S4", "(notifier") is not one."""
    raw = (stream.get("header", {}).get("tracker") or stream.get("id", "")).replace(",", " ").split()
    return [t for t in raw if TRACKER_RE.fullmatch(t)]


def _running(streams):
    """The tracker cards of the streams that run right now, each with its chain of parents."""
    out = {}
    for s in streams:
        if s.get("closed") or s.get("prepared"):
            continue
        for t in _trackers(s):
            chain = _ancestry(t)
            if chain:
                out[t] = {"chain": chain, "title": (card(t) or {}).get("title", "")}
    return out


def _chips(tasks, via=False):
    """`via` names the stage a task sits in, for the top row that shows every running task at once."""
    return [{"id": t, "title": v["title"], **({"via": v["chain"][-2] if len(v["chain"]) > 1 else ""} if via else {})}
            for t, v in sorted(tasks.items())]


_saved = {"at": 0.0}


def _remember(kind, key, value):
    with _lock:
        _cache[(kind, key)] = (time.time(), value)
        if time.time() - _saved["at"] > 60:  # the last full rows survive a restart too
            _saved["at"] = time.time()
            _save_cache()


def _recall(kind, key):
    with _lock:
        hit = _cache.get((kind, key))
    return hit[1] if hit and not (isinstance(hit[1], dict) and hit[1].get("error")) else None


def _with_running(item, mine):
    """The numbers may be old; what runs right now is always current."""
    item = {**item, "active": _chips(mine, via=True)}
    if item.get("stages"):
        stages = []
        for st in item["stages"]:
            if st["id"]:
                here = {t: v for t, v in mine.items() if len(v["chain"]) > 1 and v["chain"][-2] == st["id"]}
            else:
                here = {t: v for t, v in mine.items() if len(v["chain"]) == 1}
            stages.append({**st, "active": _chips(here)})
        item["stages"] = stages
    return item


def summary(streams):
    """Epics found from the streams' tracker cards (or config.json `epics`), each with counts and stages.

    A read that is not finished never blanks a row: the last complete answer is shown until the new one lands."""
    since = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    running = _running(streams)
    epics = {}
    for e in configured() + list(_recall("known", "epics") or []):
        epics.setdefault(e, set())
    for s in streams:
        for t in _trackers(s):
            chain = _ancestry(t)
            if not chain:
                continue
            epics.setdefault(chain[-1], set()).update(chain[:-1])
    _remember("known", "epics", sorted(epics))
    out = []
    for e in epics:
        mine = {t: v for t, v in running.items() if v["chain"][-1] == e}
        c, kids, rows = card(e), children(e), subtree(e)
        if rows is None or not c or c.get("error") or not kids or kids.get("error"):
            last = _recall("item", e)
            out.append(_with_running(last, mine) if last else
                       {"id": e, "title": (c or {}).get("title", ""), "url": (c or {}).get("url", ""), "loading": True})
            continue
        item = {"id": e, "title": c.get("title", ""), "url": c.get("url", ""), **_count(rows, since, set(mine)),
                "active": _chips(mine, via=True)}
        last = _recall("item", e) or {}
        last_stage = {st["id"]: st for st in last.get("stages", [])}
        # Children with children of their own are sub-epics; the leaves gather in one group.
        subs = [r for r in kids["rows"] if r["kids"] and r["state"] not in DEAD_STATES]
        if subs:
            stage_list = []
            for r in subs:
                below = subtree(r["id"])
                in_stage = {t: v for t, v in mine.items() if len(v["chain"]) > 1 and v["chain"][-2] == r["id"]}
                st = {"id": r["id"], "title": r["title"], "url": r["url"], "active": _chips(in_stage)}
                if below is not None:
                    stage_list.append({**st, **_count(below, since, set(in_stage))})
                elif r["id"] in last_stage:
                    stage_list.append({**last_stage[r["id"]], **st})  # numbers of the last read, chips of this one
                else:
                    stage_list.append({**st, "loading": True})
            stage_list.sort(key=lambda st: (_stage_no(st["title"]), st["id"]))
            leaves = [r for r in kids["rows"] if not r["kids"]]
            if leaves:
                direct = {t: v for t, v in mine.items() if len(v["chain"]) == 1}
                stage_list.append({"id": "", "title": "Отдельные задачи", "url": c.get("url", ""),
                                   **_count(leaves, since, set(direct)), "active": _chips(direct)})
            item.update(staged=True, stages=stage_list)
        if not any(st.get("loading") for st in item.get("stages", [])):
            _remember("item", e, item)
        out.append(item)
    out.sort(key=lambda x: x["id"])
    return out
