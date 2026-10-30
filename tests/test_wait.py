"""kx wait TARGET: runs (ended, --until, stalled), submissions, kernels; and kx exec --detach on this machine."""

import sys
import time

import kx
from kx import wait
from kx.kaggle import client as kc
from kx.ledger.store import now, open_store
from kx.runs import tracking

from conftest import P, run_kx, scored_run, submittable


def detach(*args, capsys):
    """kx exec --json --detach ARGS (--json before the command: after it, it would be the command's)."""
    import json
    from kx.cli.main import main
    code = main(["exec", "--json", "--detach", *args])
    return code, json.loads(capsys.readouterr().out)


def test_a_run_that_ended_returns_at_once(comp, capsys):
    r = scored_run("baseline", {"f0": 0.8, "f1": 0.82})
    code, res = run_kx("wait", r.name, capsys=capsys)
    assert code == 0 and res["met"] and res["events"][0]["event"] == "done"
    try:
        with kx.run("broken"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    code, res = run_kx("wait", "latest", capsys=capsys)
    assert code == wait.UNMET and res["events"][0]["event"] == "failed" and "boom" in res["events"][0]["error"]


def test_until_a_metric_or_a_score(comp, capsys):
    with kx.run("curves") as r:
        for i, auc in enumerate((0.7, 0.85, 0.93, 0.9)):
            r.step(i, val_auc=auc, loss=1 - auc)
        r.score(0.81, P, breakdown={"f0": 0.8, "f1": 0.82})
    code, res = run_kx("wait", r.name, "--until", "val_auc>=0.92", capsys=capsys)
    ev = res["events"][0]
    assert code == 0 and ev["event"] == "met" and ev["value"] == 0.93 and ev["step"] == 2
    code, res = run_kx("wait", r.name, "--until", "cv3>=0.8", capsys=capsys)
    assert code == 0 and res["events"][0]["metric"] == "cv3"
    code, res = run_kx("wait", r.name, "--until", "val_auc>=0.99", "--until", "loss<0.01", capsys=capsys)
    ev = res["events"][0]
    assert code == wait.UNMET and ev["event"] == "done" and ev["last"] == {"val_auc": 0.9, "loss": 1 - 0.9}
    code, res = run_kx("wait", r.name, "--until", "val_acc>=0.9", capsys=capsys)  # a typo: say what it does log
    assert code == wait.UNMET and "val_auc" in res["events"][0]["logged"]


def test_a_running_run_meets_stalls_or_times_out(comp, capsys):
    act = tracking.begin("long", None)
    try:
        code, res = run_kx("wait", act.run.name, "--timeout", "0.002", "--poll", "0.02", "--stall", "0", capsys=capsys)
        assert code == 1 and res["why"] == "timeout" and f"kx wait {act.run.name}" in res["again"]
        act.run.step(1, val_auc=0.95)
        code, res = run_kx("wait", act.run.name, "--until", "val_auc>=0.9", capsys=capsys)
        assert code == 0 and res["events"][0]["status"] == "running"
        time.sleep(1.2)
        code, res = run_kx("wait", act.run.name, "--until", "val_auc>=0.99", "--stall", "0.01", capsys=capsys)
        assert code == wait.UNMET and res["events"][0]["event"] == "stalled"
    finally:
        tracking.end(act)


def test_a_timeout_says_where_the_run_is(comp, capsys):
    """A look at a long run is the wait itself: progress, ETA, the --until metric first, the last line printed."""
    act = tracking.begin("long", None)
    try:
        r = act.run
        for i in range(3):
            r.progress(i + 1, total=10, unit="epochs", loss=1 - i / 10, val_auc=0.7 + i / 100, lr=0.1)
        (r.dir / "stdout.log").write_text("epoch 1\nepoch 2\r 30%|███ | 3/10\n")
        code, res = run_kx("wait", r.name, "--until", "val_auc>=0.9", "--timeout", "0.001", "--poll", "0.01",
                           capsys=capsys)
        s = res["state"][0]
        assert code == 1 and s["target"] == r.name and s["progress"]["completed"] == 3 and s["progress"]["total"] == 10
        assert list(s["metrics"])[0] == "val_auc" and s["output"] == "30%|███ | 3/10"
        from kx.cli.main import main
        main(["wait", r.name, "--timeout", "0.001", "--poll", "0.01"])
        text = capsys.readouterr().out
        assert "3/10 epochs (30%)" in text and "output" in text and "3/10" in text
    finally:
        tracking.end(act)


def test_a_submission_until_scored_or_failed(comp, fake_kaggle, monkeypatch, capsys):
    r = submittable(comp)
    run_kx("submit", r.name, "-m", "first", "--expect", "about 0.8", "--yes", capsys=capsys)
    monkeypatch.setattr(wait, "KAGGLE_EVERY_S", (0.2, 0.2))  # kx submit has just synced: the wait asks after that
    code, res = run_kx("wait", "S1", "--poll", "0.05", capsys=capsys)  # asks Kaggle (kx sync's code): scored
    ev = res["events"][0]
    assert code == 0 and ev["event"] == "scored" and ev["public"] == 0.5 and ev["expect"] == "about 0.8"

    fake_kaggle.remote.append({"ref": "2001", "description": "broken", "status": "ERROR", "date": "2026-10-15T00:00:00",
                               "errorDescription": "wrong number of rows"})
    with open_store() as st:
        sid = st.new("submissions", {"created": now(), "message": "broken", "status": "submitted"})
        st.set_setting("synced", None)
    code, res = run_kx("wait", f"submission:{sid}", capsys=capsys)
    assert code == wait.UNMET and res["events"][0]["error"] == "wrong number of rows"


def test_waits_share_kaggle(comp, fake_kaggle, monkeypatch, capsys):
    """A sync another wait (or kx sync) just made is not repeated: its answer is already in the ledger."""
    asked = []
    monkeypatch.setattr(kc, "submissions", lambda slug: asked.append(1) or [])
    with open_store() as st:
        st.new("submissions", {"created": now(), "message": "pending", "status": "submitted"})
        st.set_setting("synced", now())
    code, res = run_kx("wait", "S1", "--timeout", "0.001", "--poll", "0.01", capsys=capsys)
    assert code == 1 and not asked


def test_a_kernel_version(comp, monkeypatch, capsys):
    r = scored_run("kernel run", {"f0": 0.8, "f1": 0.8})
    code, err = run_kx("wait", f"kernel:{r.name}", capsys=capsys)
    assert code == 1 and "no kernel pushed" in err["error"]
    with open_store() as st:
        st.insert("pushes", {"run_id": r.id, "kind": "kernel", "target": "me/k", "version": 3, "created": now()})
    states = iter(["running", "complete"])
    seen = []
    monkeypatch.setattr(kc, "kernel_status", lambda k, v: seen.append((k, v)) or (next(states), ""))
    monkeypatch.setattr(wait, "KAGGLE_EVERY_S", (0, 0))
    code, res = run_kx("wait", f"kernel:{r.name}", "--poll", "0.01", capsys=capsys)
    assert code == 0 and res["events"][0]["event"] == "complete" and seen == [("me/k", 3)] * 2


def test_bad_waits_are_usage_errors(comp, fake_kaggle, capsys):
    r = scored_run("x", {"f0": 0.8, "f1": 0.8})
    assert run_kx("wait", r.name, "--until", "val_auc ~ 3", capsys=capsys)[0] == 2
    assert run_kx("wait", "--until", "val_auc>1", capsys=capsys)[0] == 2
    submittable(comp)
    with open_store() as st:
        st.new("submissions", {"created": now(), "message": "m", "status": "complete"})
    assert run_kx("wait", "S1", "--until", "val_auc>1", capsys=capsys)[0] == 2
    assert run_kx("wait", "episode:3", capsys=capsys)[0] == 2


def test_exec_detach_here(comp, capsys):
    script = "import time, kx; r = kx.current_run(); r.step(0, val_auc=0.91); time.sleep(0.5)"
    code, res = detach("-H", "in the background", "--", sys.executable, "-c", script, capsys=capsys)
    assert code == 0 and res["detached"] and res["pid"]
    code, done = run_kx("wait", res["run"], "--poll", "0.1", "--timeout", "1", capsys=capsys)
    assert code == 0 and done["events"][0]["event"] == "done"
    with open_store() as st:
        row = st.one("SELECT * FROM runs WHERE id=?", (res["run"],))
        assert row["hypothesis"] == "in the background" and row["pid"] == res["pid"]
    (comp / "dirty.py").write_text("x = 1\n")  # an experiment refuses uncommitted code: the error comes back here
    code, err = detach("--", sys.executable, "-c", "pass", capsys=capsys)
    assert code == 1 and "uncommitted" in err["error"] and "probe" in err["fix"]


def test_exec_detach_stops_on_sigterm(comp, capsys):
    import os
    import signal
    code, res = detach("--", sys.executable, "-c", "import time; time.sleep(60)", capsys=capsys)
    os.kill(res["pid"], signal.SIGTERM)
    code, done = run_kx("wait", res["run"], "--poll", "0.1", "--timeout", "0.5", capsys=capsys)
    assert code == wait.UNMET and "SIGTERM" in done["events"][0]["error"]


def test_what_a_run_came_to(comp, capsys):
    """The wait (and kx exec) says the scores, the rank and the verdict: against the parent when they share a
    protocol, else against the best other run."""
    base = scored_run("base", {"f0": 0.80, "f1": 0.81, "f2": 0.79})
    child = scored_run("child", {"f0": 0.90, "f1": 0.91, "f2": 0.905}, parent=base.name)
    code, res = run_kx("wait", child.name, capsys=capsys)
    o = res["events"][0]["result"]
    assert o["rank"] == 1 and o["of"] == 2 and o["vs"]["run"] == base.name and o["vs"]["as"] == "parent"
    assert o["vs"]["verdict"] == "better"
    other = scored_run("other", {"f0": 0.85, "f1": 0.86, "f2": 0.84})
    code, res = run_kx("wait", other.name, capsys=capsys)
    o = res["events"][0]["result"]
    assert o["rank"] == 2 and o["of"] == 3 and o["vs"] == {**o["vs"], "run": child.name, "as": "best", "verdict": "worse"}
    from kx.cli.main import main
    main(["wait", other.name])
    text = capsys.readouterr().out
    assert f"#2 of 3 · vs {child.name} (best): -0.05" in text and "▼ worse" in text


def test_exec_ends_with_what_the_run_came_to(comp, capsys):
    from kx.cli.main import main
    base = scored_run("base", {"f0": 0.7, "f1": 0.8})
    script = ("import json, os, pathlib; d = pathlib.Path(os.environ['KX_RUN_DIR']); "
              "(d / 'scores.json').write_text(json.dumps([{'protocol': 'cv3', 'spec': {'metric': 'auc', 'folds': 'f1'}, "
              "'value': 0.75, 'breakdown': {'f0': 0.7, 'f1': 0.8}}]))")
    assert main(["exec", "--", sys.executable, "-c", script]) == 0
    assert f"#1 of 2 · vs {base.id} (best): +0" in capsys.readouterr().out


def test_all_of_several_runs_or_a_group(comp, capsys):
    first = scored_run("sweep a", {"f0": 0.8, "f1": 0.8}, group="sweep")
    act = tracking.begin("sweep b", None, group="sweep")
    try:
        code, res = run_kx("wait", "group:sweep", capsys=capsys)  # the first that ended
        assert code == 0 and [e["target"] for e in res["events"]] == [first.name]
        code, res = run_kx("wait", "--all", "group:sweep", "--timeout", "0.002", "--poll", "0.02", "--stall", "0",
                           capsys=capsys)
        assert code == 1 and res["pending"] == [act.run.name] and len(res["events"]) == 1
    finally:
        tracking.end(act, "boom")
    code, res = run_kx("wait", "--all", first.name, act.run.name, capsys=capsys)
    assert code == wait.UNMET and {e["event"] for e in res["events"]} == {"done", "failed"}
    assert run_kx("wait", "group:nothing", capsys=capsys)[0] == 1
