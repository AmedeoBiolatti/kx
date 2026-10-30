"""Remote hosts: where they are configured, and the transport that reaches them (ssh, or `local` for a directory on
this machine, which tests use; a mailbox host has its own, kx.remote.mailbox). Every remote operation goes through
a Transport: run a bash script there, copy files with rsync.

~/.config/kx/hosts.yaml (machine-wide), overridden per competition by comp.yaml `hosts:`:

    lambda:
      ssh: ubuntu@203.0.113.7       # what `ssh` takes; keys from your ssh config / agent
      dir: kx-remote                # relative to the remote home (default kx-remote)
      gpus: 1
      usd_per_hour: 1.29            # paid host: wall time x price is charged to the cloud_usd budget
      env: {CUDA_VISIBLE_DEVICES: "0"}
    oldlaptop:
      ssh: me@oldlaptop
      torch: cu126                  # the lock's torch version, from download.pytorch.org/whl/cu126 (Maxwell..Volta GPUs)
    farbox:
      kind: mailbox                 # unreachable from here: a kx agent there takes runs from shared storage
      mailbox: gdrive:kx            # an rclone remote:path both machines reach (kx.remote.mailbox)
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

from kx.ledger.config import KxError

HOSTS_FILE = Path(os.environ.get("KX_HOSTS_FILE", "~/.config/kx/hosts.yaml")).expanduser()
KNOWN_HOSTS = HOSTS_FILE.parent / "known_hosts"  # host keys kx learnt from Tailscale (kx remote add), besides ssh's own
SSH_OPTS = ["-o", "BatchMode=yes", "-o", f"UserKnownHostsFile=~/.ssh/known_hosts {KNOWN_HOSTS}",
            "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
            # one connection reused for the many short calls of a run (sync every few seconds); its socket is in kx's
            # cache, which a sandboxed worker may write (not ~/.ssh)
            "-o", "ControlMaster=auto", "-o", "ControlPath=~/.cache/kx/ssh/%C", "-o", "ControlPersist=120"]


@dataclass
class Host:
    name: str
    kind: str = "ssh"               # ssh | mailbox
    ssh: str | None = None          # user@host; None with local=True
    local: bool = False             # a directory on this machine (tests, or a second disk)
    dir: str = "kx-remote"
    gpus: int = 0
    usd_per_hour: float = 0.0
    python: str | None = None       # an interpreter that already has kx and the project's packages: skips uv
    env: dict = field(default_factory=dict)
    ssh_opts: list = field(default_factory=list)
    torch: str | None = None        # a PyTorch wheel index for this host (cu126 for old GPUs): replaces the lock's torch
    mailbox: str | None = None      # kind mailbox: the rclone remote:path runs and results go through
    confine: bool = True            # runs in bubblewrap there when it has it (kx.remote.execute._confine_lines)

    @property
    def transport(self) -> "Transport":
        if self.kind == "mailbox":
            from kx.remote import mailbox
            return mailbox.Transport(self)
        return Transport(self)


def _load_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise KxError(f"{path}: invalid YAML: {e}") from None
    return data.get("hosts", data) if isinstance(data, dict) else {}


FIELDS = tuple(f.name for f in fields(Host) if f.name != "name")


def load(comp=None) -> dict[str, Host]:
    raw = dict(_load_file(HOSTS_FILE))
    if comp is not None:
        comp_raw = yaml.safe_load((comp.root / "comp.yaml").read_text()) or {}
        raw.update(comp_raw.get("hosts") or {})
    hosts = {}
    for name, h in raw.items():
        if not isinstance(h, dict):
            raise KxError(f"host {name}: must be a mapping", fix=f"edit {HOSTS_FILE}")
        unknown = sorted(set(h) - set(FIELDS))
        if unknown:  # a typo (gpu: 1) would otherwise be ignored without a word
            raise KxError(f"host {name}: unknown option(s) {', '.join(unknown)}", fix=f"options: {', '.join(FIELDS)}")
        kind = h.get("kind", "ssh")
        if kind == "ssh" and not (h.get("ssh") or h.get("local")):
            raise KxError(f"host {name}: needs `ssh: user@host` (or `local: true`, or `kind: mailbox`)", fix=f"edit {HOSTS_FILE}")
        if kind == "mailbox" and not h.get("mailbox"):
            raise KxError(f"host {name}: a mailbox host needs `mailbox: REMOTE:PATH` (an rclone location both machines reach)",
                          fix=f"edit {HOSTS_FILE}")
        if kind not in ("ssh", "mailbox"):
            raise KxError(f"host {name}: unknown kind {kind!r}", fix="kind: ssh (the default) or mailbox")
        hosts[name] = Host(name=name, **h)
    return hosts


def get(name: str, comp=None) -> Host:
    hosts = load(comp)
    if name not in hosts:
        raise KxError(f"no host {name!r}", fix=f"configured: {', '.join(hosts) or 'none'}; hosts live in {HOSTS_FILE}")
    return hosts[name]


class Transport:
    """An ssh host (or a local directory): run commands there and move files to and from it. kx.remote.mailbox has
    the same operations for a host it reaches only through a mailbox, so kx.remote.execute uses either alike."""
    POLL_S = (2, 5, 10)      # the first copies come back quickly, then every 10 s
    ARCHIVE = False          # the kx sources go as a tree (rsync sends only what changed)
    STOP_WAIT_S = 20         # after TERM, how long a run gets to end before KILL
    UNREACHABLE = "{host} is unreachable; still trying (the run goes on there)"

    def __init__(self, host: Host):
        self.host = host
        (Path.home() / ".cache" / "kx" / "ssh").mkdir(parents=True, exist_ok=True)

    @property
    def ssh_cmd(self) -> list[str]:
        """ssh with the host's options first (ssh keeps an option's first value). In kx's sandbox the system's config
        files look foreign-owned and ssh refuses them: only the user's own config is read there."""
        own = Path.home() / ".ssh" / "config"
        cfg = ["-F", str(own) if own.exists() else "none"] if os.environ.get("KX_SANDBOX") else []
        return ["ssh", *cfg, *self.host.ssh_opts, *SSH_OPTS]

    @property
    def ssh_argv(self) -> list[str]:
        return [*self.ssh_cmd, self.host.ssh]

    def run(self, script: str, *, check: bool = True, timeout: float | None = 600,
            input: str | None = None) -> subprocess.CompletedProcess:
        """Run a bash script on the host (in the home directory) and capture its output. Secrets go in `input` (its
        stdin), never in the script: that is the command line, which other users of the host can see."""
        if self.host.local:
            argv = ["bash", "-c", script]
        else:
            argv = [*self.ssh_argv, "bash -c " + shlex.quote(script)]
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=input,
                               cwd=Path.home() if self.host.local else None)
        except subprocess.TimeoutExpired:
            raise KxError(f"{self.host.name}: command timed out after {timeout}s", fix=f"kx remote check {self.host.name}") from None
        if check and p.returncode != 0:
            raise KxError(f"{self.host.name}: {(p.stderr or p.stdout).strip()[-1500:] or f'exit {p.returncode}'}",
                          fix=f"kx remote check {self.host.name}")
        return p

    def _target(self, remote_path: str) -> str:
        if self.host.local:
            return remote_path if remote_path.startswith("/") else str(Path.home() / remote_path)
        return f"{self.host.ssh}:{remote_path}"

    def _rsync(self, src: str, dst: str, extra: list[str]) -> None:
        argv = ["rsync", "-a", "--partial", "--timeout=120", *extra]  # timeout: a connection that stalls fails, not hangs
        if not self.host.local:
            argv += ["-e", " ".join(shlex.quote(x) for x in self.ssh_cmd)]
        p = subprocess.run([*argv, src, dst], capture_output=True, text=True)
        if p.returncode not in (0, 24):  # 24: some files vanished while copying (a live run dir)
            raise KxError(f"{self.host.name}: rsync failed: {(p.stderr or p.stdout).strip()[-1500:]}")

    def push(self, local: Path, remote_path: str, *, files=None, delete: bool = False, excludes=()) -> None:
        """Copy a local file, or a directory's contents, to the host (missing folders are made); with `files`, only
        those (paths relative to it; a directory brings what is under it); with `delete`, what local lacks goes."""
        src = str(local) + ("/" if local.is_dir() else "")
        extra = ["--mkpath", *(["--delete"] if delete else []), *[f"--exclude={e}" for e in excludes]]
        if files is None:
            return self._rsync(src, self._target(remote_path), extra)
        with file_list(files) as listing:
            self._rsync(src, self._target(remote_path), [*extra, "-r", f"--files-from={listing}"])

    def pull(self, remote_path: str, local: Path, *, excludes=(), max_size: str | None = None, only=()) -> None:
        """Copy a host directory's contents into a local directory (never deletes local files): files up to `max_size`
        (rsync's form, e.g. 32M) or, with `only`, just these paths."""
        local.mkdir(parents=True, exist_ok=True)
        extra = [f"--exclude={e}" for e in excludes] + ([f"--max-size={max_size}"] if max_size else [])
        if only:  # a path in a subdirectory needs its parents included too
            parents = {"/" + "/".join(n.split("/")[:i]) + "/" for n in only for i in range(1, n.count("/") + 1)}
            extra += [f"--include={p}" for p in sorted(parents)] + [f"--include=/{n}" for n in only] + ["--exclude=*"]
        self._rsync(self._target(remote_path.rstrip("/") + "/"), str(local) + "/", extra)

    def read(self, path: str) -> str | None:
        p = self.run(f"cat {shlex.quote(path)} 2>/dev/null", check=False, timeout=60)
        return p.stdout if p.returncode == 0 else None

    def write(self, path: str, text: str) -> None:
        self.run(f"mkdir -p \"$(dirname {shlex.quote(path)})\" && cat > {shlex.quote(path)}", input=text, timeout=60)

    def list_big(self, remote_path: str, size: str) -> list[tuple[str, int]]:
        """The files over `size` (32M) under a host directory: (relative path, bytes)."""
        n = int(size[:-1]) * {"K": 2**10, "M": 2**20, "G": 2**30}[size[-1]]
        p = self.run(f"cd {shlex.quote(remote_path)} 2>/dev/null || exit 0; find . -type f -size +{n}c -printf '%P\\t%s\\n'",
                     timeout=120)
        return [(a, int(b)) for a, b in (line.rsplit("\t", 1) for line in p.stdout.splitlines() if "\t" in line)]

    def start(self, job: str, meta: str) -> None:
        """Start the job folder's run.sh in its own session, so it outlives this connection."""
        self.run(f"cd {shlex.quote(job)} && if command -v setsid >/dev/null; then setsid bash run.sh </dev/null >/dev/null 2>&1 & "
                 "else nohup bash run.sh </dev/null >/dev/null 2>&1 & fi")

    def alive(self, inf: dict, started: bool) -> str:
        """'running', 'gone' (its process is not there), or 'unreachable'."""
        if not started:
            return "running"  # run.sh has not written its pid yet
        try:
            p = self.run(f"kill -0 $(cat {shlex.quote(inf['path'])}/out/.remote/pid) 2>/dev/null", check=False, timeout=60)
        except KxError:
            return "unreachable"
        if p.returncode == 0:
            return "running"
        return "unreachable" if p.returncode == 255 and not self.host.local else "gone"

    def remove(self, inf: dict) -> None:
        """Delete the host's copy of a run that has ended (storage.keep: here, once it is all here)."""
        path = inf["path"]
        if "/runs/r" not in path:  # never anything but a run's folder
            raise KxError(f"{self.host.name}: refusing to delete {path}: not a run's folder")
        self.run(f"rm -rf -- {shlex.quote(path)}", timeout=600)

    def stop(self, inf: dict, force: bool = False) -> None:
        """TERM the run's process group (KILL with force)."""
        sig, pidf = "KILL" if force else "TERM", f"{shlex.quote(inf['path'])}/out/.remote/pid"
        self.run(f"test -f {pidf} && p=$(cat {pidf}) && (kill -{sig} -- -$p 2>/dev/null || kill -{sig} $p 2>/dev/null); true",
                 check=False)


