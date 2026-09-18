"""Standard-library CLI for a self-contained Project Viz skill."""
from __future__ import annotations

import argparse
from contextlib import closing
import getpass
import ipaddress
import json
import math
import os
from pathlib import Path
import subprocess
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser

from . import __version__
from .common import atomic_json, canonical_path, context, public_text, read_json, safe_project_file, state_lock
from .codex import Collector
from .store import Store


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def endpoint(ctx):
    descriptor = read_json(ctx['state'] / 'server.json', {})
    if descriptor.get('projectId') != ctx['config']['projectId']:
        return None
    port = descriptor.get('port')
    if descriptor.get('host') not in {'127.0.0.1', 'localhost'} or type(port) is not int or not 0 < port < 65536:
        return None
    return descriptor


def request(ctx, descriptor, path, body=None, timeout=3):
    token = (ctx['state'] / 'access-token').read_text(encoding='utf-8').strip()
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(f"http://127.0.0.1:{descriptor['port']}{path}", data=data, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=timeout) as response:
        return json.load(response)


def running(ctx):
    descriptor = endpoint(ctx)
    if not descriptor:
        return None
    try:
        result = request(ctx, descriptor, '/api/health')
    except (OSError, ValueError, urllib.error.URLError):
        return None
    if result.get('projectId') == ctx['config']['projectId'] and result.get('instanceId') == descriptor.get('instanceId'):
        return {**descriptor, 'version': result.get('version')}
    return None


def address(ctx, descriptor):
    token = (ctx['state'] / 'access-token').read_text(encoding='utf-8').strip()
    return f"http://127.0.0.1:{descriptor['port']}/?token=" + urllib.parse.quote(token, safe='')


def configure_access(ctx, args):
    """Save connection coordinates, never an SSH password or a browser token."""
    saved = ctx['config'].get('remoteAccess', {})
    settings = dict(saved)
    target = getattr(args, 'ssh_target', None)
    if target:
        settings = {'sshTarget': target, 'sshPort': None, 'inferred': False,
                    'localPort': saved.get('localPort')}
    elif not settings:
        connection = os.environ.get('SSH_CONNECTION', '').split()
        if len(connection) == 4:
            try:
                server = ipaddress.ip_address(connection[2])
                port = int(connection[3])
                if not 0 < port < 65536:
                    raise ValueError('Invalid SSH port')
                settings = {'sshTarget': getpass.getuser() + '@' + str(server),
                            'sshPort': port, 'localPort': None, 'inferred': True}
            except ValueError:
                pass
    for key, flag in [('localPort', 'local_port'), ('sshPort', 'ssh_port')]:
        if getattr(args, flag, None) is not None:
            settings[key] = getattr(args, flag)
    if settings and not settings.get('sshTarget'):
        raise ValueError('Provide --ssh-target with --local-port or --ssh-port')
    if settings:
        from .tunnel import make_plan
        # Validate before starting a service or modifying configuration.
        make_plan({'projectId': ctx['config']['projectId'], 'instanceId': 'validation', 'port': 8080},
                  'x' * 43, settings['sshTarget'], settings.get('localPort'), settings.get('sshPort'))
        with state_lock(ctx['state'], 'setup'):
            update_config(ctx, {'remoteAccess': settings})
    return settings


def access_plan(ctx, descriptor):
    settings = ctx['config'].get('remoteAccess')
    if not settings:
        return None
    from .tunnel import make_plan
    token = (ctx['state'] / 'access-token').read_text(encoding='utf-8').strip()
    plan = make_plan(descriptor, token, settings['sshTarget'], settings.get('localPort'), settings.get('sshPort'))
    plan['inferredTarget'] = bool(settings.get('inferred'))
    plan['runOn'] = 'The local computer where you open the browser'
    if plan['inferredTarget']:
        plan['note'] = 'SSH target inferred from this session; override --ssh-target if you use an alias, jump host, or a different reachable address.'
    return plan


