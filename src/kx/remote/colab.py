"""Colab runtimes kx starts itself: a mailbox host with `launch: colab`.

    colab: {kind: mailbox, mailbox: gdrive:kx, launch: colab, accelerator: T4, gpus: 1, torch: cu126}

Runs go through the mailbox as on any mailbox host (kx.remote.mailbox, kx.remote.agent). What `launch: colab` adds:
when kx leaves a job and no agent for the host has reported lately, it gets a runtime from the Colab API
(https://developers.google.com/colab/api/reference/rest: runtimes.create with an eligible runtime spec), opens a
Jupyter kernel on it through the runtime's proxy (X-Colab-Runtime-Proxy-Token) and runs one cell there: the mailbox
remote's rclone config (sent from here, over TLS, to your own runtime), the kx sources from the mailbox, and the
agent with --idle-exit. The cell keeps the kernel busy while the agent runs. Once the last run on the host has ended
and no job waits, kx deletes the runtime (kx remote release HOST does it by hand); the agent's --idle-exit is the
backstop.

The Google login is the mailbox remote's own Google app (its client_id and client_secret in the rclone config): the
same app as the Drive mailbox, with the Colab API enabled in its Cloud project. `kx remote login HOST` asks for the
https://www.googleapis.com/auth/colaboratory scope once (a browser on this machine) and keeps the refresh token in
~/.config/kx/colab-token.json (0600). Standard library only, like the rest of kx.remote.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from kx.ledger.config import KxError

SCOPE = "https://www.googleapis.com/auth/colaboratory"
API = os.environ.get("KX_COLAB_API", "https://colaboratory.googleapis.com")
AUTH_URL = os.environ.get("KX_COLAB_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth")
TOKEN_URL = os.environ.get("KX_COLAB_TOKEN_URL", "https://oauth2.googleapis.com/token")
TOKEN_FILE = Path(os.environ.get("KX_COLAB_TOKEN_FILE", "~/.config/kx/colab-token.json")).expanduser()
IDLE_EXIT_S = 900          # the agent on a runtime kx started stops once no run has gone on for this long
STARTED = "kx-agent-started"


def _remote(h) -> str:
    if not h.mailbox or ":" not in h.mailbox:
        raise KxError(f"{h.name}: launch: colab needs an rclone remote mailbox (REMOTE:PATH)", fix="mailbox: gdrive:kx")
    return h.mailbox.split(":", 1)[0]


def rclone_section(h) -> str:
    """The mailbox remote's rclone config section, as `rclone config show REMOTE` prints it."""
    p = subprocess.run(["rclone", "config", "show", _remote(h)], capture_output=True, text=True, timeout=60)
    if p.returncode != 0 or not p.stdout.strip():
        raise KxError(f"{h.name}: no rclone remote {_remote(h)!r} here", fix=f"rclone config  (the remote of {h.mailbox})")
    return p.stdout


def oauth_client(h) -> tuple[str, str]:
    conf = dict(line.split(" = ", 1) for line in rclone_section(h).splitlines() if " = " in line)
    if not conf.get("client_id") or not conf.get("client_secret"):
        raise KxError(f"{h.name}: the rclone remote {_remote(h)} has no client_id/client_secret of its own",
                      fix="give rclone your own Google app (rclone docs: 'Making your own client_id') and enable the "
                          "Colab API in its Cloud project")
    return conf["client_id"], conf["client_secret"]


def _post_form(url: str, data: dict) -> dict:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, urllib.parse.urlencode(data).encode()), timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise KxError(f"Google sign-in: {e.code} {e.read().decode(errors='replace')[:400]}",
                      fix="kx remote login HOST") from None


