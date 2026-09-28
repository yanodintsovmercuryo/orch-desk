"""Token usage per session, read off Claude Code's own transcripts.

Every model call is a line with `message.usage` in ~/.claude/projects/*/<session>.jsonl (and
<session>/subagents/*.jsonl for its subagents). Nothing else records which session spent what,
so this is the only breakdown the owner can get. Files are read incrementally from a stored
offset; totals are kept per hour so a window can be summed without re-reading.
"""

import glob
import json
import os
import threading
import time
from datetime import datetime, timezone

from . import asks

CACHE = os.path.join(asks.ROOT, "usage-cache.json")
PROJECTS = os.path.expanduser("~/.claude/projects")
_lock = threading.Lock()
_cache = {"files": {}, "ts": 0.0}
_loaded = False
# A rough weight in "input-token equivalents": output is the expensive side, a cache read the cheap one.
WEIGHT = {"out": 5.0, "in": 1.0, "cc": 1.25, "cr": 0.1}


def _load():
    global _loaded, _cache
    if _loaded:
        return
    try:
        with open(CACHE, encoding="utf-8") as f:
            _cache = json.load(f)
    except (OSError, ValueError):
        _cache = {"files": {}, "ts": 0.0}
    _cache.setdefault("files", {})
    _loaded = True


def _save():
    os.makedirs(asks.ROOT, exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_cache, f, ensure_ascii=False)
    os.replace(tmp, CACHE)


def _transcripts():
    out = glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))
    out += glob.glob(os.path.join(PROJECTS, "*", "*", "subagents", "*.jsonl"))
    return out


