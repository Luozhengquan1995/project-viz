"""Public storage invariants using temporary projects and synthetic records only."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/project-viz/scripts"))
from project_viz_runtime.common import context, safe_project_file
from project_viz_runtime.store import Store, history_id


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.env = context(self.project, self.root / "state", self.root / "codex", create=True)
        self.store = Store(self.env["state"], self.env["config"]["projectId"])
        self.addCleanup(self.store.close)

    def emit(self, work="work:stable", event="checkpoint:1", **extra):
        return self.store.emit({"work_id": work, "event_id": event, "title": "Check the public contract", **extra})

    def record(self, identifier, kind="progress", text="Synthetic progress", turn="turn:1", session="session:1", timestamp="2024-01-01T00:00:00+00:00", **extra):
        value = {"id": identifier, "type": kind, "text": text, "detail": text, "label": kind,
                 "session_id": session, "turn_id": turn, "timestamp": timestamp, "status": "observed", **extra}
        self.store.ingest_codex(value)
        return value

    def works(self):
        return {work["id"]: work for work in self.store.context()["works"]}

    def snapshot(self):
        return self.store.snapshot(self.env["config"])

    def test_context_identity_and_token_survive_reinitialization(self):
        token = (self.env["state"] / "access-token").read_bytes()
        again = context(self.project / ".", self.env["state"], create=True)
        self.assertEqual(again["config"]["projectId"], self.env["config"]["projectId"])
        self.assertEqual((again["state"] / "access-token").read_bytes(), token)
        other = self.root / "other"
        other.mkdir()
        with self.assertRaises(ValueError):
            context(other, self.env["state"], create=True)
        with self.assertRaises(ValueError):
            context(self.project, self.env["state"], self.root / "different-codex", create=True)

    def test_archive_project_identity_mismatch_is_rejected_cleanly(self):
        self.emit()
        with self.assertRaises(ValueError):
            Store(self.env["state"], "different-project-uuid")
        self.assertEqual(set(self.works()), {"work:stable"})

    def test_emit_retry_is_idempotent_and_changed_retry_rejected(self):
        self.assertFalse(self.emit()["duplicate"])
        before = self.snapshot()["revision"]
        self.assertTrue(self.emit()["duplicate"])
        self.assertEqual(self.snapshot()["revision"], before)
        with self.assertRaises(ValueError):
            self.emit(summary="Different payload with the same receipt")
        self.assertEqual(len(self.store.history(history_id("work:stable"))["nodes"]), 1)
        self.assertEqual(self.snapshot()["revision"], before)

    def test_catalog_orders_parents_and_preserves_stable_ids_and_omitted_work(self):
        self.store.catalog([
            {"id": "work:retained", "parentId": "stage:contract", "label": "Review inputs"},
            {"id": "stage:contract", "kind": "stage", "label": "Public interface"},
        ])
        self.store.catalog([{"id": "stage:contract", "kind": "stage", "label": "Stable interface"}])
        self.assertEqual(set(self.works()), {"work:retained", "stage:contract"})
        self.assertEqual(self.works()["work:retained"]["parent_id"], "stage:contract")
        self.assertEqual(self.works()["stage:contract"]["title"], "Stable interface")

    def test_catalog_failure_rolls_back_partial_changes_and_revision(self):
        self.emit()
        before = self.snapshot()["revision"]
        with self.assertRaises(ValueError):
            self.store.catalog([
                {"id": "work:new", "label": "Would otherwise be inserted"},
                {"id": "work:orphan", "parentId": "missing", "label": "Missing parent"},
            ])
        self.assertEqual(set(self.works()), {"work:stable"})
        self.assertEqual(self.snapshot()["revision"], before)

    def test_emit_binding_failure_rolls_back_work_and_receipt(self):
        before = self.snapshot()["revision"]
        with self.assertRaises((ValueError, TypeError, sqlite3.Error)):
            self.emit(sources=[{"session_id": ["invalid"], "turn_id": "turn:1"}])
        self.assertEqual(self.works(), {})
        self.assertEqual(self.snapshot()["revision"], before)
        self.assertFalse(self.emit()["duplicate"])

    def test_passed_requires_evidence_and_completion_is_not_success(self):
        with self.assertRaises(ValueError):
            self.emit(execution_state="finished", outcome="passed")
        self.emit(execution_state="finished")
        node = next(n for n in self.snapshot()["nodes"] if n["id"] == "work:stable")
        self.assertEqual((node["status"], node["outcome"]), ("done", "unverified"))
        self.emit(event="checkpoint:passed", execution_state="finished", outcome="passed", evidence=[{"path": "report.txt", "line": 4, "note": "Synthetic assertion"}])
        node = next(n for n in self.snapshot()["nodes"] if n["id"] == "work:stable")
        self.assertEqual(node["status"], "success")

    def test_binding_replaces_inferred_work_without_losing_history_or_identity(self):
        self.record("goal:1", "goal", "Inspect public input contracts")
        self.record("progress:1")
        old = next(iter(self.works()))
        self.emit(sources=[{"session_id": "session:1", "turn_id": "turn:1"}])
        self.record("progress:2")
        self.emit(event="checkpoint:bind-second", sources=[{"session_id": "session:2", "turn_id": "turn:2"}])
        self.record("progress:3", session="session:2", turn="turn:2")
        self.assertEqual(set(self.works()), {"work:stable"})
        self.assertFalse(self.works()["work:stable"]["inferred"])
        self.assertEqual(self.store.resolve(old), "work:stable")
        old_history = self.store.history(history_id(old))["nodes"]
        self.assertEqual({n["id"] for n in old_history}, {"event:goal:1", "event:progress:1", "event:progress:2", "event:progress:3", "event:semantic:checkpoint:1", "event:semantic:checkpoint:bind-second"})
        self.assertTrue(all(r["work_id"] == "work:stable" for r in self.store.context()["publicRecords"]))

    def test_distinct_turn_goals_stay_separate_and_explicit_continue_reuses_work(self):
        self.record("goal:1", "goal", "Check interface errors", turn="turn:1")
        self.record("goal:2", "goal", "Write install instructions", turn="turn:2", timestamp="2024-01-01T00:01:00+00:00")
        self.assertEqual(len(self.works()), 2)
        self.record("goal:3", "goal", "Continue.", turn="turn:3", timestamp="2024-01-01T00:02:00+00:00")
        self.assertEqual(len(self.works()), 2)
        records = {r["id"]: r["work_id"] for r in self.store.context()["publicRecords"]}
        self.assertNotEqual(records["goal:1"], records["goal:2"])
        self.assertEqual(records["goal:2"], records["goal:3"])

    def test_old_import_does_not_overwrite_explicit_checkpoint(self):
        self.emit(summary="Recorded conclusion", execution_state="finished", outcome="failed", session_id="session:1", turn_id="turn:1")
        for identifier, kind in [("old:goal", "goal"), ("old:start", "turn_started"), ("old:progress", "progress")]:
            self.record(identifier, kind, "Historical observation")
        work = self.works()["work:stable"]
        self.assertEqual((work["title"], work["summary"], work["execution_state"], work["outcome"]),
                         ("Check the public contract", "Recorded conclusion", "finished", "failed"))
        node = next(n for n in self.snapshot()["nodes"] if n["id"] == "work:stable")
        self.assertEqual(node["status"], "error")

    def test_out_of_order_inferred_lifecycle_does_not_reopen_completed_turn(self):
        self.record("goal:1", "goal", "Verify the API")
        self.record("finish:1", "turn_finished", timestamp="2024-01-01T00:02:00+00:00", status="completed")
        self.record("late:start", "turn_started", timestamp="2024-01-01T00:01:00+00:00", status="running")
        work = next(iter(self.works().values()))
        self.assertEqual(work["execution_state"], "finished")
        self.assertEqual(datetime.fromisoformat(work["updatedAt"]), datetime(2024, 1, 1, 0, 2, tzinfo=timezone.utc))

    def test_lifecycle_order_uses_instants_not_timestamp_string_order(self):
        self.record("goal:1", "goal", "Verify the API")
        self.record("finish:1", "turn_finished", timestamp="2024-01-01T00:00:00.500000+00:00", status="completed")
        self.record("late:start", "turn_started", timestamp="2024-01-01T00:00:00Z", status="running")
        self.assertEqual(next(iter(self.works().values()))["execution_state"], "finished")

    def test_late_result_does_not_block_earlier_lifecycle_completion(self):
        self.record("start:1", "turn_started", status="running")
        self.record("tool:1", "tool", call_id="call:opaque", status="running")
        self.record("result:1", "result", timestamp="2024-01-01T00:02:00+00:00", call_id="call:opaque", status="completed")
        self.record("finish:1", "turn_finished", timestamp="2024-01-01T00:01:00+00:00", status="completed")
        self.assertEqual(next(iter(self.works().values()))["execution_state"], "finished")

    def test_late_tool_result_pairs_across_turns_without_creating_phantom_work(self):
        self.record("tool:1", "tool", "Run a synthetic check", call_id="call:opaque", status="running")
        original = next(iter(self.works()))
        result = self.record("result:1", "result", "Synthetic check failed", turn="turn:later", timestamp="2024-01-01T00:01:00+00:00", call_id="call:opaque", status="failed")
        self.assertFalse(self.store.ingest_codex(result))
        self.assertEqual(set(self.works()), {original})
        history = self.store.history(history_id(original))["nodes"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "error")
        self.assertIn("Synthetic check failed", history[0]["detail"])

    def test_binding_inferred_parent_never_leaves_orphan_or_cycle(self):
        self.record("goal:1", "goal", "Inspect public input contracts")
        old = next(iter(self.works()))
        try:
            self.emit(parent_id=old, sources=[{"session_id": "session:1", "turn_id": "turn:1"}])
        except ValueError:
            self.assertEqual(set(self.works()), {old})
            return
        works = self.works()
        for item in works.values():
            seen = {item["id"]}
            parent = item["parent_id"]
            while parent != "project":
                self.assertIn(parent, works, "Source binding left an orphaned parent")
                self.assertNotIn(parent, seen, "Source binding created a cycle")
                seen.add(parent)
                parent = works[parent]["parent_id"]

    def test_opaque_history_pagination_and_restart_preserve_all_records(self):
        work_id = "work:API/契约?版本=2:part"
        self.emit(work=work_id, session_id="session:1", turn_id="turn:1")
        for i in range(105):
            self.record("progress:" + str(i))
        second = Store(self.env["state"], self.env["config"]["projectId"])
        self.addCleanup(second.close)
        pending = [history_id(work_id)]
        records = []
        while pending:
            page = second.history(pending.pop())["nodes"]
            records.extend(n["id"] for n in page if n["kind"] == "action")
            pending.extend(n["id"] for n in page if n["kind"] == "history")
        self.assertEqual(len(records), 106)
        self.assertEqual(len(set(records)), 106)
        self.assertEqual(set(w["id"] for w in second.context()["works"]), {work_id})

    def test_context_pagination_exposes_all_201_works_and_public_records(self):
        origin = datetime(2024, 1, 1, tzinfo=timezone.utc)
        for index in range(201):
            self.record("goal:" + str(index), "goal", "Review synthetic topic " + str(index),
                        turn="turn:" + str(index), timestamp=(origin + timedelta(seconds=index)).isoformat())
            if index == 0:
                oldest_work = self.store.context()["works"][0]["id"]
        first = self.store.context()
        self.assertEqual((len(first["works"]), len(first["publicRecords"])), (200, 30))
        self.assertEqual(first["pagination"], {"workCount": 201, "recordCount": 201, "nextOffset": 30, "nextWorkOffset": 200})
        self.assertNotIn(oldest_work, {work["id"] for work in first["works"]})
        self.assertNotIn("goal:0", {record["id"] for record in first["publicRecords"]})
        work_ids, record_ids = [], []
        page = first
        while True:
            work_ids.extend(work["id"] for work in page["works"])
            record_ids.extend(record["id"] for record in page["publicRecords"])
            pagination = page["pagination"]
            if pagination["nextOffset"] is None and pagination["nextWorkOffset"] is None:
                break
            page = self.store.context(limit=30,
                                      offset=pagination["nextOffset"] if pagination["nextOffset"] is not None else 201,
                                      work_offset=pagination["nextWorkOffset"] if pagination["nextWorkOffset"] is not None else 201)
        self.assertEqual((len(work_ids), len(set(work_ids))), (201, 201))
        self.assertEqual((len(record_ids), len(set(record_ids))), (201, 201))
        self.assertEqual(set(record_ids), {"goal:" + str(index) for index in range(201)})
        self.assertIn(oldest_work, work_ids)
        self.assertEqual(record_ids[-1], "goal:0")

    def test_export_omits_raw_observations_sources_and_redacts_common_secrets(self):
        self.record("private:goal", "goal", "Synthetic raw transcript marker")
        self.emit(summary="api_key=synthetic-secret-value", detail="Synthetic private detail marker", evidence=[{"path": "report.txt", "note": "password=synthetic-password-value"}], sources=[{"session_id": "synthetic-session-marker", "turn_id": "synthetic-turn-marker"}])
        exported = self.store.export()
        self.assertEqual([item["id"] for item in exported], ["work:stable"])
        text = json.dumps(exported)
        for marker in ["Synthetic raw transcript marker", "Synthetic private detail marker", "synthetic-session-marker", "synthetic-secret-value", "synthetic-password-value"]:
            self.assertNotIn(marker, text)
        self.assertIn("report.txt", text)

    def test_export_reparents_curated_children_of_omitted_inferred_work(self):
        self.record("goal:1", "goal", "Observed parent")
        parent = next(iter(self.works()))
        self.emit(parent_id=parent)
        exported = self.store.export()
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0]["parentId"], "project")

    def test_evidence_paths_and_lines_are_portable_and_validated_atomically(self):
        for evidence in [{"path": "../outside.txt"}, {"path": "/absolute.txt"}, {"path": "C:/absolute.txt"}, {"path": "report.txt", "line": 0}, {"path": "report.txt", "line": True}]:
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                self.emit(evidence=[evidence])
            self.assertEqual(self.works(), {})
        self.emit(evidence=[{"path": "reports/result.txt", "line": 1}])

    def test_safe_project_file_rejects_traversal_and_private_symlink_targets(self):
        public = self.project / "report.txt"
        public.write_text("Synthetic evidence", encoding="utf-8")
        self.assertEqual(safe_project_file(self.project, "report.txt"), public)
        for name in ["../outside.txt", str(public), "C:/private.txt", ".env", ".ssh/config", "cache.sqlite"]:
            with self.subTest(path=name), self.assertRaises(ValueError):
                safe_project_file(self.project, name)
        (self.project / ".env").write_text("synthetic=yes", encoding="utf-8")
        (self.project / ".ssh").mkdir()
        (self.project / ".ssh/config").write_text("synthetic", encoding="utf-8")
        for name, target in [("env-link", self.project / ".env"), ("ssh-link", self.project / ".ssh/config"), ("outside-link", self.root / "outside.txt")]:
            try:
                (self.project / name).symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("Symlinks are unavailable on this platform")
            with self.subTest(path=name), self.assertRaises(ValueError):
                safe_project_file(self.project, name)


if __name__ == "__main__":
    unittest.main()
