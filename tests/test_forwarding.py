"""Real local OpenSSH forwarding, using only temporary keys/config/project state.

POSIX + ssh/sshd/ssh-keygen are required. Missing executables/platform support
skip explicitly; startup, authentication, or product failures never silently skip.
Nothing touches the user's SSH files, agents, known_hosts, or existing services.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import queue
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

if os.name == 'posix':
    import pwd

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'skills/project-viz/scripts'
CLI = SCRIPTS / 'project_viz.py'
sys.path.insert(0, str(SCRIPTS))
from project_viz_runtime.common import context, read_json
from project_viz_runtime.server import serve

SSH = shutil.which('ssh')
KEYGEN = shutil.which('ssh-keygen')
SSHD = shutil.which('sshd') or ('/usr/sbin/sshd' if Path('/usr/sbin/sshd').is_file() else None)
SUPPORTED = os.name == 'posix' and bool(SSH and KEYGEN and SSHD)


@unittest.skipUnless(SUPPORTED, 'Real temporary SSH forwarding requires POSIX, ssh, sshd, and ssh-keygen')
class ForwardingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='project-viz-forwarding-')
        self.root = Path(self.temporary.name)
        self.processes = []
        self.open_files = []
        self.server_thread = None
        self.descriptor = None
        self.server_errors = []
        self.token = ''
        self.wrapper_pids = self.root / 'wrapper-pids.txt'
        self.addCleanup(self.cleanup)
        self.start_temporary_sshd()
        self.project = self.root / 'synthetic-project'
        self.project.mkdir()
        (self.project / 'README.md').write_text('# Synthetic forwarding project\n', encoding='utf-8')
        self.codex = self.root / 'empty-codex'
        (self.codex / 'sessions').mkdir(parents=True)
        self.ctx = context(self.project, self.root / 'state', self.codex, create=True)
        self.token = (self.ctx['state'] / 'access-token').read_text(encoding='utf-8').strip()

        def run_server():
            try:
                serve(self.ctx)
            except Exception as error:
                self.server_errors.append(error)

        self.server_thread = threading.Thread(target=run_server, daemon=True)
        self.server_thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.descriptor = read_json(self.ctx['state'] / 'server.json')
            if self.descriptor or self.server_errors:
                break
            time.sleep(.02)
        self.assertFalse(self.server_errors)
        self.assertIsNotNone(self.descriptor, 'Synthetic viewer did not start')
        self.assertEqual(self.request(self.descriptor['port'], '/api/health', bearer=True)[0], 200)

    @staticmethod
    def free_port():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(('127.0.0.1', 0))
            return listener.getsockname()[1]

    def diagnostic(self, value):
        return str(value).replace(self.token, '<synthetic-token>') if self.token else str(value)

    def start_temporary_sshd(self):
        self.ssh_port = self.free_port()
        username = pwd.getpwuid(os.getuid()).pw_name
        for name in ('host-key', 'client-key'):
            generated = subprocess.run(
                [KEYGEN, '-q', '-t', 'ed25519', '-N', '', '-f', str(self.root / name)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
        authorized = self.root / 'authorized_keys'
        authorized.write_bytes((self.root / 'client-key.pub').read_bytes())
        authorized.chmod(0o600)
        daemon_config = self.root / 'sshd_config'
        daemon_config.write_text('\n'.join([
            'ListenAddress 127.0.0.1', 'Port ' + str(self.ssh_port),
            'HostKey ' + str(self.root / 'host-key'), 'PidFile ' + str(self.root / 'sshd.pid'),
            'AuthorizedKeysFile ' + str(authorized), 'AllowUsers ' + username,
            'PasswordAuthentication no', 'KbdInteractiveAuthentication no',
            'PubkeyAuthentication yes', 'AuthenticationMethods publickey',
            'UsePAM no', 'StrictModes no', 'PermitRootLogin prohibit-password',
            'AllowTcpForwarding local', 'PermitOpen 127.0.0.1:*',
            'PrintMotd no', 'LogLevel ERROR',
        ]) + '\n', encoding='utf-8')
        server_log = (self.root / 'sshd.log').open('w+', encoding='utf-8')
        self.open_files.append(server_log)
        daemon = subprocess.Popen(
            [SSHD, '-D', '-e', '-f', str(daemon_config)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=server_log,
        )
        self.processes.append(daemon)
        ready = False
        deadline = time.monotonic() + 5
        while daemon.poll() is None and time.monotonic() < deadline:
            try:
                with socket.create_connection(('127.0.0.1', self.ssh_port), timeout=.2) as connection:
                    ready = connection.recv(100).startswith(b'SSH-')
                    break
            except OSError:
                time.sleep(.02)
        if not ready:
            server_log.flush()
            server_log.seek(0)
            self.fail('Temporary sshd failed to start: ' + server_log.read())
        known = self.root / 'known_hosts'
        known.write_text('[127.0.0.1]:' + str(self.ssh_port) + ' ' +
                         (self.root / 'host-key.pub').read_text(encoding='utf-8'), encoding='utf-8')
        self.ssh_config = self.root / 'ssh_config'
        self.ssh_config.write_text('\n'.join([
            'Host project-viz-integration', ' HostName 127.0.0.1',
            ' User ' + username, ' Port ' + str(self.ssh_port),
            ' IdentityFile ' + str(self.root / 'client-key'),
            ' IdentitiesOnly yes', ' IdentityAgent none', ' BatchMode yes',
            ' StrictHostKeyChecking yes', ' UserKnownHostsFile ' + str(known),
            ' GlobalKnownHostsFile /dev/null', ' ControlMaster no', ' ControlPath none',
            ' LogLevel ERROR',
        ]) + '\n', encoding='utf-8')
        authentication = subprocess.run(
            [SSH, '-F', str(self.ssh_config), 'project-viz-integration', 'true'],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(authentication.returncode, 0,
                         'Temporary sshd public-key authentication failed: ' + authentication.stderr)
        wrapper_dir = self.root / 'wrapper-bin'
        wrapper_dir.mkdir()
        wrapper = wrapper_dir / 'ssh'
        wrapper.write_text('#!/bin/sh\nprintf "%s\\n" "$$" >> ' + shlex.quote(str(self.wrapper_pids)) +
                           '\nexec ' + shlex.quote(SSH) + ' -F ' + shlex.quote(str(self.ssh_config)) + ' "$@"\n',
                           encoding='utf-8')
        wrapper.chmod(0o700)
        self.env = dict(os.environ)
        self.env['PATH'] = str(wrapper_dir) + os.pathsep + self.env.get('PATH', '')
        self.env.pop('SSH_AUTH_SOCK', None)

    def request(self, port, path, method='GET', payload=None, bearer=False, cookie=None, headers=None):
        supplied = dict(headers or {})
        if bearer:
            supplied['Authorization'] = 'Bearer ' + self.token
        if cookie:
            supplied['Cookie'] = cookie
        data = None
        if payload is not None:
            data = json.dumps(payload).encode('utf-8')
            supplied['Content-Type'] = 'application/json'
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        try:
            connection.request(method, path, body=data, headers=supplied)
            response = connection.getresponse()
            raw = response.read()
            content = json.loads(raw) if raw and response.getheader('Content-Type', '').startswith('application/json') else raw
            return response.status, dict(response.getheaders()), content
        finally:
            connection.close()

    def wait_forwarded(self, port, process):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and process.poll() is None:
            try:
                status, _, _ = self.request(port, '/api/health', bearer=True)
                self.assertEqual(status, 200, 'Viewer rejected the forwarded loopback Host port')
                return
            except (OSError, http.client.HTTPException):
                time.sleep(.04)
        self.fail('Real SSH forward did not become usable')

    def check_browser_session(self, browser_url):
        parsed = urlsplit(browser_url)
        self.assertEqual(parsed.hostname, '127.0.0.1')
        self.assertNotEqual(parsed.port, self.descriptor['port'])
        self.assertEqual(self.request(parsed.port, '/api/health')[0], 403)
        status, headers, _ = self.request(parsed.port, parsed.path + '?' + parsed.query)
        self.assertEqual(status, 302)
        self.assertEqual(headers['Location'], '/')
        self.assertNotIn(self.token, headers['Location'])
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        cookie = headers['Set-Cookie'].split(';', 1)[0]
        for path in ('/', '/flow.js', '/flow.css', '/api/tree'):
            status, _, body = self.request(parsed.port, path, cookie=cookie)
            self.assertEqual(status, 200, path)
            self.assertTrue(body)
        health = self.request(parsed.port, '/api/health', cookie=cookie)[2]
        self.assertEqual(health['projectId'], self.ctx['config']['projectId'])
        self.assertEqual(health['instanceId'], self.descriptor['instanceId'])
        return parsed.port, cookie

    def test_real_ssh_different_ports_cookie_static_api_and_origin(self):
        local_port = self.free_port()
        log = (self.root / 'direct-ssh.log').open('w+', encoding='utf-8')
        self.open_files.append(log)
        child = subprocess.Popen(
            [SSH, '-F', str(self.ssh_config), '-N', '-T', '-o', 'ExitOnForwardFailure=yes',
             '-L', f'127.0.0.1:{local_port}:127.0.0.1:{self.descriptor["port"]}',
             'project-viz-integration'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
        )
        self.processes.append(child)
        self.wait_forwarded(local_port, child)
        port, cookie = self.check_browser_session(f'http://127.0.0.1:{local_port}/?token={self.token}')
        local_origin = f'http://127.0.0.1:{port}'
        remote_origin = f'http://127.0.0.1:{self.descriptor["port"]}'
        self.assertEqual(self.request(port, '/api/import-history', 'POST', {'seconds': 0},
                                      bearer=True, headers={'Origin': local_origin})[0], 200)
        self.assertEqual(self.request(port, '/api/import-history', 'POST', {'seconds': 0},
                                      bearer=True, headers={'Origin': remote_origin})[0], 403)
        self.assertEqual(self.request(port, '/api/import-history', 'POST', {'seconds': 0},
                                      cookie=cookie, headers={'Origin': local_origin})[0], 403)
        self.assertEqual(self.request(port, '/api/health', bearer=True,
                                      headers={'Host': 'untrusted.invalid:' + str(port)})[0], 403)
        self.assertEqual(self.request(port, '/api/health',
                                      headers={'Authorization': 'Bearer synthetic-wrong-token'})[0], 403)
        self.assertEqual(self.request(self.descriptor['port'], '/api/health', bearer=True)[0], 200)

    def cli(self, *arguments):
        command = [sys.executable, '-B', str(CLI), *arguments, '--project', str(self.project),
                   '--state-dir', str(self.ctx['state']), '--codex-home', str(self.codex)]
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                timeout=15, env=self.env)
        self.assertEqual(result.returncode, 0, self.diagnostic(result.stderr))
        return json.loads(result.stdout)

    def own_ssh_alive(self, pid):
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True,
                                text=True, timeout=3)
        return result.returncode == 0 and str(self.ssh_config) in result.stdout

    def test_cli_plan_real_tunnel_validates_identity_and_interrupt_is_local(self):
        planned_port = self.free_port()
        started = self.cli('start', '--ssh-target', 'project-viz-integration',
                           '--ssh-port', str(self.ssh_port), '--local-port', str(planned_port))
        self.assertEqual(started['access']['remotePort'], self.descriptor['port'])
        plan_file = self.root / 'access.json'
        plan = self.cli('access', '--ssh-target', 'project-viz-integration',
                        '--ssh-port', str(self.ssh_port), '--local-port', str(planned_port),
                        '--output', str(plan_file))
        stored = json.loads(plan_file.read_text(encoding='utf-8'))
        self.assertEqual(stored['kind'], 'project-viz-ssh')
        self.assertEqual(stored['projectId'], self.ctx['config']['projectId'])
        self.assertEqual(stored['instanceId'], self.descriptor['instanceId'])
        self.assertEqual(plan['output'], str(plan_file))
        self.assertEqual(plan_file.stat().st_mode & 0o777, 0o600)
        # Transfer-file command strings are merely display hints.
        stored['sshArgs'] = ['ssh', 'invalid-synthetic-display-only']
        stored['sshCommand'] = 'this synthetic display hint must never run'
        plan_file.write_text(json.dumps(stored), encoding='utf-8')
        stderr = (self.root / 'tunnel.log').open('w+', encoding='utf-8')
        self.open_files.append(stderr)
        tunnel = subprocess.Popen(
            [sys.executable, '-B', str(CLI), 'tunnel', '--file', str(plan_file), '--local-port', '0'],
            cwd=self.root, env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=stderr, text=True, encoding='utf-8',
        )
        self.processes.append(tunnel)
        lines = queue.Queue()
        reader = threading.Thread(target=lambda: lines.put(tunnel.stdout.readline()), daemon=True)
        reader.start()
        try:
            first = lines.get(timeout=15)
        except queue.Empty:
            self.fail('Tunnel did not report a verified connection')
        if not first:
            stderr.flush()
            stderr.seek(0)
            self.fail('Tunnel exited before ready: ' + self.diagnostic(stderr.read()))
        ready = json.loads(first)
        self.assertTrue(ready['connected'])
        self.assertEqual(ready['projectId'], self.ctx['config']['projectId'])
        self.assertEqual(ready['instanceId'], self.descriptor['instanceId'])
        port, _ = self.check_browser_session(ready['browserUrl'])
        self.assertEqual(port, ready['localPort'])
        ssh_pid = int(self.wrapper_pids.read_text(encoding='utf-8').splitlines()[-1])
        self.assertTrue(self.own_ssh_alive(ssh_pid), 'The tunnel must use a real local OpenSSH process')
        tunnel.send_signal(signal.SIGINT)
        remaining, _ = tunnel.communicate(timeout=8)
        reader.join(timeout=1)
        self.assertEqual(tunnel.returncode, 0, self.diagnostic(remaining))
        self.assertFalse(self.own_ssh_alive(ssh_pid), 'Ctrl-C left its OpenSSH child alive')
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', port), timeout=.3)
        self.assertEqual(self.request(self.descriptor['port'], '/api/health', bearer=True)[0], 200,
                         'Stopping the local tunnel must not stop the remote viewer')
        # A different live viewer identity must never produce a ready announcement.
        stored['projectId'] = 'synthetic-wrong-project'
        plan_file.write_text(json.dumps(stored), encoding='utf-8')
        rejected = subprocess.run(
            [sys.executable, '-B', str(CLI), 'tunnel', '--file', str(plan_file), '--local-port', '0'],
            cwd=self.root, env=self.env, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, encoding='utf-8', timeout=12,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertNotIn('"connected": true', rejected.stdout)
        self.assertIn('identity', rejected.stderr.lower())
        rejected_ssh = int(self.wrapper_pids.read_text(encoding='utf-8').splitlines()[-1])
        self.assertFalse(self.own_ssh_alive(rejected_ssh), 'Failed identity check left SSH running')
        self.assertEqual(self.request(self.descriptor['port'], '/api/health', bearer=True)[0], 200)

    def cleanup(self):
        for process in reversed(self.processes[1:]):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            if process.stdout is not None:
                process.stdout.close()
        # Only rescue SSH children identified by this test's unique temporary -F path.
        if self.wrapper_pids.exists():
            for value in self.wrapper_pids.read_text(encoding='utf-8').splitlines():
                pid = int(value)
                if self.own_ssh_alive(pid):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
        if self.descriptor and self.server_thread and self.server_thread.is_alive():
            try:
                self.request(self.descriptor['port'], '/api/shutdown', 'POST',
                             {'instanceId': self.descriptor['instanceId']}, bearer=True)
            except (OSError, http.client.HTTPException):
                pass
            self.server_thread.join(timeout=5)
        if self.processes:
            daemon = self.processes[0]
            if daemon.poll() is None:
                daemon.terminate()
                try:
                    daemon.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=3)
        for handle in self.open_files:
            handle.close()
        self.temporary.cleanup()
        if self.server_thread:
            self.assertFalse(self.server_thread.is_alive(), 'Synthetic viewer did not shut down during cleanup')
            self.assertFalse(self.server_errors)


if __name__ == '__main__':
    unittest.main()
