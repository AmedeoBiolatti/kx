"""MLflow into the kx run: params as config, metrics as curve points, the last values as the summary.

The client is wrapped, not the fluent functions: mlflow.log_metric, log_metrics, log_params and the autologgers all
go through MlflowClient. MLflow itself goes on logging where MLFLOW_TRACKING_URI points; without one, into the run's
dir (sqlite when MLflow has SQLAlchemy, else its file store), not a mlflow.db or mlruns/ left in the repo, which would
make the next experiment refuse to start on uncommitted changes.
"""
from __future__ import annotations

import inspect
import os
import sys

from kx import capture


def patch(mlflow):
    rec = capture.recorder()
    if rec is not None and not os.environ.get("MLFLOW_TRACKING_URI"):
        _store_in(rec.run.dir)
    from mlflow.tracking import MlflowClient
    _wrap(MlflowClient, "log_metric", _metric)
    _wrap(MlflowClient, "log_batch", _batch)
    _wrap(MlflowClient, "log_param", _param)
    _wrap(MlflowClient, "set_terminated", _terminated)


def _store_in(run_dir):
    try:
        import sqlalchemy  # noqa: F401 - MLflow's database store needs it
        os.environ["MLFLOW_TRACKING_URI"] = f"sqlite:///{run_dir}/mlflow.db"
    except ImportError:
        os.environ["MLFLOW_TRACKING_URI"] = f"file:{run_dir}/mlruns"
        os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")


_told: set = set()


def _stream(run_id):
    rec = capture.recorder()
    if rec is None:
        return None
    if not _told:
        _told.add(run_id)
        print(f"kx: MLflow run recorded in {rec.run.name} (MLflow keeps its own copy: {os.environ.get('MLFLOW_TRACKING_URI')})",
              file=sys.stderr)
    return rec.stream("mlflow", run_id, str(run_id)[:8])


def _wrap(cls, name, handler):
    original = getattr(cls, name, None)
    if original is None or getattr(original, "_kx", False):
        return
    sig = inspect.signature(original)

    def wrapper(self, *args, **kwargs):
        try:
            a = sig.bind(self, *args, **kwargs)
            a.apply_defaults()
        except TypeError:
            a = None
        result = original(self, *args, **kwargs)  # MLflow first: a call it refuses is not recorded
        if a is not None:
            handler(a.arguments)
        return result

    wrapper._kx = True
    wrapper.__wrapped__ = original
    setattr(cls, name, wrapper)


@capture.guarded
def _metric(a):
    s = _stream(a.get("run_id"))
    if s is not None:
        s.points({a["key"]: a["value"]}, a.get("step"))


@capture.guarded
def _batch(a):
    s = _stream(a.get("run_id"))
    if s is None:
        return
    if a.get("params"):
        s.config({p.key: p.value for p in a["params"]})
    by_step: dict = {}
    for m in a.get("metrics") or ():
        by_step.setdefault(m.step, {})[m.key] = m.value
    for step, values in by_step.items():
        s.points(values, step)


@capture.guarded
def _param(a):
    s = _stream(a.get("run_id"))
    if s is not None:
        s.config({a["key"]: a["value"]})


@capture.guarded
def _terminated(a):
    s = _stream(a.get("run_id"))
    if s is not None:
        s.finish()
