"""Python API used by competition code to record runs and scores."""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import sys
import tarfile
import threading
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from kx.board import tasks as _tasks
from kx.guard import actors, budgets
from kx.kaggle import data as _data
from kx.ledger import config as _config
from kx.ledger.config import KxError
from kx.ledger.store import HEARTBEAT_S, Store, dumps, loads, now, proc_start
from kx.runs import environment as _env, git
from kx.runs.compare import se_from_breakdown
from kx.runs import objects
from kx.runs.objects import atomic_json
from kx.runs.protocol import Protocol, register, split, split_text

RUN_KINDS = ("experiment", "probe")  # a probe may run on uncommitted code (its patch is saved)


class Run:
    def __init__(self, store: Store, run_id: str):
        self.store = store
        self.id = self.name = run_id
        self.dir = store.run_dir(run_id)
        self.dir.mkdir(parents=True, exist_ok=True)

    def __repr__(self):
        return f"Run({self.name})"

    def score(self, value: float, protocol: Protocol, *, se: float | None = None, breakdown: dict | None = None,
              n: int | None = None) -> str:
        """Record this run's score under `protocol`. A breakdown (per fold / opponent / seed) enables paired
        comparisons and gives `se` when it is not passed."""
        return _record_score(self.store, self.id, value, protocol, se=se, breakdown=breakdown, n=n)

    def log(self, data=None, *, step=None, **metrics):
        """Summary metrics (runtime, memory, ...), merged into the run's; with `step`, also a curve point."""
        if data is not None:
            if not isinstance(data, dict):
                raise KxError("log data must be a mapping")
            metrics = {**data, **metrics}
        if step is not None:
            self.step(step, **metrics)
        with self.store.transaction():
            row = self.store.get_run(self.id)
            merged = {**(loads(row["metrics"]) or {}), **metrics}
            self.store.update("runs", {"metrics": dumps(merged)}, id=self.id)
        if step is None:
            from kx.runs.progress import append_summary
            append_summary(self.dir, metrics)

    def step(self, step: int, **metrics):
        """Per-step metrics (training curves), appended to metrics.jsonl."""
        from kx.runs.progress import append_step
        append_step(self.dir, step, metrics)

    def progress(self, completed, total=None, *, unit=None, phase=None, **metrics):
        """Work done so far (and the total when known): shown with an ETA by kx status and kx show."""
        from kx.runs.progress import write_progress
        return write_progress(self.dir, completed, total, unit=unit, phase=phase, values=metrics)

    def note(self, text: str):
        self.store.add_note(self.id, text)

    def log_table(self, name: str, rows, columns=None) -> dict:
        """A table (rows of dicts, rows with `columns`, or a DataFrame) shown with the run (kx show, the dashboard)."""
        return objects.log_table(self, name, rows, columns)

    def artifact(self, name: str, src: str | Path | None = None) -> Path:
        """Path for an artifact in the run dir; copies `src` there when given."""
        dst = self.dir / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src is not None:
            src = Path(src)
            shutil.copytree(src, dst, dirs_exist_ok=True) if src.is_dir() else shutil.copy2(src, dst)
        return dst

def _check_score(value, protocol) -> float:
    if not isinstance(protocol, Protocol):
        raise TypeError("protocol must be a kx.Protocol (scores without a protocol cannot be compared)")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise KxError(f"score value must be a number, got {value!r}") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise KxError("score value must be finite")
    return value


