import json
import os
import tempfile
import unittest

from desk import deliver, state, terminal, transcript, tsoq, uploads, view

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
        finally:
            os.unlink(f.name)


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


class ViewTest(unittest.TestCase):
    def test_a_question_goes_to_the_stream_it_names_or_to_the_general_list(self):
        out = {"streams": [{"id": "MER-1"}, {"id": "MER-2"}]}
        qs = [{"id": "q-1", "stream": "MER-1", "title": "a"}, {"id": "q-2", "stream": "", "title": "про MER-2: выбрать"},
              {"id": "q-3", "stream": "MER-9", "title": "чужой"}]
        view.attach_questions(out, qs)
        self.assertEqual([q["id"] for q in out["streams"][0]["questions"]], ["q-1"])
        self.assertEqual([q["id"] for q in out["streams"][1]["questions"]], ["q-2"])
        self.assertEqual([q["id"] for q in out["general_questions"]], ["q-3"])


class TsoqTest(unittest.TestCase):
    LIST = ("q-3  open      orchestrator/developer  MER-3370  2h  Принять D2 и поправку?\n"
            "q-4  answered  orchestrator/developer  -         5m  Вопрос без задачи\n"
            "garbage line\n")
    SHOW = ("q-3  open  orchestrator/developer  (session abc-123)\n"
            "title: Принять D2?\n"
            "kind: choice  stream: MER-3370  checkpoint: pre-merge  deadline: -  free text: false\n"
            "context:\n  Первая строка.\n  \n  Вторая строка.\n"
            "options:\n"
            "  A  Принять — стрим сменит статус  (recommended: решение описывает срез)\n"
            "  B  Оставить — решение остаётся proposed\n"
            "answer: A via web, words: ок\n"
            "delivery (answer): delivered after 1 attempt(s)\n"
            "events:\n  question.asked  2026-10-07T10:00:00Z\n")

    def test_list_rows(self):
        rows = tsoq.parse_list(self.LIST)
        self.assertEqual([(r["id"], r["status"], r["stream"], r["title"]) for r in rows],
                         [("q-3", "open", "MER-3370", "Принять D2 и поправку?"), ("q-4", "answered", "", "Вопрос без задачи")])

    def test_show_fields_options_and_answer(self):
        q = tsoq.parse_show(self.SHOW)
        self.assertEqual((q["id"], q["status"], q["stream"], q["checkpoint"], q["deadline"]), ("q-3", "open", "MER-3370", "pre-merge", ""))
        self.assertEqual(q["context"], "Первая строка.\n\nВторая строка.")
        self.assertEqual([(o["key"], o["recommended"]) for o in q["options"]], [("A", True), ("B", False)])
        self.assertEqual((q["recommend"], q["why"]), ("A", "решение описывает срез"))
        self.assertEqual(q["answer"], {"option": "A", "surface": "web", "text": "ок", "request": False})

    def test_only_live_questions_are_listed(self):
        rows = "q-1  done  o/d  -  1d  старый\nq-2  open  o/d  MER-1  1m  живой\nq-3  withdrawn  o/d  -  1d  снят\n"
        self.assertEqual([q["id"] for q in tsoq.questions(run=lambda *_a, **_k: (0, rows, ""))], ["q-2"])

    def test_answer_codes(self):
        self.assertTrue(tsoq.answer("q-1", option="A", run=lambda *_a, **_k: (0, "delivered", ""))["acked"])
        r = tsoq.answer("q-1", text="words", run=lambda *_a, **_k: (3, "", "held"))
        self.assertEqual((r["ok"], r["acked"]), (True, False))
        self.assertFalse(tsoq.answer("q-1", option="A", run=lambda *_a, **_k: (1, "", "refused"))["ok"])
        with self.assertRaises(ValueError):
            tsoq.answer("1", option="A")
        with self.assertRaises(ValueError):
            tsoq.answer("q-1")

    def test_send_codes(self):
        self.assertTrue(tsoq.send("orchestrator/developer", "hi", run=lambda *_a, **_k: (0, "", ""))["ok"])
        self.assertTrue(tsoq.send("orchestrator/developer", "hi", run=lambda *_a, **_k: (3, "", ""))["ok"])
        r = tsoq.send("orchestrator/developer", "hi", run=lambda *_a, **_k: (5, "", ""))
        self.assertEqual((r["ok"], r["code"]), (False, 5))
        with self.assertRaises(ValueError):
            tsoq.send("developer", "hi")


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


