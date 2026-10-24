"""What leaves the machine is previewed; workers do not send; budgets refuse up front."""

import sys

from kx.cli.main import main as kx_main

from conftest import commit, submittable, run_kx


def test_a_worker_leaves_sending_to_its_person(comp, fake_kaggle, capsys, monkeypatch):
    r = submittable(comp)
    capsys.readouterr()
    monkeypatch.setenv("KX_AGENT", "w1")
    monkeypatch.setenv("KX_TASK", "T3")
    code, err = run_kx("submit", r.name, "-m", "from worker", "--yes", capsys=capsys)
    assert code == 3 and not fake_kaggle.sent
    assert err["command"] == f"kx submit {r.name} -m 'from worker' --yes" and err["preview"]["run"] == r.name
    assert "kx task wait T3 --on" in err["fix"]
    monkeypatch.delenv("KX_AGENT")
    monkeypatch.delenv("KX_TASK")
    code, res = run_kx("submit", r.name, "-m", "from worker", "--yes", capsys=capsys)  # the person runs it
    assert code == 0 and len(fake_kaggle.sent) == 1


def test_budgets_refuse_up_front(comp, fake_kaggle, capsys, monkeypatch):
    y = comp / "comp.yaml"
    y.write_text(y.read_text() + "budgets: {submissions_per_day: 1, gpu_hours_per_day: 0}\n")
    commit(comp, "budget")
    r = submittable(comp)
    capsys.readouterr()
    assert run_kx("submit", r.name, "-m", "one", "--yes", capsys=capsys)[0] == 0
    (r.dir / "submission.csv").write_text("id,y\n1,0.6\n")
    code, err = run_kx("submit", r.name, "-m", "two", "--yes", capsys=capsys)
    assert code == 4 and "submissions_per_day" in err["error"] and "comp.yaml" in err["fix"] and len(fake_kaggle.sent) == 1
    code = kx_main(["exec", "--gpus", "1", "--json", "--", sys.executable, "-c", "pass"])
    out = capsys.readouterr()
    assert code == 4 and "gpu_hours_per_day" in out.out
    code, b = run_kx("budget", capsys=capsys)
    assert {c["key"]: c["used"] for c in b["caps"]}["submissions_per_day"] == 1