def _record_score(store: Store, run_id: str, value, protocol, *, se=None, breakdown=None, n=None) -> str:
    value = _check_score(value, protocol)
    pid = register(store, protocol)
    nums = [v for v in (breakdown or {}).values() if isinstance(v, (int, float))]
    if len(nums) >= 2 and not (min(nums) - 1e-9 <= value <= max(nums) + 1e-9):
        print(f"kx: warning: {run_id} value {value:.6g} is outside its breakdown range "
              f"[{min(nums):.6g}, {max(nums):.6g}]; comparisons test the breakdown, not the value", file=sys.stderr)
    if se is None:
        se, n_auto = se_from_breakdown(breakdown, protocol.noise)
        n = n or n_auto
    elif n is None and breakdown:
        n = len(breakdown)
    first = store.one("SELECT 1 FROM scores WHERE run_id=? AND protocol_id=?", (run_id, pid)) is None
    store.insert("scores", {"run_id": run_id, "protocol_id": pid, "value": value, "se": se, "n": n,
                            "breakdown": dumps(breakdown), "created": now()}, on="replace")
    if first and (sp := split(store, pid)):  # kept on the run: a warning in its output is easily missed
        store.add_note(run_id, "kx: " + split_text(sp) + ". Run settings belong in config=, not in the protocol")
    return pid


class _Heartbeat(threading.Thread):
    """Touches runs.heartbeat (in the cache only, not the log) so `kx reap` can tell a live run from a killed one."""

    def __init__(self, db_path: Path, run_id: str, interval: float = HEARTBEAT_S):
        super().__init__(daemon=True, name=f"kx-heartbeat-{run_id}")
        self.db_path, self.run_id, self.interval = db_path, run_id, interval
        self.stopped = threading.Event()

    def run(self):
        db = sqlite3.connect(self.db_path, timeout=60, isolation_level=None)
        try:
            while not self.stopped.wait(self.interval):
                try:
                    db.execute("UPDATE runs SET heartbeat=? WHERE id=? AND status='running'", (now(), self.run_id))
                except sqlite3.Error:
                    pass
        finally:
            db.close()

    def stop(self):
        self.stopped.set()
        self.join(timeout=5)


def _save_patch(root: Path, dest: Path) -> str | None:
    """Save uncommitted tracked changes and untracked files; return their combined sha256 prefix."""
    import hashlib
    import subprocess

    dest.mkdir(parents=True, exist_ok=True)
    diff = subprocess.run(["git", "diff", "HEAD", "--binary"], cwd=root, capture_output=True).stdout
    (dest / "uncommitted.patch").write_bytes(diff)
    h = hashlib.sha256(diff)
    files = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=root,
                           capture_output=True).stdout.split(b"\0")
    files = sorted(f.decode() for f in files if f)
    if files:
        with tarfile.open(dest / "untracked.tar.gz", "w:gz") as tf:
            for f in files:
                p = root / f
                if p.is_file():
                    tf.add(p, arcname=f)
                    h.update(f.encode() + b"\0" + p.read_bytes())
    return h.hexdigest()[:16] if diff or files else None


@dataclass
class _Active:
    store: Store
    run: Run
    hb: _Heartbeat
    started: float
    gpus: int


def _parent(store: Store, parent):
    if parent is None:
        return None
    try:
        pid = store.resolve(parent)
        store.get_run(pid)
        return pid
    except KxError as e:
        if str(parent).lower() == "best":
            print(f"kx: note: no parent run ({e})", file=sys.stderr)
            return None
        raise


