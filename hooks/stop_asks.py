#!/usr/bin/env python3
"""Stop hook: an orchestrator may not end a turn with owner questions that are not filed through `tso ask`.

Runs for every Claude Code session and exits at once unless the session is a registered orchestrator.
"""

import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from desk import state, transcript, tsoq  # noqa: E402

QUESTION_REF = re.compile(r"\bq-\d+\b")


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
    if QUESTION_REF.search(item):
        return True
    tasks = transcript.refs(item)[0]
    return any((q["stream"] and q["stream"] in tasks) or transcript.similar(q["title"], item) for q in live)


def verdict(hook, live=None):
    if hook.get("stop_hook_active") or hook.get("session_id") not in orchestrator_sessions():
        return None
    path = hook.get("transcript_path") or transcript.find_transcript({"session_id": hook.get("session_id", "")})
    text, line, _ = transcript.last_status(transcript.recent_messages(path, limit=3)) if path else ("", "", "")
    live = tsoq.questions() if live is None else live
    missing = [i for i in transcript.owner_items(line) if not covered(i, live)]
    if not missing:
        return None
    reason = ("В строке waiting есть вопросы к владельцу без записи в tso: " + "; ".join(missing)
              + ". Для каждого вызови `tso ask new --title … --context-file … --option 'A=… :: …' --recommend … --why … "
                "--stream MER-…` (правила — CLAUDE.md в /Users/yanodintsov/go/src/github.com/MercuryoPro), "
                "а в строке waiting ссылайся на q-N.")
    return {"decision": "block", "reason": reason}


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
