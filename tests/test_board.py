"""Tasks and findings."""



from conftest import scored_run, run_kx


def test_tasks_flow(comp, capsys):
    code, t = run_kx("task", "add", "try lgbm dart", "--body", "why", "-p", "2", capsys=capsys)
    tid = t['id']
    code, t2 = run_kx("task", "add", "after it", "--depends", tid, capsys=capsys)
    code, lst = run_kx("task", "list", capsys=capsys)
    assert [x["blocked_by"] for x in lst if x["id"] == t2["id"]] == [[f"{tid} (open)"]]
    run_kx("task", "start", tid, capsys=capsys)
    run_kx("task", "note", tid, "halfway", capsys=capsys)
    code, shown = run_kx("task", "show", tid, capsys=capsys)
    assert shown["status"] == "active" and shown["notes"][-1]["text"] == "halfway"
    code, _ = run_kx("task", "done", tid, "--result", "negative", "--outcome", "dart did not help", capsys=capsys)
    code, lst = run_kx("task", "list", capsys=capsys)
    assert [x["id"] for x in lst] == [t2["id"]] and lst[0]["blocked_by"] == []
    code, found = run_kx("search", "dart", capsys=capsys)
    assert found["tasks"][0]["outcome"] == "dart did not help"


def test_findings(comp, capsys):
    r = scored_run("leak check", {"f0": 0.9, "f1": 0.95})
    capsys.readouterr()
    code, f = run_kx("finding", "add", "fold 1 leaks", "--evidence", r.name, "--tag", "data", capsys=capsys)
    assert f["evidence"] == [r.id] and f["tags"] == ["data"]
    code, f2 = run_kx("finding", "add", "fold 1 leaks through user_id", capsys=capsys)
    run_kx("finding", "supersede", f['id'], "--by", f2['id'], capsys=capsys)
    code, lst = run_kx("finding", "list", capsys=capsys)
    assert [x["id"] for x in lst] == [f2["id"]]


def test_a_task_waiting_on_a_command_shows_it_to_its_person(comp, capsys):
    code, t = run_kx("task", "add", "submit it", capsys=capsys)
    run_kx("task", "wait", t['id'], "--on", "kx submit r1 -m x --yes", capsys=capsys)
    code, st = run_kx("status", capsys=capsys)
    assert [(x["kind"], x["fix"]) for x in st["needs_you"]] == [("task_waits_on_you", "kx submit r1 -m x --yes")]


def test_runs_are_linked_to_the_task_their_author_started(comp, capsys):
    import kx
    code, t = run_kx("task", "add", "try lgbm", capsys=capsys)
    run_kx("task", "start", t['id'], capsys=capsys)
    with kx.run("lgbm baseline") as r:
        (r.dir / "notes.jsonl").write_text('{"text": "from a script"}\n')  # a side file kx records, then deletes
    code, shown = run_kx("task", "show", t['id'], capsys=capsys)
    assert [x["id"] for x in shown["runs"]] == [r.id]
    code, show = run_kx("show", r.name, capsys=capsys)
    assert any(n["text"] == "from a script" for n in show["notes"]) and not any("notes" in a["path"] for a in show["artifacts"])