@contextmanager
def file_list(files):
    """A temporary file listing `files`, one per line (rsync and rclone --files-from)."""
    with tempfile.NamedTemporaryFile("w", suffix=".files") as f:
        f.write("\n".join(files) + "\n")
        f.flush()
        yield f.name


def check(host: Host) -> list[dict]:
    """What a run needs on the host: reachable, rsync, git, uv (or python), bubblewrap, disk, a GPU when configured."""
    if host.kind == "mailbox":
        from kx.remote import mailbox
        return mailbox.check(host)
    out = []

    def add(ok, what, fix=None):
        out.append({"ok": ok, "check": what, "fix": fix})
    t = host.transport
    try:
        p = t.run("echo ok; uname -sm; command -v git rsync uv nvidia-smi python3; "
                  "bwrap --ro-bind / / --dev-bind /dev /dev true 2>/dev/null && echo /bwrap-usable; "
                  f"mkdir -p {shlex.quote(host.dir)} && df -Pk {shlex.quote(host.dir)} | tail -1", check=True, timeout=60)
    except KxError as e:
        add(False, f"reach {host.ssh or 'local'}", str(e))
        return out
    lines = p.stdout.splitlines()
    add(True, f"reach {host.ssh or 'local'} ({lines[1] if len(lines) > 1 else '?'})")
    found = "\n".join(lines)
    add("/rsync" in found, "rsync on the host", "install rsync")
    add("/git" in found, "git on the host (probes apply their patch with it)", "install git")
    if host.python:
        add(True, f"python: {host.python} (uv is not used)")
    else:
        add("/uv" in found, "uv on the host (sets up the project's environment)", "curl -LsSf https://astral.sh/uv/install.sh | sh")
    if host.confine:
        add("/bwrap-usable" in found, "bubblewrap on the host (runs are confined to their own dir)",
            "sudo apt install bubblewrap  (or `confine: false` for this host)")
    if host.gpus:
        add("/nvidia-smi" in found, f"{host.gpus} GPU(s) configured: nvidia-smi on the host", "install the NVIDIA driver")
    try:
        free_gb = int(lines[-1].split()[3]) / 2**20
        add(free_gb > 5, f"disk: {free_gb:.0f} GB free in {host.dir}", "free space, or set `dir:` to a bigger disk")
    except (IndexError, ValueError):
        add(False, "disk space unknown")
    return out


