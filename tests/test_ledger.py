"""The ledger is the logs: one per user@machine, the SQLite cache rebuilt from them; machines share them with kx sync
(rclone faked by fake_rclone.py: remotes are directories); old ledgers come in with kx import."""

import json
import os
import shutil
import socket
import sqlite3
import sys
from pathlib import Path

import pytest

import kx
from kx.board import tasks
from kx.ledger import config
from kx.ledger.store import Store

from conftest import P, git, run_kx, scored_run, submittable


def contents(st: Store) -> dict:
    return {t: sorted(json.dumps(dict(r), sort_keys=True) for r in st.all(f"SELECT * FROM {t}"))
            for t in ("runs", "scores", "notes", "tags", "tasks", "task_notes", "protocols")}


def test_the_cache_is_rebuilt_from_the_logs(comp, capsys):
    t = tasks.add(Store(config.load()), "try it")
    with kx.run("first", config={"seed": 0}, tags=["algorithm"]) as r:
        r.score(0.7, P, breakdown={"f0": 0.7, "f1": 0.7})
        r.note("works")
    tasks.note(Store(config.load()), t["id"], "progress")
    st = Store(config.load())
    before = contents(st)
    st.close()
    for p in (comp / ".kx" / "store").glob("cache.sqlite*"):
        p.unlink()
    st = Store(config.load())
    assert contents(st) == {**before, "runs": [json.dumps({**json.loads(x), "heartbeat": json.loads(x)["created"]},
                                                          sort_keys=True) for x in before["runs"]]}  # heartbeats: here only
    log = next((comp / ".kx" / "store" / "log").glob("*.jsonl"))
    assert all(set(json.loads(line)) >= {"at", "by", "t", "op"} for line in log.read_text().splitlines())


def test_ids_have_a_number_and_a_name(comp, capsys):
    a = scored_run("a", {"f0": 0.6, "f1": 0.6})
    b = scored_run("b", {"f0": 0.7, "f1": 0.7})
    assert a.name.startswith("r0001-") and b.name.startswith("r0002-") and a.name.count("-") == 2
    st = Store(config.load())
    name = a.name.split("-", 1)[1]
    for ref in ("r1", "1", "r0001", name, a.name, f"{name}-1"):
        assert st.resolve(ref) == a.name, ref
    t = tasks.add(st, "x")
    assert t["id"].startswith("T1-") and st.resolve("T1", "tasks") == t["id"] == st.resolve("#1", "tasks")
    code, err = run_kx("show", "r9", capsys=capsys)
    assert code == 1 and "no run r9" in err["error"]


def test_a_crash_between_the_log_and_the_cache_loses_nothing(comp, capsys):
    scored_run("a", {"f0": 0.6, "f1": 0.6})
    st = Store(config.load())
    log = st.log_path
    run_id = st.resolve("r1")
    st.close()
    with open(log, "a") as f:  # a line the cache never got, and half a line never committed
        f.write(json.dumps({"at": "2026-10-24T00:00:00+00:00", "by": "t", "t": "notes", "op": "ins",
                            "row": {"run_id": run_id, "ts": "2026-10-24T00:00:00+00:00", "author": "t", "text": "late"}}) + "\n")
        f.write('{"at": "2026-10-24T00:00:01+00:00", "t": "no')
    st = Store(config.load())
    assert [n["text"] for n in st.notes(run_id)] == ["late"]
    assert log.read_text().endswith("\n") and json.loads(log.read_text().splitlines()[-1])["row"]["text"] == "late"
    st.add_note(run_id, "after")
    assert [n["text"] for n in Store(config.load()).notes(run_id)] == ["late", "after"]


def test_a_log_line_from_a_newer_kx_is_skipped(comp, capsys):
    scored_run("a", {"f0": 0.6, "f1": 0.6})
    st = Store(config.load())
    other = st.comp.log_dir / "someone@elsewhere.jsonl"
    run_id = st.resolve("r1")
    st.close()
    other.write_text(json.dumps({"at": "2026-10-24T00:00:00+00:00", "by": "x", "t": "gadgets", "op": "ins", "row": {}}) + "\n"
                     + json.dumps({"at": "2026-10-24T00:00:00+00:00", "by": "x", "t": "notes", "op": "ins",
                                   "row": {"run_id": run_id, "ts": "2026-10-24T00:00:00+00:00", "author": "x",
                                           "text": "hi", "mood": "good"}}) + "\n"
                     + json.dumps({"at": "2026-10-24T00:00:00+00:00", "by": "x", "t": "runs", "op": "upd",
                                   "set": {"status": "failed"}, "where": {"colour": "red"}}) + "\n")
    st = Store(config.load())
    assert [n["text"] for n in st.notes(run_id)] == ["hi"] and st.get_run(run_id)["status"] == "done"


