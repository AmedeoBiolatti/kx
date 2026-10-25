"""`kx exec`: track any command (any language) as a run.

The command sees KX_RUN_ID / KX_RUN_DIR. It can write scores to $KX_RUN_DIR/scores.json and
per-step metrics to $KX_RUN_DIR/metrics.jsonl; Python code can use kx.current_run() (or kx.run,
which attaches to this run). stdout/stderr are kept in the run dir and echoed.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from kx.ledger.config import KxError, main_repo_root
from kx.runs import tracking


def _tee(src, *dests):
    # chunks, not lines: progress bars redraw with \r and only end their line when they finish
    read = getattr(src, "read1", None) or src.readline
    for chunk in iter(lambda: read(65536), b""):
        for d in dests:
            try:
                d.write(chunk)
                d.flush()
            except (OSError, ValueError):
                pass
    src.close()


_SCALE = {"": 1, "k": 1e3, "M": 1e6, "G": 1e9}
TQDM = re.compile(r"(?:(?P<desc>[^\r\n|%]{1,80}?):\s*)?\d{1,3}%\|[^|\r\n]*\|\s*(?P<n>\d+(?:\.\d+)?)(?P<ns>[kMG]?)"
                  r"/(?P<t>\d+(?:\.\d+)?)(?P<ts>[kMG]?)\s*\[[^\]\r\n]*?(?:(?P<u1>[A-Za-z]+)/s|s/(?P<u2>[A-Za-z]+))?\]")


class ProgressSniffer:
    """A tee sink that turns tqdm-style bars in a run's output ("Epoch:  45%|████  | 450/1000 [00:10<00:12, 44it/s]")
    into the run's progress (progress.json), so any script with a progress bar shows one in kx without code changes.
    It steps aside as soon as the script reports progress itself (kx.progress / `kx progress`)."""

    def __init__(self, run_dir: Path, every: float = 1.0):
        self.path, self.every = Path(run_dir) / "progress.json", every
        self.tail, self.last, self.mine, self.pending = "", 0.0, None, None
        self.off = self.path.exists()
        self.lock = threading.Lock()

    def write(self, chunk: bytes):
        with self.lock:
            if self.off:
                return
            self.tail = (self.tail + chunk.decode(errors="replace"))[-4000:]
            found = list(TQDM.finditer(self.tail))
            if found:
                self.pending = found[-1]
                if time.monotonic() - self.last >= self.every:
                    self._report()

    def flush(self):
        pass

    def close(self):
        with self.lock:
            if not self.off and self.pending is not None:
                self._report()

    def _report(self):
        m, self.pending = self.pending, None
        try:
            if self.mine is not None and self.path.stat().st_mtime_ns != self.mine:
                self.off = True  # the script reports its own progress
                return
        except OSError:
            pass
        n, total = float(m["n"]) * _SCALE[m["ns"]], float(m["t"]) * _SCALE[m["ts"]]
        desc = (m["desc"] or "").strip()
        try:
            from kx.runs.progress import write_progress
            write_progress(self.path.parent, min(n, total), total if total > 0 else None,
                           phase=desc[:160] or None, unit=(m["u1"] or m["u2"] or "it")[:160])
            self.mine = self.path.stat().st_mtime_ns
        except (KxError, OSError, ValueError):
            pass
        self.last = time.monotonic()


def project_python_env(env: dict, cwd: Path | None) -> dict:
    """Make `python` mean the project's interpreter: a .venv in the working directory or in the main checkout
    (task worktrees have none) goes first on PATH; without one, `python` falls back to python3 (Debian/Ubuntu have
    no `python`), so commands written as `python script.py` run the same for agents and for approvals."""
    import shutil
    here = Path(cwd or ".").resolve()
    for root in (here, main_repo_root(here)):
        bindir = root / ".venv" / "bin"
        if (bindir / "python").exists():
            return {**env, "PATH": f"{bindir}{os.pathsep}{env.get('PATH', '')}", "VIRTUAL_ENV": str(root / ".venv")}
    if shutil.which("python", path=env.get("PATH")) is None and (py3 := shutil.which("python3", path=env.get("PATH"))):
        shim = Path(env.get("KX_RUN_DIR") or here) / ".kx-bin"
        shim.mkdir(parents=True, exist_ok=True)
        link = shim / "python"
        if not link.exists():
            link.symlink_to(py3)
        return {**env, "PATH": f"{shim}{os.pathsep}{env.get('PATH', '')}"}
    return env


def with_capture(env: dict) -> dict:
    """The run's Python processes record what they log to wandb or MLflow (kx.capture), through a sitecustomize."""
    from kx.capture import SITE
    return {**env, "PYTHONPATH": os.pathsep.join([str(SITE), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])])}