def tailscale_peers() -> list[dict]:
    """The other machines on this machine's Tailscale network: [{name, dns, ip, os, online, host_keys}]. host_keys:
    their ssh host keys as Tailscale knows them (machines running Tailscale SSH)."""
    import json
    import shutil
    if shutil.which("tailscale") is None:
        raise KxError("tailscale is not installed here", fix="install Tailscale (tailscale.com/download), or pass --ssh USER@HOST")
    p = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=30)
    try:
        d = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise KxError(f"tailscale status failed: {(p.stderr or p.stdout).strip()[-300:]}", fix="sudo tailscale up") from None
    out = []
    for x in (d.get("Peer") or {}).values():
        dns = (x.get("DNSName") or "").rstrip(".")
        out.append({"name": x.get("HostName") or dns.split(".")[0], "dns": dns or None,
                    "ip": next(iter(x.get("TailscaleIPs") or []), None), "os": x.get("OS") or "", "online": bool(x.get("Online")),
                    "host_keys": list(x.get("sshHostKeys") or [])})
    return out


def find_peer(name: str, peers: list[dict]) -> dict:
    """The peer NAME means: its host name or the first label of its MagicDNS name, then a unique prefix of either."""
    n = name.lower()
    def labels(x):
        return {x["name"].lower(), (x["dns"] or "").split(".")[0].lower()}
    hits = [x for x in peers if n in labels(x)] or [x for x in peers if any(lab.startswith(n) for lab in labels(x))]
    if len(hits) == 1:
        return hits[0]
    known = ", ".join(f"{(x['dns'] or x['name']).split('.')[0]} ({x['os']}{'' if x['online'] else ', offline'})" for x in peers)
    if hits:
        raise KxError(f"{name!r} matches several machines on Tailscale: {known}", fix="use the full name, or --ssh USER@HOST")
    raise KxError(f"no machine {name!r} on your Tailscale network (it has: {known or 'no other machines'})",
                  fix=f"on {name}: install Tailscale, log in with your account and run `sudo tailscale up --ssh`")