@pytest.fixture
def machines(comp, tmp_path, monkeypatch):
    """Two clones of the repo, each its own store and machine name, and a shared folder (fake rclone)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "rclone").write_text(f"#!/bin/sh\nexec {sys.executable} {Path(__file__).parent / 'fake_rclone.py'} \"$@\"\n")
    (bin_dir / "rclone").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_RCLONE_ROOT", str(tmp_path / "cloud"))
    from kx import sandbox
    sandbox.MACHINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    b = tmp_path / "b" / "demo"
    git(tmp_path, "clone", "-q", str(comp), str(b))
    data = b / ".kx" / "store" / "data"
    data.mkdir(parents=True)
    for f in (comp / ".kx" / "store" / "data").iterdir():
        (data / f.name).write_text(f.read_text())

    def on(name):
        monkeypatch.chdir(comp if name == "a" else b)
        monkeypatch.setattr(socket, "gethostname", lambda: f"host-{name}")
        sandbox.MACHINE_FILE.write_text(f"share: fake:kx\nuser: me\nname: {name}\n")
    on("a")
    return on


def runs(capsys):
    return {r["run"]: r for r in run_kx("runs", "-n", "100", capsys=capsys)[1]}


def test_two_machines_share_through_sync(machines, capsys):
    t = tasks.add(Store(config.load()), "try a bigger model")
    with kx.run("on a", config={"seed": 0}) as ra:
        ra.step(1, loss=0.5)
        ra.score(0.7, P, breakdown={"f0": 0.7, "f1": 0.7})
    code, res = run_kx("sync", "--ledger", capsys=capsys)
    assert code == 0 and res["ledger"]["share"] == "fake:kx/demo"
    machines("b")
    code, res = run_kx("sync", "--ledger", capsys=capsys)
    assert res["ledger"]["pulled"] == ["me@a"] and res["ledger"]["records_taken"] == ["me@a"]
    assert set(runs(capsys)) == {ra.name}
    st = Store(config.load())
    assert "loss" in (st.run_dir(ra.name) / "metrics.jsonl").read_text()  # its curves came along
    tasks.start(st, t["id"])
    with kx.run("on b, at the same time as a's second", config={"seed": 1}) as rb:
        rb.score(0.75, P, breakdown={"f0": 0.75, "f1": 0.75})
    machines("a")
    with kx.run("a's second", config={"seed": 2}) as ra2:
        ra2.score(0.72, P, breakdown={"f0": 0.72, "f1": 0.72})
    assert ra2.name.startswith("r0002-") and rb.name.startswith("r0002-") and ra2.name != rb.name  # the same number
    run_kx("sync", "--ledger", capsys=capsys)
    machines("b")
    run_kx("sync", "--ledger", capsys=capsys)
    machines("a")
    run_kx("sync", "--ledger", capsys=capsys)
    st = Store(config.load())
    assert set(runs(capsys)) == {ra.name, ra2.name, rb.name}
    capsys.readouterr()
    assert st.resolve("r2") == ra2.name  # this machine's r2, said aloud: b's r2 exists too
    assert rb.name in capsys.readouterr().err
    assert st.resolve(rb.name) == rb.name
    assert tasks.get(st, t["id"])["status"] == "active" and tasks.get(st, t["id"])["owner"]
    machines("b")
    assert contents(Store(config.load()))["runs"] != [] and set(runs(capsys)) == {ra.name, ra2.name, rb.name}


def test_two_claims_of_one_task_the_earlier_wins(machines, capsys):
    t = tasks.add(Store(config.load()), "only one may do it")
    run_kx("sync", "--ledger", capsys=capsys)
    machines("b")
    run_kx("sync", "--ledger", capsys=capsys)
    with Store(config.load()) as st:
        tasks.start(st, t["id"], owner="bob")
    machines("a")
    import time
    time.sleep(1.1)  # later by the clock
    with Store(config.load()) as st:
        tasks.start(st, t["id"], owner="alice")
    run_kx("sync", "--ledger", capsys=capsys)
    machines("b")
    run_kx("sync", "--ledger", capsys=capsys)
    machines("a")
    code, res = run_kx("sync", "--ledger", capsys=capsys)
    assert [(x["task"], x["was"], x["now"]) for x in res["ledger"]["lost_claims"]] == [(t["id"], "alice@a", "bob@b")]
    assert any(n["kind"] == "lost_claim" for n in run_kx("status", capsys=capsys)[1]["needs_you"])  # a is told
    for m in ("a", "b"):
        machines(m)
        assert tasks.get(Store(config.load()), t["id"])["owner"] == "bob@b", m


def test_the_machine_that_lost_a_claim_cannot_finish_the_task(machines, capsys):
    t = tasks.add(Store(config.load()), "only one may do it")
    run_kx("sync", "--ledger", capsys=capsys)
    machines("b")
    run_kx("sync", "--ledger", capsys=capsys)
    with Store(config.load()) as st:
        tasks.start(st, t["id"])
    machines("a")
    import time
    time.sleep(1.1)
    with Store(config.load()) as st:  # a claims and finishes it before it hears of b's claim
        tasks.start(st, t["id"])
        tasks.finish(st, t["id"], "negative", "done on a")
    run_kx("sync", "--ledger", capsys=capsys)
    machines("b")
    run_kx("sync", "--ledger", capsys=capsys)
    for m in ("a", "b"):
        machines(m)
        run_kx("sync", "--ledger", capsys=capsys)
        got = tasks.get(Store(config.load()), t["id"])
        assert (got["owner"], got["status"], got["outcome"]) == ("me@b", "active", None), m


def test_the_same_worker_name_on_two_machines_is_two_owners(machines, capsys):
    t = tasks.add(Store(config.load()), "a's codex-1 has it")
    with Store(config.load()) as st:
        tasks.start(st, t["id"], owner="codex-1")
    run_kx("sync", "--ledger", capsys=capsys)
    machines("b")
    run_kx("sync", "--ledger", capsys=capsys)
    with Store(config.load()) as st:
        assert tasks.get(st, t["id"])["owner"] == "codex-1@a"
        with pytest.raises(kx.KxError, match="active with codex-1@a"):
            tasks.start(st, t["id"], owner="codex-1")
        assert tasks.current(st, actor="codex-1") is None  # b's codex-1 does not link its runs to a's task


OLD = [
    "CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT NOT NULL, finished TEXT, status TEXT NOT NULL, "
    "kind TEXT NOT NULL DEFAULT 'experiment', hypothesis TEXT, config TEXT, command TEXT, author TEXT, task INTEGER, "
    "parent INTEGER, grp TEXT, seed INTEGER, git_sha TEXT, git_branch TEXT, git_dirty INTEGER, worktree TEXT, patch TEXT, "
    "host TEXT, pid INTEGER, pid_start TEXT, heartbeat TEXT, env TEXT, gpus INTEGER, metrics TEXT, error TEXT, uid TEXT)",
    "CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, ts TEXT NOT NULL, author TEXT, text TEXT NOT NULL)",
    "CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT, priority INTEGER NOT NULL DEFAULT 0, "
    "depends TEXT, status TEXT NOT NULL DEFAULT 'open', owner TEXT, waiting_on TEXT, result TEXT, outcome TEXT, branch TEXT, "
    "worktree TEXT, created TEXT NOT NULL, created_by TEXT, updated TEXT)",
    "CREATE TABLE findings (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, author TEXT, text TEXT NOT NULL, "
    "status TEXT NOT NULL DEFAULT 'open', evidence TEXT, task INTEGER, reviewed_by TEXT, reviewed TEXT, review_note TEXT, "
    "superseded_by INTEGER)",
    "CREATE TABLE requests (id INTEGER PRIMARY KEY)",
]


def old_ledger(store: Path):
    store.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(store / "ledger.sqlite")
    for s in OLD:
        db.execute(s)
    db.execute("INSERT INTO runs (id, created, status, hypothesis, author, task, host) VALUES "
               "(1, '2026-01-01T00:00:00+00:00', 'done', 'old one', 'me', 2, 'old-host')")
    db.execute("INSERT INTO runs (id, created, status, hypothesis, author, parent, host) VALUES "
               "(2, '2026-01-02T00:00:00+00:00', 'failed', 'old two', 'me', 1, 'old-host')")
    db.execute("INSERT INTO notes (run_id, ts, author, text) VALUES (1, '2026-01-01T01:00:00+00:00', 'me', 'r0001 is fine')")
    db.execute("INSERT INTO tasks (id, title, created, status, waiting_on) VALUES (1, 'first', '2026-01-01T00:00:00+00:00', "
               "'waiting', 'run:r2')")
    db.execute("INSERT INTO tasks (id, title, created, depends) VALUES (2, 'second', '2026-01-01T00:00:00+00:00', '[1]')")
    db.execute("INSERT INTO findings (id, ts, text, evidence, task) VALUES (1, '2026-01-03T00:00:00+00:00', 'it works', '[1, 2]', 1)")
    db.commit()
    db.close()
    (store / "runs" / "r0001").mkdir(parents=True)
    (store / "runs" / "r0001" / "metrics.jsonl").write_text('{"step": 1, "loss": 0.3}\n')
    (store / "runs" / "r0001" / "model.pt").write_text("weights")


def test_an_old_ledger_comes_in_once_with_its_numbers(comp, tmp_path, capsys):
    old_ledger(tmp_path / "old")
    code, res = run_kx("import", str(tmp_path / "old"), capsys=capsys)
    assert code == 0 and res["ids"] == {"runs": 2, "tasks": 2, "findings": 1} and not res["in_place"]
    st = Store(config.load())
    r1, r2, t1, t2 = st.resolve("r1"), st.resolve("r2"), st.resolve("T1", "tasks"), st.resolve("T2", "tasks")
    assert r1.startswith("r0001-") and st.get_run(r1)["task"] == t2 and st.get_run(r2)["parent"] == r1
    assert [n["text"] for n in st.notes(r1)] == ["r0001 is fine"]
    assert tasks.get(st, t1)["waiting_on"] == f"run:{r2}" and tasks.get(st, t2)["depends"] == [t1]
    assert tasks.waiting_resolved(st, tasks.get(st, t1)) == f"{r2} is failed"
    f = st.one("SELECT * FROM findings")
    assert json.loads(f["evidence"]) == [r1, r2] and f["task"] == t1
    d = st.run_dir(r1)
    assert (d / "metrics.jsonl").exists() and not (d / "model.pt").exists()  # kx's files, not the outputs
    code, err = run_kx("import", str(tmp_path / "old"), capsys=capsys)
    assert code == 1 and "already imported" in err["error"]


def test_this_stores_old_ledger_is_imported_in_place(comp, capsys):
    store = comp / ".kx" / "store"
    old_ledger(store)
    code, res = run_kx("import", capsys=capsys)
    assert code == 0 and res["in_place"] and res["run_folders"] == 1
    st = Store(config.load())
    r1 = st.resolve("r1")
    assert (st.run_dir(r1) / "model.pt").exists() and not (store / "runs" / "r0001").exists()
    names = {r["id"] for r in st.all("SELECT id FROM runs")}
    for p in store.glob("cache.sqlite*"):  # the same names when the cache is built again
        p.unlink()
    assert {r["id"] for r in Store(config.load()).all("SELECT id FROM runs")} == names


def test_export_and_import_carry_the_history(comp, tmp_path, capsys, monkeypatch):
    with kx.run("exported", config={"seed": 0}) as r:
        r.step(1, loss=0.4)
        r.score(0.8, P, breakdown={"f0": 0.8, "f1": 0.8})
    code, res = run_kx("export", str(tmp_path / "h.tar.gz"), capsys=capsys)
    assert code == 0 and res["logs"] == 1
    other = tmp_path / "other" / "demo"
    git(tmp_path, "clone", "-q", str(comp), str(other))
    monkeypatch.chdir(other)
    from kx import sandbox
    sandbox.MACHINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    sandbox.MACHINE_FILE.write_text("name: elsewhere\n")
    code, res = run_kx("import", str(tmp_path / "h.tar.gz"), capsys=capsys)
    assert code == 0 and len(res["logs"]) == 1 and res["runs"] == 1
    st = Store(config.load())
    assert st.get_run(r.name)["hypothesis"] == "exported" and "loss" in (st.run_dir(r.name) / "metrics.jsonl").read_text()


def test_a_kaggle_submission_two_machines_saw_is_one(machines, fake_kaggle, capsys):
    s = submittable(None)
    capsys.readouterr()
    assert run_kx("submit", s.name, "-m", "first", "--yes", capsys=capsys)[0] == 0
    machines("b")
    run_kx("sync", capsys=capsys)  # b sees it on Kaggle before a's log says who made it: external for now
    st = Store(config.load())
    assert [r["kind"] for r in st.all("SELECT kind FROM submissions")] == ["external"]
    machines("a")
    run_kx("sync", capsys=capsys)
    machines("b")
    run_kx("sync", capsys=capsys)
    st = Store(config.load())
    assert [(r["kind"], r["run_id"]) for r in st.all("SELECT kind, run_id FROM submissions")] == [("file", s.name)]


def test_a_change_made_after_seeing_a_row_replays_after_it_whatever_the_clocks(machines, capsys, monkeypatch):
    t = tasks.add(Store(config.load()), "made on a")
    run_kx("sync", "--ledger", capsys=capsys)
    machines("b")
    run_kx("sync", "--ledger", capsys=capsys)
    import kx.ledger.store as store_mod
    real = store_mod.now
    monkeypatch.setattr(store_mod, "now", lambda: "2020-01-01T00:00:00+00:00")  # b's clock is years behind
    with Store(config.load()) as st:
        tasks.start(st, t["id"], owner="bob")
    monkeypatch.setattr(store_mod, "now", real)
    run_kx("sync", "--ledger", capsys=capsys)
    machines("a")
    run_kx("sync", "--ledger", capsys=capsys)
    assert tasks.get(Store(config.load()), t["id"])["owner"] == "bob@b"
    for p in Store(config.load()).comp.store.glob("cache.sqlite*"):
        p.unlink()
    assert tasks.get(Store(config.load()), t["id"])["owner"] == "bob@b"  # rebuilt from the logs: the same


def test_the_same_id_made_on_two_machines_stays_two_runs(machines, capsys, monkeypatch):
    from kx.ledger import names
    monkeypatch.setattr(names, "fresh", lambda used=(), rng=None: "fixed-name")
    with kx.run("a's") as ra:
        ra.score(0.6, P, breakdown={"f0": 0.6, "f1": 0.6})
    machines("b")
    with kx.run("b's") as rb:
        rb.score(0.9, P, breakdown={"f0": 0.9, "f1": 0.9})
    assert ra.name == rb.name == "r0001-fixed-name"
    run_kx("sync", "--ledger", capsys=capsys)
    machines("a")
    run_kx("sync", "--ledger", capsys=capsys)
    machines("b")
    run_kx("sync", "--ledger", capsys=capsys)
    for m, own, other in (("a", "a's", "b's"), ("b", "b's", "a's")):
        machines(m)
        st = Store(config.load())
        rows = {r["hypothesis"]: r["id"] for r in st.all("SELECT * FROM runs")}
        assert rows[own] == "r0001-fixed-name" and rows[other].startswith("r0001-fixed-name~"), m  # this machine's keeps it
        assert st.one("SELECT value FROM scores WHERE run_id=?", (rows["b's"],))["value"] == 0.9


def test_another_machines_remote_run_is_left_to_it(comp, capsys):
    from kx.remote import execute
    scored_run("mine", {"f0": 0.6, "f1": 0.6})
    st = Store(config.load())
    (st.comp.log_dir / "someone@elsewhere.jsonl").write_text(json.dumps(
        {"c": 1, "at": "2026-10-24T00:00:00+00:00", "by": "x", "t": "runs", "op": "ins",
         "row": {"id": "r0002-far-away", "n": 2, "name": "far-away", "writer": "someone@elsewhere",
                 "created": "2026-10-24T00:00:00+00:00", "status": "running", "kind": "experiment", "host": "gpu1"}}) + "\n")
    st.close()
    st = Store(config.load())
    d = st.run_dir("r0002-far-away")
    d.mkdir(parents=True)
    (d / "remote.json").write_text(json.dumps({"host": "gpu1", "launched": "2026-01-01T00:00:00+00:00", "dir": "/x"}))
    assert execute.running(st) == [] and execute.stale(st) == []
    code, err = run_kx("remote", "kill", "r2", capsys=capsys)
    assert code == 1 and "launched from someone@elsewhere" in err["error"]


def test_the_cache_keeps_this_machines_own_state_across_versions(comp, capsys):
    st = Store(config.load())
    st.x("INSERT INTO workers (id, agent, harness, started) VALUES ('w1', 'codex-1', 'codex', '2026-10-24T00:00:00+00:00')")
    st.set_setting("synced", "2026-10-24T00:00:00+00:00")
    st.db.execute("PRAGMA user_version=0")  # as if made by an older kx
    st.close()
    st = Store(config.load())
    assert st.one("SELECT agent FROM workers")["agent"] == "codex-1" and st.setting("synced")
    st.db.execute("PRAGMA user_version=99")  # a newer kx's: not dropped
    st.close()
    code, err = run_kx("runs", capsys=capsys)
    assert code == 1 and "newer kx" in err["error"]


def test_a_submission_file_recorded_elsewhere_is_made_here(comp, capsys):
    from kx.kaggle import artifacts
    r = submittable(None)
    st = Store(config.load())
    art = artifacts.build(st, r.name)
    p = artifacts.file(st, art)
    p.chmod(0o644)
    p.unlink()  # its file not here (made on another machine)
    again = artifacts.build(st, r.name)
    assert again["path"] == art["path"] and artifacts.file(st, again).read_text() == "id,y\n1,0.5\n"


def test_names_of_three_words_are_found(comp, capsys, monkeypatch):
    from kx.ledger import names
    monkeypatch.setattr(names, "fresh", lambda used=(), rng=None: "calm-quiet-otter")
    r = scored_run("a", {"f0": 0.6, "f1": 0.6})
    st = Store(config.load())
    assert r.name == "r0001-calm-quiet-otter" and st.resolve("calm-quiet-otter") == r.name == st.resolve("calm-quiet-otter-1")


def cloud_log(tmp_path, writer):
    return tmp_path / "cloud" / "fake" / "kx" / "demo" / "log" / f"{writer}.jsonl"


def test_a_machine_that_lost_its_store_gets_its_history_back(machines, tmp_path, capsys):
    for i in range(3):
        with kx.run(f"a{i}", config={"seed": i}) as r:
            r.step(1, loss=0.5)
            r.score(0.7, P)
    first = r.name
    run_kx("sync", "--ledger", capsys=capsys)
    machines("b")
    run_kx("sync", "--ledger", capsys=capsys)
    machines("a")  # a's store is lost (a new clone, rm -rf .kx), and kx is used again before a sync
    st = Store(config.load())
    store_dir = st.comp.store
    st.close()
    for d in ("log", "runs"):
        shutil.rmtree(store_dir / d)
    st.comp.db_path.unlink()
    with kx.run("a again", config={"seed": 9}) as r:
        r.score(0.7, P)
    code, res = run_kx("sync", "--ledger", capsys=capsys)
    assert code == 0 and "kept as me@a+fork-" in res["ledger"]["restored"][0]
    assert len(runs(capsys)) == 4
    assert "loss" in (Store(config.load()).run_dir(first) / "metrics.jsonl").read_text()  # its records came back
    machines("b")
    run_kx("sync", "--ledger", capsys=capsys)
    assert len(runs(capsys)) == 4


def test_a_shorter_copy_of_a_log_never_replaces_a_longer_one(machines, tmp_path, capsys):
    for i in range(2):
        with kx.run(f"a{i}", config={"seed": i}) as r:
            r.score(0.7, P)
    run_kx("sync", "--ledger", capsys=capsys)
    machines("b")
    run_kx("sync", "--ledger", capsys=capsys)
    shared = cloud_log(tmp_path, "me@a")
    full = shared.read_bytes()
    shared.write_bytes(full.split(b"\n", 1)[0] + b"\n")  # a stale upload
    run_kx("sync", "--ledger", capsys=capsys)
    assert len(runs(capsys)) == 2
    machines("a")
    code, res = run_kx("sync", "--ledger", capsys=capsys)
    assert code == 0 and not res["ledger"]["restored"] and shared.read_bytes() == full  # a puts it back


def test_run_records_go_one_run_at_a_time_when_they_change(machines, tmp_path, capsys):
    with kx.run("one", config={"seed": 0}) as r1:
        r1.step(1, loss=0.5)
    with kx.run("two", config={"seed": 1}) as r2:
        r2.step(1, loss=0.4)
    code, res = run_kx("sync", "--ledger", capsys=capsys)
    assert res["ledger"]["records_sent"] == 2
    assert sorted(p.name for p in (tmp_path / "cloud" / "fake" / "kx" / "demo" / "records" / "me@a").iterdir()) == \
        sorted([f"{r1.name}.tar.gz", f"{r2.name}.tar.gz"])
    assert run_kx("sync", "--ledger", capsys=capsys)[1]["ledger"]["records_sent"] == 0
    st = Store(config.load())
    with open(st.run_dir(r2.name) / "metrics.jsonl", "a") as f:
        f.write('{"step": 2, "loss": 0.3}\n')
    assert run_kx("sync", "--ledger", capsys=capsys)[1]["ledger"]["records_sent"] == 1
    machines("b")
    run_kx("sync", "--ledger", capsys=capsys)
    assert "0.3" in (Store(config.load()).run_dir(r2.name) / "metrics.jsonl").read_text()


def test_new_entries_of_another_machine_apply_without_a_rebuild(machines, capsys):
    with kx.run("one", config={"seed": 0}) as r:
        r.score(0.7, P)
    run_kx("sync", "--ledger", capsys=capsys)
    machines("b")
    run_kx("sync", "--ledger", capsys=capsys)
    machines("a")
    with kx.run("two", config={"seed": 1}) as r:
        r.score(0.8, P)
    run_kx("sync", "--ledger", capsys=capsys)
    machines("b")
    code, res = run_kx("sync", "--ledger", capsys=capsys)
    assert res["ledger"]["cache"] == "applied" and len(runs(capsys)) == 2
    before = contents(Store(config.load()))
    st = Store(config.load())
    with st.transaction():
        st.rebuild()
    assert contents(st) == before  # the same as replaying everything


def test_each_budget_counts_what_it_is_paid_from(machines, capsys):
    from kx import sandbox
    from kx.guard import actors, budgets
    with Store(config.load()) as st:
        me = actors.current()
        for metric, amount in (("submissions", 1), ("gpu_hours", 2), ("tokens", 100), ("kaggle_gpu_hours", 3)):
            budgets.charge(st, me, metric, amount)
    run_kx("sync", "--ledger", capsys=capsys)
    machines("b")
    run_kx("sync", "--ledger", capsys=capsys)
    with Store(config.load()) as st:  # the same person on another machine
        assert [budgets.used(st, m, "day") for m in ("submissions", "gpu_hours", "tokens")] == [1, 0, 100]
    sandbox.MACHINE_FILE.write_text("share: fake:kx\nuser: bob\nname: b\n")  # someone else
    with Store(config.load()) as st:
        assert [budgets.used(st, m, "day") for m in ("submissions", "gpu_hours", "tokens", "kaggle_gpu_hours")] == [1, 0, 0, 0]
        st.comp.budgets["scope"] = {"tokens": "team"}
        assert budgets.used(st, "tokens", "day") == 100
        st.comp.budgets["tokens_per_day"] = 150
        with pytest.raises(kx.KxError, match="team tokens_per_day"):
            budgets.check(st, actors.current(), {"tokens": 60})


def usage(st: Store, metric: str = "submissions") -> float:
    return st.one("SELECT COALESCE(SUM(amount), 0) FROM usage WHERE metric=?", (metric,))[0]


def test_a_log_restored_from_a_backup_counts_each_entry_once(machines, capsys):
    from kx.guard import actors, budgets
    with kx.run("one") as r:
        r.score(0.7, P)
    with Store(config.load()) as st:
        budgets.charge(st, actors.current(), "submissions", 1)
        st.add_note(r.name, "n1")
        log, store_dir = st.log_path, st.comp.store
    backup = log.read_bytes()
    run_kx("sync", "--ledger", capsys=capsys)
    with Store(config.load()) as st:
        st.add_note(r.name, "n2")
    run_kx("sync", "--ledger", capsys=capsys)
    log.write_bytes(backup)  # an older copy of the store comes back, and is used before a sync
    for p in store_dir.glob("cache.sqlite*"):
        p.unlink()
    with Store(config.load()) as st:
        st.add_note(r.name, "n3")
    code, res = run_kx("sync", "--ledger", capsys=capsys)
    assert "kept as me@a+fork-" in res["ledger"]["restored"][0]
    for m in ("a", "b"):
        machines(m)
        run_kx("sync", "--ledger", capsys=capsys)
        with Store(config.load()) as st:  # the history both copies have is there once
            assert usage(st) == 1 and sorted(n["text"] for n in st.notes(r.name)) == ["n1", "n2", "n3"], m


def test_two_stores_under_one_name_are_refused_until_one_is_renamed(machines, capsys):
    import time
    from kx import sandbox
    from kx.guard import actors, budgets

    def on(where, name="box"):
        time.sleep(1.1)
        machines(where)
        sandbox.MACHINE_FILE.write_text(f"share: fake:kx\nuser: me\nname: {name}\n")  # two rented boxes, one host name
    on("a")
    with Store(config.load()) as st:
        budgets.charge(st, actors.current(), "submissions", 1)
    t = tasks.add(Store(config.load()), "for b")
    run_kx("sync", "--ledger", capsys=capsys)
    on("b")
    run_kx("sync", "--ledger", capsys=capsys)
    with Store(config.load()) as st:
        budgets.charge(st, actors.current(), "submissions", 1)
        tasks.start(st, t["id"], owner="codex-1")
    with kx.run("on b") as rb:
        rb.score(0.8, P)
    on("a")
    with Store(config.load()) as st:
        budgets.charge(st, actors.current(), "tokens", 5)
    run_kx("sync", "--ledger", capsys=capsys)
    on("b")
    code, res = run_kx("sync", "--ledger", capsys=capsys)
    assert "another machine writes as me@box" in res["ledger"]["error"]
    on("b", "box2")  # as told
    code, res = run_kx("sync", "--ledger", capsys=capsys)
    assert code == 0 and "moved to me@box2" in res["ledger"]["restored"][0]
    for m in ("a", "b"):
        on(m, "box" if m == "a" else "box2")
        run_kx("sync", "--ledger", capsys=capsys)
        with Store(config.load()) as st:
            assert usage(st) == 2 and usage(st, "tokens") == 5, m
            assert st.one("SELECT writer FROM runs WHERE id=?", (rb.name,))["writer"] == "me@box2", m
            assert tasks.get(st, t["id"])["owner"] == "codex-1@box2", m
    assert not (Store(config.load()).comp.store / "log" / "me@box.jsonl").read_text().count("on b")


def test_a_half_written_line_is_cut_off_even_when_the_cache_is_rebuilt(comp, capsys):
    with kx.run("one") as r:
        r.score(0.7, P)
    st = Store(config.load())
    log, store_dir = st.log_path, st.comp.store
    st.close()
    with open(log, "a") as f:
        f.write('{"c": 999, "at": "2026-10-24T00:00:01+00:00", "t": "no')  # a crash in the middle of a write
    for p in store_dir.glob("cache.sqlite*"):  # and the cache is dropped (an upgrade of kx)
        p.unlink()
    with Store(config.load()) as st:
        st.add_note(r.name, "after")
    for p in store_dir.glob("cache.sqlite*"):
        p.unlink()
    with Store(config.load()) as st:
        assert [n["text"] for n in st.notes(r.name)] == ["after"]


def test_a_line_another_machine_was_still_writing_is_read_once_it_is_whole(comp, capsys):
    with kx.run("one") as r:
        r.score(0.7, P)
    st = Store(config.load())
    other = st.comp.log_dir / "bob@elsewhere.jsonl"

    def line(c, text):
        return json.dumps({"c": c, "at": "2026-10-24T00:00:00+00:00", "by": "bob", "t": "notes", "op": "ins",
                           "row": {"run_id": r.name, "ts": "2026-10-24T00:00:00+00:00", "author": "bob", "text": text}})
    second = line(1000, "second") + "\n"
    other.write_text(line(999, "first") + "\n" + second[:20])  # copied while bob's kx was writing
    with st.transaction():
        st.rebuild()
    other.write_text(line(999, "first") + "\n" + second)
    st.catch_up()
    assert [n["text"] for n in st.notes(r.name)] == ["first", "second"]


def test_a_failed_write_leaves_nothing_in_the_log(comp, capsys, monkeypatch):
    with kx.run("one") as r:
        r.score(0.7, P)
    st = Store(config.load())
    before = st.log_path.read_bytes()

    def full(fd):
        raise OSError(28, "No space left on device")
    fsync = os.fsync
    monkeypatch.setattr(os, "fsync", full)
    with pytest.raises(OSError):
        st.add_note(r.name, "lost")
    monkeypatch.setattr(os, "fsync", fsync)
    assert st.log_path.read_bytes() == before
    assert Store(config.load()).notes(r.name) == []


def test_only_whole_lines_go_up(machines, tmp_path, capsys):
    with kx.run("one") as r:
        r.score(0.7, P)
    st = Store(config.load())
    with open(st.log_path, "a") as f:
        f.write('{"c": 999, "at": "2026-10-24T00:00:01+00:00", "t": "no')  # a writer that crashed
    st.close()
    run_kx("sync", "--ledger", capsys=capsys)
    assert cloud_log(tmp_path, "me@a").read_bytes().endswith(b"}\n")


def test_a_renamed_machine_still_sends_what_it_wrote_before(machines, capsys):
    from kx import sandbox
    t = tasks.add(Store(config.load()), "started before the rename")
    with kx.run("one") as r:
        r.score(0.7, P)
    run_kx("sync", "--ledger", capsys=capsys)
    with kx.run("two, not synced yet") as r2:
        r2.score(0.7, P)
    with Store(config.load()) as st:
        tasks.start(st, t["id"])
    sandbox.MACHINE_FILE.write_text("share: fake:kx\nuser: me\nname: a2\n")
    with Store(config.load()) as st:  # what it held under its old name is still its own
        assert tasks.current(st, actor="me") == t["id"]
        tasks.finish(st, t["id"], "positive", "done after the rename")
    run_kx("sync", "--ledger", capsys=capsys)
    machines("b")
    run_kx("sync", "--ledger", capsys=capsys)
    assert r2.name in runs(capsys)
    assert tasks.get(Store(config.load()), t["id"])["status"] == "done"


def test_a_note_on_a_run_whose_id_two_machines_made_reaches_that_run(machines, capsys, monkeypatch):
    from kx.ledger import names
    monkeypatch.setattr(names, "fresh", lambda used=(), rng=None: "fixed-name")
    with kx.run("a's") as ra:
        ra.score(0.6, P)
    machines("b")
    with kx.run("b's") as rb:
        rb.score(0.9, P)
    for m in ("b", "a", "b"):
        machines(m)
        run_kx("sync", "--ledger", capsys=capsys)
    with Store(config.load()) as st:  # on b, a's run shows as r0001-fixed-name~<a>
        a_here = st.one("SELECT id FROM runs WHERE hypothesis=\"a's\"")["id"]
        assert a_here != "r0001-fixed-name" and st.resolve(a_here.split("-", 1)[1]) == a_here  # fixed-name~<a>
        st.add_note(a_here, "b on a's run")
        st.add_note("r0001-fixed-name", "b on its own run")
    run_kx("sync", "--ledger", capsys=capsys)
    machines("a")
    run_kx("sync", "--ledger", capsys=capsys)
    with Store(config.load()) as st:
        assert [n["text"] for n in st.notes("r0001-fixed-name")] == ["b on a's run"]
        b_here = st.one("SELECT id FROM runs WHERE hypothesis=\"b's\"")["id"]
        assert [n["text"] for n in st.notes(b_here)] == ["b on its own run"]
        assert st.resolve(a_here) == "r0001-fixed-name"  # what b calls it finds it here too
        with st.transaction():
            st.rebuild()
        assert [n["text"] for n in st.notes("r0001-fixed-name")] == ["b on a's run"]


def test_a_worker_keeps_its_task_across_a_rebuild(machines, capsys, monkeypatch):
    from kx.ledger import names
    monkeypatch.setattr(names, "fresh", lambda used=(), rng=None: "fixed-name")
    machines("b")
    tasks.add(Store(config.load()), "b's")
    run_kx("sync", "--ledger", capsys=capsys)
    machines("a")
    t = tasks.add(Store(config.load()), "a's")  # before a sees b's: the same id
    with Store(config.load()) as st:
        st.x("INSERT INTO workers (id, agent, harness, task, started) VALUES ('w1', 'codex-1', 'codex', ?, 'now')",
             (t["id"],))
    run_kx("sync", "--ledger", capsys=capsys)
    st = Store(config.load())
    for _ in range(2):
        with st.transaction():
            st.rebuild()
        assert st.one("SELECT task FROM workers WHERE id='w1'")["task"] == t["id"]
        assert tasks.get(st, t["id"])["title"] == "a's"


def test_a_task_branch_of_another_machine_is_not_this_machines_task(comp, capsys):
    st = Store(config.load())
    tasks.add(st, "one")
    t2 = tasks.add(st, "two")
    git(comp, "checkout", "-q", "-b", "task/T2-quiet-otter-other-machines-task")  # its task not synced here yet
    assert tasks.current(st) is None
    git(comp, "checkout", "-q", "-b", f"task/{t2['id']}-two")
    assert tasks.current(st) == t2["id"]


def test_a_web_submission_two_machines_took_at_once_is_one(machines, fake_kaggle, capsys):
    from kx.kaggle import submit
    fake_kaggle.remote.append({"ref": "777", "description": "made on the website", "status": "COMPLETE",
                               "publicScore": "0.5", "date": "2026-10-15T00:00:00"})
    with Store(config.load()) as st:
        submit.sync_submissions(st, games=False)
    machines("b")
    with Store(config.load()) as st:  # before a's log arrives
        submit.sync_submissions(st, games=False)
    for m in ("a", "b", "a", "b"):
        machines(m)
        run_kx("sync", "--ledger", capsys=capsys)
        rows = Store(config.load()).all("SELECT id FROM submissions")
        assert len(rows) == 1, m


def test_of_two_claims_the_earlier_by_the_clock_wins(machines, capsys):
    import time
    t = tasks.add(Store(config.load()), "only one")
    run_kx("sync", "--ledger", capsys=capsys)
    machines("b")
    run_kx("sync", "--ledger", capsys=capsys)
    machines("a")
    with Store(config.load()) as st:
        for i in range(30):  # a is busy: its logical clock runs ahead
            tasks.note(st, t["id"], f"thinking {i}")
        tasks.start(st, t["id"], owner="alice")
    time.sleep(1.1)
    machines("b")
    with Store(config.load()) as st:
        tasks.start(st, t["id"], owner="bob")  # later, without having seen a's claim
    run_kx("sync", "--ledger", capsys=capsys)
    code, res = run_kx("sync", "--ledger", capsys=capsys)
    machines("a")
    run_kx("sync", "--ledger", capsys=capsys)
    machines("b")
    code, res = run_kx("sync", "--ledger", capsys=capsys)
    for m in ("a", "b"):
        machines(m)
        assert tasks.get(Store(config.load()), t["id"])["owner"] == "alice@a", m
