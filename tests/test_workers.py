import json
import sys
from pathlib import Path

from kx.workers import runner
from kx.ledger.store import open_store

from conftest import commit, run_kx

FAKE = r'''
import json, sys, time, subprocess
n = int(sys.argv[1])
for i in range(n):
    print(json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 1000, "output_tokens": 100},
                      "content": [{"type": "tool_use", "name": "Bash", "input": {"command": f"step {i}"}}]}}), flush=True)
    time.sleep(0.05)
if sys.argv[2] == "finish":
    subprocess.run(["kx", "task", "done", __import__("os").environ["KX_TASK"], "--result", "negative", "--outcome", "nope"])
print(json.dumps({"type": "result", "result": "all done", "num_turns": n}), flush=True)
'''


def harness_dir(comp) -> Path:
    """Where a fake harness lives: inside the competition (git-ignored .kx/), the only part of a pytest tmp dir the
    worker sandbox (its own /tmp) shows."""
    d = comp / ".kx"
    d.mkdir(exist_ok=True)
    return d


def setup(comp, steps: int, mode: str = "quit", caps: str = ""):
    ws = harness_dir(comp)
    (ws / "fake_harness.py").write_text(FAKE)
    argv = json.dumps([sys.executable, str(ws / "fake_harness.py"), str(steps), mode, "{prompt}"])
    y = comp / "comp.yaml"
    y.write_text(y.read_text() + f"workers: {{fake-1: {{harness: claude, model: sonnet, argv: {argv}}}}}\n" + caps)
    commit(comp, "workers")


def test_spawn_foreground_counts_tokens_and_reopens(comp, capsys, monkeypatch):
    setup(comp, 5)
    monkeypatch.setattr(runner, "UPDATE_EVERY_S", 0)
    code, t = run_kx("task", "add", "explore", capsys=capsys)
    code, res = run_kx("spawn", t['id'], "-w", "fake-1", "--foreground", capsys=capsys)
    assert code == 0 and res["code"] == 0
    with open_store() as st:
        w = dict(st.one("SELECT * FROM workers"))
        assert w["tokens_in"] == 5000 and w["tokens_out"] == 500 and w["result"] == "all done"
        assert st.one("SELECT SUM(amount) FROM usage WHERE metric='tokens'")[0] == 5500
        task = st.one("SELECT * FROM tasks WHERE id=?", (t["id"],))
        assert task["status"] == "open" and task["worktree"]
        note = st.one("SELECT text FROM task_notes WHERE task=? ORDER BY id DESC", (t["id"],))["text"]
        assert "without kx task done" in note and "all done" in note
    assert (comp / ".kx" / "worktrees").is_dir()


def test_token_budget_stops_worker(comp, capsys, monkeypatch):
    setup(comp, 200, caps="budgets: {workers: {fake-1: {tokens_per_day: 3000}}}\n")
    monkeypatch.setattr(runner, "UPDATE_EVERY_S", 0)
    code, t = run_kx("task", "add", "burn tokens", capsys=capsys)
    code, res = run_kx("spawn", t['id'], "-w", "fake-1", "--foreground", capsys=capsys)
    with open_store() as st:
        w = dict(st.one("SELECT * FROM workers"))
        assert w["stop"].startswith("budget") and w["turns"] < 200
    code, err = run_kx("spawn", t['id'], "-w", "fake-1", "--foreground", capsys=capsys)
    assert code == 4 and "tokens_per_day" in err["error"]


def test_worker_that_finishes_its_task(comp, capsys, monkeypatch):
    setup(comp, 2, mode="finish")
    code, t = run_kx("task", "add", "quick", capsys=capsys)
    code, res = run_kx("spawn", t['id'], "-w", "fake-1", "--foreground", capsys=capsys)
    with open_store() as st:
        task = st.one("SELECT * FROM tasks WHERE id=?", (t["id"],))
        assert task["status"] == "done" and task["outcome"] == "nope"


def test_spawn_refusals(comp, capsys, monkeypatch):
    setup(comp, 1)
    code, t = run_kx("task", "add", "x", capsys=capsys)
    code, err = run_kx("spawn", t['id'], "-w", "nobody", capsys=capsys)
    assert code == 1 and "fake-1" in err["fix"]
    monkeypatch.setenv("KX_AGENT", "fake-1")
    code, err = run_kx("spawn", t['id'], "-w", "fake-1", capsys=capsys)
    assert code == 1 and "person driving" in err["error"]
    monkeypatch.delenv("KX_AGENT")
    code, dry = run_kx("spawn", t['id'], "-w", "fake-1", "--dry-run", capsys=capsys)
    assert sys.executable in dry["argv"] and t["id"] in dry["argv"][-1]


def test_a_worker_writes_only_the_competition(comp, capsys, tmp_path):
    """Workers run in kx's sandbox: the competition is writable, the home is not, /tmp is their own."""
    from kx import sandbox
    import pytest
    if not sandbox.available():
        pytest.skip("bubblewrap unavailable")
    probe = Path.home() / f"kx-sandbox-probe-{tmp_path.name}"
    tmp_mark = Path("/tmp") / f"kx-sandbox-probe-{tmp_path.name}"
    script = (f"import pathlib\n"
              f"pathlib.Path({str(comp / 'inside.txt')!r}).write_text('ok')\n"
              f"pathlib.Path({str(tmp_mark)!r}).write_text('private')\n"
              f"try:\n    pathlib.Path({str(probe)!r}).write_text('escaped')\nexcept OSError:\n    pass\n")
    ws = harness_dir(comp)
    (ws / "probe_harness.py").write_text(script)
    argv = json.dumps([sys.executable, str(ws / "probe_harness.py"), "{prompt}"])
    y = comp / "comp.yaml"
    y.write_text(y.read_text() + f"workers: {{fake-1: {{harness: claude, model: sonnet, argv: {argv}}}}}\n")
    commit(comp, "workers")
    code, t = run_kx("task", "add", "probe", capsys=capsys)
    run_kx("spawn", t['id'], "-w", "fake-1", "--foreground", capsys=capsys)
    try:
        assert (comp / "inside.txt").read_text() == "ok"
        assert not probe.exists() and not tmp_mark.exists()
    finally:
        probe.unlink(missing_ok=True)
        tmp_mark.unlink(missing_ok=True)
