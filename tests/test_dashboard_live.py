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