SILENT_RUN_S = 120  # a run longer than this without progress gets a hint


def _signal_group(p, sig) -> None:
    try:
        os.killpg(p.pid, sig)
    except ProcessLookupError:
        pass


def run_command(cmd: list[str], *, hypothesis: str | None = None, kind: str = "experiment", config=None,
                tags=(), group=None, seed=None, parent=None, echo: bool = True, timeout=None, gpus: int = 0) -> tuple[int, int]:
    """Run cmd as a tracked run; returns (run id, exit code)."""
    if not cmd:
        raise KxError("nothing to run", fix="kx exec [options] -- <command> ...")
    from kx.runs.progress import number
    if timeout is not None and (not number(timeout) or timeout <= 0):
        raise KxError("timeout must be finite and positive")
    act = tracking.begin(hypothesis or " ".join(cmd), config, parent=parent, tags=tags, group=group, seed=seed,
                         kind=kind, command=cmd, gpus=gpus)
    r = act.run
    env = project_python_env({**os.environ, "KX_RUN_ID": str(r.id), "KX_RUN_DIR": str(r.dir)}, None)
    env = with_capture(env)
    code, error = None, None
    began = time.monotonic()
    try:
        with open(r.dir / "stdout.log", "ab") as out, open(r.dir / "stderr.log", "ab") as err:
            p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 start_new_session=timeout is not None)
            sniffer = ProgressSniffer(r.dir)
            threads = [threading.Thread(target=_tee, args=(p.stdout, out, sniffer, *([sys.stdout.buffer] if echo else [])),
                                        daemon=True),
                       threading.Thread(target=_tee, args=(p.stderr, err, sniffer, *([sys.stderr.buffer] if echo else [])),
                                        daemon=True)]
            for t in threads:
                t.start()
            try:
                code = p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _signal_group(p, signal.SIGTERM)
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _signal_group(p, signal.SIGKILL)
                    p.wait()
                _signal_group(p, signal.SIGKILL)  # a descendant can ignore TERM after its parent exits
                code, error = 124, f"time limit exceeded ({timeout:g} seconds)"
            except KeyboardInterrupt:
                _signal_group(p, signal.SIGTERM) if timeout is not None else p.terminate()
                code = p.wait()
                error = "interrupted (Ctrl-C)"
            for t in threads:
                t.join()
            sniffer.close()
    except OSError as e:
        code, error = 127, f"cannot start {cmd[0]}: {e}"
        print(f"kx: {error}", file=sys.stderr)
        (r.dir / "stderr.log").open("ab").write((error + "\n").encode())
    if error is None and code != 0:
        tail = (r.dir / "stderr.log").read_bytes()[-3000:].decode(errors="replace")
        error = f"exit code {code}\n{tail}"
    took = time.monotonic() - began
    if took > SILENT_RUN_S and not (r.dir / "progress.json").exists():
        print(f"kx: {r.name} ran {took / 60:.0f} min without reporting progress: wrap the main loop in tqdm (kx exec "
              f"reads it) or call kx.current_run().progress(i, total=n) so kx status can show an ETA", file=sys.stderr)
    try:
        tracking.end(act, error)
    except KxError as e:  # e.g. malformed scores.json: the run is still closed
        print(f"kx: {e}", file=sys.stderr)
        code = code or 1
    return r.id, code