def login(h, show=print) -> dict:
    """The OAuth installed-app flow (loopback redirect, PKCE) for the Colab scope; keeps the refresh token."""
    cid, csec = oauth_client(h)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state, got = secrets.token_urlsafe(16), {}

    class Back(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            got.update(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"kx: Colab sign-in received; you can close this tab.")

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), Back)
    redirect = f"http://127.0.0.1:{srv.server_port}"
    show("open this in a browser on this machine and approve:\n" + AUTH_URL + "?" + urllib.parse.urlencode(dict(
        client_id=cid, redirect_uri=redirect, response_type="code", scope=SCOPE, state=state, access_type="offline",
        prompt="consent", code_challenge=challenge, code_challenge_method="S256")))
    while "code" not in got and "error" not in got:
        srv.handle_request()
    srv.server_close()
    if "error" in got or got.get("state") != [state]:
        raise KxError(f"Colab sign-in refused: {got.get('error', ['state mismatch'])[0]}")
    tok = _post_form(TOKEN_URL, dict(code=got["code"][0], client_id=cid, client_secret=csec, redirect_uri=redirect,
                                     grant_type="authorization_code", code_verifier=verifier))
    if SCOPE not in (tok.get("scope") or "").split() or not tok.get("refresh_token"):
        raise KxError(f"Google did not grant {SCOPE} (granted: {tok.get('scope')!r})",
                      fix="enable the Colab API in the Cloud project of the rclone app, then kx remote login HOST")
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({"client_id": cid, "refresh_token": tok["refresh_token"], "scope": tok["scope"]}))
    TOKEN_FILE.chmod(0o600)
    return {"scope": tok["scope"], "token_file": str(TOKEN_FILE)}