def add(name: str, entry: dict) -> Path:
    """Add a host to the machine-wide hosts file, keeping what is there (comments too)."""
    path = HOSTS_FILE
    text = path.read_text() if path.exists() else ""
    raw = (yaml.safe_load(text) or {}) if text.strip() else {}
    nested = isinstance(raw, dict) and isinstance(raw.get("hosts"), dict)
    line = f"{name}: {yaml.safe_dump(entry, default_flow_style=True, width=1000, sort_keys=False).strip()}\n"
    new = text + ("" if not text or text.endswith("\n") else "\n") + ("  " + line if nested else line)
    try:
        parsed = yaml.safe_load(new) or {}
        ok = (parsed.get("hosts", parsed) if nested else parsed).get(name) == entry
    except (yaml.YAMLError, AttributeError):
        ok = False
    if not ok:  # e.g. `hosts:` is not the last block: appending would not land in it
        raise KxError(f"could not add {name} to {path} without rewriting it",
                      fix=f"add it yourself: {line.strip()}" + (" (under hosts:)" if nested else ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new)
    return path


def trust(names: list[str], keys: list[str]) -> int:
    """Add a machine's host keys (from Tailscale) to kx's known_hosts, so ssh can check it is that machine."""
    have = set(KNOWN_HOSTS.read_text().splitlines()) if KNOWN_HOSTS.exists() else set()
    new = [f"{','.join(n for n in names if n)} {k.strip()}" for k in keys if k.strip()]
    new = [x for x in new if x not in have]
    if new:
        KNOWN_HOSTS.parent.mkdir(parents=True, exist_ok=True)
        with open(KNOWN_HOSTS, "a") as f:
            f.write("".join(x + "\n" for x in new))
    return len(new)
