"""Machine roles (kx.sandbox): a workspace runs no project code itself."""

import pytest

import kx
from kx.ledger.config import KxError

from kx.cli.main import main as kx_main

from conftest import run_kx


def _exec(capsys, *args):
    import json
    code = kx_main(["exec", "--json", *args])
    return code, json.loads(capsys.readouterr().out)


def test_a_workspace_sends_runs_to_an_executor(comp, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("KX_ROLE", "workspace")
    monkeypatch.setenv("KX_MACHINE_FILE", str(tmp_path / "machine.yaml"))
    import kx.sandbox as sb
    monkeypatch.setattr(sb, "MACHINE_FILE", tmp_path / "machine.yaml")
    code, err = _exec(capsys, "--", "python", "-c", "print(1)")
    assert code == 1 and "workspace" in err["error"] and "--on" in err["fix"]
    with pytest.raises(KxError, match="workspace"):
        with kx.run("here"):
            pass
    (tmp_path / "machine.yaml").write_text("role: workspace\ndefault_host: nowhere\n")
    monkeypatch.delenv("KX_ROLE")
    code, err = _exec(capsys, "--", "python", "-c", "print(1)")
    assert code == 1 and "nowhere" in err["error"]  # sent to the default executor (which does not exist here)
    code, runs = run_kx("runs", capsys=capsys)
    assert runs == []


def test_a_login_behind_a_symlink_is_hidden_where_it_really_is(monkeypatch):
    """Under WSL ~/.aws is often a link into the Windows home: bwrap gets the target, not the link."""
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    import kx.sandbox as sb
    root = Path(tempfile.mkdtemp(prefix=".kx-test-links-", dir=Path.home()))  # not in /tmp: the sandbox hides it
    try:
        home, windows = root / "home", root / "c" / "Users" / "me"
        (windows / ".aws").mkdir(parents=True)
        (windows / ".aws" / "credentials").write_text("[default]\naws_secret_access_key = x\n")
        (windows / ".git-credentials").write_text("https://u:t@github.com\n")
        home.mkdir()
        (home / ".aws").symlink_to(windows / ".aws")
        (home / ".git-credentials").symlink_to(windows / ".git-credentials")
        monkeypatch.setattr(Path, "home", lambda: home)
        argv = ["sh", "-c", f"ls -A {home / '.aws'}; cat {home / '.git-credentials'}"]
        args = sb.wrap(argv, [])
        aws, git = str((windows / ".aws").resolve()), str((windows / ".git-credentials").resolve())
        assert args[args.index(aws) - 1] == "--tmpfs" and args[args.index(git) - 2:args.index(git)] == ["--ro-bind", "/dev/null"]
        assert str(home / ".aws") not in args and str(home / ".git-credentials") not in args
        if sb.available():
            p = subprocess.run(args, capture_output=True, text=True)
            assert p.stdout == ""  # the folder is empty, the file unreadable (a nodev /dev/null)
            assert "No such file" not in p.stderr  # the links still resolve, to nothing
    finally:
        shutil.rmtree(root)
