"""Run details must refresh while open and ignore obsolete async responses."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_live_run_drawer_refresh_and_navigation():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    page = Path(__file__).parents[1] / "src/kx/views/dashboard.html"
    script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const page = fs.readFileSync(process.argv[1], 'utf8');
new vm.Script(page.split('<script>')[1].split('</script>')[0]);
const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    const classes = new Set();
    elements.set(id, {children: [], scrollTop: 0, events: {},
      classList: {add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x),
        toggle: (x, on) => on ? classes.add(x) : classes.delete(x)},
      replaceChildren(...children) { this.children = children; }, setAttribute() {},
      addEventListener(name, handler) {this.events[name] = handler;}});
  }
  return elements.get(id);
}
let timer;
const context = vm.createContext({document: {getElementById: element, addEventListener() {}, hidden: false},
  h: (...args) => args, history: {replaceState() {}}, COMP: 'fixture', TAB: 'runs',
  Date, Promise, lastLoad: Date.now(), load() {}, tickUpdated() {}, syncPicks() {}, setInterval: fn => timer = fn});
vm.runInContext(page.slice(page.indexOf('const drawer ='), page.indexOf('async function showRun')), context);
vm.runInContext(page.slice(page.lastIndexOf('setInterval(() =>'), page.indexOf('const deep =')), context);
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  let count = 0;
  context.openDrawer(async () => ['sample-' + (++count)], 'run=r1', true);
  await flush();
  const drawer = element('drawer'); drawer.scrollTop = 234;
  vm.runInContext('drawerState.updated=0', context);
  timer(); await flush();
  assert.equal(count, 2); assert.equal(drawer.children.at(-1), 'sample-2');
  assert.equal(drawer.scrollTop, 234);
  let finish;
  context.openDrawer(() => new Promise(resolve => finish = resolve), 'run=old', true);
  context.openDrawer(async () => ['new-run'], 'run=new', true);
  await flush(); finish(['obsolete']); await flush();
  assert.equal(drawer.children.at(-1), 'new-run');
  let finishClosed;
  context.openDrawer(() => new Promise(resolve => finishClosed = resolve), 'run=closing', true);
  context.closeDrawer(); finishClosed(['late']); await flush();
  assert.equal(drawer.classList.contains('open'), false);
  assert.notEqual(drawer.children.at(-1), 'late');
  let finishPending, calls=0;
  context.openDrawer(() => {calls++; return new Promise(resolve => finishPending=resolve);}, 'run=pending', true);
  timer(); timer(); assert.equal(calls, 1);
  finishPending(['finished']); await flush();
  console.log('live refresh, preserved scrolling, navigation/close races and overlapping polls passed');
})().catch(error => {console.error(error); process.exitCode=1;});
'''
    result = subprocess.run([node, "-e", script, str(page)], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_an_idle_connection_does_not_hold_up_the_dashboard():
    """Browsers open connections ahead of time and send nothing on them; the server must answer the others meanwhile."""
    import socket
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    from kx.views.dashboard import Handler

    Handler.comps, Handler.default = {}, ""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    idle = socket.create_connection(("127.0.0.1", port))
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/api/comps", timeout=5) as r:
            assert r.status == 200
    finally:
        idle.close()
        httpd.shutdown()
        httpd.server_close()


def test_switching_tabs_while_one_loads_shows_the_new_tab():
    """A tab picked while another is still loading must load (it used to be dropped, leaving the old tab's content),
    and a tab seen before shows its last content at once while it refreshes."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    page = Path(__file__).parents[1] / "src/kx/views/dashboard.html"
    script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const page = fs.readFileSync(process.argv[1], 'utf8');
const main = {children: [], classList: {add() {}, remove() {}, toggle() {}}, querySelectorAll: () => [], contains: () => false, replaceChildren(...c) { this.children = c; }};
const pending = {};
const render = tab => () => new Promise(resolve => (pending[tab] = resolve));
const context = vm.createContext({
  document: {getElementById: id => id === 'main' ? main : {textContent: ''}, querySelectorAll: () => [], activeElement: null},
  drawer: {classList: {contains: () => false}}, location: {hash: ''}, history: {replaceState() {}},
  window: {scrollY: 0, scrollTo() {}}, COMP: 'c', TAB: 'status', Date, Promise, encodeURIComponent,
  renderStatus: render('status'), renderRuns: render('runs'), renderTasks: render('tasks'), renderFindings: render('findings'),
  renderSubs: render('subs'), renderTraining: render('training'), errBox: e => e, dur: x => x, syncPicks() {}});
vm.runInContext(page.slice(page.indexOf('const RENDER ='), page.indexOf('const tabs =')), context);
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  context.setTab('status'); await flush();
  context.setTab('runs'); await flush();   // status still loading
  pending.status(['status page']); await flush();
  assert.deepEqual(main.children, []);     // the late status page is not drawn over the runs tab
  pending.runs(['runs page']); await flush();
  assert.deepEqual(main.children, ['runs page']);
  context.setTab('status'); await flush();
  pending.status(['status page 2']); await flush();
  context.setTab('runs');
  assert.deepEqual(main.children, ['runs page']);  // at once, before its fresh data
  pending.runs(['runs page 2']); await flush();
  assert.deepEqual(main.children, ['runs page 2']);
  console.log('tab switches passed');
})().catch(error => {console.error(error); process.exitCode=1;});
'''
    result = subprocess.run([node, "-e", script, str(page)], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_sync_from_the_page_does_not_hold_up_the_page(monkeypatch, tmp_path):
    """kx sync takes minutes (rclone, games): the page goes on answering, and a second sync waits for the first."""
    import json
    import threading
    import time
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from kx.views import dashboard
    from kx.views.dashboard import Handler

    started, release = threading.Event(), threading.Event()

    def slow_sync(root):
        started.set()
        release.wait(10)
        return 200, {"ok": True}
    monkeypatch.setattr(dashboard, "sync", slow_sync)
    Handler.comps, Handler.default, Handler.token = {"demo": tmp_path}, "demo", "t0k"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://localhost:{httpd.server_address[1]}/api"

    def post():
        req = urllib.request.Request(f"{url}/sync?comp=demo", data=b"{}", method="POST", headers={"X-KX-Token": "t0k"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)
    try:
        first = []
        t = threading.Thread(target=lambda: first.append(post()))
        t.start()
        assert started.wait(5)
        t0 = time.monotonic()
        with urllib.request.urlopen(f"{url}/comps", timeout=5) as r:
            assert r.status == 200 and time.monotonic() - t0 < 2  # answered while the sync runs
        assert post()[0] == 409  # one sync at a time
        release.set()
        t.join(5)
        assert first == [(200, {"ok": True})]
    finally:
        release.set()
        httpd.shutdown()
        httpd.server_close()


def test_chart_helpers_line_runs_up_pool_repeats_and_label_runs_once():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    page = Path(__file__).parents[1] / "src/kx/views/dashboard.html"
    script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const page = fs.readFileSync(process.argv[1], 'utf8');
const body = page.split('<script>')[1].split('</script>')[0];
function source(name) {  // a top-level function of the page, by its braces
  const at = body.indexOf(`function ${name}(`); let i = body.indexOf('{', body.indexOf(')', at)), depth = 0;
  for (; i < body.length; i++) { if (body[i] === '{') depth++; if (body[i] === '}' && --depth === 0) break; }
  return body.slice(at, i + 1);
}
const context = vm.createContext({});
vm.runInContext(['lo', 'hi', 'niceTicks', 'intTicks', 'interpAt', 'lineAt', 'meanBand', 'flatten', 'seedKey', 'fmt', 'compact', 'xAxis', 'dur'].map(source).join('\n'), context);
const c = context, eq = (x, y) => assert.equal(JSON.stringify(x), JSON.stringify(y));  // arrays of another context
eq(c.intTicks(74, 75, 5), [74, 75]);  // not r0074 r0074 r0074 r0075
const counter = [{step: 0, v: 0}, {step: 10, v: 1000}, {step: 20, v: 3000}];
assert.equal(c.interpAt(counter, 15), 2000);
assert.equal(c.interpAt(counter, 20), 3000);
assert.equal(c.interpAt(counter, 25), null);  // past what the counter logged
const ax = c.xAxis('m:samples', {samples: {counter: true, passes: [counter]}}, 0);
assert.equal(ax.f({step: 10}), 1000);
assert.equal(c.xAxis('m:samples', {}, 0).f({step: 10}), null);  // a run without it is left off
const {mean, band} = c.meanBand([[[0, 1], [10, 3]], [[0, 3], [5, 3]]], 10);
eq(mean[0], [0, 2]);
eq(band[0], [0, 1, 3]);
eq(mean.at(-1), [10, 3]);  // only the longer run reaches x=10
const a = {run: 'r1', kind: 'experiment', git_sha: 'abc', command: 'python train.py --seed 1', config: {lr: 0.1, seed: 1}};
const b = {...a, run: 'r2', command: 'python train.py --seed 2', config: {lr: 0.1, seed: 2}};
const d = {...a, run: 'r3', config: {lr: 0.2, seed: 1}};
assert.equal(c.seedKey(a), c.seedKey(b));
assert.notEqual(c.seedKey(a), c.seedKey(d));
assert.notEqual(c.compact(49000), c.compact(49500));
console.log('chart helpers passed');
'''
    result = subprocess.run([node, "-e", script, str(page)], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
