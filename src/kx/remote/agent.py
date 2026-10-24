"""The kx agent: runs kx runs on a machine this one cannot reach (no ssh, no Tailscale), through a mailbox: an rclone
location (Google Drive, S3, ...) both reach. It only makes outbound calls, so it works behind any NAT or firewall.

Standard library only, so a fresh machine starts it from the kx sources kx keeps in the mailbox:

    mkdir -p ~/kx-remote/kx && rclone cat gdrive:kx/_kx/kx.tar.gz | tar -xz -C ~/kx-remote/kx
    python3 ~/kx-remote/kx/src/kx/remote/agent.py NAME --mailbox gdrive:kx

Every few seconds it takes the jobs kx left for NAME, sets each up as `kx exec --on` does over ssh (same layout under
--dir: <slug>/data, <slug>/venvs, <slug>/runs/<run>), starts its run.sh, copies the run's out/ to the mailbox while
it runs (files over 32 MB, like checkpoints, once at the end; .remote/exit last, so kx sees an ended run whole), stops
a run when kx leaves a `kill` for it, and writes a heartbeat kx reads to tell a live run from a lost one.

In the mailbox (one folder per competition; what the machines exchange in _-prefixed folders, kx writes the rest):

    _kx/kx.tar.gz                            the kx sources (_kx/.kx-hash: their version)
    _hosts/<name>/agent.json                 heartbeat: when, which machine, its GPUs and disk, the runs it has
    _hosts/<name>/jobs/<slug>~<run>/         a run to start: run.sh, code.tar.gz, patches; job.json written last
    _hosts/<name>/kill/<slug>~<run>          left by kx remote kill
    <slug>/data/<version>/                   competition data, once per version (.kx-complete written last)
    <slug>/runs/<run>/out/                   the run's KX_RUN_DIR, copied back by kx
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

BIG = "32M"  # bigger files go up once the run has ended (and kx copies them back only when asked: kx fetch)
LIVE_FILES = ("stdout.log", "stderr.log", "metrics.jsonl", "progress.json")  # always copied, whatever their size
KILL_GRACE_S = 20


def join(root: str, *parts: str) -> str:
    return root + "/".join(parts) if root.endswith(":") else "/".join([root.rstrip("/"), *parts])


def log(msg: str):
    print(f"{datetime.now():%H:%M:%S} kx agent: {msg}", flush=True)


class Agent:
    def __init__(self, name: str, mailbox: str, base: Path, every: float = 10, max_runs: int = 1, config: str | None = None):
        self.name, self.root, self.base, self.every, self.max_runs = name, mailbox, base, every, max_runs
        self.rc = ["rclone", *(["--config", config] if config else [])]
        self.box = join(mailbox, "_hosts", name)
        self.procs: dict[str, subprocess.Popen] = {}  # runs this agent started (their exits are reaped here)

    def rclone(self, *args: str, input: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
        p = subprocess.run([*self.rc, *args], capture_output=True, text=True, input=input, timeout=3600)
        if check and p.returncode != 0:
            raise RuntimeError(f"rclone {args[0]}: {(p.stderr or p.stdout).strip()[-500:]}")
        return p

    def lsf(self, path: str, *flags: str) -> list[str]:
        p = self.rclone("lsf", *flags, path, check=False)
        return p.stdout.split() if p.returncode == 0 else []

    # -- runs on this machine ----------------------------------------------------------------------------------------
    def runs(self) -> dict[str, Path]:
        """slug/run -> its dir, for the runs not yet copied up whole."""
        out = {}
        for d in sorted(self.base.glob("*/runs/*")):
            if (d / "out" / ".remote" / "pid").exists() and not (d / ".agent-uploaded").exists():
                out[f"{d.parent.parent.name}/{d.name}"] = d
        return out

    def alive(self, key: str, d: Path) -> bool:
        if key in self.procs:
            return self.procs[key].poll() is None
        try:
            os.kill(int((d / "out" / ".remote" / "pid").read_text()), 0)
            return True
        except (OSError, ValueError):
            return False

    def heartbeat(self):
        usage = shutil.disk_usage(self.base)
        gpus = 0
        if shutil.which("nvidia-smi"):
            p = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True)
            gpus = sum(line.startswith("GPU ") for line in p.stdout.splitlines())
        beat = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "machine": socket.gethostname(),
                "pid": os.getpid(), "every": self.every, "gpus": gpus, "free_gb": round(usage.free / 2**30, 1),
                "running": sorted(self.runs())}
        self.rclone("rcat", join(self.box, "agent.json"), input=json.dumps(beat))

    # -- taking a job ------------------------------------------------------------------------------------------------
    def take_jobs(self):
        for entry in self.lsf(join(self.box, "jobs"), "--dirs-only"):
            name = entry.rstrip("/")
            slug, _, run = name.partition("~")
            if not run:
                continue
            d = self.base / slug / "runs" / run
            src = join(self.box, "jobs", name)
            if (d / "out" / ".remote" / "pid").exists():  # started before (an agent restarted before clearing it)
                self.rclone("purge", src, check=False)
                continue
            if sum(self.alive(k, x) for k, x in self.runs().items()) >= self.max_runs:
                return  # it waits for a free slot
            if "job.json" not in self.lsf(src, "--files-only"):
                continue  # still being uploaded
            self.start(slug, run, src, d)

    def start(self, slug: str, run: str, src: str, d: Path):
        d.mkdir(parents=True, exist_ok=True)
        self.rclone("copy", src, str(d))
        job = json.loads((d / "job.json").read_text())
        log(f"{slug} {run}: setting up")
        try:
            if job.get("kx_hash"):
                self.ensure_kx(job["kx_hash"])
            if job.get("data"):
                self.ensure_data(slug, job["data"])
            if not job.get("python") and not shutil.which("uv"):
                self.install_uv()
        except Exception as e:  # noqa: BLE001 - the run fails, with why, instead of the agent
            self.fail(d, f"kx agent: could not set up {run}: {e}")
            self.rclone("purge", src, check=False)
            return
        (d / "out" / ".remote").mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["PATH"] = f"{Path.home() / '.local/bin'}{os.pathsep}{env.get('PATH', '')}"
        self.procs[f"{slug}/{run}"] = subprocess.Popen(["bash", "run.sh"], cwd=d, env=env, stdin=subprocess.DEVNULL,
                                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                       start_new_session=True)
        for _ in range(100):  # run.sh writes its pid first: then this run counts as this machine's
            if (d / "out" / ".remote" / "pid").exists():
                break
            time.sleep(0.05)
        self.heartbeat()  # it is listed before its job goes away, so kx never sees it nowhere
        self.rclone("purge", src, check=False)
        log(f"{slug} {run}: started")

    def ensure_kx(self, digest: str):
        dest = self.base / "kx"
        mark = dest / ".kx-hash"
        if mark.exists() and mark.read_text().strip() == digest:
            return
        log("updating the kx sources")
        archive = self.base / ".kx.tar.gz"
        self.rclone("copyto", join(self.root, "_kx", "kx.tar.gz"), str(archive))
        shutil.rmtree(dest / "src", ignore_errors=True)  # files kx no longer has go too
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as tf:
            try:
                tf.extractall(dest, filter="data")
            except TypeError:  # a Python without extraction filters (before 3.10.12 / 3.11.4)
                tf.extractall(dest)
        archive.unlink()
        mark.write_text(digest + "\n")

    def ensure_data(self, slug: str, data: dict):
        dest = self.base / slug / "data" / data["version"]
        if data.get("needs"):  # what the run asked for besides Kaggle's files: copied when missing or changed
            listing = self.base / slug / f".kx-needs-{os.getpid()}"
            listing.write_text("\n".join(data["needs"]) + "\n")
            dest.mkdir(parents=True, exist_ok=True)
            self.rclone("copy", join(self.root, slug, "data", data["version"]), str(dest), "--files-from", str(listing))
            listing.unlink()
        if (dest / ".kx-complete").exists():
            return
        log(f"{slug}: copying data {data['version']}; once per version")
        dest.mkdir(parents=True, exist_ok=True)
        src = join(self.root, slug, "data", data["version"])
        self.rclone("copyto", join(src, ".kx-manifest.json"), str(dest / ".kx-manifest.json"))
        names = [".kx-manifest.json", *json.loads((dest / ".kx-manifest.json").read_text())["files"]]
        listing = dest / ".kx-files"
        listing.write_text("\n".join(names) + "\n")  # Kaggle's files only: parts sent with --needs come when asked
        self.rclone("copy", src, str(dest), "--files-from", str(listing))
        listing.unlink()
        (dest / ".kx-complete").touch()

    def install_uv(self):
        log("installing uv")
        subprocess.run("curl -LsSf https://astral.sh/uv/install.sh | sh", shell=True, check=True, capture_output=True)

    def fail(self, d: Path, why: str):
        """Record a run that cannot go on as ended with exit 1, so kx records it (with why) instead of waiting."""
        log(why)
        m = d / "out" / ".remote"
        m.mkdir(parents=True, exist_ok=True)
        with (d / "out" / "stderr.log").open("a") as f:
            f.write(why + "\n")
        t = str(int(time.time()))
        for name, value in (("pid", "0"), ("started", t), ("ended", t), ("exit", "1")):
            if not (m / name).exists():
                (m / name).write_text(value + "\n")

    # -- runs going on -----------------------------------------------------------------------------------------------
    def tend(self):
        kills = {name.replace("~", "/") for name in self.lsf(join(self.box, "kill"), "--files-only")}
        for key, d in self.runs().items():
            out = d / "out"
            dst = join(self.root, key.split("/")[0], "runs", key.split("/")[1], "out")
            exited = (out / ".remote" / "exit").exists()
            if not exited and key in kills:
                self.stop(key, d)
            if not exited and not self.alive(key, d):
                time.sleep(1)  # run.sh may be writing its exit code
                if not (out / ".remote" / "exit").exists():
                    self.fail(d, "kx agent: the run's process is gone without an exit code (killed, or out of memory?)")
                exited = True
            try:
                if exited:  # all of it, the exit code last: kx records the run once it sees that
                    self.rclone("copy", str(out), dst, "--exclude", "/.remote/exit")
                    self.rclone("copyto", str(out / ".remote" / "exit"), join(dst, ".remote", "exit"))
                    (d / ".agent-uploaded").touch()
                    self.procs.pop(key, None)
                    if key in kills:  # done with: the next agent here must not act on it
                        self.rclone("deletefile", join(self.box, "kill", key.replace("/", "~")), check=False)
                    log(f"{key}: ended, copied up")
                else:
                    self.rclone("copy", str(out), dst, "--max-size", BIG, "--exclude", "/.remote/exit")
                    self.rclone("copy", str(out), dst, *[x for n in LIVE_FILES for x in ("--include", f"/{n}")])
            except RuntimeError as e:
                log(f"{key}: could not copy up ({e}); trying again")

    def stop(self, key: str, d: Path):
        sent = d / ".agent-kill-sent"
        try:
            pid = int((d / "out" / ".remote" / "pid").read_text())
        except (OSError, ValueError):
            return
        sig = signal.SIGTERM
        if sent.exists():
            if time.time() - sent.stat().st_mtime < KILL_GRACE_S:
                return
            sig = signal.SIGKILL
        log(f"{key}: stopping ({sig.name})")
        try:
            os.killpg(pid, sig)
        except OSError:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        sent.touch()

    def once(self):
        self.heartbeat()
        self.take_jobs()
        self.tend()

    def loop(self):
        log(f"{self.name}: taking runs from {self.box} every {self.every:g} s (Ctrl-C stops the agent, not its runs)")
        while True:
            try:
                self.once()
            except Exception as e:  # noqa: BLE001 - a hiccup of the storage must not end the agent
                log(f"error: {e}")
            time.sleep(self.every)


def main(argv=None):
    p = argparse.ArgumentParser(prog="agent.py", description=__doc__.split("\n\n")[0])
    p.add_argument("name", help="this host's name in kx (hosts.yaml)")
    p.add_argument("--mailbox", required=True, help="the rclone location of the mailbox, e.g. gdrive:kx (or a local path)")
    p.add_argument("--dir", default=str(Path.home() / "kx-remote"), help="where runs, data and environments go here")
    p.add_argument("--every", type=float, default=10, help="seconds between looks at the mailbox")
    p.add_argument("--max-runs", type=int, default=1, help="runs at once (more jobs wait their turn)")
    p.add_argument("--rclone-config", help="an rclone config file (default: rclone's own)")
    p.add_argument("--once", action="store_true", help="one round, then exit")
    a = p.parse_args(argv)
    if shutil.which("rclone") is None:
        sys.exit("kx agent: rclone is not installed here (https://rclone.org/install/), or not on PATH")
    base = Path(a.dir).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    agent = Agent(a.name, a.mailbox, base, a.every, a.max_runs, a.rclone_config)
    try:
        agent.once() if a.once else agent.loop()
    except KeyboardInterrupt:
        log("stopped; runs started go on, and the next agent here picks them up")


if __name__ == "__main__":
    main()