class Api:
    """The Colab REST API as the signed-in user."""

    def __init__(self, h):
        self.h = h
        self._token, self._until = None, 0.0

    def token(self) -> str:
        if self._token and time.time() < self._until - 60:
            return self._token
        cid, csec = oauth_client(self.h)
        try:
            saved = json.loads(TOKEN_FILE.read_text())
        except (OSError, ValueError):
            saved = {}
        if not saved.get("refresh_token") or saved.get("client_id", cid) != cid:
            raise KxError(f"{self.h.name}: not signed in to Colab with the app of {self.h.mailbox}",
                          fix=f"kx remote login {self.h.name}")
        tok = _post_form(TOKEN_URL, dict(refresh_token=saved["refresh_token"], client_id=cid, client_secret=csec,
                                         grant_type="refresh_token"))
        self._token, self._until = tok["access_token"], time.time() + float(tok.get("expires_in", 3600))
        return self._token

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 120, missing_ok=False):
        req = urllib.request.Request(f"{API}/{path}", method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                text = r.read().decode()
        except urllib.error.HTTPError as e:
            if missing_ok and e.code == 404:
                return None
            detail = e.read().decode(errors="replace")
            try:
                detail = json.loads(detail)["error"]["message"]
            except (ValueError, KeyError, TypeError):
                pass
            raise KxError(f"Colab API {method} {path}: {e.code} {str(detail)[:600]}",
                          fix="is the Colab API enabled in the rclone app's Cloud project?" if e.code == 403 else None) from None
        return json.loads(text) if text.strip() else {}

    def specs(self) -> list[dict]:
        return self.call("GET", "v1beta/runtimespecs").get("runtimeSpecs", [])

    def runtime(self, name: str) -> dict | None:
        return self.call("GET", f"v1beta/{name}", missing_ok=True)

    def create(self, runtime_id: str, accelerator: str) -> dict:
        """A runtime on an eligible spec with that accelerator (T4, L4, A100; NONE for a CPU one), once it is up."""
        specs = self.specs()
        fit = [s["key"] for s in specs if s.get("eligible") and s["key"].get("accelerator", "").upper() == accelerator.upper()]
        if not fit:
            ok = sorted({s["key"].get("accelerator") for s in specs if s.get("eligible")})
            raise KxError(f"{self.h.name}: no eligible Colab runtime with accelerator {accelerator} (eligible now: "
                          f"{', '.join(ok) or 'none'})", fix=f"accelerator: {ok[0] if ok else 'T4'}  (for {self.h.name})")
        key = next((k for k in fit if k.get("shape") == "SHAPE_STANDARD"), fit[0])
        op = self.call("POST", "v1beta/runtimes?" + urllib.parse.urlencode({"runtimeId": runtime_id,
                                                                            "requestId": str(uuid.uuid4())}),
                       {"runtimeSpec": key})
        deadline = time.monotonic() + 900
        while not op.get("done"):
            if time.monotonic() > deadline:
                raise KxError(f"{self.h.name}: the Colab runtime was not ready after 15 min ({op.get('name')})")
            op = self.call("GET", f"v1/{op['name']}:wait?timeout=60s", timeout=90)
        if op.get("error"):
            e = op["error"]
            reasons = [d.get("reason") for d in e.get("details", []) if isinstance(d, dict) and d.get("reason")]
            raise KxError(f"{self.h.name}: Colab refused a runtime: {e.get('message')} {' '.join(reasons)}".strip())
        return op.get("response") or self.runtime(f"runtimes/{runtime_id}")

    def delete(self, name: str) -> bool:
        return self.call("DELETE", f"v1beta/{name}", missing_ok=True) is not None


def runtime_id(h) -> str:
    return "kx-" + re.sub(r"[^a-z0-9-]", "-", h.name.lower())[:40]


# -- a Jupyter kernel through the runtime's proxy -----------------------------------------------------------------------
class WebSocket:
    """A minimal RFC 6455 client: text frames out (masked), text/binary in, ping answered."""

    def __init__(self, url: str, headers: dict, timeout: float = 60):
        u = urllib.parse.urlparse(url)
        secure = u.scheme in ("wss", "https")
        sock = socket.create_connection((u.hostname, u.port or (443 if secure else 80)), timeout=timeout)
        self.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname) if secure else sock
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [f"GET {u.path or '/'}{'?' + u.query if u.query else ''} HTTP/1.1", f"Host: {u.netloc}",
                 "Upgrade: websocket", "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13",
                 *(f"{k}: {v}" for k, v in headers.items())]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise KxError("the runtime closed the kernel connection during the handshake")
            head += chunk
        head, self.buf = head.split(b"\r\n\r\n", 1)
        status, *fields = head.decode(errors="replace").split("\r\n")
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        got = {k.strip().lower(): v.strip() for k, v in (f.split(":", 1) for f in fields if ":" in f)}
        if " 101 " not in status + " " or got.get("sec-websocket-accept") != accept:
            raise KxError(f"the runtime refused the kernel connection: {status}")

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send(self, opcode: int, payload: bytes):
        n, mask = len(payload), os.urandom(4)
        head = bytes([0x80 | opcode]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + n.to_bytes(2, "big")
                                         if n < 65536 else bytes([0x80 | 127]) + n.to_bytes(8, "big"))
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, text: str):
        self._send(0x1, text.encode())

    def recv(self) -> str | None:
        """The next message, or None once the other side closed."""
        parts = []
        while True:
            try:
                b0, b1 = self._read(2)
            except EOFError:
                return None
            opcode, n = b0 & 0x0F, b1 & 0x7F
            n = int.from_bytes(self._read(2 if n == 126 else 8), "big") if n >= 126 else n
            mask = self._read(4) if b1 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                self._send(0xA, data)
                continue
            if opcode == 0xA:
                continue
            parts.append(data)
            if b0 & 0x80:
                return b"".join(parts).decode(errors="replace")

    def close(self):
        try:
            self._send(0x8, b"")
        except OSError:
            pass
        self.sock.close()


