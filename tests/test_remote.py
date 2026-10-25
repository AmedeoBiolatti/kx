"""kx exec --on HOST, kx remote: runs on another machine, here a `local: true` host (a directory, no ssh)."""

import json
import sys
import time
from pathlib import Path

import pytest

from kx.cli.main import main as kx_main
from kx.ledger import config
from kx.ledger.store import Store
from kx.remote import execute, hosts

from conftest import commit, run_kx

TRAIN = """
import kx
P = kx.Protocol("cv3", metric="auc", folds="f1")
rows = (kx.data_dir() / "train.csv").read_text().splitlines()
with kx.run("remote model", config={"seed": 1}, tags=["algorithm"]) as r:
    r.progress(1, total=2, phase="fit")
    r.log(rows=len(rows))
    r.score(0.8, P, breakdown={"f0": 0.79, "f1": 0.81})
    r.note("trained on " + kx.data_version())
    r.artifact("submission.csv").write_text("id,y\\n1,0.5\\n")
    print("done training")
import time; time.sleep(1.1)  # the host's clock counts whole seconds: a paid run is charged from 1 s on
"""


@pytest.fixture
def box(comp, tmp_path, monkeypatch):
    monkeypatch.setattr(hosts.Transport, "POLL_S", (0.1,))
    remote = tmp_path / "remote"
    with open(comp / "comp.yaml", "a") as f:
        f.write(f"hosts:\n  box: {{local: true, dir: {remote}, python: {sys.executable}, usd_per_hour: 36}}\n")
    (comp / "train.py").write_text(TRAIN)
    commit(comp, "train")
    return remote


def _exec(capsys, *args):
    code = kx_main(["exec", "--json", *args])
    return code, json.loads(capsys.readouterr().out)


