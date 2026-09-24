"""Read-only view of team-skills-orchestrator state on disk."""

import glob
import os
import re
from dataclasses import dataclass, field

ORDERED = ["launched", "spec-drafted", "plan-passed", "gate-round", "pre-publish", "pre-merge", "closed"]
FALLBACK_BLOCKING = {"spec-drafted", "intent-moved", "pre-publish", "pre-merge"}
PLUGIN_GLOB = os.path.expanduser(
    "~/.claude/plugins/cache/team-skills-orchestrator/team-skills-orchestrator/*/scripts/lib/checkpoints.tsv"
)


def state_root():
    home = os.environ.get("ORCHESTRATORS_HOME")
    if home:
        return home
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "orchestrators")


def _version_key(path):
    ver = path.split("/")[-4]
    return [int(p) if p.isdigit() else 0 for p in ver.split(".")]


def load_checkpoints(pattern=PLUGIN_GLOB):
    """Checkpoint names and blocking flags from the newest installed plugin."""
    paths = sorted(glob.glob(pattern), key=_version_key)
    table = {}
    if paths:
        with open(paths[-1], encoding="utf-8") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                cols = line.rstrip("\n").split("\t")
                if len(cols) >= 2:
                    table[cols[0]] = cols[1] == "yes"
    if not table:
        table = {name: name in FALLBACK_BLOCKING for name in ORDERED + ["intent-moved"]}
    return table


def parse_kv_block(lines):
    out = {}
    for line in lines:
        m = re.match(r"^([a-z][a-z_ -]*):\s?(.*)$", line)
        if m:
            out.setdefault(m.group(1), m.group(2).strip())
    return out


def parse_card(text):
    head, derived, inside = [], [], False
    for line in text.splitlines():
        if "derived: begin" in line:
            inside = True
            continue
        if "derived: end" in line:
            inside = False
            continue
        (derived if inside else head).append(line)
    header = {}
    for line in head:
        if line.startswith("perimeter:"):
            break
        header.update({k: v for k, v in parse_kv_block([line]).items() if k not in header})
    return header, parse_kv_block(derived)


@dataclass
class Event:
    ts: str
    kind: str  # "checkpoint" | "note"
    text: str
    checkpoint: str = ""
    direction: str = ""
    actor: str = ""


def parse_journal(text, checkpoints):
    events = []
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) < 3 or not re.match(r"^\d{4}-\d\d-\d\dT", cols[0]):
            continue
        if len(cols) >= 6 and cols[2] in checkpoints and cols[3] in ("in", "out"):
            events.append(Event(cols[0], "checkpoint", "\t".join(cols[5:]), cols[2], cols[3], cols[4]))
        else:
            events.append(Event(cols[0], "note", "\t".join(cols[2:])))
    return events


def progress(events):
    """Index into ORDERED of the latest ordered checkpoint, and whether intent moved after it."""
    idx, moved = -1, False
    for ev in events:
        if ev.kind != "checkpoint":
            continue
        if ev.checkpoint in ORDERED:
            idx, moved = ORDERED.index(ev.checkpoint), False
        elif ev.checkpoint == "intent-moved":
            moved = True
    return idx, moved


def milestones(events):
    """First time each ordered checkpoint was reached."""
    seen = {}
    for ev in events:
        if ev.kind == "checkpoint" and ev.checkpoint in ORDERED and ev.checkpoint not in seen:
            seen[ev.checkpoint] = ev.ts
    return [{"checkpoint": c, "ts": seen.get(c)} for c in ORDERED]


@dataclass
class Stream:
    id: str
    orchestrator: str
    header: dict
    derived: dict
    events: list = field(default_factory=list)
    error: str = ""


def read_registry(path):
    with open(path, encoding="utf-8") as f:
        return parse_kv_block(f.read().splitlines())


def orchestrators(root=None):
    root = root or state_root()
    out = []
    for reg in sorted(glob.glob(os.path.join(root, "*", "*", "registry.yaml"))):
        base = os.path.dirname(reg)
        name = os.path.relpath(base, root)
        try:
            data = read_registry(reg)
        except OSError as e:
            data = {"error": str(e)}
        out.append({"name": name, "dir": base, "registry": data})
    return out


def streams(orch, checkpoints):
    out = []
    for sdir in sorted(glob.glob(os.path.join(orch["dir"], "streams", "*"))):
        sid = os.path.basename(sdir)
        st = Stream(sid, orch["name"], {}, {})
        try:
            with open(os.path.join(sdir, "card.md"), encoding="utf-8") as f:
                st.header, st.derived = parse_card(f.read())
        except OSError as e:
            st.error = f"card.md: {e}"
        try:
            with open(os.path.join(sdir, "journal.md"), encoding="utf-8") as f:
                st.events = parse_journal(f.read(), checkpoints)
        except OSError as e:
            st.error = (st.error + "; " if st.error else "") + f"journal.md: {e}"
        out.append(st)
    return out
