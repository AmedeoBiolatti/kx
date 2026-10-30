"""kx init: the folder layout it creates, reruns, and repos set up with the older layout."""

import json

from kx.cli.main import main as kx_main
from kx.kaggle import competition
from kx.ledger import config
from kx.ledger.store import open_store

from conftest import git, run_kx, scored_run

META = {"slug": "demo", "fetched": "2026-10-16T00:00:00+00:00",
        "competition": {"ref": "https://www.kaggle.com/competitions/demo", "deadline": "2026-12-01T23:59:00", "category": "Featured"},
        "pages": [{"name": "evaluation", "content": "Submissions are scored on AUC."},
                  {"name": "code requirements", "content": "Submit through a notebook."}],
        "files": [{"name": "train.csv", "size": 10, "creationDate": "2026-01-01"}]}


def test_init_creates_the_layout(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(competition, "fetch", lambda slug: {**META, "slug": slug})
    root = tmp_path / "demo"
    root.mkdir()
    monkeypatch.chdir(root)
    assert kx_main(["init", "--no-data", "--kernel", "me/demo"]) == 0
    for rel in ["README.md", "AGENTS.md", "CLAUDE.md", "comp.yaml", "pyproject.toml", "docs/kx.md", "docs/competition.md",
                "src/comp/__init__.py", "src/comp/evaluate.py", "scripts/README.md", "notebooks/README.md",
                "kernel/run.py", "kernel/kernel-metadata.json"]:
        assert (root / rel).is_file(), rel
    assert not (root / "KX.md").exists() and not (root / "COMPETITION.md").exists()
    assert (root / "CLAUDE.md").read_text() == "@AGENTS.md\n@docs/kx.md\n"
    assert ".kx/" in (root / ".gitignore").read_text().splitlines()
    assert json.loads((root / "kernel/kernel-metadata.json").read_text())["id"] == "me/demo"
    assert config.load(root).store == root / ".kx" / "store"
    assert (root / "data").resolve() == (root / ".kx" / "store" / "data").resolve()
    assert (root / ".kx" / "store" / "log").is_dir()
    (root / "README.md").write_text("my notes\n")  # a rerun keeps what you changed
    capsys.readouterr()
    assert kx_main(["init", "--no-data"]) == 0
    assert (root / "README.md").read_text() == "my notes\n"


def test_a_missing_ledger_is_not_replaced_by_an_empty_one(comp, capsys, tmp_path):
    """A store that moved, an unmounted disk, a deleted .kx/: kx says so instead of going on with no history."""
    scored_run("x", {"f0": 0.8, "f1": 0.8})
    with open_store() as st:
        st.backup(tag="daily")
    log = comp / ".kx" / "store" / "log"
    log.rename(tmp_path / "moved-log")
    code, err = run_kx("status", capsys=capsys)
    assert code == 1 and "no ledger at" in err["error"] and "kx init" in err["fix"] and "tar xzf " in err["fix"]
    assert not log.exists()
    elsewhere = tmp_path / "elsewhere"
    with open(comp / "comp.yaml", "a") as f:
        f.write(f"store: {elsewhere}\n")
    code, err = run_kx("runs", capsys=capsys)
    assert code == 1 and str(elsewhere) in err["error"] and not elsewhere.exists()


def test_task_worktrees_live_in_dot_kx(comp, capsys):
    from kx.board import tasks
    code, t = run_kx("task", "add", "try it", capsys=capsys)
    with open_store() as st:  # as kx spawn makes it for a worker
        path, branch = tasks.make_worktree(st, tasks.get(st, t["id"]))
    wt = comp / ".kx" / "worktrees" / f"{t['id']}-try-it"
    assert path == wt and branch == f"task/{t['id']}-try-it" and (wt / "comp.yaml").exists()
    assert (wt / "data").resolve() == (comp / ".kx" / "store" / "data").resolve()
    with open_store(wt) as st:  # from inside the worktree, the same store
        assert st.comp.store == comp / ".kx" / "store"
    status = git_status(comp)
    assert status == "", status


def git_status(root):
    import subprocess
    return subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True).stdout.strip()
