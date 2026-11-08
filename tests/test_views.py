"""kx status / kx context and the --json contract."""

from kx.cli.main import main as kx_main

from conftest import scored_run, run_kx


def test_context_is_short(comp, capsys):
    for i in range(12):
        scored_run(f"run {i}", {"f0": 0.5 + i / 100, "f1": 0.51 + i / 100})
        run_kx("finding", "add", f"finding {i} " + "x" * 400, capsys=capsys)
    capsys.readouterr()
    kx_main(["context"])
    text = capsys.readouterr().out
    assert len(text.splitlines()) < 60 and len(text) < 8000


def test_every_listing_command_gives_json(comp, capsys):
    scored_run("a", {"f0": 0.7, "f1": 0.8})
    capsys.readouterr()
    for args in (["status"], ["context"], ["runs"], ["show", "latest"], ["board"], ["task", "list"], ["finding", "list"],
                 ["subs"], ["budget"], ["protocol", "list"], ["search", "a"]):
        code, data = run_kx(*args, capsys=capsys)
        assert code == 0, args
        assert data is not None, args


def test_dashboard_api_matches_the_cli_and_leaves_out_protocol_specs(comp, capsys):
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    from kx.views.dashboard import Handler

    scored_run("a", {"f0": 0.7, "f1": 0.8})
    code, cli = run_kx("protocol", "list", capsys=capsys)
    Handler.comps, Handler.default = {"demo": comp}, "demo"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    get = lambda path: json.load(urllib.request.urlopen(f"http://localhost:{httpd.server_address[1]}/api/{path}", timeout=10))
    try:
        for _ in range(2):  # the second call reuses the parser
            page = get("protocols")
            assert [p["id"] for p in page["protocols"]] == [p["id"] for p in cli["protocols"]]
            assert all("spec" not in p for p in page["protocols"]) and "spec" in cli["protocols"][0]
        assert get("run/latest")["run"] == get("runs")[0]["run"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_dashboard_answers_bad_requests_with_an_error_not_a_dropped_connection(comp, capsys):
    """argparse exits on a bad value (?n=abc) and a missing parameter raised KeyError: each must come back as a JSON
    error the page can show, and a NaN in the data must not make the whole response invalid JSON."""
    import json
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from kx.views.dashboard import Handler, to_json

    assert json.loads(to_json({"loss": float("nan"), "xs": [1.0, float("inf")], "s": "ok"})) == {"loss": None, "xs": [1.0, None], "s": "ok"}
    scored_run("a", {"f0": 0.7, "f1": 0.8})
    Handler.comps, Handler.default = {"demo": comp}, "demo"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def get(path):
        try:
            with urllib.request.urlopen(f"http://localhost:{httpd.server_address[1]}/api/{path}", timeout=10) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)
    try:
        code, d = get("runs?n=abc")
        assert code == 400 and "invalid int value" in d["error"]
        code, d = get("compare?a=r0001")
        assert code == 400 and d["error"] == "missing ?b= in the request"
        code, d = get("runs?n=5")  # the server still answers after those
        assert code == 200 and d[0]["run"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_dashboard_lists_every_competition_on_the_machine(comp, tmp_path, monkeypatch):
    """Best effort, without being told: the ones kx has opened here, the ones next to them, a look around the home
    folder; a worktree counts as its competition, a deleted one drops out, two folders with one name both show."""
    import json

    from kx.ledger import config, projects
    from kx.views.dashboard import competitions

    from conftest import git

    home = tmp_path / "home"
    monkeypatch.setattr(projects.Path, "home", lambda: home)
    monkeypatch.setattr(projects, "_kx_workspace", lambda: None)  # not this checkout's neighbours

    def competition(d):
        d.mkdir(parents=True)
        (d / "comp.yaml").write_text(f"slug: {d.name}\n")
        return d.resolve()
    elsewhere = competition(tmp_path / "elsewhere" / "far")
    config.load(elsewhere)  # opened once: remembered
    gone = competition(tmp_path / "gone" / "old")
    config.load(gone)
    (gone / "comp.yaml").unlink()
    sibling = competition(tmp_path / "ws" / "next-door")  # never opened, next to one that was
    deep = competition(home / "code" / "kaggle" / "third")  # under the home folder
    twin = competition(tmp_path / "other" / "demo")  # the same folder name as comp
    git(comp, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "t1")  # a task worktree has comp.yaml too
    remembered = json.loads(projects.FILE.read_text())
    assert str(comp.resolve()) in remembered and str(elsewhere) in remembered

    comps, default = competitions([tmp_path / "other", tmp_path / "wt"])
    assert set(comps.values()) == {comp.resolve(), elsewhere, sibling, deep, twin}
    assert comps["ws/demo"] == comp.resolve() and comps["other/demo"] == twin and comps["far"] == elsewhere
    assert comps[default] == comp.resolve()  # the one it starts in

    monkeypatch.chdir(tmp_path)  # outside any: the one used most recently
    config.load(elsewhere)
    projects._noted.clear()
    data = json.loads(projects.FILE.read_text())
    data[str(elsewhere)] = max(data.values()) + 1
    projects.FILE.write_text(json.dumps(data))
    comps, default = competitions()
    assert comps[default] == elsewhere


def test_nothing_to_show_says_where_to_look(tmp_path, monkeypatch):
    import pytest

    from kx.ledger import projects
    from kx.ledger.config import KxError
    from kx.views import dashboard
    monkeypatch.setattr(projects.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(projects, "_kx_workspace", lambda: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KX_AGENT", raising=False)
    with pytest.raises(KxError, match="no competition") as e:
        dashboard.serve(0)
    assert "kx dashboard DIR" in e.value.fix
