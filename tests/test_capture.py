"""What training code logs to wandb or MLflow is recorded in its kx run (kx.capture), under kx exec and kx.run.

The fakes below follow the real libraries' call paths (checked against wandb 0.31 and MLflow 3.17): wandb.init
returns a run whose log/finish the module's own log/finish point to; MLflow's fluent functions go through
MlflowClient. KX_TEST_TRACKERS_PYTHON=<an interpreter with wandb and mlflow> runs the same script on the real ones."""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import kx
from kx.ledger.store import open_store, loads

from kx.cli.main import main as kx_main

from conftest import commit

FAKE_WANDB = '''
import os
calls = []
class Config(dict):
    def update(self, d, **k): dict.update(self, d)
class Run:
    def __init__(self, config, mode, name, tags, notes):
        self.config, self.summary, self.mode, self.name, self.tags, self.notes = Config(config or {}), {}, mode, name, tags, notes
        self.step = 0
    def log(self, data, step=None, commit=None):
        calls.append(("log", dict(data), step))
    def finish(self, exit_code=None):
        calls.append(("finish",))
run = None
config = None
def init(project=None, config=None, mode=None, name=None, tags=None, notes=None, **k):
    global run, log, finish
    calls.append(("init", mode))
    run = Run(config, mode, name or "fake-run", tags, notes)
    globals()["config"] = run.config
    log, finish = run.log, run.finish
    return run
def log(data, step=None, commit=None): raise RuntimeError("wandb.init first")
def finish(): pass
'''

FAKE_MLFLOW = {
    "__init__.py": '''
from mlflow.tracking import MlflowClient
from mlflow.entities import Metric, Param
_run = None
class _Active:
    def __init__(self, run_id): self.info = type("I", (), {"run_id": run_id})()
    def __enter__(self): return self
    def __exit__(self, *a): end_run()
def start_run(run_name=None):
    global _run
    _run = MlflowClient().create_run("0").info.run_id
    return _Active(_run)
def end_run(status="FINISHED"):
    MlflowClient().set_terminated(_run, status)
def log_param(key, value): MlflowClient().log_param(_run, key, value)
def log_params(d): MlflowClient().log_batch(run_id=_run, metrics=[], params=[Param(k, str(v)) for k, v in d.items()], tags=[])
def log_metric(key, value, step=None): MlflowClient().log_metric(_run, key, value, step=step)
def log_metrics(d, step=None):
    MlflowClient().log_batch(run_id=_run, metrics=[Metric(k, v, 0, step or 0) for k, v in d.items()], params=[], tags=[])
def get_tracking_uri():
    import os; return os.environ.get("MLFLOW_TRACKING_URI")
''',
    "entities.py": '''
class Metric:
    def __init__(self, key, value, timestamp, step): self.key, self.value, self.timestamp, self.step = key, value, timestamp, step
class Param:
    def __init__(self, key, value): self.key, self.value = key, value
''',
    "tracking/__init__.py": '''
import itertools
_ids = itertools.count(1)
class MlflowClient:
    def create_run(self, experiment_id, tags=None, run_name=None):
        return type("R", (), {"info": type("I", (), {"run_id": f"run{next(_ids)}"})()})()
    def log_metric(self, run_id, key, value, timestamp=None, step=None, synchronous=None): pass
    def log_batch(self, run_id, metrics=(), params=(), tags=(), synchronous=None): pass
    def log_param(self, run_id, key, value, synchronous=None): return value
    def set_terminated(self, run_id, status=None, end_time=None): pass
''',
}

