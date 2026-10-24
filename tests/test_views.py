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
