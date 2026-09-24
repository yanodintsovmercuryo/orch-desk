import json
import os
import tempfile
import unittest

from desk import deliver, state, transcript, view

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
            req = transcript.request_for(msgs, "MER-1", {"122"})
            self.assertEqual(len(req), 1)
        finally:
            os.unlink(f.name)


class ViewTest(unittest.TestCase):
    def test_bare_pr_number_counts_only_without_other_stream(self):
        self.assertTrue(view._names_stream("по go-libs #162: мерж", "MER-2", 162))
        self.assertFalse(view._names_stream("приёмка #162 (MER-5)", "MER-2", 162))
        self.assertFalse(view._names_stream("приёмка #1620", "MER-2", 162))
        self.assertTrue(view._names_stream("приёмка вида #122 (MER-1) и #120 (MER-2)", "MER-2", None))


class DeliverTest(unittest.TestCase):
    def test_reply_is_one_prefixed_line(self):
        self.assertEqual(deliver.format_reply("MER-1", "принимаю\n\n  мержи \n"), "[desk] MER-1: принимаю / мержи")
        self.assertEqual(deliver.format_reply("", "ok"), "[desk] ok")

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
