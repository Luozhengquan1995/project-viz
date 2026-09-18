"""Synthetic plans and child-process fakes exercise portable SSH forwarding."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import unittest
from unittest import mock
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/project-viz/scripts'))
from project_viz_runtime import tunnel

TOKEN = 'synthetic_private_token_' + 'x' * 32
DESCRIPTOR = {'projectId': 'synthetic-project', 'instanceId': 'synthetic-instance', 'port': 43210}


class Child:
    def __init__(self, wait_error=None, initially_exited=False):
        self.alive = not initially_exited
        self.wait_error = wait_error
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self.alive else 0

    def wait(self, timeout=None):
        if self.wait_error and timeout is None and self.alive:
            raise self.wait_error
        self.alive = False
        return 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class TunnelTests(unittest.TestCase):
    def plan(self, **kwargs):
        return tunnel.make_plan(DESCRIPTOR, TOKEN, 'research-box', **kwargs)

    def launch(self, plan=None, child=None, **kwargs):
        child = child or Child()
        output = io.StringIO()
        with mock.patch.object(tunnel.shutil, 'which', return_value='/test/ssh'), \
             mock.patch.object(tunnel.subprocess, 'Popen', return_value=child) as popen, \
             mock.patch.object(tunnel, '_health', return_value=True), \
             mock.patch.object(tunnel, '_available_port', side_effect=lambda port: port or 54321) as available, \
             mock.patch.object(tunnel.webbrowser, 'open', return_value=True) as browser, \
             contextlib.redirect_stdout(output):
            result = tunnel.run_tunnel(plan or self.plan(), **kwargs)
        return result, json.loads(output.getvalue()), popen, available, browser, child

    def test_plan_respects_ssh_alias_port_and_shell_commands(self):
        plan = self.plan()
        self.assertEqual(plan['localPort'], DESCRIPTOR['port'])
        self.assertIsNone(plan['sshPort'])
        self.assertNotIn('-p', plan['sshArgs'])
        self.assertEqual(plan['sshArgs'][-1], 'research-box')
        self.assertEqual(shlex.split(plan['sshCommand']), plan['sshArgs'])
        self.assertTrue(plan['powershellCommand'].startswith("& 'ssh' '-N' '-T'"))
        self.assertNotIn(TOKEN, plan['sshCommand'])
        self.assertNotIn(TOKEN, plan['powershellCommand'])
        self.assertIn(f'127.0.0.1:43210:127.0.0.1:43210', plan['sshArgs'])

    def test_distinct_local_port_and_explicit_ssh_port(self):
        plan = self.plan(local_port=18894, ssh_port=2222)
        self.assertIn('127.0.0.1:18894:127.0.0.1:43210', plan['sshArgs'])
        self.assertEqual(plan['sshArgs'][-3:], ['-p', '2222', 'research-box'])
        self.assertTrue(plan['browserUrl'].startswith('http://127.0.0.1:18894/?token='))

    def test_target_ipv6_and_username(self):
        for target, expected in [('user@node.example', 'user@node.example'), ('user@[2001:db8::1]', 'user@2001:db8::1'), ('::1', '::1')]:
            with self.subTest(target=target):
                self.assertEqual(tunnel.make_plan(DESCRIPTOR, TOKEN, target)['sshTarget'], expected)

    def test_target_rejects_options_control_characters_and_command_injection(self):
        bad = ['-oProxyCommand=touch/tmp/bad', '--', 'host -p 2222', 'host\nCommand', 'user@-bad',
               '-user@host', 'a@b@c', 'host;id', 'host$(id)', '`id`', 'host\x00', 'host/dir',
               'host:22', 'user@[invalid]', '[::1%en0]', 'user@', '', 'a\t', 'http://host']
        for target in bad:
            with self.subTest(target=target), self.assertRaises(ValueError):
                tunnel.make_plan(DESCRIPTOR, TOKEN, target)

    def test_invalid_ports_ids_and_token(self):
        for port in [True, -1, 0, 65536, '8888', 1.5]:
            with self.subTest(port=port), self.assertRaises(ValueError):
                self.plan(local_port=port)
            with self.subTest(ssh=port), self.assertRaises(ValueError):
                self.plan(ssh_port=port)
        for bad in ['', 'a\n', 'abc', 'x' * 257]:
            with self.subTest(token=bad), self.assertRaises(ValueError):
                tunnel.make_plan(DESCRIPTOR, bad, 'host')
        with self.assertRaises(ValueError):
            tunnel.make_plan({**DESCRIPTOR, 'projectId': ''}, TOKEN, 'host')

    def test_tampered_commands_are_rebuilt_and_browser_opens_after_verification(self):
        plan = {**self.plan(), 'sshArgs': ['sh', '-c', 'invalid-command'],
                'sshCommand': 'invalid-command', 'powershellCommand': 'invalid-command'}
        result, ready, popen, available, browser, child = self.launch(plan, open_browser=True)
        self.assertEqual(popen.call_args.args[0], ['/test/ssh', *self.plan()['sshArgs'][1:]])
        self.assertIs(popen.call_args.kwargs['shell'], False)
        self.assertEqual(ready['projectId'], DESCRIPTOR['projectId'])
        self.assertTrue(ready['connected'])
        browser.assert_called_once_with(self.plan()['browserUrl'])
        self.assertTrue(result['stopped'])
        self.assertFalse(child.terminated)

    def test_local_override_zero_selects_unused_port_and_updates_url(self):
        result, ready, popen, available, browser, child = self.launch(local_port=0)
        available.assert_called_once_with(0)
        self.assertEqual(ready['localPort'], 54321)
        self.assertIn('127.0.0.1:54321:127.0.0.1:43210', popen.call_args.args[0])
        self.assertTrue(ready['url'].startswith('http://127.0.0.1:54321/'))

    def test_invalid_plan_urls_never_launch_or_contact_network(self):
        plan = self.plan()
        urls = ['https://127.0.0.1:43210/?token=' + TOKEN, 'http://example.org:43210/?token=' + TOKEN,
                'http://127.0.0.1:43210@other/?token=' + TOKEN, plan['browserUrl'] + '&token=other',
                plan['browserUrl'] + '#fragment', plan['browserUrl'].replace('43210', '12345'),
                plan['browserUrl'].replace('/?', '/other?'), plan['browserUrl'] + '\n']
        with mock.patch.object(tunnel.subprocess, 'Popen') as popen, mock.patch.object(tunnel, '_health') as health:
            for url in urls:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    tunnel.run_tunnel({**plan, 'browserUrl': url})
            popen.assert_not_called()
            health.assert_not_called()

    def test_missing_ssh_is_actionable_and_does_not_launch(self):
        with mock.patch.object(tunnel.shutil, 'which', return_value=None), mock.patch.object(tunnel.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(RuntimeError, 'OpenSSH client was not found'):
                tunnel.run_tunnel(self.plan())
            popen.assert_not_called()

    def test_busy_local_port_fails_before_launch(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            with mock.patch.object(tunnel.shutil, 'which', return_value='/test/ssh'), mock.patch.object(tunnel.subprocess, 'Popen') as popen:
                with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                    tunnel.run_tunnel(self.plan(), local_port=listener.getsockname()[1])
                popen.assert_not_called()
        self.assertGreater(tunnel._available_port(0), 0)

    def test_wrong_health_identity_cleans_child_without_browser_or_ready_output(self):
        child = Child()
        output = io.StringIO()
        with mock.patch.object(tunnel.shutil, 'which', return_value='/test/ssh'), \
             mock.patch.object(tunnel.subprocess, 'Popen', return_value=child), \
             mock.patch.object(tunnel, '_available_port', return_value=43210), \
             mock.patch.object(tunnel, '_health', side_effect=RuntimeError('identity does not match')), \
             mock.patch.object(tunnel.webbrowser, 'open') as browser, contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, 'identity does not match'):
                tunnel.run_tunnel(self.plan(), open_browser=True)
        self.assertTrue(child.terminated)
        browser.assert_not_called()
        self.assertEqual(output.getvalue(), '')

    def test_timeout_and_early_ssh_exit(self):
        for health, exited, error in [(False, False, 'timed out'), (True, True, 'exited before')]:
            child = Child(initially_exited=exited)
            with mock.patch.object(tunnel.shutil, 'which', return_value='/test/ssh'), \
                 mock.patch.object(tunnel.subprocess, 'Popen', return_value=child), \
                 mock.patch.object(tunnel, '_available_port', return_value=43210), \
                 mock.patch.object(tunnel, '_health', return_value=health):
                with self.assertRaisesRegex(RuntimeError, error):
                    tunnel.run_tunnel(self.plan(), timeout=.001)
            self.assertEqual(child.terminated, not exited)

    def test_ctrl_c_stops_only_the_created_child(self):
        result, ready, popen, available, browser, child = self.launch(child=Child(wait_error=KeyboardInterrupt()))
        self.assertTrue(result['interrupted'])
        self.assertTrue(child.terminated)
        self.assertFalse(child.killed)

    def test_unresponsive_child_is_killed_only_after_terminate_timeout(self):
        child = mock.Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired('ssh', 3), 0]
        tunnel._stop_child(child)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertEqual(child.wait.call_count, 2)

    def test_health_is_authenticated_without_proxy_or_redirect_and_checks_identity(self):
        plan = self.plan()
        response = mock.MagicMock()
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value = response
        response.read.return_value = json.dumps(DESCRIPTOR).encode()
        with mock.patch.object(tunnel.urllib.request, 'build_opener', return_value=opener) as build:
            self.assertTrue(tunnel._health(plan, .2))
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, 'http://127.0.0.1:43210/api/health')
            self.assertEqual(request.get_header('Authorization'), 'Bearer ' + TOKEN)
            self.assertEqual(build.call_args.args[0].proxies, {})
            self.assertIsNone(build.call_args.args[1].redirect_request(None, None, 302, None, None, 'https://example.org'))
            for payload in [b'{}', b'[]', b'not JSON', json.dumps({**DESCRIPTOR, 'instanceId': 'other'}).encode(), b'x' * 16385]:
                response.read.return_value = payload
                with self.subTest(payload=payload[:80]), self.assertRaisesRegex(RuntimeError, 'identity does not match'):
                    tunnel._health(plan, .2)


if __name__ == '__main__':
    unittest.main()
