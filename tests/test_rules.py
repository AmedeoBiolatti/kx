"""comp.yaml runs: (where runs happen) and storage: (remote runs keep nothing on their host; a cap on the store), kx gc."""

import json
import sys

import pytest

import kx
from kx.cli.main import main as kx_main
from kx.guard import rules
from kx.ledger import config
from kx.ledger.config import KxError
from kx.ledger.store import Store, open_store
from kx.remote import execute, hosts

from conftest import P, commit, run_kx, submittable
from test_remote import TRAIN

BIG_TRAIN = TRAIN.replace('    print("done training")',
                          '    (r.dir / "model.pt").write_bytes(b"w" * 8192)\n    print("done training")')


@pytest.fixture
def boxes(comp, tmp_path, monkeypatch):
    """Two local hosts (directories), box and other; train.py writes a model bigger than BIG (made 1K here)."""
    monkeypatch.setattr(hosts.Transport, "POLL_S", (0.1,))
    monkeypatch.setattr(execute, "BIG", "1K")
    monkeypatch.setattr(execute, "LOW_DISK", 0)
    with open(comp / "comp.yaml", "a") as f:
        f.write("hosts:\n" + "".join(f"  {n}: {{local: true, dir: {tmp_path / n}, python: {sys.executable}}}\n"
                                     for n in ("box", "other")))
    (comp / "train.py").write_text(BIG_TRAIN)
    commit(comp, "train")
    return tmp_path


def rules_yaml(comp, text):
    with open(comp / "comp.yaml", "a") as f:
        f.write(text)
    commit(comp, "rules")


def exec_json(*args, capsys):
    code = kx_main(["exec", "--json", *args])
    return code, json.loads(capsys.readouterr().out)


def test_runs_happen_only_on_the_hosts_named(boxes, comp, capsys):
    rules_yaml(comp, "runs: {on: [box]}\n")
    code, out = exec_json("--", "python", "train.py", capsys=capsys)  # no --on: the first of runs.on
    assert code == 0
    with open_store() as st:
        assert st.get_run(out["run"])["host"] == "box"
    code, err = exec_json("--on", "other", "--", "python", "train.py", capsys=capsys)
    assert code == 4 and "runs.on" in err["error"] and "--on box" in err["fix"]
    with pytest.raises(KxError) as e:  # a script started here, outside kx exec
        with kx.run("here"):
            pass
    assert e.value.code == 4


def test_bad_rules_are_config_errors(comp):
    rules_yaml(comp, "storage: {keep: there}\n")
    with pytest.raises(KxError, match="storage.keep"):
        config.load()


def test_keep_here_brings_everything_back_and_clears_the_host(boxes, comp, capsys):
    rules_yaml(comp, "storage: {keep: here}\n")
    code, out = exec_json("--on", "box", "--", "python", "train.py", capsys=capsys)
    assert code == 0
    with open_store() as st:
        d = st.run_dir(out["run"])
    assert (d / "model.pt").stat().st_size == 8192 and (d / "submission.csv").exists()
    assert not (boxes / "box" / "demo" / "runs" / out["run"]).exists()
    assert json.loads((d / "remote.json").read_text())["host_copy"] == "removed"
    code, f = run_kx("fetch", out["run"], capsys=capsys)
    assert code == 0 and f["all_here"]


def test_keep_here_leaves_on_the_host_what_has_no_room(boxes, comp, capsys, monkeypatch):
    rules_yaml(comp, "storage: {keep: here, max_gb: 1}\n")
    monkeypatch.setattr(rules, "room", lambda c: 100)  # room for the small files, not for the model
    code, out = exec_json("--on", "box", "--", "python", "train.py", capsys=capsys)
    assert code == 0
    with open_store() as st:
        d = st.run_dir(out["run"])
    inf = json.loads((d / "remote.json").read_text())
    assert not (d / "model.pt").exists() and [x["path"] for x in inf["on_host"]] == ["model.pt"]
    assert (boxes / "box" / "demo" / "runs" / out["run"]).exists() and "host_copy" not in inf


def test_a_full_store_refuses_runs_and_downloads(comp, capsys, monkeypatch):
    rules_yaml(comp, "storage: {max_gb: 1}\n")
    monkeypatch.setattr(rules, "_measure", lambda p: 2 * rules.GB)
    code, err = exec_json("--", sys.executable, "-c", "pass", capsys=capsys)
    assert code == 4 and "2.0 GB used" in err["error"] and "kx gc" in err["fix"]
    code, b = run_kx("budget", capsys=capsys)
    assert b["store"]["used_gb"] == 2 and b["store"]["max_gb"] == 1
    code, st = run_kx("status", capsys=capsys)
    assert any(x["kind"] == "storage" for x in st["needs_you"])
    code, cx = run_kx("context", capsys=capsys)
    assert any("storage.max_gb" in r for r in cx["rules"])


def test_gc_deletes_what_unneeded_runs_made(comp, fake_kaggle, capsys, monkeypatch):
    sub = submittable(comp)
    (sub.dir / "weights.bin").write_bytes(b"x" * 4096)
    run_kx("submit", sub.name, "-m", "keep me", "--yes", capsys=capsys)
    with kx.run("dead end") as r:
        r.step(0, loss=1.0)
        r.score(0.5, P, breakdown={"f0": 0.5, "f1": 0.5})
        (r.dir / "weights.bin").write_bytes(b"x" * 4096)
        (r.dir / "ckpt").mkdir()
        (r.dir / "ckpt" / "last.pt").write_bytes(b"y" * 2048)
    run_kx("tag", r.name, "discarded", capsys=capsys)
    run_kx("tag", sub.name, "discarded", capsys=capsys)
    code, prev = run_kx("gc", capsys=capsys)
    assert code == 3 and [x["run"] for x in prev["runs"]] == [r.name] and prev["bytes"] >= 6144
    assert {k["run"]: k["why"] for k in prev["kept"]}[sub.name].endswith("was submitted from it")
    assert (r.dir / "weights.bin").exists()
    monkeypatch.setenv("KX_AGENT", "w1")
    assert run_kx("gc", "--yes", capsys=capsys)[0] == 3
    monkeypatch.delenv("KX_AGENT")
    code, res = run_kx("gc", "--yes", capsys=capsys)
    assert code == 0 and res["freed"] >= 6144
    assert not (r.dir / "weights.bin").exists() and not (r.dir / "ckpt").exists()
    assert (r.dir / "metrics.jsonl").exists() and (sub.dir / "weights.bin").exists()
    code, show = run_kx("show", r.name, capsys=capsys)
    assert show["scores"][0]["value"] == 0.5 and any(n["text"].startswith("kx gc: deleted 2 file(s)") for n in show["notes"])
    assert run_kx("gc", capsys=capsys)[1]["runs"] == []  # nothing left to free
