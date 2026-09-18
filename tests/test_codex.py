"""Synthetic rollout fixtures only; no access to a user's Codex home."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/project-viz/scripts"))
from project_viz_runtime.codex import Collector, _inside


def row(kind, payload):
    return {"timestamp": "2024-01-01T00:00:00Z", "type": kind, "payload": payload}


def message(role, text, **extra):
    return row("response_item", {"type": "message", "role": role, "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}], **extra})


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.home = self.root / ".codex"
        (self.home / "sessions/2024/01").mkdir(parents=True)
        (self.home / "archived_sessions").mkdir()
        self.events = []
        self.state = self.root / "state"

    def source(self, sid, cwd=None, events=(), parent=None, archived=False):
        path = self.home / ("archived_sessions" if archived else "sessions/2024/01") / f"rollout-{sid}.jsonl"
        metadata = {"id": sid, "cwd": str(cwd or self.project)}
        if parent:
            metadata["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
        self.append(path, row("session_meta", metadata), *events)
        return path

    @staticmethod
    def append(path, *records, complete=True):
        with path.open("a", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False) + ("\n" if complete else ""))

    def collector(self, callback=None, state=None):
        collector = Collector(self.project, self.home, state or self.state, callback or self.events.append)
        self.addCleanup(lambda: collector._scan_iterator.close() if collector._scan_iterator is not None else None)
        return collector

    def drain(self, collector, limit=300):
        for _ in range(limit):
            diagnostics = collector.import_history()
            if diagnostics["historyComplete"]:
                return diagnostics
        self.fail(f"Import did not finish: {diagnostics}")

    def test_fallback_discovers_old_and_archived_sources_without_unrelated_project(self):
        self.source("old", events=[message("user", "Audit old implementation")])
        self.source("archived", cwd=self.project / "src", archived=True, events=[message("assistant", "Checking historical inputs", channel="commentary")])
        self.source("unrelated", cwd=self.root / "other", events=[message("user", "Mention " + str(self.project))])
        collector = self.collector()
        diagnostics = self.drain(collector)
        self.assertEqual({event["session_id"] for event in self.events}, {"old", "archived"})
        self.assertEqual(diagnostics["sources"], 2)
        self.assertTrue(diagnostics["fromStart"])
        self.assertTrue(all(event["source"]["line"] == 2 for event in self.events))

    def test_partial_line_does_not_advance_and_restart_does_not_duplicate(self):
        path = self.source("partial", events=[row("turn_context", {"turn_id": "turn-one"}), message("user", "Check encoding")])
        collector = self.collector()
        self.drain(collector)
        before = path.stat().st_size
        self.append(path, row("response_item", {"type": "function_call", "call_id": "late-call", "name": "exec_command", "arguments": '{"cmd":"echo ok"}'}), complete=False)
        collector.poll()
        self.assertEqual(collector.sources["partial"]["offset"], before)
        self.assertFalse(collector.diagnostics()["historyComplete"])
        self.assertEqual(len(self.events), 1)
        restarted = self.collector()
        restarted.poll()
        self.assertEqual(len(self.events), 1)
        with path.open("a", encoding="utf-8") as stream:
            stream.write("\n")
        self.drain(restarted)
        self.assertEqual(len(self.events), 2)
        call = self.events[-1]
        self.assertEqual(call["source"]["byte_offset"], before)
        self.assertEqual(call["call_id"], "late-call")
        self.append(path, row("turn_context", {"turn_id": "next-turn"}), row("response_item", {"type": "function_call_output", "call_id": "late-call", "output": "Process exited with code 0\nOK"}))
        self.drain(restarted)
        result = self.events[-1]
        self.assertEqual((result["type"], result["call_id"], result["turn_id"]), ("result", "late-call", "next-turn"))
        self.assertEqual(result["status"], "completed")
        self.assertNotEqual(result["id"], call["id"])
        self.drain(self.collector())
        self.assertEqual(len(self.events), 3)

    def test_one_megabyte_budget_resumes_without_skipping_long_complete_line(self):
        self.source("long", events=[message("user", "x" * 8000), message("assistant", "y" * 8000, channel="final")])
        collector = self.collector()
        collector.READ_BYTES = 1000
        first = collector.poll()
        self.assertLessEqual(first["bytesRead"], 1000)
        self.assertFalse(first["historyComplete"])
        self.drain(collector)
        self.assertEqual([event["type"] for event in self.events], ["goal", "progress"])
        ids = [event["id"] for event in self.events]
        replay = []
        self.drain(self.collector(callback=replay.append, state=self.root / "replay-state"))
        self.assertEqual([event["id"] for event in replay], ids)

    def test_versioned_read_only_sqlite_discovers_old_rows_and_parent_chain(self):
        child = self.source("child", cwd=self.root / "elsewhere", events=[message("assistant", "Check delegated object", channel="commentary")])
        parent = self.source("parent", events=[message("user", "Audit the module")])
        other = self.source("other", cwd=self.root / "other", events=[message("user", "Unrelated")])
        db_path = self.home / "state_27.sqlite"
        with sqlite3.connect(db_path) as database:
            database.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,cwd TEXT,updated_at INTEGER,source TEXT)")
            database.execute("CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT)")
            database.executemany("INSERT INTO threads VALUES (?,?,?,?,?)", [("child", str(child), str(self.root / "elsewhere"), 3, None), ("parent", str(parent), str(self.project), 2, None), ("other", str(other), str(self.root / "other"), 1, None)])
            database.execute("INSERT INTO thread_spawn_edges VALUES (?,?)", ("parent", "child"))
        digest = hashlib.sha256(db_path.read_bytes()).hexdigest()
        collector = self.collector()
        collector.DB_BATCH = 1
        self.drain(collector)
        self.assertEqual({event["session_id"] for event in self.events}, {"parent", "child"})
        self.assertEqual(next(event for event in self.events if event["session_id"] == "child")["parent_session_id"], "parent")
        self.assertEqual(hashlib.sha256(db_path.read_bytes()).hexdigest(), digest)

    def test_metadata_parent_chain_is_order_independent_and_unassociated_child_excluded(self):
        self.source("child", cwd=self.root / "external", parent="parent", events=[message("assistant", "Read delegated data")])
        self.source("orphan", cwd=self.root / "other", parent="missing", events=[message("user", "Other work")])
        self.source("parent", events=[message("user", "Review project")])
        collector = self.collector()
        self.drain(collector)
        self.assertEqual({event["session_id"] for event in self.events}, {"parent", "child"})
        self.assertEqual(collector.diagnostics()["pendingLineage"], 1)

    def test_public_filtering_and_redaction_before_callback(self):
        secret = "sk-proj-" + "a" * 30
        self.source("safe", events=[
            message("system", "SYSTEM_MARKER"), message("developer", "DEVELOPER_MARKER"),
            message("assistant", "REASONING_MARKER", channel="analysis"),
            row("response_item", {"type": "reasoning", "encrypted_content": "PRIVATE_MARKER"}),
            message("user", "<environment_context>ENV_MARKER</environment_context>Check the parser"),
            message("user", "# AGENTS.md instructions\nINSTRUCTION_MARKER"),
            message("assistant", "Inspect " + secret + " Bearer abcdefghijklmnop", channel="commentary"),
            row("event_msg", {"type": "task_complete", "turn_id": "turn"}),
        ])
        self.drain(self.collector())
        encoded = json.dumps(self.events)
        for forbidden in (secret, "abcdefghijklmnop", "SYSTEM_MARKER", "DEVELOPER_MARKER", "REASONING_MARKER", "PRIVATE_MARKER", "ENV_MARKER", "INSTRUCTION_MARKER"):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual([event["type"] for event in self.events], ["goal", "progress", "turn_finished"])
        self.assertEqual(self.events[-1]["status"], "completed")

    def test_rotation_truncation_and_project_identity(self):
        path = self.source("rotating", events=[message("user", "An initial task with a longer description")])
        collector = self.collector()
        self.drain(collector)
        initial_id = self.events[0]["id"]
        path.write_text("", encoding="utf-8")
        self.source("rotating", events=[message("user", "Replacement")])
        self.drain(collector)
        self.assertNotEqual(self.events[-1]["id"], initial_id)
        self.assertEqual(collector.diagnostics()["counters"]["sourceResets"], 1)
        count = len(self.events)
        self.drain(self.collector())
        self.assertEqual(len(self.events), count)
        with self.assertRaisesRegex(ValueError, "different project"):
            Collector(self.root / "other", self.home, self.state, self.events.append)

    def test_malformed_source_and_unknown_database_schema_are_diagnostic(self):
        path = self.source("bad", events=[message("user", "Still a useful task")])
        with path.open("a", encoding="utf-8") as stream:
            stream.write("not JSON\n")
        with sqlite3.connect(self.home / "state_91.sqlite") as database:
            database.execute("CREATE TABLE threads(wrong_column TEXT)")
        collector = self.collector()
        result = self.drain(collector)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(result["counters"]["unsupportedDatabaseSchemas"], 1)
        self.assertEqual(result["counters"]["malformedRecords"], 1)
        self.assertTrue(any(error["code"] == "malformedRecords" for error in result["errors"]))

    def test_ingest_failure_replays_same_id_and_keeps_cursor(self):
        self.source("retry", events=[message("user", "Retry this record")])
        attempts = []
        def failing(event):
            attempts.append(event["id"])
            if len(attempts) == 1:
                raise RuntimeError("sensitive error must not be copied")
            self.events.append(event)
        collector = self.collector(callback=failing)
        first = collector.poll()
        self.assertFalse(first["historyComplete"])
        self.drain(collector)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(len(self.events), 1)
        self.assertNotIn("sensitive", json.dumps(collector.diagnostics()))

    def test_windows_metadata_path_comparison_is_component_aware(self):
        self.assertTrue(_inside(r"C:\Work\Project\src", "c:/work/project"))
        self.assertTrue(_inside(r"\\?\C:\Work\Project", "c:/work/project"))
        self.assertFalse(_inside("C:/work/project-other", r"c:\work\project"))
        self.assertFalse(_inside("D:/work/project", r"c:\work\project"))

    def test_discovery_and_import_resume_before_first_scan_finishes(self):
        for i in range(12):
            self.source(f"old-{i}", events=[message("user", f"Inspect old object {i}")])
        collector = self.collector()
        collector.DISCOVERY_ENTRIES = 2
        collector.READ_BYTES = 200
        first = collector.poll()
        self.assertFalse(first["discoveryComplete"])
        restarted = self.collector()
        restarted.DISCOVERY_ENTRIES = 2
        restarted.READ_BYTES = 200
        self.drain(restarted)
        self.assertEqual(len(self.events), 12)
        self.assertEqual(len({event["id"] for event in self.events}), 12)

    def test_shortened_partial_tail_is_discarded_on_truncation(self):
        path = self.source("partial-reset", events=[message("user", "Original")])
        collector = self.collector()
        self.drain(collector)
        complete_prefix = path.read_bytes()
        self.append(path, message("assistant", "A long unfinished output" * 100), complete=False)
        collector.poll()
        path.write_bytes(complete_prefix)
        self.append(path, message("assistant", "A replacement output", channel="final"))
        self.drain(collector)
        self.assertEqual(self.events[-1]["text"], "A replacement output")
        self.assertEqual(collector.diagnostics()["counters"]["sourceResets"], 1)
        self.assertFalse(any("unfinished" in event["text"] for event in self.events))

    def test_repeated_sink_failure_never_skips_queued_complete_records(self):
        self.source("sink", events=[message("user", f"Object {i}") for i in range(30)])
        blocked = True
        def sink(event):
            if blocked:
                raise RuntimeError("store unavailable")
            self.events.append(event)
        collector = self.collector(callback=sink)
        collector.MAX_LINE_BYTES = 1024
        collector.poll()
        position = collector.sources["sink"]["offset"]
        bytes_read = collector.diagnostics()["bytesRead"]
        for _ in range(5):
            collector.poll()
        self.assertEqual(collector.sources["sink"]["offset"], position)
        self.assertEqual(collector.diagnostics()["bytesRead"], bytes_read)
        blocked = False
        self.drain(collector)
        self.assertEqual(len(self.events), 30)
        self.assertEqual(collector.counters["oversizedRecords"], 0)


if __name__ == "__main__":
    unittest.main()
