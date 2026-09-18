"""Authenticated loopback-only service for one project's local work graph."""
from __future__ import annotations

from datetime import datetime, timezone
import hmac
import http.cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
import uuid

from . import __version__
from .codex import Collector
from .common import atomic_json, public_text, read_json, safe_project_file, state_lock, utcnow
from .store import Store


_ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
           "/index.html": ("index.html", "text/html; charset=utf-8"),
           "/flow.js": ("flow.js", "text/javascript; charset=utf-8"),
           "/flow-labels.js": ("flow-labels.js", "text/javascript; charset=utf-8"),
           "/flow.css": ("flow.css", "text/css; charset=utf-8")}


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _coverage(collector, runtime_errors=()):
    diagnostics = collector.diagnostics()
    pending = sum(not source.get("read") or source.get("readError") or source.get("offset", 0) < source.get("size", 0)
                  for source in collector.sources.values())
    warnings = [str(error.get("code", "Collector error")) for error in diagnostics.get("errors", [])]
    warnings.extend(runtime_errors)
    counters = diagnostics.get("counters", {})
    for name in ("unsupportedDatabaseSchemas", "oversizedRecords", "oversizedRecordSkipped", "metadataNotYetRecognized", "pendingLineageLimit"):
        if counters.get(name):
            warnings.append(f"{name}: {counters[name]}")
    complete = bool(diagnostics.get("historyComplete"))
    return {"status": "degraded" if warnings else "ready" if complete else "indexing",
            "sourceCount": len(collector.sources), "indexedCount": len(collector.sources) - pending,
            "pendingSources": pending, "historyComplete": complete,
            "warnings": list(dict.fromkeys(warnings))[-20:], "diagnostics": diagnostics}