def begin(hypothesis: str, config: dict | None = None, *, parent=None, tags=(), group=None, seed=None,
          kind: str = "experiment", command: list[str] | None = None, gpus: int = 0, host: str | None = None,
          task=None) -> _Active:
    """Create a running run (shared by kx.run and kx exec; host: the remote host it runs on, None for here; task: the
    task it is for, when the caller names it)."""
    if kind not in RUN_KINDS:
        raise KxError(f"run kind must be one of {RUN_KINDS}")
    comp = _config.load()
    from kx.guard import rules
    rules.check_host(comp, host)
    rules.check_room(comp, what="a new run")
    store = Store(comp, auto_backup=True)
    try:
        _data.ensure_link(comp)
        root = Path.cwd()
        g = git.info(root)
        if g is None:
            raise KxError(f"{root} is not in a git repository: results would not be reproducible", fix="git init && git commit")
        if g["sha"] is None:
            raise KxError("the repository has no commits yet", fix="commit first")
        if g["dirty"] and kind == "experiment":
            raise KxError("refusing to run an experiment on uncommitted changes:\n  " + "\n  ".join(g["changes"][:20]),
                          fix="commit first (git add -A && git commit), gitignore outputs, or run it as a probe "
                              "(kind='probe' / kx exec --probe saves the patch)")
        who = actors.current()
        if gpus:
            budgets.check(store, who, {"gpu_hours": 0.01 * gpus})
        if seed is None and isinstance(config, dict) and isinstance(config.get("seed"), int):
            seed = config["seed"]
        env, _ = _env.capture(comp.main_root)
        task_id = _tasks.get(store, task)["id"] if task else _tasks.current(store, root, who.name, hypothesis)
        with store.transaction():
            parent_id = _parent(store, parent)
            key = store.new("runs", {
                "created": now(), "status": "running", "kind": kind, "hypothesis": hypothesis, "config": dumps(config),
                "command": json.dumps(command or sys.argv), "author": who.name, "task": task_id, "parent": parent_id,
                "grp": group, "seed": seed, "git_sha": g["sha"], "git_branch": g["branch"], "git_dirty": int(g["dirty"]),
                "worktree": g["worktree"], "host": socket.gethostname(), "pid": os.getpid(),
                "pid_start": proc_start(os.getpid()), "heartbeat": now(), "env": dumps(env), "gpus": gpus or None})
            r = Run(store, key)
            store.add_tags(r.id, tags)
        if g["dirty"]:
            store.update("runs", {"patch": _save_patch(Path(g["worktree"]), r.dir / "code")}, id=r.id)
    except BaseException:
        store.close()
        raise
    hb = _Heartbeat(comp.db_path, r.id)
    hb.start()
    print(f"kx: started {r.name} ({kind}) -> {r.dir}", file=sys.stderr)
    if task_id is None and len(active := _tasks.active_for(store, who.name)) > 1:
        print(f"kx: {r.name} is linked to no task: {', '.join(active)} are all active for you. "
              "`kx task start T<n>` makes one current (or kx exec --task T<n>, or start the hypothesis with T<n>)",
              file=sys.stderr)
    return _Active(store, r, hb, time.monotonic(), gpus)


def end(act: _Active, error: BaseException | str | None = None):
    store, r = act.store, act.run
    if act.hb is not None:
        act.hb.stop()
    ingest_error = None
    try:
        try:
            ingest(store, r)
        except KxError as e:  # never leave the run "running" because of a bad scores.json
            ingest_error = e
            error = error or f"scores not recorded: {e}"
        if error is not None:
            text = error if isinstance(error, str) else "".join(traceback.format_exception(error))
            store.update("runs", {"status": "failed", "finished": now(), "error": text[-4000:]}, id=r.id)
            print(f"kx: {r.name} failed", file=sys.stderr)
        else:
            store.update("runs", {"status": "done", "finished": now()}, id=r.id)
            n = store.one("SELECT COUNT(*) FROM scores WHERE run_id=?", (r.id,))[0]
            print(f"kx: {r.name} done ({n} score(s))", file=sys.stderr)
        if act.gpus:
            hours = act.gpus * (time.monotonic() - act.started) / 3600
            budgets.charge(store, actors.current(), "gpu_hours", round(hours, 4), r.name)
    finally:
        store.close()
    if ingest_error is not None:
        raise ingest_error


def _ingest_extras(store: Store, r: Run):
    """notes.jsonl, logged_metrics.json and attach.json written by non-Python commands (and by kx.run on a remote
    host). Each is deleted once recorded: it is in the ledger then, not one of the run's files."""
    d = r.dir
    if (p := d / "attach.json").exists():
        try:
            a = json.loads(p.read_text())
            _attach(store, r.id, a.get("hypothesis"), a.get("config"), a.get("seed"), a.get("group"))
            store.add_tags(r.id, [t for t in a.get("tags") or [] if isinstance(t, str)])
        except (json.JSONDecodeError, AttributeError):
            pass
        p.unlink()
    if (p := d / "notes.jsonl").exists():
        for line in p.read_text().splitlines():
            try:
                store.add_note(r.id, json.loads(line)["text"])
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
        p.unlink()
    if (p := d / "logged_metrics.json").exists():
        try:
            Run.log(r, **json.loads(p.read_text()))
        except (json.JSONDecodeError, TypeError):
            pass
        p.unlink()


