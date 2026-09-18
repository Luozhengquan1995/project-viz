"""Read-only, bounded Codex rollout collection. No Codex database is written.

This is a format adapter, not a promise of a stable upstream logging API. Both
discovery and import advance in bounded batches; completeness is explicit.
"""
from __future__ import annotations

from collections import Counter, deque
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import time
from typing import Callable


def _path_key(value):
    value = str(value)
    if re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", value):
        value = value.removeprefix("\\\\?\\")
        return PureWindowsPath(value).as_posix().rstrip("/").casefold()
    return str(Path(value).expanduser().resolve()).rstrip("/") or "/"


def _inside(value, project):
    if not isinstance(value, (str, Path)) or not str(value):
        return False
    child, parent = _path_key(value), _path_key(project)
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def _safe(value, limit=12000):
    value = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    value = re.sub(r"-----BEGIN (?:[A-Z ]+)?PRIVATE KEY-----.*?(?:-----END (?:[A-Z ]+)?PRIVATE KEY-----|$)", "[REDACTED PRIVATE KEY]", value, flags=re.S)
    value = re.sub(r"\b(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b", "[REDACTED KEY]", value)
    value = re.sub(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{8,}", r"\1[REDACTED]", value)
    value = re.sub(r'''(?i)(\bsshpass\s+-p\s*)(?:'[^']*'|"[^"]*"|[^\s;]+)''', r"\1'[REDACTED]'", value)
    value = re.sub(r'''(?i)(["']?\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password|passwd)\b["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;}]+)''', r'\1"[REDACTED]"', value)
    value = re.sub(r"(https?://[^\s/:@]+:)[^\s/@]+(@)", r"\1[REDACTED]\2", value)
    return value if len(value) <= limit else value[:limit - 45] + "\n[Excerpt; full record remains in source log.]"


def _parent(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, dict):
        return None
    sub = value.get("subagent", {})
    spawn = sub.get("thread_spawn", {}) if isinstance(sub, dict) else {}
    return spawn.get("parent_thread_id") or value.get("parent_thread_id")


def _message(payload):
    content = payload.get("content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(part if isinstance(part, str) else part["text"] for part in content
                     if isinstance(part, str) or isinstance(part, dict) and isinstance(part.get("text"), str))


class Collector:
    """One project's public records, with replay-safe IDs and resumable cursors."""

    READ_BYTES = 1024 * 1024
    MAX_LINE_BYTES = 4 * 1024 * 1024
    DISCOVERY_ENTRIES = 96
    DB_BATCH = 128
    MAX_PENDING = 4096
    RESCAN_SECONDS = 30

    def __init__(self, project: Path, codex_home: Path, state_dir: Path,
                 ingest: Callable[[dict], None]):
        self.project = Path(project).expanduser().resolve()
        self.codex_home = Path(codex_home).expanduser().resolve()
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.ingest = ingest
        self.state_path = self.state_dir / "collector.json"
        self.identity = _path_key(self.project)
        self.sources = {}
        self.pending = {}
        self.counters = Counter()
        self.errors = deque(maxlen=20)
        self._buffers = {}
        self._scan_iterator = None
        self._scan_skip = 0
        self._round_robin = 0
        self._last_scan_at = None
        self._scan = {"queue": [], "current": None, "index": 0, "complete": False}
        self._db = {"path": None, "cursor": None, "complete": False}
        self._last_discovery = 0
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if state.get("projectIdentity") != self.identity or state.get("codexHome") != _path_key(self.codex_home):
                raise ValueError("Collector state belongs to a different project or Codex home")
            if state.get("version") != 1:
                raise ValueError("Unsupported collector state version")
            self.sources = state.get("sources", {})
            self.pending = state.get("pending", {})
            self.counters.update(state.get("counters", {}))
            self._scan = state.get("scan", self._scan)
            self._db = state.get("database", self._db)
            self._last_discovery = state.get("lastDiscovery", 0)
            # Opening an iterator again skips at most one bounded batch per call.
            self._scan_skip = self._scan.get("index", 0)
            for source in self.sources.values():
                if not self._allowed(source.get("path")):
                    raise ValueError("Collector state contains a source outside Codex rollout directories")
        else:
            self._reset_discovery()

    def _error(self, code, path=None, exc=None):
        item = {"code": code}
        if path is not None:
            item["path"] = _safe(str(path), 500)
        if exc is not None:
            item["exception"] = type(exc).__name__
        if not self.errors or self.errors[-1] != item:
            self.errors.append(item)
        self.counters[code] += 1

    def _allowed(self, path):
        if not isinstance(path, (str, Path)):
            return None
        resolved = Path(path).expanduser().resolve()
        if resolved.name.startswith("rollout-") and resolved.suffix == ".jsonl" and any(
            resolved.is_relative_to(self.codex_home / name) for name in ("sessions", "archived_sessions")
        ):
            return resolved
        return None

    def _reset_discovery(self):
        if self._scan_iterator is not None:
            self._scan_iterator.close()
        self._scan_iterator = None
        self._scan_skip = 0
        self._scan = {"queue": [str(self.codex_home / name) for name in ("sessions", "archived_sessions")
                                if (self.codex_home / name).is_dir()], "current": None, "index": 0, "complete": False}
        self._db = {"path": None, "cursor": None, "complete": False}
        self._last_discovery = time.time()

    def _metadata(self, path):
        try:
            with path.open("rb") as stream:
                header = stream.read(65536)
            for line in header.splitlines(keepends=True)[:20]:
                if not line.endswith(b"\n"):
                    continue
                record = json.loads(line)
                if record.get("type") == "session_meta" and isinstance(record.get("payload"), dict):
                    meta = record["payload"]
                    return {"id": meta.get("id"), "path": str(path), "cwd": meta.get("cwd"),
                            "parent": meta.get("parent_thread_id") or _parent(meta.get("source"))}
            self.counters["metadataNotYetRecognized"] += 1
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            self._error("metadataReadError", path, exc)
        return None

    def _consider(self, candidate):
        sid = candidate.get("id")
        path = self._allowed(candidate.get("path"))
        if not isinstance(sid, str) or not sid or path is None:
            self.counters["invalidCandidates"] += 1
            return
        candidate = {"id": sid, "path": str(path), "cwd": candidate.get("cwd"), "parent": candidate.get("parent")}
        if sid in self.sources:
            known = self.sources[sid]
            if candidate.get("parent") and not known.get("parent"):
                known["parent"] = candidate["parent"]
            if known["path"] != str(path) and not Path(known["path"]).exists():
                known["path"] = str(path)
                self._buffers.pop(sid, None)
            return
        if _inside(candidate.get("cwd"), self.project) or candidate.get("parent") in self.sources:
            self.sources[sid] = {**candidate, "offset": 0, "line": 1, "generation": 0,
                                 "turn": None, "identity": None, "head": None, "headBytes": 0, "size": 0, "read": False}
            self.pending.pop(sid, None)
            self.counters["discoveredSessions"] += 1
        elif candidate.get("parent"):
            if len(self.pending) < self.MAX_PENDING or sid in self.pending:
                self.pending[sid] = candidate
            else:
                self._error("pendingLineageLimit")
        else:
            self.counters["excludedCandidates"] += 1

    def _resolve_pending(self):
        for _ in range(len(self.pending)):
            accepted = [item for item in self.pending.values() if item.get("parent") in self.sources]
            if not accepted:
                break
            for item in accepted:
                self._consider(item)

    def _discover_database(self):
        if self._db["complete"]:
            return
        databases = sorted(self.codex_home.glob("state_*.sqlite"), key=lambda path: int(re.search(r"(\d+)\.sqlite$", path.name)[1]) if re.search(r"(\d+)\.sqlite$", path.name) else -1, reverse=True)
        if self._db["path"]:
            databases = [Path(self._db["path"])]
        for database in databases:
            try:
                with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=.25)) as connection:
                    connection.row_factory = sqlite3.Row
                    connection.execute("PRAGMA query_only=ON")
                    columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
                    if not {"id", "rollout_path", "cwd"} <= columns:
                        self.counters["unsupportedDatabaseSchemas"] += 1
                        continue
                    order = "updated_at" if "updated_at" in columns else "id"
                    source = "substr(source,1,8192) AS source" if "source" in columns else "NULL AS source"
                    where, arguments = "", []
                    cursor = self._db["cursor"]
                    if cursor is not None:
                        where, arguments = f" WHERE ({order}<? OR ({order}=? AND id<?))", [cursor[0], cursor[0], cursor[1]]
                    rows = connection.execute(f"SELECT id,rollout_path,cwd,{source},{order} AS ordering FROM threads" + where + f" ORDER BY {order} DESC,id DESC LIMIT ?", arguments + [self.DB_BATCH]).fetchall()
                    parents = {}
                    edge_columns = {row[1] for row in connection.execute("PRAGMA table_info(thread_spawn_edges)")}
                    if rows and {"parent_thread_id", "child_thread_id"} <= edge_columns:
                        placeholders = ",".join("?" for _ in rows)
                        parents = dict(connection.execute("SELECT child_thread_id,parent_thread_id FROM thread_spawn_edges WHERE child_thread_id IN (" + placeholders + ")", [row["id"] for row in rows]))
                    for row in rows:
                        self._consider({"id": row["id"], "path": row["rollout_path"], "cwd": row["cwd"], "parent": parents.get(row["id"]) or _parent(row["source"])})
                    self._db["path"] = str(database)
                    self._db["cursor"] = [rows[-1]["ordering"], rows[-1]["id"]] if rows else cursor
                    self._db["complete"] = len(rows) < self.DB_BATCH
                    return
            except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
                self._error("databaseDiscoveryError", database, exc)
        self._db["complete"] = True

    def _discover_files(self):
        if self._scan["complete"]:
            return
        for _ in range(self.DISCOVERY_ENTRIES):
            if self._scan_iterator is None:
                if not self._scan["current"]:
                    if not self._scan["queue"]:
                        self._scan["complete"] = True
                        return
                    self._scan["current"] = self._scan["queue"].pop(0)
                    self._scan["index"] = 0
                try:
                    self._scan_iterator = os.scandir(self._scan["current"])
                except OSError as exc:
                    self._error("directoryDiscoveryError", self._scan["current"], exc)
                    self._scan["current"] = None
                    continue
            try:
                entry = next(self._scan_iterator)
            except StopIteration:
                self._scan_iterator.close()
                self._scan_iterator = None
                self._scan.update(current=None, index=0)
                self._scan_skip = 0
                continue
            if self._scan_skip:
                self._scan_skip -= 1
                continue
            self._scan["index"] += 1
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    self._scan["queue"].append(entry.path)
                elif entry.name.startswith("rollout-") and entry.name.endswith(".jsonl"):
                    candidate = self._metadata(Path(entry.path))
                    if candidate:
                        self._consider(candidate)
            except OSError as exc:
                self._error("entryDiscoveryError", entry.path, exc)

    def _normalize(self, source, raw, offset, line):
        if not isinstance(raw, dict) or not isinstance(raw.get("payload"), dict):
            self.counters["unsupportedRecords"] += 1
            return None
        payload, outer = raw["payload"], raw.get("type")
        kind = payload.get("type")
        if outer == "turn_context":
            if isinstance(payload.get("turn_id"), str):
                source["turn"] = payload["turn_id"]
            return None
        if outer == "session_meta":
            return None
        label, text, detail, status, normalized = "", "", "", "observed", None
        call_id = payload.get("call_id") or payload.get("id")
        if outer == "response_item" and kind == "message":
            role = payload.get("role")
            if role not in {"user", "assistant"} or payload.get("channel") in {"analysis", "reasoning"} or payload.get("phase") in {"analysis", "reasoning"}:
                self.counters["filteredRecords"] += 1
                return None
            if role == "assistant" and payload.get("channel") not in (None, "commentary", "final"):
                self.counters["filteredRecords"] += 1
                return None
            text = _message(payload)
            if role == "user":
                if text.lstrip().startswith(("# AGENTS.md instructions", "<subagent_notification>", "<permissions instructions>", "<skills_instructions>", "<developer")):
                    self.counters["filteredRecords"] += 1
                    return None
                text = re.sub(r"<(environment_context|recommended_plugins|skills_instructions|permissions_instructions|system_reminder)\b[^>]*>.*?</\1>", "", text, flags=re.S | re.I)
            if not text.strip():
                self.counters["filteredRecords"] += 1
                return None
            normalized, label = ("goal", "User goal") if role == "user" else ("progress", "Public update")
            detail = text
        elif outer == "response_item" and kind in {"function_call", "custom_tool_call", "local_shell_call", "web_search_call"}:
            normalized, label = "tool", str(payload.get("name") or kind)
            detail = payload.get("arguments", payload.get("input", payload.get("command", payload.get("action", ""))))
            text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
            status = "completed" if payload.get("status") == "completed" else "running"
        elif outer == "response_item" and kind in {"function_call_output", "custom_tool_call_output"}:
            normalized, label = "result", "Tool result"
            detail = payload.get("output", "")
            text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
            result = detail
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except ValueError:
                    pass
            failed = isinstance(result, dict) and (result.get("success") is False or result.get("exit_code", 0) not in (0, "0", None))
            failed = failed or bool(re.search(r'(?:exit(?:ed with)? (?:code|status)[:= ]+|"exit_code"\s*:\s*)[1-9]\d*|Traceback \(most recent call last\)', text, re.I))
            status = "failed" if failed else "completed"
        elif outer == "event_msg" and kind in {"task_started", "task_complete", "turn_aborted", "task_failed"}:
            normalized, label, status = {"task_started": ("turn_started", "Turn started", "running"), "task_complete": ("turn_finished", "Turn finished", "completed"), "turn_aborted": ("turn_failed", "Turn interrupted", "failed"), "task_failed": ("turn_failed", "Turn failed", "failed")}[kind]
            text = detail = label
            if payload.get("turn_id"):
                source["turn"] = str(payload["turn_id"])
        elif outer == "event_msg" and kind in {"exec_command_end", "patch_apply_end", "web_search_end"} and call_id:
            normalized, label, text = "result", "Tool finished", "Tool lifecycle completed"
            detail = text
            status = "failed" if payload.get("success") is False or payload.get("exit_code", 0) not in (0, None) else "completed"
        else:
            self.counters["filteredRecords"] += 1
            return None
        if not source.get("turn"):
            source["turn"] = str(payload.get("turn_id") or f"unattributed:{offset}")
        event_key = f'{source["id"]}:{source["generation"]}:{offset}:{normalized}'
        event = {"id": "codex:" + hashlib.sha256(event_key.encode("utf-8")).hexdigest()[:32],
                 "session_id": source["id"], "turn_id": source["turn"], "type": normalized,
                 "label": _safe(label, 160), "text": _safe(text), "detail": _safe(detail),
                 "status": status, "timestamp": raw.get("timestamp"),
                 "source": {"path": source["path"], "line": line, "byte_offset": offset}}
        if call_id and normalized in {"tool", "result"}:
            event["call_id"] = str(call_id)
        if source.get("parent"):
            event["parent_session_id"] = source["parent"]
        return event

    def _read(self, sid, budget):
        source = self.sources[sid]
        try:
            with Path(source["path"]).open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = [stat.st_dev, stat.st_ino]
                head_length = source.get("headBytes", 0)
                head = stream.read(head_length)
                rotated = source.get("identity") is not None and (source["identity"] != identity or stat.st_size < max(source["offset"], source.get("size", 0)) or head_length and hashlib.sha256(head).hexdigest() != source.get("head"))
                if rotated:
                    source.update(offset=0, line=1, turn=None, generation=source["generation"] + 1, headBytes=0, head=None)
                    source.pop("skipOversize", None)
                    self._buffers.pop(sid, None)
                    self.counters["sourceResets"] += 1
                source.update(identity=identity, size=stat.st_size, read=True, readError=False)
                if not source.get("headBytes") and stat.st_size:
                    stream.seek(0)
                    head = stream.read(min(512, stat.st_size))
                    source.update(headBytes=len(head), head=hashlib.sha256(head).hexdigest())
                pending = self._buffers.get(sid, b"")
                stream.seek(source["offset"] + len(pending))
                # Retry a failed callback before accumulating more complete
                # records; otherwise a persistently failing sink could exhaust
                # memory or wrongly classify queued records as one large line.
                extra = stream.read(budget) if b"\n" not in pending else b""
                pending += extra
                self.counters["bytesRead"] += len(extra)
                consumed = 0
                while True:
                    newline = pending.find(b"\n", consumed)
                    if newline < 0:
                        break
                    end = newline + 1
                    line = pending[consumed:end]
                    offset = source["offset"]
                    if source.pop("skipOversize", False) or len(line) > self.MAX_LINE_BYTES:
                        self.counters["oversizedRecords"] += 1
                    else:
                        try:
                            raw = json.loads(line)
                        except (ValueError, UnicodeDecodeError) as exc:
                            self._error("malformedRecords", source["path"], exc)
                        else:
                            event = self._normalize(source, raw, offset, source["line"])
                            if event:
                                try:
                                    self.ingest(event)
                                except Exception as exc:
                                    self._error("ingestError", source["path"], exc)
                                    break
                                self.counters["recordsIngested"] += 1
                    source["offset"] += len(line)
                    source["line"] += 1
                    consumed = end
                pending = pending[consumed:]
                if len(pending) > self.MAX_LINE_BYTES and b"\n" not in pending:
                    source["offset"] += len(pending)
                    source["skipOversize"] = True
                    pending = b""
                    self._error("oversizedRecordSkipped", source["path"])
                self._buffers[sid] = pending
                return len(extra)
        except OSError as exc:
            source["readError"] = True
            self._error("sourceReadError", source["path"], exc)
            return 0

    def _save(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        state = {"version": 1, "projectIdentity": self.identity, "codexHome": _path_key(self.codex_home),
                 "sources": self.sources, "pending": self.pending, "scan": self._scan, "database": self._db,
                 "lastDiscovery": self._last_discovery, "counters": dict(self.counters)}
        temporary = self.state_path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
        temporary.replace(self.state_path)

    def poll(self):
        if self._scan["complete"] and self._db["complete"] and time.time() - self._last_discovery > self.RESCAN_SECONDS:
            self._reset_discovery()
        self._discover_database()
        self._discover_files()
        self._resolve_pending()
        keys = list(self.sources)
        budget = self.READ_BYTES
        if keys:
            start = self._round_robin % len(keys)
            for index in range(len(keys)):
                sid = keys[(start + index) % len(keys)]
                budget -= self._read(sid, budget)
                self._round_robin = (start + index + 1) % len(keys)
                if budget <= 0:
                    break
        self._last_scan_at = datetime.now(timezone.utc).isoformat()
        self._save()
        return self.diagnostics()

    def import_history(self):
        """Advance one bounded chunk; repeat until backlogComplete or time budget."""
        return self.poll()

    def diagnostics(self):
        discovery_complete = self._scan["complete"] and self._db["complete"]
        pending_bytes = sum(max(0, source.get("size", 0) - source["offset"]) for source in self.sources.values())
        complete = discovery_complete and pending_bytes == 0 and all(source.get("read") and not source.get("readError") for source in self.sources.values())
        return {"sources": len(self.sources), "sessionCount": len(self.sources), "pendingBytes": pending_bytes,
                "historyComplete": complete, "backlogComplete": complete, "discoveryComplete": discovery_complete,
                "fromStart": True, "bytesRead": self.counters["bytesRead"], "recordsIngested": self.counters["recordsIngested"],
                "lastScanAt": self._last_scan_at, "pendingLineage": len(self.pending),
                "counters": dict(self.counters), "errors": list(self.errors)}
