"""What training code logs to wandb or MLflow, recorded in its kx run as well, without changing the code.

Every library speaks to kx through the same four things, the vocabulary of a run kx keeps:

    config(values)        settings (merged into the run's config; a seed in it becomes the run's seed)
    step(step, values)    a curve point (metrics.jsonl)
    summary(values)       final numbers (the run's metrics)
    note(text)            a note on the run

An adapter (kx.capture.wandb, kx.capture.mlflow) turns its library's calls into these; another library is another
adapter. Under `kx exec` a sitecustomize puts an import hook in every Python process of the run (see site/), and
`kx.run` installs it in its own process: when the code imports wandb or mlflow, the adapter wraps it. The library
keeps working as it was: wandb is switched off unless KX_WANDB=also (nothing leaves the machine); MLflow logs where
MLFLOW_TRACKING_URI points, else into the run's dir. KX_CAPTURE=off turns all of it off.

Only numbers are kept of what is logged (nested dicts flatten to a/b); media, tables and artifacts stay in the
library. A capture never breaks the code it watches: an error in it is reported once and capture stops.
"""
from __future__ import annotations

import atexit
import importlib
import importlib.util
import json
import math
import os
import sys
import threading
from pathlib import Path

ADAPTERS = {"wandb": "kx.capture.wandb", "mlflow": "kx.capture.mlflow"}  # module the code imports -> adapter
RESERVED = {"step", "ts"}  # metrics.jsonl's own keys: a metric of that name keeps it with a trailing _

_lock = threading.RLock()
_recorder = None
_active = None  # the run of kx.run in this process (outside kx exec), set by kx.runs.tracking
_failed = False


def enabled() -> bool:
    return os.environ.get("KX_CAPTURE", "").lower() not in ("off", "0", "false", "no")


def set_active(run):
    """kx.run's run in this process (None when it ends): what is captured goes there."""
    global _active, _recorder
    with _lock:
        if run is None and _recorder is not None:
            _recorder.finish()
        _active, _recorder = run, None


def recorder() -> Recorder | None:
    """The recorder of this process's kx run, or None outside one."""
    global _recorder
    if not enabled() or _failed:
        return None
    with _lock:
        if _recorder is None:
            run = _active
            if run is None and os.environ.get("KX_RUN_ID"):
                from kx.runs.tracking import current_run
                run = current_run()
            if run is None:
                return None
            _recorder = Recorder(run)
            atexit.register(_recorder.finish)
        return _recorder


def guarded(fn):
    """Run a capture step; an error in it is reported once and capture stops, the user's code goes on."""
    def wrapper(*args, **kwargs):
        global _failed
        if _failed:
            return None
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - capture must never break the code it watches
            _failed = True
            print(f"kx: capture stopped ({type(e).__name__}: {e}); the run goes on without it", file=sys.stderr)
            return None
    return wrapper