def _wait_synced(capsys, run, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        code, items = run_kx("remote", "sync", run, capsys=capsys)
        if items[0]["state"] not in ("running", "unreachable"):
            return items[0]
        time.sleep(0.2)
    raise AssertionError("remote run did not end")


def test_remote_run_records_scores_notes_artifacts(box, comp, capsys):
    code, out = _exec(capsys, "--on", "box", "--", "python", "train.py")
    assert code == 0 and out["exit"] == 0
    code, show = run_kx("show", out["run"], capsys=capsys)
    assert show["status"] == "done" and show["host"] == "box"
    assert show["scores"][0]["protocol"].startswith("cv3@") and show["scores"][0]["value"] == 0.8
    assert any("trained on kaggle:" in n["text"] for n in show["notes"])
    assert "algorithm" in show["tags"] and show["seed"] == 1
    d = Store(config.load()).run_dir(out["run"])
    assert (d / "submission.csv").read_text().startswith("id,y") and "done training" in (d / "stdout.log").read_text()
    inf = json.loads((d / "remote.json").read_text())
    assert inf["exit"] == 0 and inf["hours"] is not None
    assert (box / "demo" / "data").is_dir() and (box / "demo" / "runs" / out["run"] / "code.tar.gz").exists()
    with Store(config.load()) as st:  # usd_per_hour x wall time, charged to the run's author
        assert st.one("SELECT COUNT(*) FROM usage WHERE metric='cloud_usd'")[0] == 1


def test_detached_probe_ships_patch_and_failure_is_recorded(box, comp, capsys):
    (comp / "probe.py").write_text("import sys; print('probe ran'); sys.exit(3)\n")  # untracked: shipped with the patch
    code, out = _exec(capsys, "--on", "box", "--detach", "--probe", "--", "python", "probe.py")
    assert code == 0 and out["detached"]
    code, st = run_kx("status", capsys=capsys)
    assert any(r["run"] == out["run"] and r["host"] == "box" for r in st["running"]["runs"])
    assert not any(x["kind"] == "dead_run" for x in st["needs_you"])
    item = _wait_synced(capsys, out["run"])
    assert item["state"] == "failed" and item["exit"] == 3
    code, show = run_kx("show", out["run"], capsys=capsys)
    assert "exit code 3" in show["error"]
    assert "probe ran" in (Store(config.load()).run_dir(out["run"]) / "stdout.log").read_text()


def test_kill_and_stale_remote_run_shows_in_status(box, comp, capsys):
    code, out = _exec(capsys, "--on", "box", "--detach", "--", "python", "-c", "import time; time.sleep(60)")
    run_id = out["run"]
    d = Store(config.load()).run_dir(run_id)
    inf = json.loads((d / "remote.json").read_text())
    inf["launched"] = "2026-01-01T00:00:00+00:00"
    (d / "remote.json").write_text(json.dumps(inf))
    code, st = run_kx("status", capsys=capsys)
    assert any(x["kind"] == "remote_run" and x["ref"] == out["run"] for x in st["needs_you"])
    pid = box / "demo" / "runs" / out["run"] / "out" / ".remote" / "pid"
    for _ in range(100):
        if pid.exists():
            break
        time.sleep(0.05)
    code, res = run_kx("remote", "kill", out["run"], capsys=capsys)
    assert code == 0 and res["killed"]
    code, show = run_kx("show", out["run"], capsys=capsys)
    assert show["status"] == "failed" and "killed" in show["error"]


def test_paid_host_refused_over_budget(box, comp, capsys):
    with open(comp / "comp.yaml", "a") as f:
        f.write("budgets: {cloud_usd_per_day: 0.1}\n")
    commit(comp, "budget")
    code, out = _exec(capsys, "--on", "box", "--", "python", "train.py")
    assert code == 4 and "over budget" in out["error"]


def test_remote_list_and_check(box, comp, capsys):
    code, data = run_kx("remote", "list", capsys=capsys)
    assert code == 0 and data["hosts"][0]["name"] == "box"
    code, data = run_kx("remote", "check", "box", capsys=capsys)
    assert data["checks"][0]["ok"] and any("git" in c["check"] for c in data["checks"])


SSH_SHIM = """#!/usr/bin/env bash
# what ssh does on the far end: the words after the host are joined and run by the login shell, in $HOME
while [ $# -gt 0 ]; do
  case "$1" in
    -o|-p|-i|-l) shift 2 ;;
    -*) shift ;;
    *) shift; break ;;
  esac
done
cd "$HOME" && exec bash -c "$*"
"""


def test_ssh_transport(comp, tmp_path, monkeypatch, capsys):
    """The ssh code path (quoting, rsync -e ssh), against an ssh stand-in."""
    import os
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "ssh").write_text(SSH_SHIM)
    (bindir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(hosts.Transport, "POLL_S", (0.1,))
    with open(comp / "comp.yaml", "a") as f:
        f.write(f"hosts:\n  far: {{ssh: me@far.example, dir: {tmp_path / 'far'}, python: {sys.executable}}}\n")
    (comp / "train.py").write_text(TRAIN.replace("time.sleep(1.1)", "pass"))
    commit(comp, "train")
    code, out = _exec(capsys, "--on", "far", "-H", "it's \"quoted\" $HOME", "--", "python", "train.py", "a b", "$x")
    assert code == 0 and out["exit"] == 0
    code, show = run_kx("show", out["run"], capsys=capsys)
    assert show["status"] == "done" and show["scores"][0]["value"] == 0.8 and show["host"] == "far"
    assert (tmp_path / "far" / "demo" / "runs" / out["run"] / "code.tar.gz").exists()
    code, data = run_kx("remote", "check", "far", capsys=capsys)
    assert data["checks"][0]["ok"]


PEERS = [{"name": "Laptop2", "dns": "laptop2.tail1.ts.net", "ip": "100.64.0.2", "os": "linux", "online": True,
          "host_keys": ["ssh-ed25519 AAAAkey"]},
         {"name": "WINBOX", "dns": "winbox.tail1.ts.net", "ip": "100.64.0.3", "os": "windows", "online": True}]


@pytest.fixture
def tailnet(comp, tmp_path, monkeypatch):
    """Two machines on Tailscale, reached through the ssh stand-in; a hosts file with an entry and a comment."""
    import os

    from kx.remote import hosts
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "ssh").write_text(SSH_SHIM)
    (bindir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # the stand-in's far end: kx-remote lands here
    hf = tmp_path / "hosts.yaml"
    hf.write_text("# my machines\nhosts:\n  old: {ssh: me@old.example}\n")
    monkeypatch.setattr(hosts, "HOSTS_FILE", hf)
    monkeypatch.setattr(hosts, "KNOWN_HOSTS", tmp_path / "known_hosts")
    monkeypatch.setattr(hosts, "tailscale_peers", lambda: PEERS)
    return hf


def test_remote_add_finds_the_machine_on_tailscale_and_sets_it_up(tailnet, tmp_path, capsys):
    code, r = run_kx("remote", "add", "lap", "--user", "me", "--python", sys.executable, capsys=capsys)
    assert code == 0 and r["entry"]["ssh"] == "me@laptop2.tail1.ts.net"
    assert r["setup"]["data_version"] and (tmp_path / "home" / "kx-remote" / "demo" / "data" / r["setup"]["data_version"]).is_dir()
    assert (tmp_path / "known_hosts").read_text() == "laptop2.tail1.ts.net,100.64.0.2 ssh-ed25519 AAAAkey\n"
    text = tailnet.read_text()
    assert text.startswith("# my machines\n") and "  lap: {ssh: me@laptop2.tail1.ts.net, python: " in text  # appended under hosts:, comment kept
    code, ls = run_kx("remote", "list", capsys=capsys)
    assert {h["name"] for h in ls["hosts"]} == {"old", "lap"}
    code, err = run_kx("remote", "add", "lap", capsys=capsys)
    assert code != 0 and "already" in err["error"]


def test_remote_add_explains_windows_and_unknown_names(tailnet, capsys):
    code, err = run_kx("remote", "add", "winbox", capsys=capsys)
    assert code != 0 and "Windows" in err["error"] and "kx remote add winbox-wsl" in err["fix"]
    code, err = run_kx("remote", "add", "nope", capsys=capsys)
    assert code != 0 and "laptop2 (linux)" in err["error"] and "tailscale up --ssh" in err["fix"]
    assert "lap" not in tailnet.read_text()


def test_host_torch_index_swaps_in_the_locked_version(comp):
    from conftest import commit
    from kx.remote import script
    (comp / "uv.lock").write_text('version = 1\n\n[[package]]\nname = "torch"\nversion = "2.14.1+cpu"\n'
                                  'source = { registry = "https://download.pytorch.org/whl/cpu" }\n')
    commit(comp, "lock")
    key, locked, version = script.env_spec(comp, "HEAD", "cu126")
    assert version == "2.14.1" and locked and key.endswith("-cu126") and key != script.env_spec(comp, "HEAD")[0]  # its own env
    text = "\n".join(script.env_lines(key, locked, version, "cu126"))
    assert "--index-url https://download.pytorch.org/whl/cu126" in text and "torch==2.14.1" in text
    assert "--all-extras" in text and "torch" not in "\n".join(script.env_lines(key, locked, None, None))


def test_setup_ships_kx_and_data_ahead_of_runs(box, comp, capsys):
    code, r = run_kx("remote", "setup", "box", capsys=capsys)
    assert code == 0 and r["data_version"] and r["env"] is None  # python: set, no uv environment
    assert (box / "demo" / "data" / r["data_version"] / ".kx-complete").exists()


def test_big_files_stay_on_the_host_until_fetched(box, comp, capsys, monkeypatch):
    """A checkpoint is not copied on every poll (that made syncs take minutes), nor when the run ends: the run lists
    it, and kx fetch copies it when someone wants it."""
    monkeypatch.setattr(execute, "LOW_DISK", 0)  # whatever this machine's disk has free
    script = ("import os, time, kx\n"
              "with kx.run('big') as r:\n"
              "    r.step(0, loss=1.0)\n"
              "    with open(r.dir / 'model.pt', 'wb') as f: f.truncate(40 * 2**20)\n"
              "    (r.dir / 'ckpt').mkdir()\n"
              "    with open(r.dir / 'ckpt' / 'best.pt', 'wb') as f: f.truncate(33 * 2**20)\n"
              "    (r.dir / 'small.json').write_text('{}')\n"
              "    (r.dir / 'remote.json').write_text('{\"ssh_opts\": [\"-oProxyCommand=evil\"]}')\n"
              "    while not (r.dir / 'go').exists(): time.sleep(0.05)\n")  # the job's /tmp is its own
    (comp / "big.py").write_text(script)
    commit(comp, "big")
    code, out = _exec(capsys, "--on", "box", "--detach", "--", "python", "big.py")
    d = Store(config.load()).run_dir(out["run"])
    remote = box / "demo" / "runs" / out["run"] / "out"
    for _ in range(200):
        if (remote / "small.json").exists():
            break
        time.sleep(0.05)
    run_kx("remote", "sync", out["run"], capsys=capsys)
    assert (d / "small.json").exists() and (d / "metrics.jsonl").exists() and not (d / "model.pt").exists()
    (remote / "go").write_text("")
    item = _wait_synced(capsys, out["run"])
    assert item["state"] == "done" and not (d / "model.pt").exists()
    code, show = run_kx("show", out["run"], capsys=capsys)
    assert sorted(x["path"] for x in show["remote"]["on_host"]) == ["ckpt/best.pt", "model.pt"]
    assert "ProxyCommand" not in (d / "remote.json").read_text()  # a run's output never replaces kx's own files
    code, r = run_kx("fetch", out["run"], "ckpt/best.pt", capsys=capsys)
    assert code == 0 and (d / "ckpt" / "best.pt").stat().st_size == 33 * 2**20 and not (d / "model.pt").exists()
    assert [x["path"] for x in r["on_host"]] == ["model.pt"]
    code, r = run_kx("fetch", out["run"], capsys=capsys)
    assert (d / "model.pt").stat().st_size == 40 * 2**20 and r["on_host"] == []


def test_low_disk_space_leaves_big_files_on_the_host(box, comp, capsys, monkeypatch):
    (comp / "ckpt.py").write_text("import kx\nwith kx.run('ckpt') as r:\n"
                                  "    with open(r.dir / 'model.pt', 'wb') as f: f.truncate(40 * 2**20)\n")
    commit(comp, "ckpt")
    code, out = _exec(capsys, "--on", "box", "--", "python", "ckpt.py")
    d = Store(config.load()).run_dir(out["run"])
    monkeypatch.setattr(execute, "LOW_DISK", 2**62)  # "no room here"
    code, r = run_kx("fetch", out["run"], capsys=capsys)
    assert not (d / "model.pt").exists() and r["on_host"][0]["path"] == "model.pt"
    monkeypatch.setattr(execute, "LOW_DISK", 0)  # space freed
    code, items = run_kx("remote", "sync", out["run"], capsys=capsys)
    assert items[0]["complete"] and (d / "model.pt").stat().st_size == 40 * 2**20


def test_a_remote_run_is_confined(box, comp, capsys, tmp_path):
    """On the host the run writes only its own dir and the data's derived/ caches; the home it sees is empty."""
    from kx import sandbox
    if not sandbox.available():
        pytest.skip("bubblewrap unavailable")
    probe = Path.home() / f"kx-run-probe-{tmp_path.name}"
    script = ("import os, pathlib, kx\n"
              "d = kx.data_dir()\n"
              "def tries(p, text):\n"
              "    try:\n        pathlib.Path(p).write_text(text); return 'wrote'\n"
              "    except OSError:\n        return 'refused'\n"
              "(d / 'derived').mkdir(exist_ok=True)\n"
              f"print(tries(d / 'train.csv', 'x'), tries(d / 'derived' / 'cache.txt', 'c'), tries({str(probe)!r}, 'h'),"
              " len(os.listdir(pathlib.Path.home() / '.ssh')) if (pathlib.Path.home() / '.ssh').exists() else 'no-ssh')\n")
    (comp / "probe.py").write_text(script)
    commit(comp, "probe")
    code, out = _exec(capsys, "--on", "box", "--", "python", "probe.py")
    d = Store(config.load()).run_dir(out["run"])
    try:
        assert code == 0, (d / "stderr.log").read_text()
        assert (d / "stdout.log").read_text().split() == ["refused", "wrote", "wrote", "no-ssh"]
        assert not probe.exists()  # written in the run's own empty home, not the real one
    finally:
        probe.unlink(missing_ok=True)


def test_hosts_get_kaggle_files_and_only_the_parts_a_run_needs(box, comp, capsys):
    data = Store(config.load()).comp.store / "data"
    for name in ("feat", "other"):
        (data / "derived" / name).mkdir(parents=True)
        (data / "derived" / name / "x.txt").write_text(name)
    (comp / "uses.py").write_text("import kx\nprint((kx.data_dir() / 'derived' / 'feat' / 'x.txt').read_text())\n")
    commit(comp, "uses")
    code, out = _exec(capsys, "--on", "box", "--needs", "data/derived/feat", "--", "python", "uses.py")
    assert code == 0 and out["exit"] == 0
    host_data = next((box / "demo" / "data").iterdir())
    assert (host_data / "train.csv").exists() and (host_data / "derived" / "feat" / "x.txt").exists()
    assert not (host_data / "derived" / "other").exists()  # never asked for: never sent
    code, err = _exec(capsys, "--on", "box", "--needs", "../secrets", "--", "true")
    assert code == 2
