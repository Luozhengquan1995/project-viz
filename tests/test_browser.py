"""Optional Playwright regression using only JSON synthetic project data.

python3 -m unittest discover -s tests -p test_browser.py -v
Install Playwright and a Chromium browser to run; otherwise this test is skipped.
Set PROJECT_VIZ_BROWSER to a browser executable when automatic discovery is not
appropriate. This test never connects to a production server or takes screenshots.
"""
from copy import deepcopy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

try:
    from playwright.sync_api import sync_playwright, expect
except ImportError:
    sync_playwright = None

SITE = Path(__file__).resolve().parents[1] / 'skills/project-viz/assets/site'
NOW = '2026-01-01T12:00:00Z'
HISTORY = 'records:opaque/topic:00?part=1&extra'
MORE = 'records:opaque/topic:00?part=2&extra'


def fixture():
    """The fixture contains no paths, titles, or data from a real project."""
    nodes = [{'id': 'project', 'parentId': None, 'kind': 'project', 'label': 'Portable synthetic project', 'status': 'active', 'curatedLabel': True}]
    for group in range(6):
        nodes.append({'id': f'group-{group}', 'parentId': 'project', 'kind': 'stage', 'label': f'合成工作组 {group}', 'status': 'partial', 'curatedLabel': True})
    for index in range(48):
        identifier = f'topic:{index:02}'
        nodes.append({'id': identifier, 'parentId': f'group-{index // 8}', 'kind': 'task', 'label': f'工作主题 {index:02}：核对独立对象', 'summary': 'Only synthetic public work.', 'detail': '合成工作详情：应按服务端身份展示。', 'status': 'active' if index == 0 else 'done', 'outcome': 'failed' if index == 7 else 'success' if index == 8 else 'unverified', 'curatedLabel': True, 'updatedAt': NOW, 'hasChildren': True, 'sessionId': 'same-source-must-not-merge', 'evidence': [{'path': 'README.synthetic.md', 'line': 7, 'note': '合成证据，不代表生产记录。'}], 'files': ['README.synthetic.md']})
        nodes.append({'id': HISTORY if index == 0 else f'records:opaque-{index}', 'parentId': identifier, 'kind': 'history', 'label': '查看支持记录', 'status': 'done', 'updatedAt': NOW, 'eventCount': 2, 'hasChildren': True})
    return {'revision': 'fixture-1', 'rootId': 'project', 'project': {'id': 'fixture-project', 'title': 'Portable synthetic project', 'path': '/synthetic/project'}, 'nodes': nodes, 'activePath': ['project', 'group-0', 'topic:00'], 'activeNodeIds': ['topic:00'], 'currentActivity': '正在核对合成输入协议', 'lastScanAt': NOW, 'monitorStatus': 'watching', 'pollSeconds': .5, 'coverage': {'status': 'indexing', 'sourceCount': 3, 'indexedCount': 1, 'pendingSources': 2, 'historyComplete': False, 'warnings': ['合成警告：另有来源尚未索引']}}


