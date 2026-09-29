import json
import os
import tempfile
import unittest

from desk import asks, deliver, source, state, terminal, transcript, uploads, view

CHECKPOINTS = state.load_checkpoints(pattern="/nonexistent/*")

CARD = """stream: MER-1
repository: /tmp/orca/workspaces/notifier/MER-1-x
branch: MER-1-x
tracker: MER-1
permission-mode: bypassPermissions
perimeter:
owed by: not-a-header

<!-- derived: begin -->
last checkpoint: pre-merge in by developer, 2026-09-24T21:25:36Z (0m ago)
phase: merge
owed by: the orchestrator
<!-- derived: end -->
"""

JOURNAL = "\n".join([
    "2026-09-24T17:11:59Z\tMER-1\tlaunched\tout\tdeveloper\topened",
    "2026-09-24T17:14:27Z\tMER-1\tspec-drafted\tin\tdeveloper\tspec 9 items",
    "2026-09-24T17:20:00Z\tMER-1\tr1 triage\treport sha256 abc",
    "2026-09-24T18:00:00Z\tMER-1\tpre-publish\tin\tdeveloper\tPR #122 head e9",
    "2026-09-24T18:30:00Z\tMER-1\tintent-moved\tin\tdeveloper\titem 7 amended",
    "not a journal line",
])


class StateTest(unittest.TestCase):
    def test_card_header_stops_at_perimeter_and_reads_derived(self):
        header, derived = state.parse_card(CARD)
        self.assertEqual(header["tracker"], "MER-1")
        self.assertEqual(header["permission-mode"], "bypassPermissions")
        self.assertNotIn("owed by", header)
        self.assertEqual(derived["owed by"], "the orchestrator")
        self.assertEqual(derived["phase"], "merge")

    def test_registry_keys_with_underscores(self):
        self.assertEqual(state.parse_kv_block(["session_id: abc", "transcript:"]),
                         {"session_id": "abc", "transcript": ""})

    def test_journal_splits_checkpoints_and_notes(self):
        events = state.parse_journal(JOURNAL, CHECKPOINTS)
        self.assertEqual([e.kind for e in events], ["checkpoint", "checkpoint", "note", "checkpoint", "checkpoint"])
        self.assertEqual(events[2].text, "r1 triage\treport sha256 abc")

    def test_progress_keeps_position_on_intent_moved(self):
        idx, moved = state.progress(state.parse_journal(JOURNAL, CHECKPOINTS))
        self.assertEqual(state.ORDERED[idx], "pre-publish")
        self.assertTrue(moved)

    def test_milestones_record_first_reach(self):
        ms = {m["checkpoint"]: m["ts"] for m in state.milestones(state.parse_journal(JOURNAL, CHECKPOINTS))}
        self.assertEqual(ms["launched"], "2026-09-24T17:11:59Z")
        self.assertIsNone(ms["pre-merge"])

    def test_a_brief_only_stream_is_prepared_not_broken(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "streams", "MER-9"))
            open(os.path.join(d, "streams", "MER-9", "brief.md"), "w").close()
            [st] = state.streams({"dir": d, "name": "o/n"}, CHECKPOINTS)
            self.assertTrue(st.prepared)
            self.assertEqual(st.error, "")

    def test_plugin_table_is_read_when_present(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "team-skills-orchestrator", "9.9.9", "scripts", "lib")
            os.makedirs(path)
            with open(os.path.join(path, "checkpoints.tsv"), "w") as f:
                f.write("# header\nlaunched\tno\tdesign\tspec-drafted\nnew-one\tyes\tx\ty\n")
            table = state.load_checkpoints(os.path.join(d, "team-skills-orchestrator", "*", "scripts", "lib", "checkpoints.tsv"))
            self.assertEqual(table, {"launched": False, "new-one": True})


MESSAGE = """MER-1 (PR #122) is ready for your look.

| PR | Ask |
|---|---|
| **notifier #122** (MER-1) | look again |
| **go-libs #162** (MER-2) | merge |

Unrelated paragraph.

in flight: MER-3 (fixes) · waiting: ты — приёмка вида #122 (MER-1); по go-libs #162: мерж; два ADR по MER-9 · 2026-09-25 02:04 +04"""