def execute(conn: dict, code: str, until: str | None = None, timeout: float = 900) -> str:
    """Run code in a new kernel on the runtime; return its output once `until` appears in it (the cell goes on) or
    once the cell has finished."""
    base = conn["url"].rstrip("/")
    headers = {"X-Colab-Runtime-Proxy-Token": conn["token"], "X-Colab-Client-Agent": "kx"}
    req = urllib.request.Request(f"{base}/api/kernels", data=b"{}", method="POST",
                                 headers=headers | {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            kernel = json.load(r)["id"]
    except urllib.error.HTTPError as e:
        raise KxError(f"the runtime refused a kernel: {e.code} {e.read().decode(errors='replace')[:300]}") from None
    session, msg_id = uuid.uuid4().hex, uuid.uuid4().hex
    ws = WebSocket(base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
                   + f"/api/kernels/{kernel}/channels?session_id={session}", headers)
    ws.send(json.dumps({"header": {"msg_id": msg_id, "username": "kx", "session": session, "msg_type": "execute_request",
                                   "version": "5.3", "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                        "parent_header": {}, "metadata": {}, "channel": "shell",
                        "content": {"code": code, "silent": False, "store_history": False, "user_expressions": {},
                                    "allow_stdin": False, "stop_on_error": True}}))
    out, deadline = [], time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            raw = ws.recv()
            if raw is None:
                break
            m = json.loads(raw)
            if (m.get("parent_header") or {}).get("msg_id") != msg_id:
                continue
            kind, content = m.get("msg_type") or m.get("header", {}).get("msg_type"), m.get("content") or {}
            if kind == "stream":
                out.append(content.get("text", ""))
                if until and until in "".join(out):
                    return "".join(out)
            elif kind == "error":
                raise KxError(f"the cell on the runtime failed: {content.get('ename')}: {content.get('evalue')}\n"
                              + "".join(out)[-1500:])
            elif kind == "status" and content.get("execution_state") == "idle":
                return "".join(out)
        raise KxError(f"no answer from the runtime's kernel after {timeout:g} s\n" + "".join(out)[-1500:])
    finally:
        ws.close()


def bootstrap(h, conf: str, kx_tar: str) -> str:
    """The cell that makes the runtime a mailbox host: rclone config, rclone, the kx sources, the agent."""
    d = h.dir if h.dir.startswith(("/", "~")) else f"~/{h.dir}"
    return "\n".join([
        "import pathlib, shutil, subprocess, sys",
        "conf = pathlib.Path.home() / '.config/rclone/rclone.conf'",
        "conf.parent.mkdir(parents=True, exist_ok=True)",
        f"conf.write_text({conf!r}); conf.chmod(0o600)",
        "if shutil.which('rclone') is None:",
        "    subprocess.run('curl -fsSL https://rclone.org/install.sh | bash', shell=True, check=True,",
        "                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)",
        f"d = pathlib.Path({d!r}).expanduser(); (d / 'kx').mkdir(parents=True, exist_ok=True)",
        f"subprocess.run(['bash', '-c', 'rclone cat \"$0\" | tar -xz -C \"$1\"', {kx_tar!r}, str(d / 'kx')], check=True)",
        "log = open(d / 'agent.log', 'ab')",
        f"p = subprocess.Popen([sys.executable, '-u', str(d / 'kx/src/kx/remote/agent.py'), {h.name!r}, '--mailbox', "
        f"{h.mailbox!r}, '--dir', str(d), '--idle-exit', '{IDLE_EXIT_S}'], stdout=log, stderr=subprocess.STDOUT, "
        "start_new_session=True)",
        f"print({STARTED!r}, p.pid, flush=True)",
        "p.wait()  # the kernel stays busy while the agent runs",
        "print('kx-agent-ended', p.returncode, flush=True)"])


def _first_report(h, since: float, timeout: float = 300):
    from datetime import datetime
    from kx.remote import mailbox
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        beat = mailbox.heartbeat(h.mailbox, h.name) or {}
        try:
            if datetime.fromisoformat(str(beat.get("at")).replace("Z", "+00:00")).timestamp() >= since - 1:
                return beat
        except ValueError:
            pass
        time.sleep(2)
    raise KxError(f"{h.name}: the agent started on Colab but has not reported after {timeout:g} s",
                  fix="its log is agent.log in the host's dir on the runtime; kx remote release HOST, then try again")


def launch(h) -> dict:
    """Get the host's runtime (an existing one, or a new one) and start its agent there."""
    from kx.remote.agent import join
    api = Api(h)
    name = f"runtimes/{runtime_id(h)}"
    rt = api.runtime(name)
    created = rt is None
    if created:
        print(f"kx: {h.name}: asking Colab for a {h.accelerator or 'T4'} runtime", file=sys.stderr)
        rt = api.create(runtime_id(h), h.accelerator or "T4")
    rt = api.runtime(rt.get("name", name)) or rt  # a fresh proxy token
    conn = rt.get("connectionInfo") or {}
    if not conn.get("url") or not conn.get("token"):
        raise KxError(f"{h.name}: the Colab runtime {rt.get('name')} has no connection info yet")
    began = time.time()
    out = execute(conn, bootstrap(h, rclone_section(h), join(h.mailbox, "_kx", "kx.tar.gz")), until=STARTED)
    if STARTED not in out:  # the cell ended before the agent started: say why instead of waiting for a report
        raise KxError(f"{h.name}: the bootstrap cell on {rt.get('name')} ended without starting the agent:\n{out[-1500:]}")
    _first_report(h, began)  # a run queued right after must see this agent, not start a second one
    keeper = start_keepalive(h)
    print(f"kx: {h.name}: agent started on Colab runtime {rt.get('name')}; this machine keeps it connected "
          f"(pid {keeper}, log {_keeper_files(h)[1]})", file=sys.stderr)
    return {"host": h.name, "runtime": rt.get("name"), "created": created, "spec": rt.get("runtimeSpec"),
            "agent": out.strip().splitlines()[-1] if out.strip() else None}


def ensure(h) -> dict | None:
    """Start the host's agent on Colab unless its runtime is up and its agent has reported lately (a heartbeat alone
    is not enough: it outlives a runtime kx has just released by up to mailbox.AGENT_STALE_S)."""
    from kx.remote import mailbox
    from kx.ledger.store import age_s
    beat = mailbox.heartbeat(h.mailbox, h.name)
    fresh = beat is not None and (age_s(beat.get("at")) or 1e9) <= mailbox.AGENT_STALE_S
    if fresh and Api(h).runtime(f"runtimes/{runtime_id(h)}") is not None:
        start_keepalive(h)  # if the one keeping it connected is gone (this machine restarted, say)
        return None
    return launch(h)


def release(h) -> bool:
    """Delete the host's runtime (True if there was one)."""
    return Api(h).delete(f"runtimes/{runtime_id(h)}")


# -- keeping the runtime connected --------------------------------------------------------------------------------------
# Colab reclaims a runtime that no client is connected to (only Pro+ has background execution): a busy kernel is not
# enough, so the run on it dies with it. While the host has runs, a detached process here holds a kernel websocket
# open as a browser tab or the VS Code extension would, answers its pings, polls the runtime through its proxy, and
# reconnects with a fresh proxy token before the old one expires. It releases the runtime itself once the host has
# had nothing to run for KEEP_IDLE_S (or its agent has not reported for that long), and ends when the runtime is gone.
KEEP_EVERY_S = float(os.environ.get("KX_COLAB_KEEP_EVERY_S", 30))
KEEP_IDLE_S = float(os.environ.get("KX_COLAB_KEEP_IDLE_S", 600))


def _keeper_files(h) -> tuple[Path, Path]:
    d = Path(os.environ.get("KX_COLAB_KEEPER_DIR", "~/.cache/kx")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d / f"colab-keepalive-{runtime_id(h)}.pid", d / f"colab-keepalive-{runtime_id(h)}.log"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def start_keepalive(h) -> int:
    """The pid of the process keeping the host's runtime connected, started unless one runs."""
    import dataclasses
    pidfile, logfile = _keeper_files(h)
    try:
        pid = int(pidfile.read_text())
        if _alive(pid):
            return pid
    except (OSError, ValueError):
        pass
    env = os.environ | {"KX_COLAB_API": API, "KX_COLAB_TOKEN_URL": TOKEN_URL, "KX_COLAB_TOKEN_FILE": str(TOKEN_FILE)}
    src = str(Path(__file__).resolve().parents[2])  # this kx, whatever runs it
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    with open(logfile, "ab") as log:
        p = subprocess.Popen([sys.executable, "-m", "kx.remote.colab", "keepalive", json.dumps(dataclasses.asdict(h))],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    pidfile.write_text(f"{p.pid}\n")
    return p.pid


def _host_idle(h) -> bool:
    """Nothing to run: the agent reports no runs and no job waits (or the agent has stopped reporting)."""
    from kx.remote import mailbox
    from kx.remote.agent import join
    from kx.ledger.store import age_s
    beat = mailbox.heartbeat(h.mailbox, h.name)
    if beat is None or (age_s(beat.get("at")) or 1e9) > mailbox.AGENT_STALE_S:
        return True
    if beat.get("running"):
        return False
    jobs = mailbox.rclone("lsf", "--dirs-only", join(h.mailbox, "_hosts", h.name, "jobs"), check=False, timeout=120)
    return not (jobs.returncode == 0 and jobs.stdout.strip())


def _expires(conn: dict) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(conn.get("expireTime")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time() + 600


class _Connection:
    """One kernel websocket through the runtime's proxy, held open."""

    def __init__(self, conn: dict):
        self.base, self.until = conn["url"].rstrip("/"), _expires(conn)
        self.headers = {"X-Colab-Runtime-Proxy-Token": conn["token"], "X-Colab-Client-Agent": "kx"}
        req = urllib.request.Request(f"{self.base}/api/kernels", headers=self.headers)
        with urllib.request.urlopen(req, timeout=60) as r:
            kernels = json.load(r)
        if not kernels:
            raise KxError("no kernel on the runtime to stay connected to")
        self.session = uuid.uuid4().hex
        self.ws = WebSocket(self.base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
                            + f"/api/kernels/{kernels[0]['id']}/channels?session_id={self.session}", self.headers, timeout=20)

    def touch(self, wait: float = 5):
        """A kernel_info_request on the control channel (answered even while the bootstrap cell runs), the pending
        messages read (pings answered), and the proxy polled."""
        self.ws.send(json.dumps({"header": {"msg_id": uuid.uuid4().hex, "username": "kx", "session": self.session,
                                            "msg_type": "kernel_info_request", "version": "5.3",
                                            "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                                 "parent_header": {}, "metadata": {}, "content": {}, "channel": "control"}))
        self.ws.sock.settimeout(wait)
        try:
            while self.ws.recv() is not None:
                pass
            raise EOFError  # the runtime closed the connection: reconnect
        except (socket.timeout, TimeoutError):
            pass
        finally:
            self.ws.sock.settimeout(20)
        with urllib.request.urlopen(urllib.request.Request(f"{self.base}/api/kernels", headers=self.headers), timeout=60):
            pass

    def close(self):
        self.ws.close()


def keepalive(h, every: float = KEEP_EVERY_S, idle_s: float = KEEP_IDLE_S) -> int:
    def say(msg):
        print(time.strftime("%Y-%m-%d %H:%M:%S"), f"{h.name}: {msg}", flush=True)
    api, name, conn, idle_since = Api(h), f"runtimes/{runtime_id(h)}", None, None
    say(f"keeping {name} connected every {every:g} s")
    try:
        while True:
            try:
                rt = api.runtime(name)
                if rt is None:
                    say("the runtime is gone: done")
                    return 0
                if _host_idle(h):
                    idle_since = idle_since or time.monotonic()
                    if time.monotonic() - idle_since >= idle_s:
                        say(f"nothing to run for {idle_s:g} s: releasing the runtime")
                        api.delete(name)
                        return 0
                else:
                    idle_since = None
                if conn is None or time.time() > conn.until - 300:  # a fresh proxy token before this one expires
                    if conn is not None:
                        conn.close()
                    conn = _Connection(rt["connectionInfo"])
                    say("connected to the kernel")
                conn.touch(wait=min(5.0, max(0.2, every)))
            except (OSError, EOFError, KxError, ValueError, KeyError) as e:  # a hiccup: reconnect at the next look
                say(f"reconnecting after: {e}")
                if conn is not None:
                    try:
                        conn.close()
                    except OSError:
                        pass
                conn = None
            time.sleep(every)
    finally:
        pidfile = _keeper_files(h)[0]
        try:
            if int(pidfile.read_text()) == os.getpid():
                pidfile.unlink()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    if sys.argv[1:2] == ["keepalive"]:
        from kx.remote.hosts import Host
        sys.exit(keepalive(Host(**json.loads(sys.argv[2]))))

