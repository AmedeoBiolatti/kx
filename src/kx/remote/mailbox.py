"""Mailbox hosts (`kind: mailbox`): machines kx cannot reach (no ssh, no Tailscale), run through an rclone location
both reach, e.g. a Google Drive folder. kx leaves each run there as a job; the kx agent on the machine
(kx.remote.agent, whose docstring has the mailbox's layout) takes it, runs it, and copies its out/ back there; kx
copies that back here like an ssh host's and records the run. Only rclone runs here: the machine is never contacted.

    farbox: {kind: mailbox, mailbox: gdrive:kx, gpus: 1}

The data and the kx sources travel through the mailbox too, once per version.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from kx.ledger.config import KxError
from kx.ledger.store import age_s
from kx.remote.agent import join
from kx.remote.hosts import file_list

AGENT_STALE_S = 180  # no heartbeat for this long: the agent is not running (or cannot reach the mailbox)


def rclone(*args: str, input: str | None = None, check: bool = True, timeout: float = 3600) -> subprocess.CompletedProcess:
    if shutil.which("rclone") is None:
        raise KxError("mailbox hosts need rclone here", fix="install rclone (https://rclone.org/install/), then "
                      "`rclone config` a remote for the mailbox (Google Drive: scope drive.file)")
    try:
        p = subprocess.run(["rclone", *args], capture_output=True, text=True, input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise KxError(f"rclone {args[0]} timed out after {timeout:g} s") from None
    if check and p.returncode != 0:
        raise KxError(f"rclone {args[0]}: {(p.stderr or p.stdout).strip()[-800:]}", fix="rclone config  (is the remote set up?)")
    return p


class Transport:
    """The operations of kx.remote.hosts.Transport, through the mailbox. Paths are rclone locations."""
    POLL_S = (5, 10, 15)     # a mailbox answers in tens of seconds at best: polling faster only costs requests
    ARCHIVE = True           # the kx sources go as one kx.tar.gz: Google Drive takes seconds per file
    STOP_WAIT_S = 180        # the agent stops a run at its next look, KILL 20 s after TERM
    UNREACHABLE = "no kx agent for {host} has reported lately: is it running there? still waiting"

    def __init__(self, host):
        self.host = host

    def push(self, local: Path, remote_path: str, *, files=None, delete: bool = False, excludes=()) -> None:
        # rsync's `--exclude=NAME` also skips a directory's contents; rclone's needs NAME/** for that
        args = [x for e in excludes for x in ([f"--exclude={e}"] + ([f"--exclude={e}/**"] if not set("*/") & set(e) else []))]
        if local.is_file():
            rclone("copyto", str(local), remote_path)
        elif files is None:
            rclone("sync" if delete else "copy", str(local), remote_path, *args)
        else:
            with file_list(files) as listing:
                rclone("copy", str(local), remote_path, "--files-from", listing, *args)

    def pull(self, remote_path: str, local: Path, *, excludes=(), max_size: str | None = None, only=()) -> None:
        local.mkdir(parents=True, exist_ok=True)
        args = [f"--exclude={e}" for e in excludes] + ([f"--max-size={max_size}"] if max_size else [])
        args += [f"--include=/{n}" for n in only]
        p = rclone("copy", remote_path, str(local), *args, check=False, timeout=1800)
        if p.returncode != 0 and "directory not found" not in p.stderr:  # nothing up yet: nothing to copy
            raise KxError(f"{self.host.name}: copying back from the mailbox failed: {p.stderr.strip()[-800:]}")

    def read(self, path: str) -> str | None:
        p = rclone("cat", path, check=False, timeout=120)
        return p.stdout if p.returncode == 0 else None

    def write(self, path: str, text: str) -> None:
        rclone("rcat", path, input=text, timeout=120)

    def list_big(self, remote_path: str, size: str) -> list[tuple[str, int]]:
        p = rclone("lsf", "-R", "--files-only", "--min-size", size, "--format", "ps", remote_path, check=False, timeout=600)
        if p.returncode != 0:
            return []
        return [(a, int(b)) for a, b in (line.rsplit(";", 1) for line in p.stdout.splitlines() if ";" in line)]

    def start(self, job: str, meta: str) -> None:
        self.write(join(job, "job.json"), meta)  # last: the agent takes a job only once this is there

    def alive(self, inf: dict, started: bool) -> str:
        """'running' (or waiting for the agent to take it), 'gone' (the agent has it nowhere), or 'unreachable' (no
        agent has reported lately)."""
        beat = heartbeat(self.host.mailbox, self.host.name)
        if beat is None or (age_s(beat.get("at")) or 1e9) > AGENT_STALE_S:
            return "unreachable"
        if inf["key"] in beat.get("running", []):
            return "running"
        parent, name = inf["job"].rsplit("/", 1)
        if f"{name}/" in rclone("lsf", "--dirs-only", parent, check=False, timeout=120).stdout.split():
            return "running"  # queued: the agent takes it when a slot is free
        return "gone"

    def stop(self, inf: dict, force: bool = False) -> None:  # the agent sends TERM, then KILL itself
        self.write(join(self.host.mailbox, "_hosts", self.host.name, "kill", inf["key"].replace("/", "~")), "kill\n")


def heartbeat(mailbox: str, host: str) -> dict | None:
    p = rclone("cat", join(mailbox, "_hosts", host, "agent.json"), check=False, timeout=120)
    try:
        return json.loads(p.stdout) if p.returncode == 0 else None
    except ValueError:
        return None


def check(h) -> list[dict]:
    out = []

    def add(ok, what, fix=None):
        out.append({"ok": ok, "check": what, "fix": fix})
    if shutil.which("rclone") is None:
        add(False, "rclone here", "install rclone (https://rclone.org/install/), then `rclone config` the mailbox's remote")
        return out
    p = rclone("lsf", "--max-depth", "1", h.mailbox, check=False, timeout=120)
    if p.returncode != 0 and "directory not found" not in p.stderr:
        add(False, f"reach the mailbox {h.mailbox}", (p.stderr or p.stdout).strip()[-300:] + "  (rclone config?)")
        return out
    add(True, f"reach the mailbox {h.mailbox}")
    beat = heartbeat(h.mailbox, h.name)
    if beat is None:
        add(False, f"an agent for {h.name}", f"none has reported yet: start it there ({start_command(h)}); kx remote setup {h.name} "
                                            "first copies the kx sources it starts from")
        return out
    age = age_s(beat.get("at")) or 0
    add(age <= AGENT_STALE_S, f"agent on {beat.get('machine', '?')}: last report {age:.0f} s ago",
        f"is it still running there? ({start_command(h)})")
    if h.gpus:
        add(beat.get("gpus", 0) >= h.gpus, f"{h.gpus} GPU(s) configured: the agent sees {beat.get('gpus', 0)}", "nvidia-smi there")
    free = beat.get("free_gb")
    if free is not None:
        add(free > 5, f"disk: {free:.0f} GB free there", "free space, or start the agent with --dir on a bigger disk")
    return out


def start_command(h) -> str:
    d = h.dir if h.dir.startswith(("/", "~")) else f"~/{h.dir}"
    return (f"mkdir -p {d}/kx && rclone cat {join(h.mailbox, '_kx', 'kx.tar.gz')} | tar -xz -C {d}/kx && "
            f"python3 {d}/kx/src/kx/remote/agent.py {h.name} "
            f"--mailbox {h.mailbox}" + (f" --dir {d}" if d != "~/kx-remote" else ""))