class TranscriptTest(unittest.TestCase):
    def test_owner_items(self):
        _, status = MESSAGE.rsplit("\n", 1)[0], MESSAGE.splitlines()[-1]
        self.assertEqual(transcript.owner_items(status),
                         ["приёмка вида #122 (MER-1)", "по go-libs #162: мерж", "два ADR по MER-9"])

    def test_items_for_other_parties_are_ignored(self):
        self.assertEqual(transcript.owner_items("waiting: MER-3 stream — report · 2026"), [])

    def test_paragraphs_keep_table_header_and_own_rows(self):
        paras = transcript.paragraphs_about(MESSAGE, "MER-1", {"122"})
        self.assertEqual(paras[0], "MER-1 (PR #122) is ready for your look.")
        self.assertIn("| PR | Ask |", paras[1])
        self.assertIn("notifier #122", paras[1])
        self.assertNotIn("go-libs", paras[1])
        self.assertEqual(len(paras), 2)

    def test_transcript_tail_finds_last_status_message(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            for text in ["old\n\nwaiting: ты — old ask · t", MESSAGE, "mid-turn chatter"]:
                f.write(json.dumps({"type": "assistant", "timestamp": "T",
                                    "message": {"content": [{"type": "text", "text": text}]}}) + "\n")
            f.write("{broken json\n")
            f.write(json.dumps({"type": "user", "message": {"content": "waiting: ты — fake · x"}}) + "\n")
        try:
            msgs = transcript.recent_messages(f.name)
            text, status, _ = transcript.last_status(msgs)
            self.assertEqual(text, MESSAGE)
            self.assertTrue(status.startswith("in flight: MER-3"))
            thread = transcript.ask_thread(msgs, lambda i: "MER-1" in i,
                                           lambda t: transcript.paragraphs_about(t, "MER-1", {"122"}))
            self.assertIn("MER-1 (PR #122) is ready", thread["origin"]["text"])
            self.assertNotIn("waiting:", thread["origin"]["text"])
        finally:
            os.unlink(f.name)


class AskThreadTest(unittest.TestCase):
    def msgs(self, *texts):
        return [(t, f"T{n}") for n, t in enumerate(texts)]  # newest first

    def test_origin_is_the_oldest_turn_of_the_run_that_talks_about_it(self):
        msgs = self.msgs(
            "update on MER-1\n\nwaiting: ты — вариант A по MER-1 · t",
            "carry only\n\nwaiting: ты — вариант A по MER-1 · t",
            "MER-1: options A, B\n\nOption A details\n\nwaiting: ты — вариант A по MER-1 · t",
            "before the ask\n\nwaiting: ты — something else · t",
            "still before\n\nwaiting: ты — another thing · t",
            "old MER-1 talk\n\nwaiting: ты — вариант A по MER-1 · t",
        )
        about = lambda t: transcript.paragraphs_about(t, "MER-1", set())
        thread = transcript.ask_thread(msgs, lambda i: "MER-1" in i, about)
        self.assertEqual(thread["origin"]["ts"], "T2")
        self.assertIn("Option A details", thread["origin"]["text"])
        self.assertEqual([u["ts"] for u in thread["updates"]], ["T0"])

    def test_one_folded_turn_does_not_end_the_run(self):
        msgs = self.msgs(
            "now\n\nwaiting: ты — вариант A по MER-1 · t",
            "folded\n\nwaiting: ты — и вопросы выше · t",
            "MER-1: the options\n\nwaiting: ты — вариант A по MER-1 · t",
        )
        about = lambda t: transcript.paragraphs_about(t, "MER-1", set())
        self.assertEqual(transcript.ask_thread(msgs, lambda i: "MER-1" in i, about)["origin"]["ts"], "T2")

    def test_general_ask_matches_by_shared_words(self):
        self.assertTrue(transcript.similar("какие задачи go-libs завести из 11",
                                           "права на пакетное создание, какие задачи go-libs заводить"))
        self.assertFalse(transcript.similar("вид MER-3226 на 20469", "какие задачи go-libs заводить"))

    def test_a_general_ask_keeps_its_whole_section(self):
        msg = ("**Смержено.** всё хорошо\n\n**Вопрос. Какие предложения заводить задачами?**\n\n"
               "Варианты:\n1. первое\n2. второе\n\nРекомендация: 1.\n\n**Дальше.** другое\n\n"
               "waiting: ты — какие предложения заводить задачами · t")
        paras = transcript.paragraphs_matching(msg, "какие предложения заводить задачами")
        self.assertEqual(paras[0], "**Вопрос. Какие предложения заводить задачами?**")
        self.assertIn("Варианты:\n1. первое\n2. второе", paras)
        self.assertIn("Рекомендация: 1.", paras)
        self.assertFalse(any("Дальше" in p or "Смержено" in p for p in paras))

    def test_no_ask_no_thread(self):
        self.assertEqual(transcript.ask_thread(self.msgs("x\n\nwaiting: ты — y · t"), lambda i: "MER-1" in i), {})


SCREEN = """⏺ earlier output
  waiting: orchestrator · reply
──────────────────────────────────────────────
 ☐ pre-publish
│ Вы вставили ответ оркестратора без своего текста.
│ Действовать по нему?
❯ 1. Да, действуй по нему (Recommended)
     Считаю вставку вашим словом: push.
  2. Нет, жди прямого ответа
     Жду прямого ответа.
  3. Type something.
──────────────────────────────────────────────
  4. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel
─────────────────────────────────────── MER-2914 ─""".splitlines()


class TerminalPromptTest(unittest.TestCase):
    def test_parses_question_options_and_hints(self):
        p = terminal.parse_prompt(SCREEN)
        self.assertEqual(p["question"][0], "pre-publish")
        self.assertIn("Действовать по нему?", p["question"])
        self.assertEqual([o["key"] for o in p["options"]], ["1", "2", "3", "4"])
        self.assertEqual(p["options"][0]["label"], "Да, действуй по нему (Recommended)")
        self.assertEqual(p["options"][0]["hint"], "Считаю вставку вашим словом: push.")
        self.assertEqual(p["options"][2]["hint"], "")


class SourceTest(unittest.TestCase):
    def test_only_files_under_the_owner_roots_are_readable(self):
        self.assertEqual(source.allowed("/etc/hosts"), "")
        self.assertEqual(source.allowed("~/.ssh/config"), "")
        self.assertEqual(source.allowed("~/orca/workspaces/../.zshrc"), "")
        self.assertTrue(source.allowed("~/orca/workspaces/notifier/x.go").endswith("/orca/workspaces/notifier/x.go"))

    def test_read_focuses_a_line_within_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f.go")
            with open(path, "w") as f:
                f.write("a\nb\nc\n")
            orig, source.roots = source.roots, lambda: [os.path.realpath(d)]
            try:
                out = source.read(path, 99)
            finally:
                source.roots = orig
            self.assertEqual(out["lines"], ["a", "b", "c"])
            self.assertEqual(out["line"], 3)


class ViewTest(unittest.TestCase):
    def test_bare_pr_number_counts_only_without_other_stream(self):
        self.assertTrue(view._names_stream("по go-libs #162: мерж", "MER-2", {162}))
        self.assertFalse(view._names_stream("приёмка #162 (MER-5)", "MER-2", {"162"}))
        self.assertFalse(view._names_stream("приёмка #1620", "MER-2", {"162"}))
        self.assertTrue(view._names_stream("приёмка вида #122 (MER-1) и #120 (MER-2)", "MER-2", set()))


class UploadsTest(unittest.TestCase):
    PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._dir, uploads.DIR = uploads.DIR, self.tmp.name

    def tearDown(self):
        uploads.DIR = self._dir
        self.tmp.cleanup()

    def test_saves_an_image_whose_bytes_match_its_type(self):
        path = uploads.save(self.PNG, "image/png")
        self.assertTrue(path.endswith(".png"))
        self.assertEqual(uploads.checked([path]), [os.path.realpath(path)])

    def test_refuses_a_mislabelled_or_foreign_file(self):
        with self.assertRaises(ValueError):
            uploads.save(b"<svg/>", "image/png")
        with self.assertRaises(ValueError):
            uploads.save(self.PNG, "image/svg+xml")
        with self.assertRaises(ValueError):
            uploads.checked(["/etc/hosts"])
        with self.assertRaises(ValueError):
            uploads.checked([os.path.join(uploads.DIR, "..", "x.png")])


class DeliverTest(unittest.TestCase):
    def test_images_are_appended_as_paths(self):
        self.assertEqual(deliver.format_reply("MER-1", "см. скрин", ["/u/a.png", "/u/b.png"]),
                         "[desk] MER-1: см. скрин · картинки: /u/a.png /u/b.png")
        self.assertEqual(deliver.format_reply("MER-1", "", ["/u/a.png"]), "[desk] MER-1: картинки: /u/a.png")

    def test_reply_is_one_prefixed_line(self):
        self.assertEqual(deliver.format_reply("MER-1", "принимаю\n\n  мержи \n"), "[desk] MER-1: принимаю / мержи")

    def test_reply_names_the_question_it_answers(self):
        self.assertEqual(deliver.format_reply("MER-1", "Принимаю", asks=["приёмка вида #120"]),
                         "[desk] MER-1 · на «приёмка вида #120»: Принимаю")
        self.assertEqual(deliver.format_reply("", "Принимаю", asks=["два ADR\nпо MER-9"]),
                         "[desk] на «два ADR / по MER-9»: Принимаю")

    def test_note_is_marked_as_a_comment(self):
        self.assertEqual(deliver.format_reply("MER-2914", "без глобального логгера", kind="note"),
                         "[desk] MER-2914 · комментарий: без глобального логгера")
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                deliver.send("s", "", "x", os.path.join(d, "r.jsonl"), kind="note")

    def test_reply_without_task_or_question_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                deliver.send("s", "", "Принимаю", os.path.join(d, "r.jsonl"))

    def test_handle_from_process_environment(self):
        env = "claude ORCA_AGENT_HOOK_ENDPOINT=/Users/x/Library/Application Support/y ORCA_TERMINAL_HANDLE=term_2425f0b2-aa1b ORCA_TAB_ID=z"
        self.assertEqual(deliver.HANDLE_RE.search(env).group(1), "term_2425f0b2-aa1b")

    def test_empty_and_long_replies_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "r.jsonl")
            with self.assertRaises(ValueError):
                deliver.send("s", "MER-1", "  ", log)
            with self.assertRaises(ValueError):
                deliver.send("s", "MER-1", "x" * (deliver.MAX_LEN + 1), log)
            self.assertFalse(os.path.exists(log))