SCRIPT = '''
import sys
if "wandb" in sys.argv:
    import wandb
    run = wandb.init(project="p", config={"lr": 0.1, "model": {"depth": 4}, "seed": 7}, tags=["baseline"])
    wandb.config.update({"batch": 32})
    for i in range(5):
        wandb.log({"train": {"loss": 1.0 / (i + 1)}, "epoch": i, "note": "text is left out"})
    wandb.log({"val_auc": 0.8}, commit=False)
    wandb.log({"val_auc": 0.85})
    wandb.log({"test_auc": 0.9})   # once: the summary only
    wandb.finish()
if "mlflow" in sys.argv:
    import mlflow
    print("URI", mlflow.get_tracking_uri())
    with mlflow.start_run():
        mlflow.log_param("max_depth", 6)
        mlflow.log_params({"booster": "dart", "eta": 0.3})
        for i in range(4):
            mlflow.log_metrics({"rmse": 2.0 - i / 10})     # no step: MLflow logs them at 0
        for i in range(3):
            mlflow.log_metric("mae", 1.0 - i / 10, step=10 * i)
        mlflow.log_metric("best_iteration", 3)
'''


def fakes(root: Path) -> Path:
    d = root.parent / "fakes"
    (d / "mlflow" / "tracking").mkdir(parents=True, exist_ok=True)
    (d / "wandb.py").write_text(FAKE_WANDB)
    for rel, text in FAKE_MLFLOW.items():
        (d / "mlflow" / rel).write_text(textwrap.dedent(text))
    return d


def recorded(run_id):
    with open_store() as st:
        r = st.get_run(run_id)
        points = [json.loads(line) for line in (st.run_dir(run_id) / "metrics.jsonl").read_text().splitlines()]
        return r, loads(r["config"]), loads(r["metrics"]), points, st.run_dir(run_id)


def curve(points, name):
    return [(p["step"], p[name]) for p in points if name in p and "step" in p]  # not the summary lines


def exec_script(comp, capsys, python, which, monkeypatch, extra_path=None):
    (comp / "train.py").write_text(SCRIPT)
    commit(comp, "train")
    if extra_path:
        monkeypatch.setenv("PYTHONPATH", str(extra_path))
    capsys.readouterr()
    code = kx_main(["exec", "-H", "trackers", "--json", "--", python, "train.py", *which.split()])
    res = json.loads(capsys.readouterr().out)
    assert code == 0, res
    return res["run"]


def check_wandb(run_id):
    r, cfg, metrics, points, _ = recorded(run_id)
    assert cfg["lr"] == 0.1 and cfg["model"] == {"depth": 4} and cfg["batch"] == 32 and cfg["wandb_tags"] == ["baseline"]
    assert r["seed"] == 7
    assert curve(points, "train/loss") == [(i, 1.0 / (i + 1)) for i in range(5)]  # nested dicts flatten to a/b
    assert curve(points, "val_auc") == [(5, 0.85)]  # commit=False: the next call is the same step, the later value kept
    assert curve(points, "test_auc") == [] and metrics["test_auc"] == 0.9  # logged once: a summary, not a curve
    assert metrics["train/loss"] == 0.2 and "note" not in metrics


def check_mlflow(run_id):
    r, cfg, metrics, points, d = recorded(run_id)
    assert cfg["max_depth"] == 6 and cfg["booster"] == "dart" and cfg["eta"] == 0.3  # params back to numbers
    assert curve(points, "rmse") == [(i, 2.0 - i / 10) for i in range(4)]  # no step: each metric's own count
    assert curve(points, "mae") == [(0, 1.0), (10, 0.9), (20, 0.8)]
    assert curve(points, "best_iteration") == [] and metrics["best_iteration"] == 3
    assert (d / "stdout.log").read_text().count(str(d)) == 1  # MLflow logged into the run's dir, not the repo


def test_wandb_and_mlflow_calls_are_recorded_in_the_kx_exec_run(comp, capsys, monkeypatch):
    run_id = exec_script(comp, capsys, sys.executable, "wandb mlflow", monkeypatch, fakes(comp))
    check_wandb(run_id)
    check_mlflow(run_id)
    assert not (comp / "mlruns").exists() and not (comp / "mlflow.db").exists()
    err = (recorded(run_id)[4] / "stderr.log").read_text()
    assert "wandb itself is off" in err  # wandb was started with mode="disabled": nothing left the machine