def ingest(store: Store, r: Run) -> int:
    """Record what a non-Python command wrote to the run dir (scores.json: a list or one entry), once."""
    _ingest_extras(store, r)
    path = r.dir / "scores.json"
    if not path.exists():
        return 0
    try:
        items = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise KxError(f"{path}: invalid JSON ({e})") from None
    items = items if isinstance(items, list) else [items]
    for it in items:
        try:
            proto = Protocol(it["protocol"], direction=it.get("direction"), noise=it.get("noise", "independent"),
                             **(it.get("spec") or {}))
            _record_score(store, r.id, float(it["value"]), proto, se=it.get("se"), breakdown=it.get("breakdown"),
                          n=it.get("n"))
        except (KeyError, TypeError, ValueError) as e:
            raise KxError(f"{path}: bad entry {it!r} ({e})",
                          fix='entries look like {"protocol": "cv5", "spec": {...}, "value": 0.81, "breakdown": {...}}') from None
    path.unlink()
    return len(items)


class FileRun(Run):
    """A run on a remote host (kx exec --on, KX_REMOTE=1): there is no ledger there, so everything is written to files
    in the run dir, which kx copies back and records (the same files a non-Python command may write)."""

    def __init__(self, run_dir: Path, run_id: str):
        self.store, self.id, self.name, self.dir = None, run_id, run_id, Path(run_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def score(self, value, protocol, *, se=None, breakdown=None, n=None) -> str:
        value = _check_score(value, protocol)
        path = self.dir / "scores.json"
        items = json.loads(path.read_text()) if path.exists() else []
        items = [i for i in (items if isinstance(items, list) else [items]) if i.get("protocol") != protocol.name
                 or i.get("spec") != protocol.spec]  # scoring again under the same protocol replaces, as in the ledger
        items.append({"protocol": protocol.name, "spec": protocol.spec, "direction": protocol.direction,
                      "noise": protocol.noise, "value": value, "se": se, "breakdown": breakdown, "n": n})
        path.write_text(json.dumps(items, default=str))
        return protocol.id

    def log(self, data=None, *, step=None, **metrics):
        if data is not None:
            if not isinstance(data, dict):
                raise KxError("log data must be a mapping")
            metrics = {**data, **metrics}
        if step is not None:
            self.step(step, **metrics)
        path = self.dir / "logged_metrics.json"
        atomic_json(path, {**(json.loads(path.read_text()) if path.exists() else {}), **metrics})

    def note(self, text: str):
        objects.append_jsonl(self.dir / "notes.jsonl", {"text": str(text)})


def remote() -> bool:
    """True inside a run launched on another machine by kx exec --on (no ledger there)."""
    return os.environ.get("KX_REMOTE") == "1"


def _no_ledger(what: str):
    raise KxError(f"{what} needs the ledger, which is not on this remote host",
                  fix="do it on the main machine (kx exec without --on)")


def _attach(store: Store, run_id: str, hypothesis, config, seed, group):
    """What kx.run says about the run of the kx exec it runs in, where kx exec left it empty; a config adds to the
    one there (keys of both: this one's)."""
    if seed is None and isinstance(config, dict) and isinstance(config.get("seed"), int):
        seed = config["seed"]
    row = store.get_run(run_id)
    old = loads(row["config"])
    if isinstance(old, dict) and isinstance(config, dict):
        config = {**old, **config}
    values = {k: v for k, v in (("hypothesis", hypothesis), ("seed", seed), ("grp", group)) if v is not None and row[k] is None}
    if config is not None and config != old:
        values["config"] = dumps(config)
    if values:
        store.update("runs", values, id=run_id)


def merge_config(r: Run, config: dict):
    """Add to a run's config (what kx.capture saw wandb or MLflow given); a seed in it becomes the run's seed."""
    if isinstance(r, FileRun):
        path = r.dir / "attach.json"
        a = json.loads(path.read_text()) if path.exists() else {}
        a["config"] = {**(a.get("config") or {}), **config}
        atomic_json(path, a)
        return
    with r.store.transaction():
        _attach(r.store, r.id, None, config, None, None)


def current_run() -> Run | None:
    """The run of the enclosing `kx exec` (KX_RUN_ID), if any."""
    run_id = os.environ.get("KX_RUN_ID")
    if not run_id:
        return None
    if remote():
        return FileRun(Path(os.environ["KX_RUN_DIR"]), run_id)
    return Run(Store(_config.load()), run_id)


@contextmanager
def run(hypothesis: str, config: dict | None = None, *, parent=None, tags=(), group: str | None = None,
        seed: int | None = None, kind: str = "experiment"):
    """Track one run.

    >>> with kx.run("add neutral-loss features", config=cfg, seed=0) as r:
    ...     r.score(0.81, PROTOCOL, breakdown=fold_scores)

    `kind`: experiment (default; committed code only) or probe (uncommitted code allowed, the patch is saved).
    Inside `kx exec` this attaches to that command's run instead of creating another.
    """
    outer = current_run()
    if isinstance(outer, FileRun):
        from kx import capture
        path = outer.dir / "attach.json"
        had = (json.loads(path.read_text()) if path.exists() else {}).get("config")  # what capture saw already
        if isinstance(had, dict) and isinstance(config, dict):
            config = {**had, **config}
        atomic_json(path, {"hypothesis": hypothesis, "config": config if config is not None else had, "seed": seed,
                           "group": group, "tags": list(tags)})
        capture.install()
        yield outer
        return
    if remote():
        _no_ledger("kx.run outside kx exec --on")
    if outer is not None:
        from kx import capture
        capture.install()
        st = outer.store
        _attach(st, outer.id, hypothesis, config, seed, group)
        st.add_tags(outer.id, tags)
        try:
            yield outer
        finally:
            st.close()
        return
    from kx import capture, sandbox
    sandbox.no_local_runs("kx.run")
    act = begin(hypothesis, config, parent=parent, tags=tags, group=group, seed=seed, kind=kind)
    capture.set_active(act.run)  # what the code logs to wandb or MLflow meanwhile goes to this run too
    capture.install()
    try:
        yield act.run
    except BaseException as e:
        capture.set_active(None)
        end(act, e)
        raise
    else:
        capture.set_active(None)
        end(act)


def score(run_ref, value: float, protocol: Protocol, **kw) -> str:
    """Score an existing run, e.g. re-evaluating a baseline under a new protocol."""
    if remote():
        _no_ledger("scoring another run")
    with Store(_config.load()) as store:
        run_id = store.resolve(run_ref)
        store.get_run(run_id)
        return _record_score(store, run_id, value, protocol, **kw)


def run_dir(run_ref) -> Path:
    """Artifact directory of an existing run (e.g. to load another run's predictions)."""
    if remote():
        _no_ledger("another run's directory")
    with Store(_config.load()) as store:
        return store.run_dir(store.resolve(run_ref))


def data_dir() -> Path:
    """Local competition data (outside git); also links it as `data/` here."""
    if remote():
        return Path(os.environ["KX_DATA_DIR"])
    comp = _config.load()
    _data.ensure_link(comp)
    d = _data.data_dir(comp)
    if not d.exists():
        raise KxError("no data downloaded yet", fix="kx data")
    return d


def data_version() -> str:
    """Identifier of the downloaded data, for Protocol specs: kx.Protocol("cv5", data=kx.data_version(), ...)."""
    if remote():
        return os.environ["KX_DATA_VERSION"]
    return _data.version(_config.load())
