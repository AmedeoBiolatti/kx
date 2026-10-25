"""wandb into the kx run: wandb.init's config, tags and notes, each log call as a step, the summary at finish.

wandb is switched off (mode="disabled": no service, no network, no files) unless KX_WANDB=also, which leaves it as the
code set it up. The run object wandb.init returns is wrapped, whatever its class, so `wandb.log` and `run.log` are both
seen; steps follow wandb's rule: a call without `step` is at the run's counter, which moves on unless commit=False.
"""
from __future__ import annotations

import os
import sys

from kx import capture


def patch(wandb):
    original = wandb.init

    def init(*args, **kwargs):
        rec = capture.recorder()
        if rec is None:
            return original(*args, **kwargs)
        also = os.environ.get("KX_WANDB", "").lower() == "also"
        if not also:
            kwargs["mode"] = "disabled"
        run = original(*args, **kwargs)
        _wrap(wandb, run, rec, also)
        return run

    wandb.init = init


@capture.guarded
def _wrap(wandb, run, rec, also):
    s = rec.stream("wandb", id(run), getattr(run, "name", None))
    s.config(_config(run))
    tags = list(getattr(run, "tags", None) or [])
    if tags:
        rec.config({"wandb_tags": tags})
    notes = getattr(run, "notes", None)
    if isinstance(notes, str) and notes:
        rec.note(notes)
    log, finish = run.log, run.finish

    def logged(data, step=None, commit=None, *args, **kwargs):
        _point(s, data, step, commit)
        return log(data, step, commit, *args, **kwargs)

    def finished(*args, **kwargs):
        _finish(s, run)
        return finish(*args, **kwargs)

    run.log, run.finish = logged, finished
    wandb.log, wandb.finish = logged, finished  # as wandb.init does: the module's log is the latest run's
    print(f"kx: wandb run recorded in {rec.run.name}" + ("" if also else " (wandb itself is off: KX_WANDB=also keeps it)"),
          file=sys.stderr)


def _config(run) -> dict:
    try:
        return dict(run.config)
    except Exception:  # noqa: BLE001
        return {}


@capture.guarded
def _point(s, data, step, commit):
    if not isinstance(data, dict):
        return
    at = s.counter if step is None else step
    s.points(data, at)
    if step is not None:
        s.counter = max(s.counter, step)
    if step is None and commit is not False:
        s.counter = at + 1


@capture.guarded
def _finish(s, run):
    s.config(_config(run))
    try:
        s.summary({k: v for k, v in dict(run.summary).items() if not str(k).startswith("_")})
    except Exception:  # noqa: BLE001 - a disabled run's summary keeps nothing: the last values stand in
        pass
    s.finish()
