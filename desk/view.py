"""Assemble the page's JSON from orchestrator state, transcripts and links."""

import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor

from . import deliver, links, state, transcript, workspace

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
            "session_status": deliver.session_status(reg.get("session_id", "")),
        })
        for st in state.streams(orch, checkpoints):
            all_streams.append((orch["name"], st, messages, items))

    def assemble(row):
        orch_name, st, messages, items = row
        journal_prs, mentioned = set(), set()
        for ev in st.events:
            # Only "PR #N" names the stream's own PR; a bare #N may be another repository's.
            journal_prs |= set(re.findall(r"\bPR #(\d+)\b", ev.text))
            mentioned |= transcript.refs(ev.text)[1]
        lk = _links(st, journal_prs)
        pr_no = lk["pr"].get("number")
        own_prs = {str(pr_no)} if pr_no else journal_prs
        # Until GitHub answers, any #N the journal mentions may identify the stream in an ask.
        match_prs = own_prs if pr_no else journal_prs | mentioned
        idx, moved = state.progress(st.events)
        asks = [i for i in items if _names_stream(i, st.id, match_prs)]
        ask = transcript.ask_thread(
            messages, lambda i: _names_stream(i, st.id, match_prs),
            lambda t: transcript.paragraphs_about(t, st.id, own_prs)) if asks else {}
        closed = idx == len(state.ORDERED) - 1
        contour = {} if closed else workspace.status(st.header.get("repository", ""))
        if contour.get("app_up"):
            contour["app_up"]["tail"] = workspace.log_tail(contour["app_up"]["log"])
        return {
            "workspace": contour,
            "id": st.id, "orchestrator": orch_name, "header": st.header, "derived": st.derived,
            "error": st.error, "progress": {"index": idx, "total": len(state.ORDERED), "intent_moved": moved},
            "milestones": state.milestones(st.events),
            "events": [_event_json(e) for e in st.events],
            "last_ts": st.events[-1].ts if st.events else "",
            "closed": closed,
            "owner_asks": asks,
            "ask": ask,
            "links": lk,
        }

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(assemble, all_streams))

    matched = {a for r in rows for a in r["owner_asks"]}
    by_orch = {}
    for orch_name, _, messages, _ in all_streams:
        by_orch.setdefault(orch_name, messages)
    for o in out["orchestrators"]:
        for item in o["owner_items"]:
            if item in matched:
                continue
            ids = transcript.refs(item)[0]
            # The wording of a general ask drifts turn to turn; match it by the tasks it names or its words.
            same = (lambda i, ids=ids: bool(transcript.refs(i)[0] & ids)) if ids \
                else (lambda i, item=item: transcript.similar(i, item))
            out["general_asks"].append({
                # Keyed by the tasks it names when it names any: the wording shifts turn to turn.
                "id": "g-" + hashlib.sha1((" ".join(sorted(ids)) or item).encode()).hexdigest()[:8],
                "orchestrator": o["name"], "item": item,
                "ask": _general_thread(transcript.ask_thread(
                    by_orch.get(o["name"], []), same, lambda t, item=item: transcript.paragraphs_matching(t, item)), item),
            })

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


def _general_thread(thread, item):
    # A general ask is usually one line of a longer turn: show the paragraphs about it, keep the rest.
    origin = thread.get("origin")
    if origin:
        paras = transcript.paragraphs_matching(origin["text"], item)
        if paras:
            origin["full"], origin["text"] = origin["text"], "\n\n".join(paras)
    return thread


def general_asks(orch_name):
    return [g["item"] for g in build()["general_asks"] if g["orchestrator"] == orch_name]


def current_asks(orch_name, stream_id):
    """Owner asks naming the stream in the orchestrator's latest waiting line."""
    for o in build()["orchestrators"]:
        if o["name"] == orch_name:
            return [i for i in o["owner_items"] if stream_id in transcript.refs(i)[0]]
    return []


def stream_repo(stream_id, root=None):
    """The worktree a live stream's card names; empty for an unknown stream."""
    for orch in state.orchestrators(root):
        card = os.path.join(orch["dir"], "streams", stream_id, "card.md")
        if os.path.isfile(card):
            with open(card, encoding="utf-8") as f:
                return state.parse_card(f.read())[0].get("repository", "")
    return ""


def _names_stream(item, stream_id, pr_numbers):
    ids, prs = transcript.refs(item)
    if stream_id in ids:
        return True
    # A bare PR number counts only when the item names no other stream.
    return bool(prs & {str(n) for n in pr_numbers}) and not ids


def _neg(ts):
    # Sort newest first inside a stable ascending sort.
    return tuple(-ord(c) for c in ts)
