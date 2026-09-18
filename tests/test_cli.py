"""Subprocess tests exercise the same self-contained entry point users install."""
from __future__ import annotations
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / 'skills/project-viz/scripts/project_viz.py'


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='project-viz-cli-')
        self.root = Path(self.temp.name)
        self.project = self.root / 'project with 空格'
        self.project.mkdir()
        self.state = self.root / 'private state'
        self.codex = self.root / 'empty-codex'
        self.codex.mkdir()
        self.env = {**os.environ, 'PYTHONIOENCODING': 'utf-8'}
        self.env.pop('SSH_CONNECTION', None)
        self.env.pop('SSH_TTY', None)

    def tearDown(self):
        self.call('stop', check=False)
        self.temp.cleanup()

    def call(self, command, *args, payload=None, check=True, state=None, entry=None):
        process = subprocess.run([sys.executable, str(entry or ENTRY), command, '--project', str(self.project),
                                  '--state-dir', str(state or self.state), '--codex-home', str(self.codex), *map(str, args)],
                                 input=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                                 capture_output=True, text=True, encoding='utf-8', env=self.env, timeout=35)
        if check:
            self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
            return json.loads(process.stdout)
        return process

    def test_uninitialized_doctor_and_status_are_read_only(self):
        self.assertFalse(self.call('doctor')['initialized'])
        self.assertFalse(self.call('status')['running'])
        self.assertFalse(self.state.exists())
        self.assertFalse(self.call('stop')['running'])
        self.assertFalse(self.state.exists())

    def test_initialize_concurrently_reuses_identity(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(lambda _: self.call('init', '--title', '研究流程'), range(4)))
        self.assertEqual(len({value['projectId'] for value in values}), 1)
        self.assertEqual(self.call('status')['title'], '研究流程')

    def test_semantic_roundtrip_rollback_and_export(self):
        self.call('init', '--title', 'Work', '--goal', 'Deliver an importer')
        event = {'event_id': 'step-1', 'work_id': 'import', 'title': 'Build importer', 'execution_state': 'running'}
        self.assertFalse(self.call('emit', payload=event)['duplicate'])
        self.assertTrue(self.call('emit', payload=event)['duplicate'])
        bad = self.call('emit', payload={**event, 'title': 'conflicting'}, check=False)
        self.assertNotEqual(bad.returncode, 0)
        catalog = self.root / 'catalog.json'
        catalog.write_text(json.dumps({'project': {'title': 'Importer'}, 'nodes': [
            {'id': 'verify', 'parentId': 'import', 'label': 'Verify input', 'execution_state': 'finished', 'outcome': 'passed', 'evidence': [{'path': 'report.md', 'line': 1}]}]}), encoding='utf-8')
        self.call('catalog', '--file', catalog)
        self.assertEqual(self.call('status')['title'], 'Importer')
        (self.project / 'README.md').write_text('Import tool\n', encoding='utf-8')
        ctx = self.call('context')
        self.assertEqual(len(ctx['works']), 2)
        self.assertEqual(ctx['documents'][0]['path'], 'README.md')
        output = self.root / 'graph.json'
        self.call('export', '--output', output)
        exported = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(exported['project']['title'], 'Importer')
        self.assertEqual(len(exported['nodes']), 2)
        self.assertNotIn(str(self.state), json.dumps(exported))
        self.assertNotIn('publicRecords', exported)
        catalog.write_text(json.dumps({'project': {'title': 'Should not apply'}, 'nodes': [{'id': 'x', 'label': 'X'}, {'id': 'bad', 'label': 'bad', 'parentId': 'missing'}]}), encoding='utf-8')
        self.assertNotEqual(self.call('catalog', '--file', catalog, check=False).returncode, 0)
        self.assertEqual(self.call('status')['title'], 'Importer')
        self.assertEqual(len(self.call('context')['works']), 2)

    def test_start_reuse_live_update_and_matching_stop(self):
        launched = self.call('start')
        self.assertTrue(launched['running'])
        self.assertFalse(launched['reused'])
        reused = self.call('start')
        self.assertTrue(reused['reused'])
        self.assertEqual(launched['instanceId'], reused['instanceId'])
        self.call('emit', payload={'event_id': 'e', 'work_id': 'work', 'title': 'Verify live updates'})
        token = (self.state / 'access-token').read_text()
        request = urllib.request.Request(f"http://127.0.0.1:{launched['port']}/api/tree", headers={'Authorization': 'Bearer ' + token})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request) as response:
            tree = json.load(response)
        self.assertIn('work', {n['id'] for n in tree['nodes']})
        result = self.call('import-history', '--seconds', '.3')
        self.assertIsInstance(result, dict)
        self.assertTrue(self.call('stop')['stopped'])
        self.assertFalse(self.call('status')['running'])
        restarted = self.call('start')
        self.assertNotEqual(restarted['instanceId'], launched['instanceId'])
        self.assertEqual(self.call('context')['works'][0]['id'], 'work')

    def test_stale_or_other_instance_descriptor_does_not_stop_viewer(self):
        live = self.call('start')
        descriptor_file = self.state / 'server.json'
        descriptor = json.loads(descriptor_file.read_text())
        modified = {**descriptor, 'instanceId': 'different-instance'}
        descriptor_file.write_text(json.dumps(modified), encoding='utf-8')
        try:
            self.assertFalse(self.call('stop')['stopped'])
        finally:
            descriptor_file.write_text(json.dumps(descriptor), encoding='utf-8')
        self.assertTrue(self.call('status')['running'])
        self.assertEqual(self.call('status')['instanceId'], live['instanceId'])

    def test_binding_state_to_other_project_fails(self):
        self.call('init')
        self.project = self.root / 'unrelated'
        self.project.mkdir()
        failed = self.call('init', check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('different project', failed.stderr)

    def test_remote_access_plan_reuses_viewer_and_preserves_ssh_alias_port(self):
        launched = self.call('start', '--ssh-target', 'synthetic-server', '--local-port', '8894')
        access = launched['access']
        self.assertEqual(access['localPort'], 8894)
        self.assertEqual(access['remotePort'], launched['port'])
        self.assertEqual(access['sshTarget'], 'synthetic-server')
        self.assertIsNone(access['sshPort'])
        self.assertNotIn('-p', access['sshArgs'])
        self.assertIn('http://127.0.0.1:8894/?token=', launched['url'])
        self.assertEqual(self.call('status')['access']['browserUrl'], launched['url'])
        output = self.root / 'project-viz-access.json'
        changed = self.call('access', '--local-port', '8895', '--ssh-port', '2222', '--output', output)
        saved = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(saved['localPort'], 8895)
        self.assertEqual(saved['sshPort'], 2222)
        self.assertEqual(saved['instanceId'], launched['instanceId'])
        self.assertIn('8895', saved['browserUrl'])
        self.assertEqual(self.call('status')['instanceId'], launched['instanceId'])
        self.assertIn('-p', changed['sshArgs'])
        if os.name != 'nt':
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_ssh_session_inference_and_explicit_reachable_target_override(self):
        self.env['SSH_CONNECTION'] = '192.0.2.10 43210 198.51.100.20 2222'
        launched = self.call('start')
        self.assertTrue(launched['access']['inferredTarget'])
        self.assertTrue(launched['access']['sshTarget'].endswith('@198.51.100.20'))
        self.assertEqual(launched['access']['sshPort'], 2222)
        changed = self.call('access', '--ssh-target', 'reachable-alias')
        self.assertFalse(changed['inferredTarget'])
        self.assertIsNone(changed['sshPort'])
        self.assertEqual(changed['sshTarget'], 'reachable-alias')

    def test_invalid_access_target_does_not_start_viewer_or_change_plan(self):
        failed = self.call('start', '--ssh-target=-oProxyCommand=bad', check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(self.call('status')['running'])
        launched = self.call('start')
        failed = self.call('access', '--local-port', '8894', check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.call('status')['instanceId'], launched['instanceId'])
        self.assertNotIn('access', self.call('status'))

    def test_start_replaces_older_viewer_without_losing_project_history(self):
        older = self.root / 'older-skill'
        shutil.copytree(ROOT / 'skills/project-viz', older, ignore=shutil.ignore_patterns('__pycache__'))
        (older / 'scripts/project_viz_runtime/__init__.py').write_text('__version__ = "0.1.0"\n', encoding='utf-8')
        old = self.call('start', entry=older / 'scripts/project_viz.py')
        self.assertEqual(old['serverVersion'], '0.1.0')
        self.call('emit', payload={'event_id': 'old-work', 'work_id': 'preserved', 'title': 'Preserve history'})
        upgraded = self.call('start', '--ssh-target', 'synthetic-server', '--local-port', 8894)
        self.assertTrue(upgraded['restartedForUpgrade'])
        self.assertEqual(upgraded['serverVersion'], '0.2.0')
        self.assertNotEqual(old['instanceId'], upgraded['instanceId'])
        self.assertEqual(old['port'], upgraded['port'])
        self.assertEqual(self.call('context')['works'][0]['id'], 'preserved')


if __name__ == '__main__':
    unittest.main()