if __name__ == "__main__":
    unittest.main()


class SupervisorTest(unittest.TestCase):
    def test_nudges_and_notifies_on_idle_capacity_silence_unread_and_new_asks(self):
        from datetime import datetime, timedelta, timezone
        from desk import supervisor
        with tempfile.TemporaryDirectory() as d:
            saved = (supervisor.ROOT, supervisor.CONFIG, supervisor.MEMORY, supervisor.EVENTS, asks.ROOT, asks.DIR)
            supervisor.ROOT = asks.ROOT = d
            asks.DIR = os.path.join(d, "asks")
            supervisor.CONFIG, supervisor.MEMORY, supervisor.EVENTS = (os.path.join(d, n) for n in ("c.json", "m.json", "e.jsonl"))
            ago = lambda m: (datetime.now(timezone.utc) - timedelta(minutes=m)).isoformat()
            data = {"orchestrators": [{"name": "o/d", "session_id": "S", "message_ts": ago(30)}],
                    "streams": [{"id": "MER-1", "orchestrator": "o/d", "closed": False, "last_ts": ago(60),
                                 "header": {"repository": "/w/1"}, "prompt": None},
                                {"id": "MER-2", "orchestrator": "o/d", "closed": False, "last_ts": ago(5),
                                 "header": {"repository": "/w/2"}, "prompt": None}]}
            said, told = [], []
            patches = [(supervisor.view, "build", lambda: data),
                       (supervisor.deliver, "agents", lambda max_age=0: [
                           {"sessionId": "S", "status": "idle"}, {"sessionId": "x", "cwd": "/w/1", "status": "idle"}]),
                       (supervisor.deliver, "poke", lambda sid, text: said.append(text) or {"ok": True}),
                       (supervisor, "notify", lambda t, x: told.append(t) or True)]
            originals = [(obj, name, getattr(obj, name)) for obj, name, _ in patches]
            for obj, name, fn in patches:
                setattr(obj, name, fn)
            try:
                a = asks.create("вопрос?", task="MER-9")
                b = asks.create("отвечен?")
                asks.answer(b["id"], "да")
                with open(asks._path(b["id"])) as f:
                    rec = json.load(f)
                rec["answer"]["ts"] = ago(10)
                asks._write(rec)
                supervisor.Supervisor().tick()
                self.assertTrue(any("непрочитанные ответы" in t for t in said))
                self.assertTrue(any("в работе 2 из 4" in t for t in said))
                self.assertTrue(any("MER-1 молчит" in t for t in said))
                self.assertFalse(any("MER-2" in t for t in said))
                self.assertIn(f"Вопрос {a['id']} · MER-9", told)
                said.clear(); told.clear()
                supervisor.Supervisor().tick()
                self.assertEqual((said, told), ([], []), "a second tick inside the rate window stays quiet")
            finally:
                for obj, name, fn in originals:
                    setattr(obj, name, fn)
                (supervisor.ROOT, supervisor.CONFIG, supervisor.MEMORY, supervisor.EVENTS, asks.ROOT, asks.DIR) = saved