def status(ctx):
    descriptor = running(ctx)
    result = {'initialized': True, 'running': bool(descriptor), 'projectId': ctx['config']['projectId'],
              'title': ctx['config']['title'], 'stateDir': str(ctx['state']), 'version': __version__}
    if descriptor:
        result.update(instanceId=descriptor['instanceId'], pid=descriptor['pid'], port=descriptor['port'], serverVersion=descriptor.get('version'), url=address(ctx, descriptor))
        plan = access_plan(ctx, descriptor)
        if plan:
            result.update(access=plan, serverUrl=result['url'], url=plan['browserUrl'], urlLocation='local-computer-after-forwarding')
        else:
            result['urlLocation'] = 'computer-running-the-viewer'
            result['remoteAccessHint'] = 'For a remote server, run access --ssh-target YOUR_SSH_ALIAS to get a local forwarding command and browser URL.'
    return result


def start(ctx, args):
    if args.host not in {'127.0.0.1', 'localhost'}:
        raise ValueError('This version binds only to loopback; use an authorized SSH tunnel for remote viewing')
    if args.foreground:
        from .server import serve
        if args.open and not ctx['config'].get('remoteAccess'):
            import threading
            def open_when_ready():
                for _ in range(100):
                    live = running(ctx)
                    if live:
                        webbrowser.open(address(ctx, live))
                        return
                    time.sleep(.1)
            threading.Thread(target=open_when_ready, daemon=True).start()
        serve(ctx, host=args.host, port=args.port)
        return {'running': False, 'stopped': True}
    with state_lock(ctx['state'], 'start', timeout=15):
        live = running(ctx)
        upgraded = bool(live and live.get('version') != __version__)
        if upgraded:
            old_port = live['port']
            _stop_instance(ctx, live)
            if args.port == 0:
                args.port = old_port
            live = None
        reused = bool(live)
        if not live:
            script = Path(__file__).resolve().parents[1] / 'project_viz.py'
            command = [sys.executable, str(script), 'start', '--foreground', '--project', str(ctx['project']),
                       '--state-dir', str(ctx['state']), '--host', args.host, '--port', str(args.port)]
            options = {'stdin': subprocess.DEVNULL, 'cwd': str(ctx['project']), 'close_fds': True}
            if os.name == 'nt':
                options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            else:
                options['start_new_session'] = True
            with (ctx['state'] / 'server.log').open('ab') as log:
                child = subprocess.Popen(command, stdout=log, stderr=log, **options)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                live = running(ctx)
                if live:
                    break
                if child.poll() is not None:
                    raise RuntimeError('Viewer could not start; inspect ' + str(ctx['state'] / 'server.log'))
                time.sleep(.15)
            if not live:
                raise RuntimeError('Viewer startup has not completed; run status and inspect ' + str(ctx['state'] / 'server.log'))
        result = status(ctx)
        result['reused'] = reused
        result['restartedForUpgrade'] = upgraded
        if args.open and 'access' not in result:
            webbrowser.open(result['url'])
        return result


def _stop_instance(ctx, live):
    request(ctx, live, '/api/shutdown', {'instanceId': live['instanceId']})
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        current = endpoint(ctx)
        if (not current or current.get('instanceId') != live['instanceId']) and not running(ctx):
            # Descriptor cleanup precedes collector lease release by a few lines.
            with state_lock(ctx['state'], 'collector', timeout=2):
                pass
            return {'running': False, 'stopped': True, 'instanceId': live['instanceId']}
        time.sleep(.1)
    raise RuntimeError('Shutdown requested; matching viewer has not yet stopped')


def stop(ctx):
    with state_lock(ctx['state'], 'start', timeout=15):
        live = running(ctx)
        if not live:
            return {'running': False, 'stopped': False, 'message': 'No matching viewer is running'}
        return _stop_instance(ctx, live)


def read_payload(filename):
    if filename:
        with Path(filename).expanduser().open('r', encoding='utf-8') as stream:
            text = stream.read(4 * 1024 * 1024 + 1)
    else:
        text = sys.stdin.read(4 * 1024 * 1024 + 1)
    if len(text) > 4 * 1024 * 1024:
        raise ValueError('Input exceeds 4 MiB; divide the catalog into smaller upserts')
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError('Expected a JSON object')
    return value


def metadata(value):
    if not isinstance(value, dict):
        raise ValueError('project must be an object')
    result = {}
    for key, limit in [('title', 240), ('goal', 8000)]:
        if key in value:
            item = value[key]
            if not isinstance(item, str) or len(item) > limit or (key == 'title' and not item.strip()):
                raise ValueError('Invalid project ' + key)
            result[key] = public_text(item.strip(), limit)
    return result


def update_config(ctx, changes):
    config = read_json(ctx['state'] / 'config.json')
    config.update(changes)
    atomic_json(ctx['state'] / 'config.json', config)
    ctx['config'] = config


