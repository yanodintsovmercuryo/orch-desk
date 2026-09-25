#!/usr/bin/env python3
"""Stop hook: an orchestrator may not end a turn with owner asks outside desk or owner answers unread.

Runs for every Claude Code session and exits at once unless the session is a registered orchestrator.
"""

import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from desk import asks, state, transcript  # noqa: E402

ASK_REF = re.compile(r"\bask-\d+\b")


def orchestrator_sessions():
    ids = set()
    for reg in glob.glob(os.path.join(state.state_root(), "*", "*", "registry.yaml")):
        try:
            sid = state.read_registry(reg).get("session_id", "")
        except OSError:
            continue
        if sid:
            ids.add(sid)
    return ids


def covered(item, live):
    if ASK_REF.search(item):
        return True
    tasks = transcript.refs(item)[0]
    return any((a.get("task") and a["task"] in tasks) or transcript.similar(a["question"], item) for a in live)


def verdict(hook):
    if hook.get("stop_hook_active") or hook.get("session_id") not in orchestrator_sessions():
        return None
    reasons = []
    pending = asks.pending_answers()
    if pending:
        reasons.append("Есть непрочитанные ответы владельца (" + ", ".join(a["id"] for a in pending)
                       + "): выполни `desk answers` и действуй по ним.")
    path = hook.get("transcript_path") or transcript.find_transcript({"session_id": hook.get("session_id", "")})
    text, line, _ = transcript.last_status(transcript.recent_messages(path, limit=3)) if path else ("", "", "")
    live = [a for a in asks.all_asks() if a["status"] in ("open", "answered")]
    missing = [i for i in transcript.owner_items(line) if not covered(i, live)]
    if missing:
        reasons.append("В строке waiting есть вопросы к владельцу без записи в desk: " + "; ".join(missing)
                       + ". Для каждого вызови `desk ask --task MER-… --question … --option 'A=… :: …' "
                         "--recommend … --why … --link …` (правила — CLAUDE.md в ~/go/src/github.com/MercuryoPro), "
                         "а в строке waiting ссылайся на ask-N.")
    return {"decision": "block", "reason": " ".join(reasons)} if reasons else None


def main():
    try:
        hook = json.load(sys.stdin)
        out = verdict(hook)
    except Exception:
        # A broken hook must never wedge a session.
        return
    if out:
        print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