class UsageTest(unittest.TestCase):
    def test_scan_buckets_calls_per_hour_and_keeps_last_context(self):
        import json as _json, tempfile
        from desk import usage
        lines = [
            {"type": "user", "cwd": "/tmp/repo", "timestamp": "2026-09-28T10:00:00Z", "message": {"content": "You are the stream session MER-1, launched"}},
            {"type": "assistant", "timestamp": "2026-09-28T10:05:00Z", "message": {"model": "claude-opus-5-5", "usage": {"input_tokens": 10, "output_tokens": 100, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 50000}}},
            {"type": "assistant", "timestamp": "2026-09-28T11:05:00Z", "message": {"model": "claude-opus-5-5", "usage": {"input_tokens": 20, "output_tokens": 200, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 80000}}},
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(_json.dumps(l) for l in lines) + "\n" + '{"type":"assistant","timestamp":"2026-09-28T12:00:00Z","message":{"usage":{"output_tokens":5')
            path = f.name
        rec = {}
        usage._scan(path, rec)
        self.assertEqual(rec["cwd"], "/tmp/repo")
        self.assertTrue(rec["first"].startswith("You are the stream session MER-1"))
        self.assertEqual(rec["hours"]["2026-09-28T10"], [100, 10, 1000, 50000, 1])
        self.assertEqual(rec["hours"]["2026-09-28T11"], [200, 20, 0, 80000, 1])
        self.assertEqual(rec["last_ctx"], 80020)
        self.assertEqual(rec["model"], "claude-opus-5-5")
        # The torn last line waits for the next read: the offset stops before it.
        with open(path, "rb") as fh:
            self.assertEqual(fh.read()[rec["offset"]:][:8], b'{"type":')
        self.assertAlmostEqual(usage.weight([100, 10, 1000, 50000]), 100 * 5 + 10 + 1250 + 5000)


