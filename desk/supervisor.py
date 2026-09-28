"""A deterministic watchdog: notices what an LLM orchestrator forgets and says so, rate-limited.

It never acts on the work itself. It notifies the owner (macOS notifications) and nudges the
orchestrator with one short line in its terminal; every alert goes to an events log the page shows.
"""

import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime

from . import asks, deliver, usage, view

ROOT = asks.ROOT
CONFIG = os.path.join(ROOT, "config.json")
MEMORY = os.path.join(ROOT, "supervisor.json")
EVENTS = os.path.join(ROOT, "events.jsonl")
DEFAULTS = {"wip": 4, "nudge": True, "notify": True, "interval": 60,
            "idle_min": 10, "silent_min": 45, "answer_min": 5, "consumed_hours": 6, "ctx_limit": 250000}
_lock = threading.Lock()


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, data):
    os.makedirs(ROOT, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def config():
    return {**DEFAULTS, **_load(CONFIG, {})}


def set_config(patch):
    cfg = config()
    old_wip = cfg.get("wip")
    for k, v in patch.items():
        if k in ("nudge", "notify"):
            cfg[k] = bool(v)
        elif k == "wip":
            cfg[k] = max(1, min(8, int(v)))
        elif k == "ctx_limit":
            cfg[k] = max(100000, min(900000, int(v)))
    _save(CONFIG, {k: cfg[k] for k in ("wip", "nudge", "notify", "ctx_limit")})
    if "wip" in patch and cfg["wip"] != old_wip:
        _mandate_wip(cfg["wip"])
    return cfg


def _mandate_wip(n):
    """The site's wip control is cosmetic until this runs: the orchestrator's real cap lives in
    its own queue.md as an owner rule, read only from mandates.md — this is the one write path
    that reaches it, so a click here is the only thing that changes what actually launches."""
    orch_bin = shutil.which("orch") or os.path.expanduser("~/.local/bin/orch")
    text = (f"От владельца {datetime.now().astimezone().strftime('%Y-%m-%d')}: лимит одновременных "
            f"стримов — {n} (изменено на desk).")
    try:
        subprocess.run([orch_bin, "mandate", text], capture_output=True, text=True, timeout=15, check=True)
    except Exception as e:
        _event("error", f"не удалось записать мандат лимита стримов: {str(e)[:200]}")
        return
    _event("nudge", f"мандат записан: лимит стримов {n}")
    for orch in view.build_orchestrators():
        if not orch.get("session_id"):
            continue
        try:
            ok = deliver.poke(orch["session_id"],
                              f"[desk] владелец поменял лимит параллельных стримов на {n} — "
                              f"мандат записан в mandates.md, применяй.").get("ok")
        except Exception:
            ok = False
        _event("nudge", f"{orch['name']}: лимит стримов {n}", sent=ok)


def events(limit=30):
    if not os.path.exists(EVENTS):
        return []
    with open(EVENTS, encoding="utf-8") as f:
        lines = f.readlines()[-limit:]
    out = []
    for l in lines:
        try:
            out.append(json.loads(l))
        except ValueError:
            continue
    return list(reversed(out))


def _event(kind, text, sent=None):
    rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "kind": kind, "text": text}
    if sent is not None:
        rec["sent"] = sent
    os.makedirs(ROOT, exist_ok=True)
    with _lock, open(EVENTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def notify(title, text):
    """A macOS notification; arguments go through argv, never into the script source."""
    script = ['on run argv', 'display notification (item 1 of argv) with title (item 2 of argv) sound name "Glass"', 'end run']
    args = ["osascript"] + sum((["-e", s] for s in script), []) + [text[:220], title[:80]]
    try:
        subprocess.run(args, capture_output=True, timeout=10)
        return True
    except Exception:
        return False


class Supervisor:
    def __init__(self):
        self.mem = _load(MEMORY, {"last": {}})

    def _due(self, key, every_min):
        last = self.mem["last"].get(key, 0)
        if time.time() - last < every_min * 60:
            return False
        self.mem["last"][key] = time.time()
        return True

    def _tell_owner(self, key, every_min, title, text, cfg):
        if cfg["notify"] and self._due("notify:" + key, every_min):
            notify(title, text)
            _event("notify", f"{title}: {text}")

    def _nudge(self, key, every_min, orch, text, cfg):
        if not (cfg["nudge"] and orch.get("session_id") and self._due("nudge:" + key, every_min)):
            return
        try:
            ok = deliver.poke(orch["session_id"], "[desk] надсмотрщик: " + text).get("ok")
        except Exception:
            ok = False
        _event("nudge", text, sent=ok)

    def tick(self):
        cfg = config()
        data = view.build()
        agents = {a.get("sessionId"): a for a in deliver.agents(max_age=0)}
        live = [a for a in asks.all_asks() if a["status"] in ("open", "answered")]
        for orch in data["orchestrators"]:
            agent = agents.get(orch["session_id"])
            if not agent:
                self._tell_owner("orch-gone:" + orch["name"], 60, "Оркестратор не запущен",
                                 f"{orch['name']}: сессии {orch['session_id'][:8]} нет среди запущенных", cfg)
                continue
            idle = agent.get("status") == "idle"
            # A context past the limit makes every turn cost that much again; only the owner can /compact.
            ctx, model = usage.orchestrator_context(orch["session_id"])
            if ctx > cfg["ctx_limit"]:
                self._tell_owner("ctx:" + orch["name"], 60, "Контекст оркестратора раздулся",
                                 f"{orch['name']}: {ctx // 1000}k токенов на вызов ({model or 'модель ?'}) — в его окне набери /compact", cfg)
            unread = [a for a in live if a["status"] == "answered"
                      and time.time() - datetime.fromisoformat(a["answer"]["ts"]).timestamp() > cfg["answer_min"] * 60]
            if idle and unread:
                self._nudge("unread", 15, orch, "есть непрочитанные ответы владельца ("
                            + ", ".join(a["id"] for a in unread) + "): выполни `desk answers`.", cfg)
            # Read but never closed: the owner's tracker stays on "прочитал" until `desk done` or `withdraw`.
            stale = [a for a in asks.all_asks() if a["status"] == "consumed"
                     and time.time() - datetime.fromisoformat(a["history"][-1]["ts"]).timestamp() > cfg["consumed_hours"] * 3600]
            if stale:
                self._nudge("stale-consumed", 6 * 60, orch, "ответы владельца прочитаны, но вопросы не закрыты дольше "
                            f"{cfg['consumed_hours']} ч (" + ", ".join(a["id"] for a in stale)
                            + "): для каждого `desk done ask-N --note …` или `desk withdraw`, либо напиши, чем заблокирован.", cfg)
            streams = [s for s in data["streams"] if s["orchestrator"] == orch["name"] and not s["closed"]]
            running = [s for s in streams if not s.get("prepared")]
            last_turn = orch.get("message_ts") or ""
            quiet_min = (time.time() - datetime.fromisoformat(last_turn.replace("Z", "+00:00")).timestamp()) / 60 if last_turn else 0
            if idle and len(running) < cfg["wip"] and quiet_min > cfg["idle_min"]:
                self._nudge("wip", 30, orch, f"в работе {len(running)} из {cfg['wip']} стримов, ты простаиваешь "
                            f"{int(quiet_min)} мин: бери следующую задачу из очереди.", cfg)
            for s in running:
                session = next((a for a in agents.values() if a.get("cwd") == s["header"].get("repository")), None)
                prompt = s.get("prompt")
                if prompt and not prompt.get("error"):
                    q = (prompt.get("question") or ["вопрос в терминале"])
                    self._tell_owner("prompt:" + s["id"] + ":" + (q[1] if len(q) > 1 else q[0])[:60], 24 * 60,
                                     f"{s['id']} ждёт ввода в терминале", q[1] if len(q) > 1 else q[0], cfg)
                if not s["last_ts"] or (session and session.get("status") != "idle"):
                    continue
                if any(a.get("task") == s["id"] for a in live):
                    continue
                silent = (time.time() - datetime.fromisoformat(s["last_ts"].replace("Z", "+00:00")).timestamp()) / 60
                if silent > cfg["silent_min"]:
                    self._nudge("silent:" + s["id"], 60, orch, f"{s['id']} молчит {int(silent)} мин, сессия стрима "
                                f"{'простаивает' if session else 'не найдена'}: проверь, не завис ли.", cfg)
        for a in asks.all_asks():
            if a["status"] == "open":
                self._tell_owner("new-ask:" + a["id"], 10 ** 7, "Вопрос " + a["id"] + (f" · {a['task']}" if a.get("task") else ""),
                                 a["question"], cfg)
        _save(MEMORY, self.mem)


def start():
    sup = Supervisor()

    def loop():
        time.sleep(20)
        usage.refresh_async()
        while True:
            try:
                sup.tick()
            except Exception as e:
                _event("error", f"надсмотрщик упал на проверке: {str(e)[:200]}")
            time.sleep(config()["interval"])

    threading.Thread(target=loop, daemon=True).start()
