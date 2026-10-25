"""kind: mailbox: a host kx reaches only through shared storage, where an agent there takes runs and leaves results
(rclone faked by fake_rclone.py: remotes are directories)."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kx.cli.main import main as kx_main
from kx.ledger import config
from kx.ledger.store import Store
from kx.remote import hosts, mailbox

from conftest import commit, run_kx
from test_remote import TRAIN

AGENT = Path(__file__).parents[1] / "src/kx/remote/agent.py"


@pytest.fixture
def mbox(comp, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "rclone").write_text(f"#!/bin/sh\nexec {sys.executable} {Path(__file__).parent / 'fake_rclone.py'} \"$@\"\n")
    (bin_dir / "rclone").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_RCLONE_ROOT", str(tmp_path / "cloud"))
    monkeypatch.setattr(hosts.Transport, "POLL_S", (0.1,))
    monkeypatch.setattr(mailbox.Transport, "POLL_S", (0.1,))
    far = tmp_path / "far"
    with open(comp / "comp.yaml", "a") as f:
        f.write(f"hosts:\n  mb: {{kind: mailbox, mailbox: 'fake:kx', python: {sys.executable}, dir: {far}}}\n")
    (comp / "train.py").write_text(TRAIN)
    commit(comp, "train")
    return far


@pytest.fixture
def agent(mbox):
    p = subprocess.Popen([sys.executable, str(AGENT), "mb", "--mailbox", "fake:kx", "--dir", str(mbox), "--every", "0.2"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    yield p
    p.terminate()
    p.wait(10)


def test_a_mailbox_run_is_recorded_like_any_remote_run(agent, mbox, comp, capsys):
    code = kx_main(["exec", "--json", "--on", "mb", "--", "python", "train.py"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["exit"] == 0
    code, show = run_kx("show", out["run"], capsys=capsys)
    assert show["status"] == "done" and show["host"] == "mb" and show["scores"][0]["value"] == 0.8
    assert any("trained on kaggle:" in n["text"] for n in show["notes"])
    d = Store(config.load()).run_dir(out["run"])
    assert (d / "submission.csv").read_text().startswith("id,y") and "done training" in (d / "stdout.log").read_text()
    assert json.loads((d / "remote.json").read_text())["kind"] == "mailbox"
    assert not list((Path(os.environ["FAKE_RCLONE_ROOT"]) / "fake/kx/_hosts/mb/jobs").iterdir())  # taken
    code, chk = run_kx("remote", "check", "mb", capsys=capsys)
    assert all(c["ok"] for c in chk["checks"] if not c["check"].startswith("disk")), chk  # this disk may be full
    assert any(c["check"].startswith("agent on ") and c["ok"] for c in chk["checks"])


def test_kill_through_the_mailbox(agent, mbox, comp, capsys):
    code = kx_main(["exec", "--json", "--on", "mb", "--detach", "--", "python", "-c", "import time; time.sleep(60)"])
    out = json.loads(capsys.readouterr().out)
    pid = mbox / "demo" / "runs" / out["run"] / "out" / ".remote" / "pid"
    for _ in range(200):
        if pid.exists():
            break
        time.sleep(0.05)
    code, res = run_kx("remote", "kill", out["run"], capsys=capsys)
    assert code == 0 and res["killed"]
    code, show = run_kx("show", out["run"], capsys=capsys)
    assert show["status"] == "failed" and "killed" in show["error"]


def test_without_an_agent_the_run_waits_and_check_says_why(mbox, comp, capsys):
    code, chk = run_kx("remote", "check", "mb", capsys=capsys)
    assert not chk["ok"] and any("agent.py mb --mailbox fake:kx" in (c["fix"] or "") for c in chk["checks"])
    code = kx_main(["exec", "--json", "--on", "mb", "--detach", "--", "python", "train.py"])
    out = json.loads(capsys.readouterr().out)
    code, items = run_kx("remote", "sync", out["run"], capsys=capsys)
    assert items[0]["state"] == "unreachable"
    subprocess.run([sys.executable, str(AGENT), "mb", "--mailbox", "fake:kx", "--dir", str(mbox), "--once"], check=True,
                   capture_output=True)  # an agent comes along: it takes the job
    assert (mbox / "demo" / "runs" / out["run"] / "out" / ".remote" / "pid").exists() or \
        (mbox / "demo" / "runs" / out["run"] / "out" / ".remote" / "exit").exists()


def test_kx_sources_travel_through_the_mailbox_and_uv_builds_the_env(mbox, comp, capsys, tmp_path):
    """No `python:`: like a fresh machine, the agent gets kx from the mailbox and the run's env is built with uv."""
    import shutil
    if shutil.which("uv") is None:
        pytest.skip("uv unavailable")
    text = (comp / "comp.yaml").read_text().replace(f"python: {sys.executable}, ", "")
    (comp / "comp.yaml").write_text(text)
    commit(comp, "no python")
    code, setup = run_kx("remote", "setup", "mb", capsys=capsys)
    assert code == 0 and "agent.py mb" in setup["start"]
    cloud = Path(os.environ["FAKE_RCLONE_ROOT"]) / "fake" / "kx"
    import tarfile
    with tarfile.open(cloud / "_kx" / "kx.tar.gz") as tf:  # one archive, without caches
        names = tf.getnames()
    assert "src/kx/remote/agent.py" in names and not any("__pycache__" in n for n in names)
    far_kx = mbox / "kx"  # what the start command does: the agent from the mailbox's copy of kx
    far_kx.mkdir(parents=True)  # the start command kx printed: rclone cat .../kx.tar.gz | tar -xz -C <dir>/kx
    subprocess.run(f"rclone cat fake:kx/_kx/kx.tar.gz | tar -xz -C {far_kx}", shell=True, check=True)
    p = subprocess.Popen([sys.executable, str(far_kx / "src/kx/remote/agent.py"), "mb", "--mailbox", "fake:kx", "--dir", str(mbox),
                          "--every", "0.2"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        code = kx_main(["exec", "--json", "--on", "mb", "--", "python", "train.py"])
        out = json.loads(capsys.readouterr().out)
    finally:
        p.terminate()
        p.wait(10)
    d = Store(config.load()).run_dir(out["run"])
    assert code == 0 and out["exit"] == 0, (d / "stderr.log").read_text()[-2000:]
    assert (far_kx / ".kx-hash").exists() and list((mbox / "demo" / "venvs").glob("*/.kx-ready"))


def test_the_mailbox_carries_kaggle_files_and_only_needed_parts(agent, mbox, comp, capsys):
    data = Store(config.load()).comp.store / "data"
    for name in ("feat", "other"):
        (data / "derived" / name).mkdir(parents=True)
        (data / "derived" / name / "x.txt").write_text(name)
    (comp / "uses.py").write_text("import kx\nprint((kx.data_dir() / 'derived' / 'feat' / 'x.txt').read_text())\n")
    commit(comp, "uses")
    code = kx_main(["exec", "--json", "--on", "mb", "--needs", "derived/feat", "--", "python", "uses.py"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["exit"] == 0
    cloud = next((Path(os.environ["FAKE_RCLONE_ROOT"]) / "fake/kx/demo/data").iterdir())
    assert (cloud / "train.csv").exists() and (cloud / "derived/feat/x.txt").exists() and not (cloud / "derived/other").exists()
    far = next((mbox / "demo" / "data").iterdir())
    assert (far / "derived/feat/x.txt").read_text() == "feat" and not (far / "derived/other").exists()