class EpicCountTest(unittest.TestCase):
    ROWS = [
        {"id": "MER-1", "state": "Done", "completed": ""},
        {"id": "MER-2", "state": "In Progress", "completed": ""},
        {"id": "MER-3", "state": "Backlog", "completed": ""},
        {"id": "MER-4", "state": "Backlog", "completed": ""},
        {"id": "MER-5", "state": "Canceled", "completed": ""},
    ]

    def test_started_is_linear_progress_plus_open_streams_without_double_counting(self):
        from datetime import datetime
        from desk import epics
        since = datetime.now().astimezone()
        got = epics._count(self.ROWS, since, {"MER-2", "MER-3"})
        self.assertEqual((got["total"], got["done"], got["started"], got["dead"]), (4, 1, 2, 1))

    def test_a_done_or_canceled_card_with_an_open_stream_is_not_working(self):
        from datetime import datetime
        from desk import epics
        since = datetime.now().astimezone()
        got = epics._count(self.ROWS, since, {"MER-1", "MER-5"})
        self.assertEqual(got["started"], 1)  # only MER-2 (In Progress in Linear)


class EpicCacheTest(unittest.TestCase):
    def setUp(self):
        from desk import epics
        self.epics = epics
        self.key = ("test", "epic-cache")
        epics._cache.pop(self.key, None)
        epics._pending.discard(self.key)

    def tearDown(self):
        self.epics._cache.pop(self.key, None)
        self.epics._pending.discard(self.key)

    def test_a_failed_refresh_keeps_the_last_good_answer_and_retries_soon(self):
        import time
        e = self.epics
        e._cache[self.key] = (time.time() - 10000, {"rows": [1]})

        def boom():
            raise RuntimeError("linear timed out")
        e._refresh(self.key, boom, 300)
        ts, value = e._cache[self.key]
        self.assertEqual(value, {"rows": [1]})
        self.assertLess(time.time() - ts, 300)          # not stale enough to refetch every request...
        self.assertGreater(time.time() - ts, 300 - 60)  # ...but due again within the retry window

    def test_a_failed_first_read_is_recorded_as_an_error(self):
        e = self.epics

        def boom():
            raise RuntimeError("no network")
        e._refresh(self.key, boom, 300)
        self.assertIn("error", e._cache[self.key][1])

    def test_a_good_refresh_replaces_the_answer(self):
        import time
        e = self.epics
        e._cache[self.key] = (time.time() - 10000, {"rows": [1]})
        e._refresh(self.key, lambda: {"rows": [1, 2]}, 300)
        self.assertEqual(e._cache[self.key][1], {"rows": [1, 2]})

    def test_a_card_that_errored_makes_the_chain_unknown_not_short(self):
        import time
        e = self.epics
        e._cache[("card", "T-1")] = (time.time(), {"id": "T-1", "parent": "T-2"})
        e._cache[("card", "T-2")] = (time.time(), {"error": "timeout"})
        try:
            self.assertIsNone(e._ancestry("T-1"))
        finally:
            e._cache.pop(("card", "T-1"), None)
            e._cache.pop(("card", "T-2"), None)


