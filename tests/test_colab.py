"""launch: colab hosts (kx.remote.colab) against a fake Colab: its REST API (runtime specs, runtimes, operations) and a
Jupyter server whose kernel really runs the bootstrap cell, so the real agent takes the job through the fake mailbox."""

import base64
import hashlib
import http.server
import json
import os
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path

import pytest

from kx.cli.main import main as kx_main
from kx.remote import colab, hosts, mailbox

from conftest import commit, run_kx
from test_remote import TRAIN

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class FakeColab(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, home: Path):
        super().__init__(("127.0.0.1", 0), Handler)
        self.home, self.runtimes, self.deleted, self.cells, self.procs, self.specs_eligible = home, {}, [], [], [], {"T4"}
        self.pings, self.proxy_gets, self.expire = 0, 0, "2099-01-01T00:00:00Z"

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server_port}"


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body=None):
        data = json.dumps(body if body is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def api_ok(self):
        if self.headers.get("Authorization") != "Bearer fake-access":
            self.reply(401, {"error": {"message": "no token"}})
            return False
        return True

    def runtime(self, rid):
        s = self.server
        return {"name": f"runtimes/{rid}", "runtimeSpec": s.runtimes[rid],
                "connectionInfo": {"url": f"{s.base}/rt/{rid}", "token": "proxy-token", "expireTime": s.expire}}

    def do_POST(self):
        s, u = self.server, urllib.parse.urlparse(self.path)
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if u.path == "/token":
            form = urllib.parse.parse_qs(body.decode())
            ok = form.get("client_id") == ["fake-client"] and form.get("refresh_token") == ["fake-refresh"]
            return self.reply(200 if ok else 400, {"access_token": "fake-access", "expires_in": 3600} if ok else {"error": "bad"})
        if u.path == "/v1beta/runtimes" and self.api_ok():
            rid = urllib.parse.parse_qs(u.query)["runtimeId"][0]
            s.runtimes[rid] = json.loads(body)["runtimeSpec"]
            return self.reply(200, {"name": f"operations/{rid}", "done": False})
        if u.path.endswith("/api/kernels") and self.headers.get("X-Colab-Runtime-Proxy-Token") == "proxy-token":
            return self.reply(201, {"id": "k1"})
        self.reply(404, {"error": {"message": "nothing here"}})

    def do_DELETE(self):
        s = self.server
        rid = self.path.rsplit("/", 1)[-1]
        if self.api_ok():
            if rid not in s.runtimes:
                return self.reply(404, {"error": {"message": "no such runtime"}})
            del s.runtimes[rid]
            s.deleted.append(rid)
            self.reply(200, {})

    def do_GET(self):
        s, u = self.server, urllib.parse.urlparse(self.path)
        if u.path.endswith("/channels"):
            return self.kernel()
        if u.path.endswith("/api/kernels") and self.headers.get("X-Colab-Runtime-Proxy-Token") == "proxy-token":
            s.proxy_gets += 1
            return self.reply(200, [{"id": "k1"}])
        if not self.api_ok():
            return
        if u.path == "/v1beta/runtimespecs":
            return self.reply(200, {"runtimeSpecs": [
                {"key": {"accelerator": a, "shape": "SHAPE_STANDARD", "variant": "VARIANT_GPU"}, "eligible": a in s.specs_eligible}
                for a in ("T4", "A100")]})
        if u.path.startswith("/v1/operations/"):
            rid = u.path.split("/")[-1].split(":")[0]
            return self.reply(200, {"name": f"operations/{rid}", "done": True, "response": self.runtime(rid)})
        if u.path.startswith("/v1beta/runtimes/"):
            rid = u.path.rsplit("/", 1)[-1]
            return self.reply(200, self.runtime(rid)) if rid in s.runtimes else self.reply(404, {"error": {"message": "none"}})
        self.reply(404, {"error": {"message": "nothing here"}})

    # a kernel: an execute_request's code runs in its own Python, its output comes back as stream messages; a
    # kernel_info_request (a client keeping the runtime connected) is answered; the connection stays open until closed
    def kernel(self):
        assert self.headers.get("X-Colab-Runtime-Proxy-Token") == "proxy-token"
        accept = base64.b64encode(hashlib.sha1((self.headers["Sec-WebSocket-Key"] + GUID).encode()).digest()).decode()
        self.wfile.write(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                         f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode())
        self.wfile.flush()
        lock = threading.Lock()

        def send(parent, kind, content):
            data = json.dumps({"msg_type": kind, "parent_header": {"msg_id": parent}, "content": content}).encode()
            n = len(data)
            head = bytes([0x81]) + (bytes([n]) if n < 126 else bytes([126]) + n.to_bytes(2, "big") if n < 65536
                                    else bytes([127]) + n.to_bytes(8, "big"))
            with lock:
                self.wfile.write(head + data)
                self.wfile.flush()

        def run(code, parent):
            env = dict(os.environ, HOME=str(self.server.home))
            p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
            self.server.procs.append(p)
            try:
                for line in p.stdout:
                    send(parent, "stream", {"name": "stdout", "text": line})
                p.wait()
                send(parent, "status", {"execution_state": "idle"})
            except (OSError, ValueError):  # kx went away once the agent had started: the cell goes on
                for _ in p.stdout:
                    pass
        while True:
            try:
                b0, b1 = self.rfile.read(2)
            except ValueError:
                return  # closed
            n = b1 & 0x7F
            n = int.from_bytes(self.rfile.read(2 if n == 126 else 8), "big") if n >= 126 else n
            mask = self.rfile.read(4)
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(self.rfile.read(n)))
            if b0 & 0x0F == 0x8:
                return
            msg = json.loads(data)
            kind, parent = msg["header"]["msg_type"], msg["header"]["msg_id"]
            if kind == "execute_request":
                self.server.cells.append(msg["content"]["code"])
                threading.Thread(target=run, args=(msg["content"]["code"], parent), daemon=True).start()
            elif kind == "kernel_info_request":
                self.server.pings += 1
                try:
                    send(parent, "kernel_info_reply", {"status": "ok"})
                except OSError:
                    return


