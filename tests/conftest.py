import json
import os
import subprocess
from pathlib import Path

import pytest

import kx
from kx.cli.main import main as kx_main
from kx.kaggle import client as kaggle
from kx.ledger import config
from kx.ledger.store import Store


def git(root: Path, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})


class FakeKaggle:
    def __init__(self):
        self.sent = []
        self.remote = []

    def submissions(self, slug):
        return list(self.remote)

    def submit_file(self, slug, path, message):
        self.sent.append((str(path), message))
        self.remote.append({"ref": str(1000 + len(self.sent)), "description": message, "status": "COMPLETE",
                            "publicScore": "0.5", "date": "2026-10-15T00:00:00"})
        return "Successfully submitted"

    def leaderboard(self, slug):
        return [{"Rank": "1", "Score": "0.9", "TeamMemberUserNames": "someone"},
                {"Rank": "2", "Score": "0.5", "TeamMemberUserNames": "me"}]


@pytest.fixture(autouse=True, scope="session")
def this_machines_config_untouched():
    """A test that writes this machine's kx config (machine.yaml, hosts.yaml) fails the run: they are someone's."""
    real = Path("~/.config/kx").expanduser()
    before = {p.name: p.read_bytes() for p in real.glob("*.yaml")} if real.is_dir() else {}
    yield
    after = {p.name: p.read_bytes() for p in real.glob("*.yaml")} if real.is_dir() else {}
    assert after == before, f"a test changed {real}: " + ", ".join(sorted(k for k in set(before) | set(after)
                                                                          if before.get(k) != after.get(k)))


@pytest.fixture(autouse=True)
def own_hosts(tmp_path, monkeypatch):
    """Never this machine's hosts, host keys or machine role."""
    from kx.remote import hosts
    monkeypatch.setattr(hosts, "HOSTS_FILE", tmp_path / "kx-config" / "hosts.yaml")
    monkeypatch.setattr(hosts, "KNOWN_HOSTS", tmp_path / "kx-config" / "known_hosts")
    from kx import sandbox  # nor its machine role
    monkeypatch.setattr(sandbox, "MACHINE_FILE", tmp_path / "kx-config" / "machine.yaml")
    monkeypatch.delenv("KX_ROLE", raising=False)


@pytest.fixture
def fake_kaggle(monkeypatch):
    fk = FakeKaggle()
    monkeypatch.setattr(kaggle, "submissions", fk.submissions)
    monkeypatch.setattr(kaggle, "submit_file", fk.submit_file)
    monkeypatch.setattr(kaggle, "leaderboard", fk.leaderboard)
    monkeypatch.setattr(kaggle, "username", lambda: "me")
    monkeypatch.setattr(kaggle, "episodes", lambda ref: [])
    return fk


@pytest.fixture
def comp(tmp_path, monkeypatch, fake_kaggle):
    """A committed competition repo (kind file, predict) with its store in tmp, and data 'downloaded'."""
    ws = tmp_path / "ws"
    root = ws / "demo"
    root.mkdir(parents=True)
    (root / "comp.yaml").write_text(
        "slug: demo\nkind: file\nmetric: {name: auc, direction: max}\n"
        "submission: {daily_limit: 3, file: submission.csv}\n"
        "tags: [algorithm, data, eval]\n")
    (root / ".gitignore").write_text("/data\n.kx/\n")
    store = root / ".kx" / "store"
    (store / "data").mkdir(parents=True)
    (store / "data" / "train.csv").write_text("a,b\n1,2\n")
    (store / "data" / ".kx-manifest.json").write_text(json.dumps(
        {"slug": "demo", "files": {"train.csv": {"size": 8, "creationDate": "2026-01-01"}}}))
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    monkeypatch.chdir(root)
    Store(config.load(root), create=True).close()  # what kx init does
    for k in ("KX_AGENT", "KX_TASK", "KX_RUN_ID", "KX_RUN_DIR", "KX_ROOT"):
        monkeypatch.delenv(k, raising=False)
    return root


def run_kx(*args, capsys=None):
    """Run the CLI in-process with --json; returns (exit code, parsed JSON)."""
    code = kx_main([*args, "--json"])
    out = capsys.readouterr().out if capsys else ""
    return code, (json.loads(out) if out.strip() else None)


def commit(root: Path, msg="change"):
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", msg)


P = kx.Protocol("cv3", metric="auc", folds="f1")


def scored_run(hyp, folds, protocol=P, **kw):
    with kx.run(hyp, config={"seed": 0}, **kw) as r:
        r.score(sum(folds.values()) / len(folds), protocol, breakdown=folds)
        r.progress(1, total=1)
    return r


def submittable(comp):
    with kx.run("model") as r:
        r.score(0.8, P, breakdown={"f0": 0.79, "f1": 0.81})
        r.artifact("submission.csv").write_text("id,y\n1,0.5\n")
    return r