def documents(project):
    results = []
    for folder in [project, project / 'docs', project / 'plans', project / 'reports']:
        if not folder.is_dir():
            continue
        with os.scandir(folder) as entries:
            for index, entry in enumerate(entries):
                if index >= 300 or len(results) >= 30:
                    break
                path = Path(entry.path)
                if not entry.is_file(follow_symlinks=False) or path.suffix.lower() not in {'.md', '.txt', '.rst'}:
                    continue
                relative = path.relative_to(project).as_posix()
                try:
                    safe = safe_project_file(project, relative)
                    with safe.open('rb') as source:
                        snippet = source.read(6000).decode('utf-8', errors='replace')
                    results.append({'path': relative, 'excerpt': public_text(snippet, 2500)})
                except (OSError, ValueError):
                    continue
    results.sort(key=lambda item: (not Path(item['path']).name.lower().startswith(('readme', 'plan')), item['path']))
    return results


def doctor(args):
    project = Path(args.project or Path.cwd()).expanduser().resolve()
    if not project.is_dir():
        raise ValueError('Project must be an existing directory')
    codex_home = Path(args.codex_home or os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser().resolve()
    initialized = False
    try:
        ctx = context(args.project, args.state_dir, args.codex_home)
        codex_home = Path(ctx['config']['codexHome'])
        initialized = True
    except FileNotFoundError:
        pass
    return {'version': __version__, 'python': sys.version.split()[0], 'platform': sys.platform,
            'project': str(project), 'initialized': initialized, 'codexHome': str(codex_home),
            'sources': {name: (codex_home / name).is_dir() for name in ('sessions', 'archived_sessions')},
            'databaseCandidates': [p.name for p in codex_home.glob('state_*.sqlite') if p.is_file()] if codex_home.is_dir() else [],
            'runtimeDependencies': 'Python standard library',
            'sshClientAvailable': bool(shutil.which('ssh')),
            'notes': ['Source availability does not prove import coverage. Use import-history to inspect coverage.',
                      'No source database or session log is modified. No background model calls.']}


def import_history(ctx, seconds):
    if not math.isfinite(seconds) or not 0 < seconds <= 300:
        raise ValueError('--seconds must be greater than 0 and at most 300')
    live = running(ctx)
    deadline = time.monotonic() + seconds
    if live:
        result = {}
        while time.monotonic() < deadline:
            budget = min(15, max(.05, deadline - time.monotonic()))
            result = request(ctx, live, '/api/import-history', {'seconds': budget}, timeout=budget + 10)
            diagnostic = result.get('diagnostics', result)
            if diagnostic.get('historyComplete'):
                break
        return result
    with state_lock(ctx['state'], 'collector', timeout=0), closing(Store(ctx['state'], ctx['config']['projectId'])) as store:
        collector = Collector(ctx['project'], Path(ctx['config']['codexHome']), ctx['state'], store.ingest_codex)
        result = collector.diagnostics()
        while time.monotonic() < deadline:
            result = collector.import_history()
            if result.get('historyComplete'):
                break
            time.sleep(.025)
        return result


def parser():
    cli = argparse.ArgumentParser(description='Persistent local work trees for Codex projects')
    cli.add_argument('--version', action='version', version=__version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--project', help='Exact project directory (default: current directory)')
    common.add_argument('--state-dir', help='Exact private state directory for this project')
    common.add_argument('--codex-home', help='Codex source home (default: CODEX_HOME or ~/.codex)')
    commands = cli.add_subparsers(dest='command', required=True)
    for name in ['doctor', 'init', 'start', 'status', 'stop', 'import-history', 'emit', 'catalog', 'context', 'export', 'access']:
        command = commands.add_parser(name, parents=[common])
        if name in {'start', 'access'}:
            command.add_argument('--ssh-target', help='SSH alias or user@server reachable from the local computer')
            command.add_argument('--ssh-port', type=int, help='SSH port; omitted uses the local SSH configuration')
            command.add_argument('--local-port', type=int, help='Browser computer port; defaults to the viewer port')
        if name == 'init':
            command.add_argument('--title')
            command.add_argument('--goal')
        elif name == 'start':
            command.add_argument('--host', default='127.0.0.1')
            command.add_argument('--port', type=int, default=0)
            command.add_argument('--foreground', action='store_true')
            command.add_argument('--open', action='store_true')
        elif name in {'emit', 'catalog'}:
            command.add_argument('--file', required=name == 'catalog')
        elif name == 'import-history':
            command.add_argument('--seconds', type=float, default=15)
        elif name == 'context':
            command.add_argument('--limit', type=int, default=30)
            command.add_argument('--offset', type=int, default=0, help='Public record page offset')
            command.add_argument('--work-offset', type=int, default=0, help='Work page offset (200 works per page)')
        elif name == 'export':
            command.add_argument('--output', required=True)
        elif name == 'access':
            command.add_argument('--output', help='Optional private JSON connection file to copy to the browser computer')
    tunnel = commands.add_parser('tunnel', help='Run on the browser computer to forward a remote viewer over SSH')
    tunnel.add_argument('--file', required=True, help='Connection JSON written by the remote access command')
    tunnel.add_argument('--local-port', type=int, help='Override local port; zero selects a free port')
    tunnel.add_argument('--open', action='store_true', help='Open browser after verifying the forwarded service')
    tunnel.add_argument('--timeout', type=float, default=30, help='Connection and authentication timeout in seconds')
    return cli


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == 'tunnel':
        from .tunnel import run_tunnel
        return run_tunnel(read_payload(args.file), local_port=args.local_port, open_browser=args.open, timeout=args.timeout)
    if args.command == 'doctor':
        return doctor(args)
    try:
        ctx = context(args.project, args.state_dir, args.codex_home, create=args.command in {'init', 'start'})
    except FileNotFoundError:
        if args.command in {'status', 'stop'}:
            return {'initialized': False, 'running': False}
        raise
    if args.command == 'init':
        changes = metadata({k: getattr(args, k) for k in ('title', 'goal') if getattr(args, k) is not None})
        with state_lock(ctx['state'], 'setup'):
            update_config(ctx, changes)
        with closing(Store(ctx['state'], ctx['config']['projectId'])):
            pass
        return {'initialized': True, 'projectId': ctx['config']['projectId'], 'title': ctx['config']['title'], 'stateDir': str(ctx['state'])}
    if args.command == 'start':
        if not 0 <= args.port < 65536:
            raise ValueError('Invalid port')
        configure_access(ctx, args)
        return start(ctx, args)
    if args.command == 'access':
        live = running(ctx)
        if not live:
            raise RuntimeError('Start this project viewer before preparing remote access')
        configure_access(ctx, args)
        plan = access_plan(ctx, live)
        if plan is None:
            raise ValueError('Provide --ssh-target with an SSH alias or user@server reachable from your local computer')
        if args.output:
            output = Path(args.output).expanduser().resolve()
            if output.suffix.lower() != '.json' or output.is_relative_to(ctx['state']):
                raise ValueError('Choose a JSON connection file outside private runtime state')
            atomic_json(output, plan)
            plan['output'] = str(output)
        return plan
    if args.command == 'status':
        return status(ctx)
    if args.command == 'stop':
        return stop(ctx)
    if args.command == 'import-history':
        return import_history(ctx, args.seconds)
    with closing(Store(ctx['state'], ctx['config']['projectId'])) as store:
        if args.command == 'emit':
            return store.emit(read_payload(args.file))
        if args.command == 'catalog':
            payload = read_payload(args.file)
            changes = metadata(payload.get('project', {}))
            with state_lock(ctx['state'], 'setup'):
                nodes = store.catalog(payload.get('nodes'))
                update_config(ctx, changes)
            return {'updated': len(nodes), 'projectId': ctx['config']['projectId'], 'workIds': [n['id'] for n in nodes]}
        if args.command == 'context':
            result = store.context(args.limit, args.offset, args.work_offset)
            result.update(project={'title': ctx['config']['title'], 'goal': ctx['config']['goal']}, documents=documents(ctx['project']))
            return result
        if args.command == 'export':
            output = Path(args.output).expanduser().resolve()
            if output == ctx['state'] or output.is_relative_to(ctx['state']):
                raise ValueError('Choose an export file outside private runtime state')
            exported = {'schemaVersion': 1, 'project': {key: ctx['config'][key] for key in ('title', 'goal')}, 'nodes': store.export()}
            atomic_json(output, exported)
            return {'output': str(output), 'nodes': len(exported['nodes']), 'includesRawLogs': False}
    raise ValueError('Unknown command')