@pytest.fixture
def fake_colab(tmp_path, monkeypatch):
    srv = FakeColab(tmp_path / "runtime-home")
    (tmp_path / "runtime-home").mkdir()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    token = tmp_path / "colab-token.json"
    token.write_text(json.dumps({"client_id": "fake-client", "refresh_token": "fake-refresh", "scope": colab.SCOPE}))
    monkeypatch.setattr(colab, "API", srv.base)
    monkeypatch.setattr(colab, "TOKEN_URL", srv.base + "/token")
    monkeypatch.setattr(colab, "TOKEN_FILE", token)
    monkeypatch.setenv("KX_COLAB_KEEPER_DIR", str(tmp_path / "keepers"))
    monkeypatch.setenv("KX_COLAB_KEEP_EVERY_S", "0.3")
    monkeypatch.setenv("KX_COLAB_KEEP_IDLE_S", "2")
    yield srv
    for pidfile in (tmp_path / "keepers").glob("*.pid"):
        try:
            os.kill(int(pidfile.read_text()), 15)
        except (OSError, ValueError):
            pass
    for p in srv.procs:
        p.terminate()
        p.wait(10)
    srv.shutdown()


@pytest.fixture
def cbox(comp, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "rclone").write_text(f"#!/bin/sh\nexec {sys.executable} {Path(__file__).parent / 'fake_rclone.py'} \"$@\"\n")
    (bin_dir / "rclone").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_RCLONE_ROOT", str(tmp_path / "cloud"))
    monkeypatch.setattr(hosts.Transport, "POLL_S", (0.1,))
    monkeypatch.setattr(mailbox.Transport, "POLL_S", (0.1,))
    far = tmp_path / "far"
    with open(comp / "comp.yaml", "a") as f:
        f.write(f"hosts:\n  cb: {{kind: mailbox, mailbox: 'fake:kx', launch: colab, accelerator: T4, python: {sys.executable}, "
                f"dir: {far}}}\n")
    (comp / "train.py").write_text(TRAIN)
    commit(comp, "train")
    return far


