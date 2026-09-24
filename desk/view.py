"""Assemble the page's JSON from orchestrator state, transcripts and links."""

import os
import re
from concurrent.futures import ThreadPoolExecutor

from . import links, state, transcript

CLOSED_LIMIT = 15


def _event_json(ev):
    return {"ts": ev.ts, "kind": ev.kind, "checkpoint": ev.checkpoint, "direction": ev.direction,
            "actor": ev.actor, "text": ev.text}


def _links(st, prs):
    tracker = st.header.get("tracker") or st.id
    return {
        "linear": links.linear(tracker),
        "pr": links.pull_request(st.header.get("repository", ""), st.header.get("branch", ""), prs),
    }


def build(root=None):
    checkpoints = state.load_checkpoints()
    out = {"root": root or state.state_root(), "orchestrators": [], "streams": [], "general_asks": []}
    all_streams = []
    for orch in state.orchestrators(root):
        reg = orch["registry"]
        path = transcript.find_transcript(reg)
        messages, msg, status, ts, error = [], "", "", "", ""
        if path:
            try:
                messages = transcript.recent_messages(path)
                msg, status, ts = transcript.last_status(messages)
            except OSError as e:
                error = str(e)
        else:
            error = "transcript not found"
        items = transcript.owner_items(status)
        out["orchestrators"].append({
            "name": orch["name"], "session_id": reg.get("session_id", ""), "transcript": path,
            "status_line": status, "message": msg, "message_ts": ts, "owner_items": items, "error": error,
        })
        for st in state.streams(orch, checkpoints):
            all_streams.append((orch["name"], st, messages, items))

    def assemble(row):
        orch_name, st, messages, items = row
        journal_prs = set()
        for ev in st.events:
            # Only "PR #N": a bare #N in a journal may be another repository's PR.
            journal_prs |= set(re.findall(r"\bPR #(\d+)\b", ev.text))
        lk = _links(st, journal_prs)
        pr_no = lk["pr"].get("number")
        own_prs = {str(pr_no)} if pr_no else set()
        idx, moved = state.progress(st.events)
        asks = [i for i in items if _names_stream(i, st.id, pr_no)]
        request = transcript.request_for(messages, st.id, own_prs) if asks else []
        return {
            "id": st.id, "orchestrator": orch_name, "header": st.header, "derived": st.derived,
            "error": st.error, "progress": {"index": idx, "total": len(state.ORDERED), "intent_moved": moved},
            "milestones": state.milestones(st.events),
            "events": [_event_json(e) for e in st.events],
            "last_ts": st.events[-1].ts if st.events else "",
            "closed": idx == len(state.ORDERED) - 1,
            "owner_asks": asks,
            "request": request,
            "links": lk,
        }

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(assemble, all_streams))

    matched = {a for r in rows for a in r["owner_asks"]}
    for o in out["orchestrators"]:
        out["general_asks"] += [{"orchestrator": o["name"], "item": i} for i in o["owner_items"] if i not in matched]

    open_rows = sorted((r for r in rows if not r["closed"]),
                       key=lambda r: (not r["owner_asks"], _neg(r["last_ts"])))
    closed_rows = sorted((r for r in rows if r["closed"]), key=lambda r: r["last_ts"], reverse=True)
    out["streams"] = open_rows + closed_rows[:CLOSED_LIMIT]
    out["checkpoints"] = state.ORDERED
    out["replies_log"] = os.environ.get("DESK_REPLIES_LOG", "")
    return out


def build_orchestrators(root=None):
    """Session ids and stream ids only: the cheap check a reply is validated against."""
    out = []
    for orch in state.orchestrators(root):
        sids = [os.path.basename(p) for p in sorted(os.listdir(os.path.join(orch["dir"], "streams")))] \
            if os.path.isdir(os.path.join(orch["dir"], "streams")) else []
        out.append({"name": orch["name"], "session_id": orch["registry"].get("session_id", ""), "streams": sids})
    return out


def _names_stream(item, stream_id, pr_no):
    ids, prs = transcript.refs(item)
    if stream_id in ids:
        return True
    # A bare PR number counts only when the item names no other stream.
    return bool(pr_no) and str(pr_no) in prs and not ids


def _neg(ts):
    # Sort newest first inside a stable ascending sort.
    return tuple(-ord(c) for c in ts)
