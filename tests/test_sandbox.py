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