class SyntheticServer:
    def __init__(self):
        self.lock = threading.RLock()
        self.tree = fixture()
        self.history_requests = []
        self.file_requests = []
        self.requests = 0
        owner = self

        class Handler(SimpleHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                url = urlsplit(self.path)
                query = parse_qs(url.query)
                body = None
                with owner.lock:
                    if url.path == '/api/tree':
                        owner.requests += 1
                        body = deepcopy(owner.tree)
                    elif url.path == '/api/history':
                        identifier = query.get('id', [''])[0]
                        owner.history_requests.append(identifier)
                        if identifier == HISTORY:
                            body = {'id': identifier, 'nodes': [{'id': 'record:one', 'parentId': HISTORY, 'kind': 'action', 'label': '进度更新', 'summary': '实现合成结果解析器', 'detail': '<img src=x onerror="window.syntheticInjection=true">', 'status': 'done'}, {'id': MORE, 'parentId': HISTORY, 'kind': 'history', 'label': '更多合成记录', 'status': 'done', 'hasChildren': True}]}
                        else:
                            body = {'id': identifier, 'nodes': [{'id': 'record:two', 'parentId': identifier, 'kind': 'action', 'label': '执行命令 · synthetic_tool', 'summary': '这是已记录的合成失败', 'detail': 'Synthetic nonzero exit.', 'status': 'error'}]}
                    elif url.path == '/api/file':
                        path = query.get('path', [''])[0]
                        owner.file_requests.append(path)
                        body = {'path': path, 'text': "const synthetic = '<script>never execute</script>';", 'binary': False, 'truncated': False, 'modifiedAt': NOW}
                if body is not None:
                    raw = json.dumps(body, ensure_ascii=False).encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json; charset=utf-8')
                    self.send_header('Content-Length', str(len(raw)))
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if url.path == '/':
                    self.path = '/index.html'
                super().do_GET()

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(SITE)))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = 'http://127.0.0.1:' + str(self.server.server_port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def node(page, identifier):
    return page.locator('#flow-world [data-node-id=' + json.dumps(identifier) + ']')


def ids(page):
    return page.locator('#flow-world [data-node-id]').evaluate_all('(nodes)=>nodes.map(n=>n.dataset.nodeId)')


def camera(page):
    return page.locator('#flow-world').evaluate('el=>{const m=new DOMMatrix(getComputedStyle(el).transform);return [m.a,m.e,m.f]}')


def section(page, name):
    locator = page.locator('details[data-section=' + json.dumps(name) + ']')
    locator.wait_for()
    if not locator.evaluate('el=>el.open'):
        locator.locator(':scope > summary').click()
    return locator


@unittest.skipIf(sync_playwright is None, 'Optional dependency playwright is not installed')
class ProjectVizBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.driver = sync_playwright().start()
        configured = os.environ.get('PROJECT_VIZ_BROWSER')
        candidates = [configured] if configured else [cls.driver.chromium.executable_path] + [shutil.which(name) for name in ('chromium', 'chromium-browser', 'google-chrome', 'chrome', 'msedge')]
        executable = next((candidate for candidate in candidates if candidate and Path(candidate).is_file()), None)
        if not executable:
            cls.driver.stop()
            raise unittest.SkipTest('Chromium is unavailable; install Playwright Chromium or set PROJECT_VIZ_BROWSER')
        try:
            cls.browser = cls.driver.chromium.launch(executable_path=executable, headless=True, args=['--no-sandbox'])
        except Exception:
            cls.driver.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.driver.stop()

    def test_semantic_graph_history_polling_and_mobile(self):
        server = SyntheticServer()
        context = self.browser.new_context(viewport={'width': 1600, 'height': 1100})
        errors = []
        try:
            page = context.new_page()
            page.set_default_timeout(8000)
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(server.origin, wait_until='networkidle')
            expect(page.locator('#flow-world .kind-task')).to_have_count(48)
            expected = {'project'} | {f'group-{i}' for i in range(6)} | {f'topic:{i:02}' for i in range(48)}
            self.assertEqual(set(ids(page)), expected)
            self.assertEqual(len(ids(page)), len(expected))
            expect(page.locator('#overview-button, #current-button')).to_have_count(0)
            expect(page.locator('#flow-canvas')).to_have_count(1)
            expect(page.locator('a[href*="agentviz"],a[href*="list.html"],a[href*="overview.html"]')).to_have_count(0)
            for index in (0, 1, 47):
                expect(node(page, f'topic:{index:02}').locator('.node-label')).to_have_text(f'工作主题 {index:02}：核对独立对象')
            expect(node(page, 'topic:07')).to_have_attribute('data-visual-status', 'error')
            expect(node(page, 'topic:08')).to_have_attribute('data-visual-status', 'success')
            expect(node(page, 'topic:09')).to_have_attribute('data-visual-status', 'done')
            expect(page.locator('#project-name')).to_have_text('Portable synthetic project')
            page.locator('#coverage-label').click()
            expect(page.locator('#coverage-content')).to_contain_text('历史索引尚未完成')
            expect(page.locator('#coverage-content')).to_contain_text('另有来源尚未索引')
            page.locator('#coverage-label').click()

            node(page, 'topic:00').locator('.node-detail').click()
            evidence = section(page, 'evidence')
            expect(evidence).to_contain_text('第 7 行')
            expect(evidence).to_contain_text('合成证据')
            evidence.locator('[data-file="README.synthetic.md"]').click()
            expect(page.locator('.file-preview')).to_have_text("const synthetic = '<script>never execute</script>';")
            self.assertEqual(server.file_requests, ['README.synthetic.md'])
            page.locator('[data-back-detail]').click()
            section(page, 'groups')
            page.locator('[data-detail=' + json.dumps(HISTORY) + ']').click()
            page.locator('[data-history-load=' + json.dumps(HISTORY) + ']').click()
            section(page, 'execution')
            record = page.locator('[data-record-id="record:one"]')
            record.locator(':scope > summary').click()
            expect(record).to_contain_text('<img src=x onerror=')
            self.assertFalse(page.evaluate('Boolean(window.syntheticInjection)'))
            section(page, 'groups')
            page.locator('[data-detail=' + json.dumps(MORE) + ']').click()
            page.locator('[data-history-load=' + json.dumps(MORE) + ']').click()
            section(page, 'execution')
            expect(page.locator('[data-record-id="record:two"]')).to_have_attribute('data-visual-status', 'error')
            self.assertEqual(server.history_requests, [HISTORY, MORE])
            self.assertEqual(set(ids(page)), expected, 'loaded progress must not create semantic work cards')
            page.locator('#close-detail').click()

            # One tree remains intact through user navigation.
            node(page, 'group-2').locator('.node-main').click()
            expect(node(page, 'topic:16')).to_have_count(0)
            page.get_by_role('button', name='定位正在执行的任务', exact=True).click()
            for identifier in ('project', 'topic:47', 'group-2'):
                expect(node(page, identifier)).to_have_count(1)
            expect(node(page, 'topic:16')).to_have_count(0)
            page.locator('#node-search').fill('工作主题 47')
            page.locator('[data-search-node="topic:47"]').click()
            expect(node(page, 'project')).to_have_count(1)
            expect(node(page, 'topic:00')).to_have_count(1)
            expect(node(page, 'topic:16')).to_have_count(0)
            page.locator('#node-search').fill('')
            page.locator('#fit-view').click()
            before_ids = set(ids(page))
            node(page, 'group-5').locator('.node-main').dblclick()
            self.assertEqual(set(ids(page)), before_ids)
            page.locator('#fit-view').click()

            # Same-revision coverage changes must not rebuild or prune work.
            page.evaluate('window.syntheticRoot=document.querySelector("[data-node-id=project]")')
            with server.lock:
                server.tree['coverage'].update(status='ready', indexedCount=3, pendingSources=0, historyComplete=True, warnings=[])
            page.evaluate('window.dispatchEvent(new Event("online"))')
            expect(page.locator('#coverage-content')).to_contain_text('已登记来源的历史已索引')
            self.assertTrue(page.evaluate('window.syntheticRoot.isConnected'))
            self.assertEqual(set(ids(page)), before_ids)

            # A real semantic update preserves the user's camera and closures.
            canvas = page.locator('#flow-canvas').bounding_box()
            x, y = canvas['x'] + 35, canvas['y'] + 35
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x + 83, y + 51, steps=4)
            page.mouse.up()
            before_camera = camera(page)
            with server.lock:
                next(item for item in server.tree['nodes'] if item['id'] == 'topic:47').update(label='修正合成输出协议', status='error')
                server.tree.update(revision='fixture-2', currentActivity='正在执行不同的合成工作')
            page.evaluate('window.dispatchEvent(new Event("online"))')
            expect(node(page, 'topic:47').locator('.node-label')).to_have_text('修正合成输出协议')
            for before, after in zip(before_camera, camera(page)):
                self.assertAlmostEqual(before, after, places=3)
            self.assertEqual(set(ids(page)), before_ids)
            expect(node(page, 'topic:16')).to_have_count(0)
            expect(page.locator('#project-name')).to_have_text('Portable synthetic project')
            self.assertFalse(errors, errors)

            mobile = self.browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
            try:
                small = mobile.new_page()
                small.on('pageerror', lambda error: errors.append(str(error)))
                small.goto(server.origin, wait_until='networkidle')
                expect(small.locator('#flow-world .kind-task')).to_have_count(48)
                self.assertEqual(small.evaluate('document.body.scrollWidth'), 390)
                expect(small.get_by_role('button', name='定位正在执行的任务', exact=True)).to_be_visible()
                expect(small.locator('#coverage-label')).to_be_visible()
                small.locator('#node-search').fill('修正合成输出协议')
                small.locator('[data-search-node="topic:47"]').click()
                expect(node(small, 'project')).to_have_count(1)
                node(small, 'topic:47').locator('.node-detail').click()
                expect(small.locator('#detail-title')).to_have_text('修正合成输出协议')
                expect(small.locator('#drawer-content > .detail-status')).to_have_attribute('data-visual-status', 'error')
                self.assertFalse(errors, errors)
            finally:
                mobile.close()
        finally:
            context.close()
            server.close()


if __name__ == '__main__':
    unittest.main()