def test_capture_can_be_turned_off(comp, capsys, monkeypatch):
    monkeypatch.setenv("KX_CAPTURE", "off")
    run_id = exec_script(comp, capsys, sys.executable, "wandb", monkeypatch, fakes(comp))
    with open_store() as st:
        assert loads(st.get_run(run_id)["config"]) is None
        assert not (st.run_dir(run_id) / "metrics.jsonl").exists()


def test_kx_run_records_what_its_own_process_logs(comp, capsys, monkeypatch):
    sys.path.insert(0, str(fakes(comp)))
    for m in [m for m in sys.modules if m == "wandb" or m.startswith("mlflow")]:
        del sys.modules[m]
    try:
        with kx.run("in process", kind="probe") as r:
            import wandb
            run = wandb.init(config={"lr": 0.5})
            for i in range(3):
                wandb.log({"loss": 3.0 - i})
            run.finish()
        _, cfg, metrics, points, _ = recorded(r.id)
        assert cfg == {"lr": 0.5} and curve(points, "loss") == [(0, 3.0), (1, 2.0), (2, 1.0)] and metrics["loss"] == 1.0
    finally:
        sys.path.remove(str(fakes(comp)))
        for m in [m for m in sys.modules if m == "wandb" or m.startswith("mlflow")]:
            del sys.modules[m]
        os.environ.pop("MLFLOW_TRACKING_URI", None)


def test_a_broken_adapter_never_breaks_the_training_code(comp, capsys, monkeypatch):
    d = fakes(comp)
    (d / "wandb.py").write_text(FAKE_WANDB.replace("class Run:", "class Run:\n    __slots__ = ('config', 'summary', "
                                                   "'mode', 'name', 'tags', 'notes', 'step')"))  # run.log cannot be wrapped
    run_id = exec_script(comp, capsys, sys.executable, "wandb", monkeypatch, d)
    with open_store() as st:
        assert st.get_run(run_id)["status"] == "done"
        assert "capture stopped" in (st.run_dir(run_id) / "stderr.log").read_text()


@pytest.mark.skipif(not os.environ.get("KX_TEST_TRACKERS_PYTHON"), reason="KX_TEST_TRACKERS_PYTHON: an interpreter with wandb and mlflow")
def test_the_real_wandb_and_mlflow(comp, capsys, monkeypatch):
    monkeypatch.setenv("WANDB_SILENT", "true")
    monkeypatch.setenv("MLFLOW_DISABLE_AGENT_HINT", "1")
    run_id = exec_script(comp, capsys, os.environ["KX_TEST_TRACKERS_PYTHON"], "wandb mlflow", monkeypatch)
    check_wandb(run_id)
    check_mlflow(run_id)
    assert not (comp / "mlruns").exists() and not (comp / "mlflow.db").exists() and not (comp / "wandb").exists()


def test_on_a_remote_host_what_is_logged_goes_to_the_run_files(comp, tmp_path):
    """No ledger there (KX_REMOTE=1): config into attach.json, curves into metrics.jsonl, the summary into
    logged_metrics.json, all of which kx records when the run comes back."""
    from kx.capture import SITE
    out = tmp_path / "out"
    out.mkdir()
    (comp / "train.py").write_text(SCRIPT)
    env = {**os.environ, "KX_REMOTE": "1", "KX_RUN_ID": "r0042-far-away", "KX_RUN_DIR": str(out),
           "PYTHONPATH": os.pathsep.join([str(SITE), str(fakes(comp))])}
    p = subprocess.run([sys.executable, "train.py", "wandb", "mlflow"], cwd=comp, env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    attach = json.loads((out / "attach.json").read_text())
    assert attach["config"]["lr"] == 0.1 and attach["config"]["booster"] == "dart"
    assert json.loads((out / "logged_metrics.json").read_text())["test_auc"] == 0.9
    points = [json.loads(line) for line in (out / "metrics.jsonl").read_text().splitlines()]
    assert curve(points, "train/loss")[-1] == (4, 0.2) and curve(points, "rmse")[0] == (0, 2.0)
    assert str(out) in p.stdout  # MLflow's own copy in the run's dir there too
