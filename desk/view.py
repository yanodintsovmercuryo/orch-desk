"""Assemble the page's JSON: streams from the plugin's files, owner questions from tsod, links from Linear and GitHub."""

import os
import re
from concurrent.futures import ThreadPoolExecutor

from . import deliver, links, state, terminal, tsoq, workspace

CLOSED_LIMIT = 100
TASK_ID = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


def _event_json(ev):
    return {"ts": ev.ts, "kind": ev.kind, "checkpoint": ev.checkpoint, "direction": ev.direction,
            "actor": ev.actor, "text": ev.text}


def _links(st, prs):
    tracker = st.header.get("tracker") or st.id
    return {
        "linear": links.linear(tracker),
        "pr": links.pull_request(st.header.get("repository", ""), st.header.get("branch", ""), prs),
    }


def _stage(st, lk):
    """Index of the stage and whether it was read off the PR because the journal says nothing."""
    idx, moved = state.progress(st.events)
    inferred = False
    pr = lk["pr"]
    if idx < 0 and not st.events:
        prs = pr.get("state")
        idx = state.ORDERED.index("pre-merge") if prs == "MERGED" else state.ORDERED.index("pre-publish") if prs == "OPEN" \
            else state.ORDERED.index("plan-passed")
        inferred = True
    # An open, non-draft PR means published even when the journal stayed silent about it.
    if 0 <= idx < state.ORDERED.index("pre-merge") and pr.get("state") == "OPEN" and not pr.get("isDraft") and not pr.get("draft"):
        idx = state.ORDERED.index("pre-merge")
    return idx, moved, inferred


def build(root=None):
    checkpoints = state.load_checkpoints()
    out = {"root": root or state.state_root(), "orchestrators": [], "streams": []}
    all_streams = []
    under_tso = tsoq.lines_under_tso()
    for orch in state.orchestrators(root):
        reg = orch["registry"]
        out["orchestrators"].append({
            "name": orch["name"], "session_id": reg.get("session_id", ""),
            "session_status": deliver.session_status(reg.get("session_id", "")), "under_tso": under_tso,
        })
        for st in state.streams(orch, checkpoints):
            all_streams.append((orch["name"], st))

    def assemble(row):
        orch_name, st = row
        journal_prs = set()
        for ev in st.events:
            # Only "PR #N" names the stream's own PR; a bare #N may be another repository's.
            journal_prs |= set(re.findall(r"\bPR #(\d+)\b", ev.text))
        lk = _links(st, journal_prs)
        idx, moved, inferred = _stage(st, lk)
        closed = idx == len(state.ORDERED) - 1
        prompt = None if closed else terminal.waiting_prompt(st.header.get("repository", ""))
        contour = {} if closed else workspace.status(st.header.get("repository", ""))
        if contour.get("app_up"):
            contour["app_up"]["tail"] = workspace.log_tail(contour["app_up"]["log"])
        return {
            "prompt": prompt, "prepared": st.prepared, "workspace": contour,
            "id": st.id, "orchestrator": orch_name, "header": st.header, "derived": st.derived,
            "error": st.error, "note": st.note,
            "progress": {"index": idx, "total": len(state.ORDERED), "intent_moved": moved, "inferred": inferred},
            "milestones": state.milestones(st.events),
            "events": [_event_json(e) for e in st.events],
            "last_ts": st.events[-1].ts if st.events else "",
            "closed": closed, "links": lk,
        }

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(assemble, all_streams))

    # A record with no journal line is only "taken into work" while a session still runs in its worktree.
    try:
        live_cwds = {(a.get("cwd") or "").rstrip("/") for a in deliver.agents()}
    except Exception:
        live_cwds = None
    if live_cwds is not None:
        rows = [r for r in rows if r["closed"] or r["events"] or (r["header"].get("repository") or "").rstrip("/") in live_cwds]

    open_rows = sorted((r for r in rows if not r["closed"]), key=lambda r: (not r["prompt"], _neg(r["last_ts"])))
    closed_rows = sorted((r for r in rows if r["closed"]), key=lambda r: r["last_ts"], reverse=True)
    out["streams"] = open_rows + closed_rows[:CLOSED_LIMIT]
    out["checkpoints"] = state.ORDERED
    attach_questions(out, tsoq.questions())
    return out


def attach_questions(out, qs):
    """Each open question goes to the stream it names (its --stream, or a task id in its title); the rest stay general."""
    ids = {s["id"] for s in out["streams"]}
    for s in out["streams"]:
        s["questions"] = []
    general = []
    for q in qs:
        sid = q["stream"] if q["stream"] in ids else next((t for t in TASK_ID.findall(q["title"]) if t in ids), "")
        if sid:
            next(s for s in out["streams"] if s["id"] == sid)["questions"].append(q)
        else:
            general.append(q)
    out["questions"] = qs
    out["general_questions"] = general


def build_orchestrators(root=None):
    """Session ids, names and stream ids only: the cheap check a reply is validated against."""
    out = []
    for orch in state.orchestrators(root):
        sids = [os.path.basename(p) for p in sorted(os.listdir(os.path.join(orch["dir"], "streams")))] \
            if os.path.isdir(os.path.join(orch["dir"], "streams")) else []
        out.append({"name": orch["name"], "session_id": orch["registry"].get("session_id", ""), "streams": sids})
    return out


def stream_repo(stream_id, root=None):
    """The worktree a live stream's card names; empty for an unknown stream."""
    for orch in state.orchestrators(root):
        card = os.path.join(orch["dir"], "streams", stream_id, "card.md")
        if os.path.isfile(card):
            with open(card, encoding="utf-8") as f:
                return state.parse_card(f.read())[0].get("repository", "")
    return ""


def _neg(ts):
    # Sort newest first inside a stable ascending sort.
    return tuple(-ord(c) for c in ts)