def _hour(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%dT%H")
    except (ValueError, AttributeError):
        return None


def _scan(path, rec):
    """Read the lines added since the stored offset into the file's per-hour buckets."""
    size = os.path.getsize(path)
    if size < rec.get("offset", 0):
        rec.update(offset=0, hours={})  # rewritten: start over
    with open(path, "rb") as f:
        f.seek(rec.get("offset", 0))
        data = f.read()
    # A partial last line is left for the next read.
    cut = data.rfind(b"\n")
    if cut < 0:
        return
    rec["offset"] = rec.get("offset", 0) + cut + 1
    hours = rec.setdefault("hours", {})
    for line in data[:cut].split(b"\n"):
        if b'"usage"' not in line and b'"cwd"' not in line and b'"type":"user"' not in line and b'"type": "user"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not rec.get("cwd") and d.get("cwd"):
            rec["cwd"] = d["cwd"]
        m = d.get("message") or {}
        if not rec.get("first") and d.get("type") == "user" and isinstance(m.get("content"), str):
            rec["first"] = m["content"][:80].replace("\n", " ")
        u = m.get("usage") if isinstance(m, dict) else None
        if not u:
            continue
        h = _hour(d.get("timestamp") or "")
        if not h:
            continue
        b = hours.setdefault(h, [0, 0, 0, 0, 0])
        b[0] += u.get("output_tokens", 0)
        b[1] += u.get("input_tokens", 0)
        b[2] += u.get("cache_creation_input_tokens", 0)
        b[3] += u.get("cache_read_input_tokens", 0)
        b[4] += 1
        ctx = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
        rec["last_ctx"] = ctx
        rec["last_ts"] = d.get("timestamp")
        rec["model"] = m.get("model") or rec.get("model")
        recent = rec.setdefault("recent_ctx", [])
        recent.append(ctx)
        del recent[:-20]


_refreshing = {"on": False}


def refresh_async():
    """A page never waits on a scan: the first one reads days of transcripts and takes a minute."""
    with _lock:
        if _refreshing["on"]:
            return
        _refreshing["on"] = True

    def run():
        try:
            refresh(max_age=30)
        finally:
            _refreshing["on"] = False
    threading.Thread(target=run, daemon=True).start()


def refresh(max_age=60):
    """Re-read what changed; cheap when nothing did (mtime and size are compared first)."""
    with _lock:
        _load()
        if time.time() - _cache.get("ts", 0) < max_age:
            return
        files = _cache["files"]
        seen = set()
        for path in _transcripts():
            try:
                st = os.stat(path)
            except OSError:
                continue
            if time.time() - st.st_mtime > 8 * 24 * 3600:
                continue
            seen.add(path)
            rec = files.setdefault(path, {})
            if rec.get("mtime") == st.st_mtime and rec.get("size") == st.st_size:
                continue
            try:
                _scan(path, rec)
            except OSError:
                continue
            rec.update(mtime=st.st_mtime, size=st.st_size)
            # Buckets older than the retention are dropped so the cache stays small.
            floor = datetime.now(timezone.utc).timestamp() - 8 * 24 * 3600
            rec["hours"] = {h: v for h, v in rec.get("hours", {}).items()
                            if datetime.strptime(h, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc).timestamp() >= floor}
        for path in list(files):
            if path not in seen:
                del files[path]
        _cache["ts"] = time.time()
        _save()


def _session_of(path):
    base = os.path.basename(path)[:-6]
    if "/subagents/" in path:
        return os.path.basename(os.path.dirname(os.path.dirname(path))), True
    return base, False


def weight(b):
    return b[0] * WEIGHT["out"] + b[1] * WEIGHT["in"] + b[2] * WEIGHT["cc"] + b[3] * WEIGHT["cr"]


def summary(orchestrators, streams, hours=24):
    """Per group and per session for the last `hours`, plus each orchestrator's context per call."""
    refresh_async()
    with _lock:
        _load()
    since = datetime.now(timezone.utc).timestamp() - hours * 3600
    orch_ids = {o["session_id"]: o["name"] for o in orchestrators if o.get("session_id")}
    repo_of = {}
    for s in streams:
        repo = (s.get("header") or {}).get("repository") or ""
        if repo:
            repo_of[repo.rstrip("/")] = s["id"]
    sessions = {}
    with _lock:
        for path, rec in _cache["files"].items():
            sid, sub = _session_of(path)
            tot = [0, 0, 0, 0, 0]
            for h, b in rec.get("hours", {}).items():
                if datetime.strptime(h, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc).timestamp() + 3600 < since:
                    continue
                for i in range(5):
                    tot[i] += b[i]
            if not tot[4]:
                continue
            row = sessions.setdefault(sid, {"id": sid, "out": 0, "in": 0, "cc": 0, "cr": 0, "calls": 0, "files": 0,
                                            "cwd": "", "first": "", "model": "", "ctx": 0, "ctx_median": 0, "last_ts": ""})
            row["out"] += tot[0]; row["in"] += tot[1]; row["cc"] += tot[2]; row["cr"] += tot[3]; row["calls"] += tot[4]
            row["files"] += 1
            if not sub:
                row.update(cwd=rec.get("cwd", ""), first=rec.get("first", ""), model=rec.get("model", ""),
                           ctx=rec.get("last_ctx", 0), last_ts=rec.get("last_ts", ""))
                recent = sorted(rec.get("recent_ctx", []))
                row["ctx_median"] = recent[len(recent) // 2] if recent else 0
            elif not row["cwd"]:
                row.update(cwd=rec.get("cwd", ""), model=row["model"] or rec.get("model", ""))
    groups = {}
    for row in sessions.values():
        cwd = (row["cwd"] or "").rstrip("/")
        if row["id"] in orch_ids:
            g, row["stream"] = "оркестратор", orch_ids[row["id"]]
        elif cwd in repo_of or "You are the stream session" in (row["first"] or ""):
            g = "потоки"
            row["stream"] = repo_of.get(cwd) or (row["first"].split(",")[0].replace("You are the stream session ", "") if row["first"] else "")
        else:
            g, row["stream"] = "прочие сессии", ""
        row["group"] = g
        row["weight"] = weight([row["out"], row["in"], row["cc"], row["cr"]])
        gr = groups.setdefault(g, {"group": g, "out": 0, "cr": 0, "cc": 0, "calls": 0, "sessions": 0, "weight": 0.0})
        gr["out"] += row["out"]; gr["cr"] += row["cr"]; gr["cc"] += row["cc"]; gr["calls"] += row["calls"]
        gr["sessions"] += 1; gr["weight"] += row["weight"]
    total = sum(g["weight"] for g in groups.values()) or 1.0
    for g in groups.values():
        g["share"] = round(100 * g["weight"] / total, 1)
    top = sorted(sessions.values(), key=lambda r: -r["weight"])[:12]
    orch_ctx = [{"name": orch_ids[r["id"]], "session_id": r["id"], "ctx": r["ctx"], "ctx_median": r["ctx_median"],
                 "calls": r["calls"], "model": r["model"]} for r in sessions.values() if r["id"] in orch_ids]
    return {"hours": hours, "groups": sorted(groups.values(), key=lambda g: -g["weight"]), "top": top,
            "orchestrators": orch_ctx, "ts": _cache.get("ts", 0), "scanning": _refreshing["on"]}


def orchestrator_context(session_id):
    """The last call's context size for one session, or 0 when unknown."""
    refresh_async()
    with _lock:
        _load()
        for path, rec in _cache["files"].items():
            sid, sub = _session_of(path)
            if sid == session_id and not sub:
                return rec.get("last_ctx", 0), rec.get("model", "")
    return 0, ""
