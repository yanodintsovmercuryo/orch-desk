"""The owner's questions and the messages to the orchestrator, both through tso (tsod).

tsod binds the asker from the calling process's ancestry, so a question is only ever filed from the
orchestrator's own shell (`tso ask new`); the desk reads questions, answers them and sends messages.
`tso answer` refuses any process that has a Claude session among its ancestors, which the desk server,
started by launchd, does not.
"""

import os
import re
import shutil
import subprocess

TSO = shutil.which("tso") or os.path.expanduser("~/.local/bin/tso")
LIVE = ("open", "answered", "acked")  # done and withdrawn are history


def _run(args, timeout=30):
    res = subprocess.run([TSO, *args], capture_output=True, text=True, timeout=timeout)
    return res.returncode, res.stdout, res.stderr


def parse_list(text):
    """Rows of `tso ask list`: id, status, name, stream, age, title, split on two or more spaces."""
    rows = []
    for line in text.splitlines():
        parts = re.split(r"\s{2,}", line.strip(), maxsplit=5)
        if len(parts) < 6 or not re.fullmatch(r"q-\d+", parts[0]):
            continue
        rows.append({"id": parts[0], "status": parts[1], "name": parts[2],
                     "stream": "" if parts[3] == "-" else parts[3], "age": parts[4], "title": parts[5]})
    return rows


_OPTION = re.compile(r"^(\S+)  (.*?) — (.*?)(?:  \(recommended(?:: (.*))?\))?$")


def parse_show(text):
    """The question as `tso ask show` prints it: header, fields, context, options, answer, deliveries."""
    lines = text.splitlines()
    q = {"id": "", "status": "", "name": "", "session": "", "title": "", "kind": "", "stream": "", "checkpoint": "",
         "deadline": "", "free_text": False, "context": "", "options": [], "recommend": "", "why": "",
         "answer": None, "delivery": [], "note": ""}
    if not lines:
        return q
    head = re.match(r"^(q-\d+)\s+(\S+)\s+(\S+)\s+\(session (\S+)\)", lines[0])
    if head:
        q.update(id=head.group(1), status=head.group(2), name=head.group(3), session=head.group(4))
    section, ctx = "", []
    for line in lines[1:]:
        if line.startswith("title: "):
            q["title"] = line[7:]
        elif line.startswith("kind: "):
            for key, value in re.findall(r"(kind|stream|checkpoint|deadline|free text): (\S+)", line):
                value = "" if value == "-" else value
                if key == "free text":
                    q["free_text"] = value == "true"
                else:
                    q[key] = value
        elif line == "context:":
            section = "context"
        elif line == "options:":
            section = "options"
        elif line == "events:":
            section = "events"
        elif line.startswith("answer: "):
            section = ""
            m = re.match(r"answer: (\S+) via (\w+)(?:, words: (.*?))?( \(a request, not a grant\))?$", line)
            if m:
                q["answer"] = {"option": "" if m.group(1) == "-" else m.group(1), "surface": m.group(2),
                               "text": m.group(3) or "", "request": bool(m.group(4))}
        elif line.startswith("delivery ("):
            section = ""
            q["delivery"].append(line)
        elif line.startswith("note: "):
            section = ""
            q["note"] = line[6:]
        elif section == "context" and line.startswith("  "):
            ctx.append(line[2:])
        elif section == "options" and line.startswith("  "):
            m = _OPTION.match(line[2:])
            if m:
                rec = "(recommended" in line
                q["options"].append({"key": m.group(1), "label": m.group(2), "consequence": m.group(3), "recommended": rec})
                if rec:
                    q["recommend"], q["why"] = m.group(1), m.group(4) or ""
    q["context"] = "\n".join(ctx)
    return q


def questions(run=_run):
    """Open questions with their stream, newest ids last. Empty on any tso failure (the page shows the rest)."""
    try:
        code, out, _ = run(["ask", "list", "--all"])
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [r for r in parse_list(out) if r["status"] in LIVE] if code == 0 else []


def question(qid, run=_run):
    if not re.fullmatch(r"q-\d+", qid or ""):
        raise ValueError("не номер вопроса")
    code, out, err = run(["ask", "show", qid])
    if code != 0:
        raise RuntimeError((err or out).strip()[:300])
    return parse_show(out)


def answer(qid, option="", text="", run=_run):
    """Answer through tsod. Exit 0 acknowledged, 3 recorded but not acknowledged yet, anything else refused."""
    if not re.fullmatch(r"q-\d+", qid or ""):
        raise ValueError("не номер вопроса")
    args = ["answer", qid]
    if option:
        args += ["--option", option]
    if text:
        args += ["--text", text]
    if not option and not text:
        raise ValueError("пустой ответ")
    code, out, err = run(args, timeout=90)
    return {"ok": code in (0, 3), "acked": code == 0, "code": code, "message": (out or err).strip()[:400]}


SEND_CODES = {0: "доставлено и подтверждено", 3: "доставлено, подтверждения нет", 5: "некому доставить: линия не запущена",
              12: "линия закрыта"}


def send(name, text, run=_run):
    """One message to the line that holds `role/name`. 0 and 3 are delivered; 5 means nobody took it."""
    if not name or "/" not in name:
        raise ValueError("нужно имя вида role/name")
    code, out, err = run(["send", name, text], timeout=60)
    return {"ok": code in (0, 3), "code": code, "message": SEND_CODES.get(code) or (err or out).strip()[:300]}


def lines_under_tso(run=_run):
    """False when `tso status` says no session was launched by tso: such a line has no attested delivery."""
    try:
        code, out, _ = run(["status"])
    except (OSError, subprocess.TimeoutExpired):
        return None
    return None if code != 0 else "no session launched by tso" not in out
