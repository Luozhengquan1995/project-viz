"""Real loopback HTTP tests using isolated projects and synthetic rollouts."""
from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/project-viz/scripts"))
from project_viz_runtime.common import atomic_json, context, read_json
from project_viz_runtime.server import serve


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.home = self.root / "codex"
        (self.home / "sessions").mkdir(parents=True)
        self.ctx = context(self.project, self.root / "state", self.home, create=True)
        self.token = (self.ctx["state"] / "access-token").read_text(encoding="utf-8").strip()
        self.failures = []
        self.threads = []
        self.descriptor = None
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.stop)

    def start(self):
        def run():
            try:
                serve(self.ctx)
            except Exception as exc:
                self.failures.append(exc)
        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        for _ in range(300):
            self.descriptor = read_json(self.ctx["state"] / "server.json")
            if self.descriptor or self.failures:
                break
            time.sleep(.01)
        self.assertFalse(self.failures, self.failures)
        self.assertIsNotNone(self.descriptor)

    def request(self, path, method="GET", payload=None, bearer=False, cookie=None, headers=None):
        request_headers = dict(headers or {})
        if bearer:
            request_headers["Authorization"] = "Bearer " + self.token
        if cookie:
            request_headers["Cookie"] = cookie
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.descriptor["port"], timeout=5)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            raw = response.read()
            result = json.loads(raw) if raw and response.getheader("Content-Type", "").startswith("application/json") else raw
            return response.status, dict(response.getheaders()), result
        finally:
            connection.close()

    def stop(self):
        live_threads = [thread for thread in self.threads if thread.is_alive()]
        if self.descriptor and live_threads:
            try:
                self.request("/api/shutdown", "POST", {"instanceId": self.descriptor["instanceId"]}, bearer=True)
            except (OSError, http.client.HTTPException):
                pass
        for thread in self.threads:
            thread.join(timeout=5)
        if any(thread.is_alive() for thread in self.threads):
            self.fail("Synthetic server did not stop")

    def fixture(self):
        stamp = datetime.now(timezone.utc).isoformat()
        records = [
            {"type": "session_meta", "payload": {"id": "synthetic-session", "cwd": str(self.project)}},
            {"type": "turn_context", "payload": {"turn_id": "synthetic-turn"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Check synthetic protocol"}]}},
        ]
        (self.home / "sessions/rollout-synthetic.jsonl").write_text("".join(json.dumps({"timestamp": stamp, **record}) + "\n" for record in records), encoding="utf-8")

    def test_authenticated_entry_cookie_static_allowlist_and_health(self):
        self.start()
        for path in ("/", "/flow.js", "/api/health", "/api/tree", "/api/file?path=readme.md"):
            self.assertEqual(self.request(path)[0], 403)
        status, headers, _ = self.request("/?token=" + self.token + "&view=graph")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/?view=graph")
        self.assertNotIn(self.token, headers["Location"])
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertIn(self.descriptor["instanceId"], cookie.split("=", 1)[0])
        for path in ("/", "/index.html", "/flow.js", "/flow.css", "/flow-labels.js"):
            status, headers, data = self.request(path, cookie=cookie)
            self.assertEqual(status, 200, path)
            self.assertTrue(data)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(self.request("/collector.json", bearer=True)[0], 404)
        self.assertEqual(self.request("/../scripts/project_viz_runtime/server.py", bearer=True)[0], 404)
        status, _, health = self.request("/api/health", bearer=True)
        self.assertEqual(status, 200)
        self.assertEqual(health["projectId"], self.ctx["config"]["projectId"])
        self.assertEqual(health["instanceId"], self.descriptor["instanceId"])
        self.assertIn("version", health)
        self.assertEqual(self.request("/api/health?token=" + self.token)[0], 403)
        self.assertEqual(self.request("/api/health", cookie="project_viz_other=" + self.token)[0], 403)

    def test_file_boundary_private_paths_binary_and_truncation(self):
        (self.project / "readme.md").write_text("合成文档\npassword=testing-secret", encoding="utf-8")
        (self.project / "large.txt").write_text("x" * (128 * 1024 + 100), encoding="utf-8")
        (self.project / "binary.bin").write_bytes(b"abc\0def")
        (self.project / ".env").write_text("PRIVATE", encoding="utf-8")
        (self.root / "outside.txt").write_text("OUTSIDE", encoding="utf-8")
        try:
            (self.project / "outside-link.txt").symlink_to(self.root / "outside.txt")
        except OSError:
            pass
        self.start()
        status, _, result = self.request("/api/file?path=readme.md", bearer=True)
        self.assertEqual(status, 200)
        self.assertEqual(result["path"], "readme.md")
        self.assertIn("合成文档", result["text"])
        self.assertNotIn("testing-secret", result["text"])
        self.assertFalse(result["binary"])
        self.assertIn("modifiedAt", result)
        for relative in ("../outside.txt", str(self.root / "outside.txt"), ".env", "C:/private.txt", "sub\\secret.txt"):
            self.assertEqual(self.request("/api/file?path=" + quote(relative), bearer=True)[0], 400, relative)
        if (self.project / "outside-link.txt").is_symlink():
            self.assertEqual(self.request("/api/file?path=outside-link.txt", bearer=True)[0], 400)
        self.assertEqual(self.request("/api/file?path=missing.txt", bearer=True)[0], 404)
        binary = self.request("/api/file?path=binary.bin", bearer=True)[2]
        self.assertTrue(binary["binary"])
        self.assertEqual(binary["text"], "")
        self.assertTrue(self.request("/api/file?path=large.txt", bearer=True)[2]["truncated"])

    def test_live_import_snapshot_title_refresh_and_history(self):
        self.fixture()
        self.start()
        status, _, imported = self.request("/api/import-history", "POST", {"seconds": 1}, bearer=True)
        self.assertEqual(status, 200)
        self.assertTrue(imported["historyComplete"])
        tree = self.request("/api/tree", bearer=True)[2]
        tasks = [node for node in tree["nodes"] if node["kind"] == "task"]
        self.assertEqual(tasks[0]["label"], "Check synthetic protocol")
        self.assertEqual(tree["coverage"]["sourceCount"], 1)
        self.assertEqual(tree["coverage"]["indexedCount"], 1)
        self.assertEqual(tree["coverage"]["pendingSources"], 0)
        history = next(node["id"] for node in tree["nodes"] if node["kind"] == "history")
        records = self.request("/api/history?id=" + quote(history), bearer=True)[2]
        self.assertIn("Check synthetic protocol", json.dumps(records))
        changed = dict(self.ctx["config"], title="Renamed synthetic project")
        atomic_json(self.ctx["state"] / "config.json", changed)
        refreshed = self.request("/api/tree", bearer=True)[2]
        self.assertEqual(refreshed["project"]["title"], changed["title"])
        self.assertEqual(next(node["label"] for node in refreshed["nodes"] if node["kind"] == "task"), tasks[0]["label"])

    def test_mutations_require_bearer_same_origin_and_correct_instance(self):
        self.start()
        cookie = self.request("/?token=" + self.token)[1]["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.request("/api/import-history", "POST", {"seconds": 0}, cookie=cookie)[0], 403)
        self.assertEqual(self.request("/api/import-history", "POST", {"seconds": 0}, bearer=True, headers={"Origin": "https://outside.example"})[0], 403)
        self.assertEqual(self.request("/api/health", bearer=True, headers={"Host": "outside.example"})[0], 403)
        origin = f'http://127.0.0.1:{self.descriptor["port"]}'
        self.assertEqual(self.request("/api/import-history", "POST", {"seconds": 0}, bearer=True, headers={"Origin": origin})[0], 200)
        self.assertEqual(self.request("/api/import-history", "POST", {"seconds": 0}, bearer=True, headers={"Origin": f'http://localhost:{self.descriptor["port"]}'})[0], 403)
        for seconds in (-1, 31, True, "1"):
            self.assertEqual(self.request("/api/import-history", "POST", {"seconds": seconds}, bearer=True)[0], 400)
        self.assertEqual(self.request("/api/shutdown", "POST", {"instanceId": "other-instance", "pid": 1}, bearer=True)[0], 409)
        self.assertEqual(self.request("/api/health", bearer=True)[0], 200)
        status = self.request("/api/shutdown", "POST", {"instanceId": self.descriptor["instanceId"]}, bearer=True)[0]
        self.assertEqual(status, 200)
        self.threads[-1].join(timeout=5)
        self.assertFalse(self.threads[-1].is_alive())
        self.assertFalse((self.ctx["state"] / "server.json").exists())
        self.assertFalse(self.failures)

    def test_loopback_only_and_exclusive_collector_lease(self):
        for host in ("0.0.0.0", "192.168.0.1", "example.com", "::1"):
            with self.assertRaisesRegex(ValueError, "loopback"):
                serve(self.ctx, host=host)
        self.start()
        with self.assertRaises(RuntimeError):
            serve(self.ctx)
        self.assertEqual(self.request("/api/health", bearer=True)[0], 200)

    def test_forwarded_host_accepts_local_port_but_rejects_other_hosts_and_origins(self):
        self.start()
        local_port = 8894 if self.descriptor['port'] != 8894 else 8895
        for host in [f'127.0.0.1:{local_port}', f'localhost:{local_port}', f'[::1]:{local_port}', 'localhost']:
            self.assertEqual(self.request('/api/health', bearer=True, headers={'Host': host})[0], 200, host)
            status, headers, _ = self.request('/?token=' + self.token, headers={'Host': host})
            self.assertEqual(status, 302)
            self.assertEqual(headers['Location'], '/')
            cookie = headers['Set-Cookie'].split(';', 1)[0]
            self.assertEqual(self.request('/api/tree', cookie=cookie, headers={'Host': host})[0], 200)
            self.assertEqual(self.request('/api/import-history', 'POST', {'seconds': 0}, bearer=True,
                                          headers={'Host': host, 'Origin': 'http://' + host})[0], 200)
        for host in ['localhost.evil.example:8894', '127.0.0.1.evil.example:8894', '0.0.0.0:8894',
                     '127.0.0.1:0', '127.0.0.1:65536', 'user@localhost:8894', 'localhost:8894/path']:
            self.assertEqual(self.request('/api/health', bearer=True, headers={'Host': host})[0], 403, host)
        self.assertEqual(self.request('/api/import-history', 'POST', {'seconds': 0}, bearer=True,
                                      headers={'Host': f'localhost:{local_port}', 'Origin': f'http://localhost:{local_port + 1}'})[0], 403)

    def test_shutdown_does_not_delete_another_instance_descriptor(self):
        self.start()
        foreign = {**self.descriptor, "instanceId": "replacement-instance"}
        atomic_json(self.ctx["state"] / "server.json", foreign)
        self.stop()
        self.assertEqual(read_json(self.ctx["state"] / "server.json")["instanceId"], "replacement-instance")


if __name__ == "__main__":
    unittest.main()
