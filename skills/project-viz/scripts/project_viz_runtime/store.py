"""Durable semantic work, public observations and explicit source bindings."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading

from .common import public_text, redact, utcnow


STATES = {"planned", "running", "waiting", "finished", "blocked", "unknown"}
OUTCOMES = {"unverified", "passed", "failed", "inconclusive"}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalized_time(value):
    if not value:
        return utcnow()
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.replace(tzinfo=stamp.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (TypeError, ValueError, OverflowError):
        return "1970-01-01T00:00:00.000000+00:00"


def short_title(value):
    value = re.sub(r"\s+", " ", public_text(value or "Observed work", 12000)).strip()
    first = re.split(r"[\n。！？!?]", value)[0]
    return first[:54] + ("…" if len(first) > 54 else "")


def history_id(work_id, offset=0):
    return "records:" + base64.urlsafe_b64encode(encoded([work_id, offset]).encode()).decode().rstrip("=")


def visual_status(work):
    state, outcome = work.get("execution_state", "unknown"), work.get("outcome", "unverified")
    if state == "running":
        return "active"
    if outcome == "failed":
        return "error"
    if state in {"waiting", "blocked"}:
        return "waiting"
    if outcome == "passed":
        return "success"
    if state == "finished":
        return "done"
    if outcome == "inconclusive":
        return "partial"
    return state if state == "planned" else "unknown"


class Store:
    def __init__(self, state_dir, project_id):
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(Path(state_dir) / "events.sqlite", timeout=10, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=10000")
        self.connection.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS works(id TEXT PRIMARY KEY,parent_id TEXT NOT NULL,kind TEXT NOT NULL,data TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,work_id TEXT NOT NULL,session_id TEXT,turn_id TEXT,kind TEXT NOT NULL,call_id TEXT,data TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_work ON events(work_id,created_at,id);
            CREATE INDEX IF NOT EXISTS events_call ON events(session_id,call_id,kind);
            CREATE TABLE IF NOT EXISTS bindings(session_id TEXT NOT NULL,turn_id TEXT NOT NULL,work_id TEXT NOT NULL,PRIMARY KEY(session_id,turn_id));
            CREATE TABLE IF NOT EXISTS aliases(id TEXT PRIMARY KEY,target TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY,digest TEXT NOT NULL,work_id TEXT NOT NULL);
        ''')
        for key, value in [("project_id", project_id), ("schema_version", "1")]:
            row = self.connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if row and row["value"] != value:
                self.connection.close()
                raise ValueError("Archive project identity or schema does not match")
        with self.connection:
            for key, value in [("project_id", project_id), ("schema_version", "1")]:
                self.connection.execute("INSERT OR IGNORE INTO meta VALUES (?,?)", (key, value))
            self.connection.execute("INSERT OR IGNORE INTO meta VALUES ('revision','0')")

    def close(self):
        with self.lock:
            self.connection.close()

    def _bump(self):
        self.connection.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")

    @contextmanager
    def _transaction(self):
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise

    def resolve(self, identifier):
        seen = set()
        while identifier not in seen:
            seen.add(identifier)
            row = self.connection.execute("SELECT target FROM aliases WHERE id=?", (identifier,)).fetchone()
            if not row:
                return identifier
            identifier = row["target"]
        raise ValueError("Circular work alias")

    def _work(self, identifier):
        row = self.connection.execute("SELECT data FROM works WHERE id=?", (self.resolve(identifier),)).fetchone()
        return json.loads(row["data"]) if row else None

    def _save_work(self, work):
        self.connection.execute("INSERT INTO works VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET parent_id=excluded.parent_id,kind=excluded.kind,data=excluded.data,updated_at=excluded.updated_at",
                                (work["id"], work["parent_id"], work.get("kind", "task"), encoded(work), work["updatedAt"]))

    def _check_parent(self, identifier, parent):
        seen = {identifier}
        while parent != "project":
            if parent in seen:
                raise ValueError("Work hierarchy must not contain a cycle")
            seen.add(parent)
            work = self._work(parent)
            if not work:
                raise ValueError("Parent work does not exist: " + parent)
            parent = work["parent_id"]

    def _bind(self, session, turn, work_id):
        if not session or not turn:
            return
        old = self.connection.execute("SELECT work_id FROM bindings WHERE session_id=? AND turn_id=?", (session, turn)).fetchone()
        self.connection.execute("INSERT INTO bindings VALUES (?,?,?) ON CONFLICT(session_id,turn_id) DO UPDATE SET work_id=excluded.work_id", (session, turn, work_id))
        self.connection.execute("UPDATE events SET work_id=? WHERE session_id=? AND turn_id=?", (work_id, session, turn))
        if old and old["work_id"] != work_id:
            previous = self._work(old["work_id"])
            remaining = self.connection.execute("SELECT 1 FROM bindings WHERE work_id=? LIMIT 1", (old["work_id"],)).fetchone()
            if previous and previous.get("inferred") and not remaining:
                # The original source records survive; only a generated placeholder
                # is replaced with an explicit semantic identity.
                target = self._work(work_id)
                ancestor = target
                while ancestor and ancestor["parent_id"] != "project":
                    if ancestor["parent_id"] == previous["id"]:
                        target["parent_id"] = previous["parent_id"]
                        self._save_work(target)
                        break
                    ancestor = self._work(ancestor["parent_id"])
                children = self.connection.execute("SELECT data FROM works WHERE parent_id=?", (previous["id"],)).fetchall()
                for child in children:
                    item = json.loads(child["data"])
                    if item["id"] != work_id:
                        item["parent_id"] = work_id
                        self._save_work(item)
                self.connection.execute("UPDATE events SET work_id=? WHERE work_id=?", (work_id, previous["id"]))
                self.connection.execute("INSERT OR REPLACE INTO aliases VALUES (?,?)", (previous["id"], work_id))
                self.connection.execute("DELETE FROM works WHERE id=?", (previous["id"],))

    def _semantic(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object")
        identifier = str(payload.get("work_id") or payload.get("id") or "")
        if not identifier or identifier == "project" or identifier.startswith(("event:", "records:")) or len(identifier) > 160 or re.search(r"[\x00-\x1f]", identifier):
            raise ValueError("Provide a stable work_id (not project)")
        identifier = self.resolve(identifier)
        before = self._work(identifier)
        title = payload.get("title", payload.get("label", (before or {}).get("title", "")))
        if not isinstance(title, str) or not title.strip() or len(title) > 240:
            raise ValueError("Work requires a nonempty short title (up to 240 characters)")
        parent = str(payload.get("parent_id", payload.get("parentId", (before or {}).get("parent_id", "project"))) or "project")
        parent = self.resolve(parent)
        self._check_parent(identifier, parent)
        kind = payload.get("kind", (before or {}).get("kind", "task"))
        if kind not in {"task", "stage"}:
            raise ValueError("Work kind must be task or stage")
        state = payload.get("execution_state", (before or {}).get("execution_state", "planned"))
        outcome = payload.get("outcome", (before or {}).get("outcome", "unverified"))
        if state not in STATES or outcome not in OUTCOMES:
            raise ValueError("Invalid execution_state or outcome")
        evidence = payload.get("evidence", (before or {}).get("evidence", []))
        sources = payload.get("sources", (before or {}).get("sources", []))
        if not isinstance(evidence, list) or not isinstance(sources, list) or any(not isinstance(item, dict) for item in evidence + sources):
            raise ValueError("evidence and sources must be lists of objects")
        for item in evidence:
            path = item.get("path")
            if path is not None and (not isinstance(path, str) or not path or "\\" in path or "\x00" in path or path.startswith("/") or re.match(r"^[A-Za-z]:", path) or ".." in Path(path).parts):
                raise ValueError("Evidence paths must stay project-relative")
            if "line" in item and (type(item["line"]) is not int or item["line"] < 1):
                raise ValueError("Evidence line must be a positive integer")
        if outcome == "passed" and not evidence:
            raise ValueError("A passed outcome needs evidence; execution completion alone is finished/unverified")
        stamp = utcnow()
        work = {**(before or {}), "id": identifier, "parent_id": parent, "kind": kind,
                "title": public_text(title.strip(), 240), "summary": public_text(payload.get("summary", (before or {}).get("summary", "")), 4000),
                "detail": public_text(payload.get("detail", (before or {}).get("detail", "")), 16000),
                "execution_state": state, "outcome": outcome, "evidence": redact(evidence), "sources": redact(sources),
                "inferred": False, "executionUpdatedAt": stamp, "summaryUpdatedAt": stamp, "titleUpdatedAt": stamp, "createdAt": (before or {}).get("createdAt", stamp), "updatedAt": stamp}
        self._save_work(work)
        pairs = sources + [{"session_id": payload.get("session_id"), "turn_id": payload.get("turn_id")}]
        for source in pairs:
            self._bind(source.get("session_id"), source.get("turn_id"), identifier)
        return self._work(identifier)

    def emit(self, payload):
        receipt = str(payload.get("event_id") or "")
        if not receipt or len(receipt) > 240:
            raise ValueError("Provide a unique event_id for retry-safe updates")
        digest = hashlib.sha256(encoded(payload).encode()).hexdigest()
        with self._transaction():
            previous = self.connection.execute("SELECT * FROM receipts WHERE id=?", (receipt,)).fetchone()
            if previous:
                if previous["digest"] != digest:
                    raise ValueError("event_id already exists with different content")
                return {"work": self._work(previous["work_id"]), "duplicate": True}
            work = self._semantic(payload)
            self.connection.execute("INSERT INTO receipts VALUES (?,?,?)", (receipt, digest, work["id"]))
            event = {"id": "semantic:" + receipt, "label": "Work checkpoint", "text": work["summary"],
                     "detail": public_text(payload), "timestamp": work["updatedAt"], "status": visual_status(work)}
            self.connection.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)", (event["id"], work["id"], payload.get("session_id"), payload.get("turn_id"), "checkpoint", None, encoded(event), work["updatedAt"]))
            self._bump()
            return {"work": work, "duplicate": False}

    def catalog(self, nodes):
        if not isinstance(nodes, list) or len(nodes) > 5000:
            raise ValueError("Catalog nodes must be a list of at most 5000 items")
        with self._transaction():
            pending = list(nodes)
            results = []
            if len({n.get("id", n.get("work_id")) for n in nodes if isinstance(n, dict)}) != len(nodes):
                raise ValueError("Catalog node IDs must be unique")
            while pending:
                previous_size = len(pending)
                for item in list(pending):
                    if not isinstance(item, dict):
                        raise ValueError("Catalog nodes must be objects")
                    parent = item.get("parent_id", item.get("parentId", "project")) or "project"
                    if parent == "project" or self._work(parent):
                        results.append(self._semantic(item))
                        pending.remove(item)
                if len(pending) == previous_size:
                    raise ValueError("Catalog has missing parents or a cycle")
            self._bump()
            return results

    def _observed_work(self, record):
        session, turn = str(record.get("session_id") or "unknown"), str(record.get("turn_id") or "unknown")
        bound = self.connection.execute("SELECT work_id FROM bindings WHERE session_id=? AND turn_id=?", (session, turn)).fetchone()
        if bound:
            return self._work(bound["work_id"])
        identifier = "observed:" + hashlib.sha256((session + "\0" + turn).encode()).hexdigest()[:24]
        parent = "project"
        parent_session = record.get("parent_session_id")
        if parent_session:
            row = self.connection.execute("SELECT b.work_id FROM bindings b JOIN works w ON w.id=b.work_id WHERE b.session_id=? ORDER BY w.updated_at DESC LIMIT 1", (parent_session,)).fetchone()
            if row:
                parent = row["work_id"]
        stamp = record.get("timestamp") or utcnow()
        work = {"id": identifier, "parent_id": parent, "kind": "task", "title": "Observed work",
                "summary": "", "detail": "Public execution observed; work grouping is inferred until organized.",
                "execution_state": "unknown", "executionUpdatedAt": "", "summaryUpdatedAt": "", "titleUpdatedAt": "", "outcome": "unverified", "inferred": True,
                "evidence": [], "sources": [{"session_id": session, "turn_id": turn}], "createdAt": stamp, "updatedAt": stamp}
        self._save_work(work)
        self._bind(session, turn, identifier)
        return work

    def ingest_codex(self, raw):
        record = redact(raw)
        identifier = str(record["id"])
        record["timestamp"] = normalized_time(record.get("timestamp"))
        with self._transaction():
            if self.connection.execute("SELECT 1 FROM events WHERE id=?", (identifier,)).fetchone():
                return False
            session, turn, kind = str(record.get("session_id") or "unknown"), str(record.get("turn_id") or "unknown"), record.get("type", "progress")
            call = record.get("call_id")
            tool = self.connection.execute("SELECT work_id FROM events WHERE session_id=? AND call_id=? AND kind='tool' ORDER BY created_at DESC LIMIT 1", (session, call)).fetchone() if kind == "result" and call else None
            work = self._work(tool["work_id"]) if tool else self._observed_work(record)
            body = public_text(record.get("text") or record.get("detail") or "", 12000)
            stamp = record["timestamp"]
            if kind == "goal" and work.get("inferred"):
                if re.fullmatch(r"(?:continue|proceed|go on|继续|继续吧|接着做)[.!。！\s]*", body.strip(), re.I):
                    older = self.connection.execute("SELECT w.data FROM bindings b JOIN works w ON w.id=b.work_id WHERE b.session_id=? AND b.turn_id<>? AND w.updated_at<=? ORDER BY w.updated_at DESC LIMIT 1", (session, turn, stamp)).fetchone()
                    if older:
                        work = json.loads(older["data"])
                        self._bind(session, turn, work["id"])
                        work = self._work(work["id"])
                elif stamp >= work.get("titleUpdatedAt", ""):
                    work.update(title=short_title(body), detail=body, titleUpdatedAt=stamp)
            # Lifecycle ordering is independent from later tool output timestamps.
            state = {"goal": "running", "turn_started": "running", "turn_finished": "finished", "turn_failed": "blocked"}.get(kind)
            lifecycle_at = work.get("executionUpdatedAt", work.get("updatedAt", "") if not work.get("inferred") else "")
            if state and stamp >= lifecycle_at:
                work.update(execution_state=state, executionUpdatedAt=stamp)
            if kind == "progress" and stamp >= work.get("summaryUpdatedAt", ""):
                work.update(summary=body[:1600], summaryUpdatedAt=stamp)
            work["updatedAt"] = max(stamp, work.get("updatedAt", ""))
            self._save_work(work)
            record.update(detail=public_text(record.get("detail") or body), text=body)
            self.connection.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)", (identifier, work["id"], session, turn, kind, call, encoded(record), stamp))
            self._bump()
            return True

    def _public_work(self, item):
        files = list(dict.fromkeys(e["path"] for e in item.get("evidence", []) if isinstance(e.get("path"), str)))
        return {"id": item["id"], "parentId": item["parent_id"], "kind": item["kind"], "label": item["title"],
                "summary": item.get("summary", ""), "detail": item.get("detail", ""), "status": visual_status(item),
                "outcome": item.get("outcome", "unverified"), "executionState": item["execution_state"],
                "inferred": item.get("inferred", False), "curatedLabel": not item.get("inferred", False),
                "updatedAt": item["updatedAt"], "evidence": item.get("evidence", []), "files": files, "hasChildren": True}

    def snapshot(self, config, coverage=None):
        with self.lock:
            works = [json.loads(row[0]) for row in self.connection.execute("SELECT data FROM works ORDER BY rowid")]
            counts = {r[0]: r[1] for r in self.connection.execute("SELECT work_id,COUNT(*) FROM events GROUP BY work_id")}
            revision = self.connection.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]
            nodes = [self._public_work(work) for work in works]
            now = __import__("time").time()
            for node in list(nodes):
                if node["status"] == "active":
                    try:
                        age = now - __import__("datetime").datetime.fromisoformat(node["updatedAt"].replace("Z", "+00:00")).timestamp()
                    except (ValueError, TypeError):
                        age = float("inf")
                    if age > 120:
                        node["status"] = "unknown"
                    elif age > 30:
                        node["status"] = "waiting"
                count = counts.get(node["id"], 0)
                if count:
                    nodes.append({"id": history_id(node["id"]), "parentId": node["id"], "kind": "history", "label": "Execution records · " + str(count), "status": "idle", "hasChildren": True, "updatedAt": node["updatedAt"], "eventCount": count})
            # Iterate only works for selection; archive containers never become work.
            candidates = [node for node in nodes if node["kind"] == "task"]
            current = max(candidates, key=lambda n: (n["status"] == "active", n["status"] == "waiting", n["updatedAt"]), default=None)
            lookup = {n["id"]: n for n in nodes}
            path, seen = [], set()
            cursor = current
            while cursor and cursor["id"] not in seen:
                seen.add(cursor["id"])
                path.insert(0, cursor["id"])
                cursor = lookup.get(cursor["parentId"])
            path.insert(0, "project")
            root = {"id": "project", "parentId": None, "kind": "project", "label": config["title"], "summary": config.get("goal", ""), "detail": config.get("goal", ""), "status": "active" if any(n["status"] == "active" for n in candidates) else "partial", "curatedLabel": True, "hasChildren": bool(works)}
            coverage = coverage or {}
            visible_coverage = {key: value for key, value in coverage.items() if key != "diagnostics"}
            signature = hashlib.sha256(encoded([revision, config.get("title"), config.get("goal"), [(n["id"], n["status"]) for n in nodes], visible_coverage]).encode()).hexdigest()[:20]
            return {"revision": signature, "rootId": "project", "project": {"id": config["projectId"], "title": config["title"], "path": config["projectRoot"]}, "nodes": [root] + nodes,
                    "activePath": path, "activeNodeIds": path if current and current["status"] == "active" else [], "currentActivity": (current or {}).get("summary", ""), "pollSeconds": 2, "lastScanAt": utcnow(), "lastActivityAt": (current or {}).get("updatedAt"), "monitorStatus": "degraded" if coverage.get("warnings") else "watching", "coverage": coverage}

    def history(self, identifier):
        if not identifier.startswith("records:") or len(identifier) > 1000:
            raise ValueError("Invalid history ID")
        try:
            raw = identifier.split(":", 1)[1]
            work_id, offset = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        except Exception as exc:
            raise ValueError("Invalid history ID") from exc
        if not isinstance(work_id, str) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Invalid history page")
        with self.lock:
            work_id = self.resolve(work_id)
            if not self._work(work_id):
                raise FileNotFoundError("Work not found")
            rows = self.connection.execute("SELECT * FROM events WHERE work_id=? AND (kind<>'result' OR NOT EXISTS(SELECT 1 FROM events t WHERE t.session_id=events.session_id AND t.call_id=events.call_id AND t.kind='tool')) ORDER BY created_at DESC,id DESC LIMIT 101 OFFSET ?", (work_id, offset)).fetchall()
            nodes = []
            for row in rows[:100]:
                record = json.loads(row["data"])
                detail = record.get("detail", record.get("text", ""))
                status = record.get("status", "unknown")
                if row["kind"] == "tool" and row["call_id"]:
                    result = self.connection.execute("SELECT data FROM events WHERE session_id=? AND call_id=? AND kind='result' ORDER BY created_at DESC LIMIT 1", (row["session_id"], row["call_id"])).fetchone()
                    if result:
                        output = json.loads(result["data"])
                        detail += "\n\nResult:\n" + output.get("detail", output.get("text", ""))
                        status = output.get("status", "unknown")
                nodes.append({"id": "event:" + row["id"], "parentId": identifier, "kind": "action", "label": record.get("label") or row["kind"], "summary": record.get("text", "")[:240], "detail": detail, "status": {"running": "active", "completed": "done", "failed": "error"}.get(status, status), "updatedAt": record.get("timestamp"), "files": record.get("files", []), "source": record.get("source"), "hasChildren": False})
            if len(rows) > 100:
                nodes.append({"id": history_id(work_id, offset + 100), "parentId": identifier, "kind": "history", "label": "More records", "status": "idle", "hasChildren": True})
            return {"id": identifier, "nodes": nodes}

    def context(self, limit=30, offset=0, work_offset=0):
        limit, offset, work_offset = max(1, min(limit, 200)), max(0, offset), max(0, work_offset)
        with self.lock:
            rows = self.connection.execute("SELECT work_id,data FROM events WHERE kind IN ('goal','progress') ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            works = [json.loads(row[0]) for row in self.connection.execute("SELECT data FROM works ORDER BY updated_at DESC,id LIMIT 200 OFFSET ?", (work_offset,))]
            record_count = self.connection.execute("SELECT COUNT(*) FROM events WHERE kind IN ('goal','progress')").fetchone()[0]
            work_count = self.connection.execute("SELECT COUNT(*) FROM works").fetchone()[0]
            return {"works": works, "publicRecords": [{"work_id": r["work_id"], **json.loads(r["data"])} for r in rows],
                    "pagination": {"workCount": work_count, "recordCount": record_count,
                                   "nextOffset": offset + len(rows) if offset + len(rows) < record_count else None,
                                   "nextWorkOffset": work_offset + len(works) if work_offset + len(works) < work_count else None}}

    def export(self):
        with self.lock:
            works = [json.loads(row[0]) for row in self.connection.execute("SELECT data FROM works ORDER BY rowid")]
            curated = {w["id"] for w in works if not w.get("inferred")}
            return [{"id": w["id"], "parentId": w["parent_id"] if w["parent_id"] in curated else "project", "kind": w["kind"], "label": w["title"], "summary": w.get("summary", ""), "execution_state": w["execution_state"], "outcome": w["outcome"], "evidence": [{key: value for key, value in item.items() if key in {"path", "line", "note"}} for item in w.get("evidence", [])]} for w in works if w["id"] in curated]