class LinksRateLimitTest(unittest.TestCase):
    def setUp(self):
        from desk import links
        self.links = links
        self.key = ("test", "links-cache")
        links._cache.pop(self.key, None)
        links._pending.discard(self.key)
        links._pause["until"] = 0.0

    def tearDown(self):
        self.links._cache.pop(self.key, None)
        self.links._pending.discard(self.key)
        self.links._pause["until"] = 0.0

    def test_a_rate_limit_pauses_linear_and_serves_the_stale_answer(self):
        import time
        l = self.links
        l._cache[self.key] = (time.time() - 10000, {"title": "kept"})

        def limited():
            raise RuntimeError("Rate limit exceeded. Only 2500 requests are allowed per 1 hour.")
        l._refresh(self.key, limited, 300)
        self.assertTrue(l.paused())
        self.assertEqual(l._cache[self.key][1], {"title": "kept"})
        # while paused, a Linear read starts no fetch at all and still answers with what it has
        self.assertEqual(l.cached(self.key, limited, needs_linear=True), {"title": "kept"})
        self.assertNotIn(self.key, l._pending)

    def test_other_failures_do_not_pause(self):
        l = self.links

        def broken():
            raise RuntimeError("connection reset")
        l._refresh(self.key, broken, 300)
        self.assertFalse(l.paused())