def test_a_run_gets_a_colab_runtime_its_agent_and_gives_it_back(cbox, fake_colab, comp, capsys):
    code = kx_main(["exec", "--json", "--on", "cb", "--", "python", "train.py"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["exit"] == 0
    assert fake_colab.deleted == ["kx-cb"] and not fake_colab.runtimes  # released once the last run ended
    (cell,) = fake_colab.cells
    assert "client_id = fake-client" in cell and "--idle-exit" in cell and "fake:kx/_kx/kx.tar.gz" in cell
    conf = fake_colab.home / ".config/rclone/rclone.conf"
    assert conf.read_text().startswith("[fake]") and oct(conf.stat().st_mode & 0o777) == "0o600"
    assert (cbox / "agent.log").exists()


def test_a_second_run_reuses_the_runtime_and_agent_while_they_are_up(cbox, fake_colab, comp, capsys):
    run_kx("remote", "launch", "cb", capsys=capsys)
    assert kx_main(["exec", "--json", "--on", "cb", "--", "python", "train.py"]) == 0
    assert len(fake_colab.cells) == 1  # the runtime was up and its agent reporting: no second bootstrap


def test_a_run_right_after_a_release_gets_a_new_runtime(cbox, fake_colab, comp, capsys):
    assert kx_main(["exec", "--json", "--on", "cb", "--", "python", "train.py"]) == 0  # ends: runtime released
    capsys.readouterr()
    assert fake_colab.deleted == ["kx-cb"]
    assert kx_main(["exec", "--json", "--on", "cb", "--", "python", "train.py"]) == 0  # the old heartbeat is still fresh
    assert len(fake_colab.cells) == 2 and fake_colab.deleted == ["kx-cb", "kx-cb"]


def test_no_eligible_accelerator_leaves_the_job_waiting_and_says_why(cbox, fake_colab, comp, capsys):
    fake_colab.specs_eligible = {"A100"}
    code = kx_main(["exec", "--json", "--on", "cb", "--detach", "--", "python", "train.py"])
    err = capsys.readouterr().err
    assert code == 0 and "no eligible Colab runtime with accelerator T4" in err and "kx remote launch cb" in err
    assert not fake_colab.runtimes and not fake_colab.cells


def test_launch_and_release_by_hand(cbox, fake_colab, comp, capsys):
    code, r = run_kx("remote", "launch", "cb", capsys=capsys)
    assert code == 0 and r["created"] and r["runtime"] == "runtimes/kx-cb" and r["spec"]["accelerator"] == "T4"
    code, r = run_kx("remote", "release", "cb", capsys=capsys)
    assert code == 0 and r["released"] and fake_colab.deleted == ["kx-cb"]
    code, r = run_kx("remote", "release", "cb", capsys=capsys)
    assert code == 0 and not r["released"]


def test_launch_is_refused_for_other_hosts_and_bad_options(comp, capsys, tmp_path):
    with open(comp / "comp.yaml", "a") as f:
        f.write(f"hosts:\n  box: {{local: true, dir: {tmp_path / 'box'}}}\n")
    code, _ = run_kx("remote", "launch", "box", capsys=capsys)
    assert code != 0
    (comp / "comp.yaml").write_text((comp / "comp.yaml").read_text().replace("local: true", "local: true, launch: colab"))
    code, _ = run_kx("remote", "list", capsys=capsys)
    assert code != 0 and "launch must be colab, on a mailbox host" in capsys.readouterr().err + str(_)


def test_the_websocket_client_round_trips_long_messages(fake_colab):
    conn = {"url": f"{fake_colab.base}/rt/x", "token": "proxy-token"}
    out = colab.execute(conn, "print('y' * 70000)\nprint('done')")
    assert out == "y" * 70000 + "\ndone\n"


def _wait(cond, timeout=30):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.1)
    return False


def test_a_launched_runtime_is_kept_connected_until_it_is_released(cbox, fake_colab, comp, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("KX_COLAB_KEEP_IDLE_S", "600")  # not released for idling during this test
    code, r = run_kx("remote", "launch", "cb", capsys=capsys)
    assert code == 0
    pidfile = tmp_path / "keepers" / "colab-keepalive-kx-cb.pid"
    pid = int(pidfile.read_text())
    assert _wait(lambda: fake_colab.pings >= 2 and fake_colab.proxy_gets >= 2)  # held open, touched again and again
    assert run_kx("remote", "launch", "cb", capsys=capsys)[0] == 0 and int(pidfile.read_text()) == pid  # one keeper
    run_kx("remote", "release", "cb", capsys=capsys)
    assert _wait(lambda: not pidfile.exists())  # the runtime is gone: it ends
    log = (tmp_path / "keepers" / "colab-keepalive-kx-cb.log").read_text()
    assert "connected to the kernel" in log and "the runtime is gone" in log


def test_the_keeper_releases_an_idle_runtime_and_reconnects_before_the_token_expires(cbox, fake_colab, comp, capsys, tmp_path):
    from datetime import datetime, timedelta, timezone
    fake_colab.expire = (datetime.now(timezone.utc) + timedelta(seconds=200)).isoformat()  # inside the 5-min margin
    assert run_kx("remote", "launch", "cb", capsys=capsys)[0] == 0
    log = tmp_path / "keepers" / "colab-keepalive-kx-cb.log"
    assert _wait(lambda: log.exists() and log.read_text().count("connected to the kernel") >= 2)  # a fresh token each look
    assert _wait(lambda: fake_colab.deleted == ["kx-cb"])  # nothing ran for KX_COLAB_KEEP_IDLE_S: released
    assert _wait(lambda: "releasing the runtime" in log.read_text())