def numbers(values, prefix="") -> dict:
    """The finite numbers in a logged dict, nested dicts flattened to a/b; numpy and torch scalars included."""
    out = {}
    for k, v in (values or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(numbers(v, key + "/"))
            continue
        if not isinstance(v, (int, float)) and callable(getattr(v, "item", None)):
            try:
                v = v.item()  # numpy / torch scalars (a bigger array raises: not a number)
            except Exception:  # noqa: BLE001
                continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            continue
        out[key + "_" if key in RESERVED else key] = float(v)
    return out


def plain(value):
    """A config value as JSON can hold it (MLflow params arrive as strings: "4" is 4 again)."""
    if isinstance(value, str):
        try:
            v = json.loads(value)
            return v if isinstance(v, (int, float, bool)) or v is None else value
        except ValueError:
            return value
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if callable(getattr(value, "item", None)):
        try:
            return value.item()
        except Exception:  # noqa: BLE001
            pass
    return str(value)


class Recorder:
    """The kx run this process works for, as the adapters see it. A second run of a library in the same process
    (wandb.init again, an MLflow child run) gets its own stream, its names prefixed with that run's name."""

    def __init__(self, run):
        self.run = run
        self.streams: dict = {}
        self.firsts: set = set()
        self.done = False

    def stream(self, library: str, key, name: str | None = None) -> Stream:
        with _lock:
            s = self.streams.get((library, key))
            if s is None:
                prefix = "" if library not in self.firsts else f"{name or key}/"
                self.firsts.add(library)
                s = self.streams[(library, key)] = Stream(self, prefix)
            return s

    @guarded
    def config(self, values: dict):
        if values:
            from kx.runs.tracking import merge_config
            merge_config(self.run, plain(values))

    @guarded
    def step(self, step, values: dict):
        if values:
            self.run.step(step, **values)

    @guarded
    def summary(self, values: dict):
        if values:
            self.run.log(**values)

    @guarded
    def note(self, text: str):
        if text:
            self.run.note(str(text))

    def finish(self):
        with _lock:
            if self.done:
                return
            self.done = True
            for s in list(self.streams.values()):
                s.finish()


class Stream:
    """One library run's points into the kx run: steps where the library gave none (MLflow logs those at 0, wandb
    counts its log calls), and the last value of each metric as the summary when it finishes. A metric logged once
    goes only to the summary: one point is not a curve."""

    def __init__(self, rec: Recorder, prefix: str):
        self.rec, self.prefix = rec, prefix
        self.counter = 0          # wandb: the step of the next log call without one
        self.seen: dict = {}      # metric -> (points so far, last step)
        self.held: dict = {}      # metric -> its first point, until a second one makes it a curve
        self.last: dict = {}      # metric -> last value
        self.extra: dict = {}     # summary values set explicitly
        self.cfg: dict = {}
        self.finished = False

    def config(self, values: dict):
        values = {f"{self.prefix}{k}" if self.prefix else k: v for k, v in (values or {}).items()}
        new = {k: v for k, v in values.items() if self.cfg.get(k, object()) != v}
        if new:
            self.cfg.update(new)
            self.rec.config(new)

    def points(self, values: dict, step=None):
        """values at `step` (None: the library gave none); each metric's own count stands in for it."""
        rows: dict = {}
        with _lock:
            for k, v in numbers(values).items():
                k = self.prefix + k
                n, prev = self.seen.get(k, (0, None))
                s = n if step is None or (step == 0 and prev is not None) else step  # 0 again: MLflow's "no step"
                self.seen[k] = (n + 1, s)
                self.last[k] = v
                if n == 0:
                    self.held[k] = (s, v)
                    continue
                if k in self.held:
                    s0, v0 = self.held.pop(k)
                    rows.setdefault(s0, {})[k] = v0
                rows.setdefault(s, {})[k] = v
        for s in sorted(rows):
            self.rec.step(s, rows[s])

    def summary(self, values: dict):
        self.extra.update({self.prefix + k: v for k, v in numbers(values).items()})

    def finish(self):
        with _lock:
            if self.finished:
                return
            self.finished = True
            final = {**self.last, **self.extra}
        self.rec.summary(final)


class Finder:
    """An import hook: when the code imports a library kx has an adapter for, the adapter wraps it."""

    def find_spec(self, name, path=None, target=None):
        if name not in ADAPTERS or name in _patching:
            return None
        _patching.add(name)
        try:
            spec = importlib.util.find_spec(name)
        finally:
            _patching.discard(name)
        if spec is None or spec.loader is None:
            return None
        loader = spec.loader
        exec_module = loader.exec_module

        def exec_and_patch(module):
            exec_module(module)
            patch(name, module)
        loader.exec_module = exec_and_patch
        return spec


_patching: set = set()
_installed = False


def patch(name: str, module):
    if getattr(module, "_kx_captured", False):
        return
    try:
        importlib.import_module(ADAPTERS[name]).patch(module)
        module._kx_captured = True
    except Exception as e:  # noqa: BLE001 - a library version kx does not know: no capture, the code still runs
        print(f"kx: cannot capture {name} ({type(e).__name__}: {e}); log with kx.run's r.step / r.log", file=sys.stderr)


def install():
    """Wrap wandb / mlflow now if they are imported, and when they are imported later."""
    global _installed
    if not enabled():
        return
    with _lock:
        if not _installed:
            sys.meta_path.insert(0, Finder())
            _installed = True
    for name in ADAPTERS:
        if name in sys.modules:
            patch(name, sys.modules[name])


SITE = Path(__file__).parent / "site"  # the sitecustomize kx exec puts on PYTHONPATH