class _Handler(BaseHTTPRequestHandler):
    server_version = "ProjectViz/" + __version__
    sys_version = ""

    def log_message(self, *args):
        # A browser entry URL contains a capability; never log request URLs.
        pass

    def _reply(self, status, body=b"", content_type="application/json; charset=utf-8", headers=None, head=False):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _matches(self, supplied):
        return isinstance(supplied, str) and len(supplied) <= 512 and hmac.compare_digest(supplied.encode("utf-8"), self.server.token.encode("utf-8"))

    def _bearer(self):
        header = self.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        return scheme.lower() == "bearer" and self._matches(token.strip())

    def _authorized(self):
        if self._bearer():
            return True
        cookie = http.cookies.SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            value = cookie.get(self.server.cookie_name)
            return bool(value and self._matches(value.value))
        except (http.cookies.CookieError, ValueError):
            return False

    def _local_host(self):
        return self.headers.get("Host", "").lower() in self.server.allowed_hosts

    def _origin_allowed(self):
        origin = self.headers.get("Origin")
        return origin is None or origin == "http://" + self.headers.get("Host", "").lower()

    def _config(self):
        config = read_json(self.server.ctx["state"] / "config.json", self.server.ctx["config"])
        original = self.server.ctx["config"]
        if config.get("projectId") != original["projectId"] or config.get("projectRoot") != original["projectRoot"]:
            raise ValueError("Project identity changed")
        return config

    def do_GET(self):
        self._get(False)

    def do_HEAD(self):
        self._get(True)

    def _get(self, head):
        if not self._local_host():
            return self._reply(403, {"error": "Loopback Host header required"}, head=head)
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        if parsed.path in {"/", "/index.html"} and self._matches(query.get("token", [None])[0]):
            query.pop("token", None)
            target = urlunsplit(("", "", parsed.path, urlencode(query, doseq=True), ""))
            return self._reply(302, headers={"Location": target,
                "Set-Cookie": f"{self.server.cookie_name}={self.server.token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=86400"}, head=head)
        if not self._authorized():
            return self._reply(403, {"error": "Use the private local access link or a Bearer token"}, head=head)
        try:
            if parsed.path in _ASSETS:
                filename, mime = _ASSETS[parsed.path]
                return self._reply(200, (self.server.asset_root / filename).read_bytes(), mime, head=head)
            if parsed.path == "/api/health":
                return self._reply(200, {"projectId": self.server.ctx["config"]["projectId"],
                    "instanceId": self.server.instance_id, "pid": os.getpid(), "version": __version__}, head=head)
            with self.server.collector_mutex:
                if self.server.stopping.is_set():
                    return self._reply(503, {"error": "Service is stopping"}, head=head)
                if parsed.path == "/api/tree":
                    coverage = _coverage(self.server.collector, self.server.runtime_errors)
                    result = self.server.store.snapshot(self._config(), coverage)
                elif parsed.path == "/api/history":
                    result = self.server.store.history(query.get("id", [""])[0])
                elif parsed.path == "/api/file":
                    relative = query.get("path", [""])[0]
                    path = safe_project_file(self.server.ctx["project"], relative)
                    if not path.is_file():
                        raise FileNotFoundError("File not found")
                    with path.open("rb") as stream:
                        raw = stream.read(128 * 1024 + 1)
                    truncated = len(raw) > 128 * 1024
                    raw = raw[:128 * 1024]
                    binary = b"\0" in raw
                    result = {"path": path.relative_to(self.server.ctx["project"]).as_posix(),
                              "text": "" if binary else public_text(raw.decode("utf-8", errors="replace"), 128 * 1024),
                              "truncated": truncated, "binary": binary,
                              "modifiedAt": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()}
                else:
                    return self._reply(404, {"error": "Endpoint not found"}, head=head)
            self._reply(200, result, head=head)
        except FileNotFoundError:
            self._reply(404, {"error": "Project file or record not found"}, head=head)
        except (ValueError, TypeError, KeyError):
            self._reply(400, {"error": "Invalid request or project-relative path"}, head=head)
        except Exception as exc:
            self._reply(503, {"error": "Local service operation failed", "type": type(exc).__name__}, head=head)

    def do_POST(self):
        if not self._local_host() or not self._origin_allowed():
            return self._reply(403, {"error": "Cross-origin requests are not accepted"})
        if not self._bearer():
            return self._reply(403, {"error": "This operation requires a Bearer token"})
        parsed = urlsplit(self.path)
        if parsed.path not in {"/api/import-history", "/api/shutdown"}:
            return self._reply(404, {"error": "Endpoint not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192 or self.headers.get_content_type() != "application/json":
                return self._reply(400, {"error": "Send a JSON object of at most 8192 bytes"})
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Expected an object")
            if parsed.path == "/api/shutdown":
                if payload.get("instanceId") != self.server.instance_id:
                    return self._reply(409, {"error": "Instance identity does not match"})
                self._reply(200, {"ok": True, "instanceId": self.server.instance_id})
                self.server.stopping.set()
                threading.Thread(target=self.server.shutdown, name="project-viz-shutdown", daemon=True).start()
                return
            seconds = payload.get("seconds", 5)
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 <= seconds <= 30:
                return self._reply(400, {"error": "seconds must be a finite number from 0 to 30"})
            deadline = time.monotonic() + seconds
            while not self.server.stopping.is_set() and time.monotonic() < deadline:
                with self.server.collector_mutex:
                    diagnostics = self.server.collector.import_history()
                if diagnostics.get("historyComplete"):
                    break
                self.server.stopping.wait(.001)
            with self.server.collector_mutex:
                coverage = _coverage(self.server.collector, self.server.runtime_errors)
            self._reply(200, {"coverage": coverage, "diagnostics": coverage["diagnostics"], "historyComplete": coverage["historyComplete"]})
        except (ValueError, TypeError, UnicodeDecodeError):
            self._reply(400, {"error": "Invalid JSON request"})
        except Exception as exc:
            self._reply(503, {"error": "Local service operation failed", "type": type(exc).__name__})


def serve(ctx, host="127.0.0.1", port=0):
    """Block until authenticated shutdown; one collector lease per project state."""
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Only IPv4 loopback hosts 127.0.0.1 and localhost are supported")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("Invalid loopback port")
    state = Path(ctx["state"])
    token = (state / "access-token").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise ValueError("Access token is missing or invalid; initialize the project first")
    with state_lock(state, "collector", timeout=0):
        store = Store(state, ctx["config"]["projectId"])
        httpd = None
        collector = None
        thread = None
        stop = threading.Event()
        mutex = threading.RLock()
        instance = uuid.uuid4().hex
        descriptor = state / "server.json"
        try:
            collector = Collector(ctx["project"], Path(ctx["config"]["codexHome"]), state, store.ingest_codex)
            httpd = _Server(("127.0.0.1", port), _Handler)
            httpd.ctx, httpd.token = ctx, token
            httpd.instance_id = instance
            httpd.cookie_name = "project_viz_" + instance
            httpd.asset_root = Path(__file__).resolve().parents[2] / "assets/site"
            httpd.collector, httpd.store = collector, store
            httpd.collector_mutex, httpd.stopping = mutex, stop
            httpd.runtime_errors = []
            httpd.allowed_hosts = {f"127.0.0.1:{httpd.server_port}", f"localhost:{httpd.server_port}"}

            def monitor():
                while not stop.is_set():
                    try:
                        with mutex:
                            if not stop.is_set():
                                collector.poll()
                    except Exception as exc:
                        with mutex:
                            message = "Collector poll failed: " + type(exc).__name__
                            if message not in httpd.runtime_errors:
                                httpd.runtime_errors.append(message)
                            del httpd.runtime_errors[:-20]
                    stop.wait(2)

            thread = threading.Thread(target=monitor, name="project-viz-collector", daemon=True)
            thread.start()
            atomic_json(descriptor, {"projectId": ctx["config"]["projectId"], "instanceId": instance,
                "pid": os.getpid(), "host": "127.0.0.1", "port": httpd.server_port, "startedAt": utcnow()})
            httpd.serve_forever(poll_interval=.1)
        finally:
            stop.set()
            if httpd:
                httpd.server_close()
            if thread:
                thread.join()
            with mutex:
                iterator = getattr(collector, "_scan_iterator", None)
                if iterator is not None:
                    iterator.close()
                store.close()
            try:
                current = read_json(descriptor, {})
                if current.get("instanceId") == instance:
                    descriptor.unlink(missing_ok=True)
            except (OSError, ValueError, TypeError):
                pass
