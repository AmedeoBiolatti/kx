"""Submissions: preview, --yes, duplicates."""



from conftest import submittable, run_kx


def test_submit_preview_yes_duplicate(comp, fake_kaggle, capsys):
    r = submittable(comp)
    capsys.readouterr()
    code, prev = run_kx("submit", r.name, "-m", "first", "--why", "baseline", capsys=capsys)
    assert code == 3 and prev["preview"]["run"] == r.name and not fake_kaggle.sent
    assert prev["command"].endswith("--yes")
    code, res = run_kx("submit", r.name, "-m", "first", "--yes", capsys=capsys)
    assert code == 0 and len(fake_kaggle.sent) == 1
    code, prev = run_kx("submit", r.name, "-m", "again", capsys=capsys)  # the same file again: the preview says so
    assert code == 3 and any("already submitted as S1" in n for n in prev["preview"]["notes"])
    run_kx("sync", "--no-lb", capsys=capsys)
    code, subs = run_kx("subs", capsys=capsys)
    assert subs["submissions"][0]["ref"] == "1001" and subs["submissions"][0]["public"] == 0.5
    code, one = run_kx("subs", "S1", capsys=capsys)
    assert code == 0 and one["submission"].startswith("S1-") and one["games"] is None


def test_kernel_push_waits_for_dataset_and_pins_its_run(comp, fake_kaggle, monkeypatch, capsys):
    """A kernel pushed right after a dataset upload gets the dataset's previous version: kx refuses while the dataset
    is not ready, and writes into the kernel code which run each dataset must come from (the kernel checks it)."""
    import json as _json

    import pytest

    import kx
    from conftest import commit
    from kx.kaggle import client as kc
    from kx.kaggle import kernels
    from kx.ledger import config
    from kx.ledger.config import KxError
    from kx.ledger.store import Store

    text = (comp / "comp.yaml").read_text().replace(
        "submission: {daily_limit: 3, file: submission.csv}", "submission: {daily_limit: 3, kernel: me/k, kernel_dir: kernel}")
    (comp / "comp.yaml").write_text(text)
    (comp / "kernel").mkdir()
    (comp / "kernel" / "kernel-metadata.json").write_text(_json.dumps(
        {"id": "me/k", "title": "k", "code_file": "run.py", "dataset_sources": ["me/data"]}))
    (comp / "kernel" / "run.py").write_text('KX = "__KX_PUSH__"\n')
    commit(comp, "kernel")
    with kx.run("package") as r:
        (r.dir / "dataset").mkdir()
        (r.dir / "dataset" / "dataset-metadata.json").write_text(_json.dumps({"id": "me/data", "title": "data"}))

    monkeypatch.setattr(kc, "dataset_version", lambda folder, msg, create=False: "https://www.kaggle.com/datasets/me/data")
    pushed = {}

    def fake_push(folder, timeout=None, accelerator=None):
        pushed["code"] = (folder / "run.py").read_text()
        return 7, "Kernel version 7 successfully pushed. https://www.kaggle.com/code/me/k"
    monkeypatch.setattr(kc, "kernel_push", fake_push)
    with Store(config.load()) as st:
        monkeypatch.setattr(kc, "dataset_status", lambda ref: ("ready", 1))  # version 1 exists: this upload is v2
        kernels.push_dataset(st, r.id, "dataset")
        assert _json.loads((r.dir / "dataset" / "kx-dataset.json").read_text())["run"] == r.name
        with pytest.raises(KxError, match="previous version"):  # "ready", but that is still v1
            kernels.push_kernel(st, r.id)
        monkeypatch.setattr(kc, "dataset_status", lambda ref: ("pending", 2))
        with pytest.raises(KxError, match="previous version"):
            kernels.push_kernel(st, r.id)
        assert not pushed
        monkeypatch.setattr(kc, "dataset_status", lambda ref: ("ready", 2))
        kernels.push_kernel(st, r.id)
    ns = {}
    exec(pushed["code"], ns)  # still valid Python
    kx_push = _json.loads(ns["KX"])
    assert kx_push["run"] == r.name and kx_push["datasets"] == {"me/data": r.name}


def test_lb_bounds_from_last_submission_dates():
    from kx.kaggle.submit import lb_bounds
    # 10 teams, 0.95 .. 0.50 in rank order; the top five last submitted on Oct 3, the rest on Oct 1
    rows = [{"Rank": str(i + 1), "Score": f"{0.95 - 0.05 * i:.2f}",
             "LastSubmissionDate": f"2026-10-0{3 if i < 5 else 1} 12:00:00"} for i in range(10)]
    b = lb_bounds(rows)
    assert [e["day"] for e in b] == ["2026-10-01", "2026-10-02", "2026-10-03"]
    # Oct 1: five teams final (0.70 .. 0.50). Medal ranks today (10 teams): gold 1, silver 2, bronze 4;
    # with only the five final teams on the board: gold 1, silver 1, bronze 2
    assert b[0]["final"] == 5 and b[1] == {**b[0], "day": "2026-10-02"}
    assert b[0]["gold"] == [0.70, 0.95] and b[0]["silver"] == [0.65, 0.95] and b[0]["bronze"] == [0.55, 0.90]
    # Oct 3: every team final, so each range closes on today's value
    assert b[2]["top"] == [0.95, 0.95] and b[2]["silver"] == [0.90, 0.90] and b[2]["bronze"] == [0.80, 0.80]
    assert set(lb_bounds(rows, medals=False)[0]) == {"day", "final", "top"}