class EpicCountTest(unittest.TestCase):
    ROWS = [
        {"id": "MER-1", "state": "Done", "completed": ""},
        {"id": "MER-2", "state": "In Progress", "completed": ""},
        {"id": "MER-3", "state": "Backlog", "completed": ""},
        {"id": "MER-4", "state": "Backlog", "completed": ""},
        {"id": "MER-5", "state": "Canceled", "completed": ""},
    ]

    def test_epics_are_not_counted_as_tasks(self):
        from datetime import datetime
        from desk import epics
        rows = [{"id": "E", "state": "Done", "kids": True, "completed": None},
                {"id": "T1", "state": "Done", "kids": False, "completed": None},
                {"id": "T2", "state": "Backlog", "kids": False, "completed": None}]
        c = epics._count(rows, datetime.now().astimezone())
        self.assertEqual((c["total"], c["done"]), (2, 1))

    def test_started_is_the_cards_with_an_open_stream_only(self):
        from datetime import datetime
        from desk import epics
        since = datetime.now().astimezone()
        got = epics._count(self.ROWS, since, {"MER-2", "MER-3"})
        self.assertEqual((got["total"], got["done"], got["started"], got["dead"]), (4, 1, 2, 1))  # MER-2 In Progress without a stream does not count
        self.assertEqual(epics._count(self.ROWS, since, set())["started"], 0)

    def test_a_done_or_canceled_card_with_an_open_stream_is_not_working(self):
        from datetime import datetime
        from desk import epics
        since = datetime.now().astimezone()
        got = epics._count(self.ROWS, since, {"MER-1", "MER-5"})
        self.assertEqual(got["started"], 0)


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


class EpicTrackersTest(unittest.TestCase):
    def test_free_text_in_a_header_is_not_a_card_id(self):
        from desk import epics
        s = {"id": "MER-3690-S4", "header": {"tracker": "MER-3690-S4 (notifier step) MER-3690, MER-3711"}}
        self.assertEqual(epics._trackers(s), ["MER-3690", "MER-3711"])
        self.assertEqual(epics._trackers({"id": "MER-4170", "header": {}}), ["MER-4170"])


class DeliverViaTsoTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self._tso, self._term = tsoq.send, deliver._terminal_send

    def tearDown(self):
        tsoq.send, deliver._terminal_send = self._tso, self._term

    def test_a_delivered_message_does_not_touch_the_terminal(self):
        tsoq.send = lambda name, text: {"ok": True, "code": 0, "message": "ok"}
        deliver._terminal_send = lambda *_a: self.fail("terminal used")
        out = deliver._deliver("sid", "orchestrator/developer", "line")
        self.assertEqual((out["via"], out["ok"]), ("tso", True))

    def test_nobody_under_tso_falls_back_to_the_terminal(self):
        tsoq.send = lambda name, text: {"ok": False, "code": 5, "message": "nobody"}
        deliver._terminal_send = lambda sid, line: {"ok": True, "target": {}, "stages": []}
        out = deliver._deliver("sid", "orchestrator/developer", "line")
        self.assertEqual((out["via"], out["ok"]), ("terminal", True))

    def test_a_refusal_other_than_nobody_is_reported_not_retyped(self):
        tsoq.send = lambda name, text: {"ok": False, "code": 1, "message": "refused"}
        deliver._terminal_send = lambda *_a: self.fail("terminal used")
        out = deliver._deliver("sid", "orchestrator/developer", "line")
        self.assertEqual((out["via"], out["ok"], out["error"]), ("tso", False, "refused"))


class StopHookTest(unittest.TestCase):
    def test_a_question_without_a_tso_record_blocks_and_a_covered_one_passes(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("stop_asks", os.path.join(os.path.dirname(__file__), "..", "hooks", "stop_asks.py"))
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        live = [{"id": "q-5", "stream": "MER-1", "title": "Принять D2?"}]
        self.assertTrue(hook.covered("приёмка D2 по MER-1", live))
        self.assertTrue(hook.covered("вопрос q-9", live))
        self.assertFalse(hook.covered("совсем другое про MER-7", live))


if __name__ == "__main__":
    unittest.main()
