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


def test_runs_reach_their_task_with_several_active(comp, capsys):
    """Tasks left active used to make every run's task ambiguous, so no run was linked: the last started is current."""
    import kx
    ids = [run_kx("task", "add", f"task {i}", capsys=capsys)[1]["id"] for i in range(3)]
    for i in ids:
        run_kx("task", "start", i, capsys=capsys)
    with kx.run("after starting the last") as r:
        pass
    run_kx("task", "start", ids[0], capsys=capsys)
    with kx.run("after going back to the first") as r2:
        pass
    with kx.run(f"{ids[1].split('-')[0]} named in the hypothesis") as r3:  # "T2 ...": by its number
        pass
    run_kx("task", "done", ids[0], "--result", "positive", "--outcome", "ok", capsys=capsys)
    capsys.readouterr()
    with kx.run("the current one is done: the other two are active, which is ambiguous") as r4:
        pass
    assert "linked to no task" in capsys.readouterr().err
    from kx.ledger.store import open_store
    with open_store() as st:
        got = [st.get_run(x.id)["task"] for x in (r, r2, r3, r4)]
    assert got == [ids[2], ids[0], ids[1], None]


def test_relink_from_the_hypothesis(comp, capsys):
    import kx
    from kx.ledger.store import open_store
    t = run_kx("task", "add", "old work", capsys=capsys)[1]["id"]
    with kx.run(f"{t.split('-')[0]} first try") as a, kx.run("no task named") as b, kx.run("T999 unknown task") as c:
        pass
    with open_store() as st:  # as runs were before kx linked by the hypothesis
        for x in (a, b, c):
            st.update("runs", {"task": None}, id=x.id)
    code, prev = run_kx("task", "relink", capsys=capsys)
    assert code == 3 and prev["runs"] == 1 and prev["tasks"] == {t: [a.id]}
    code, res = run_kx("task", "relink", "--yes", capsys=capsys)
    assert code == 0 and res["linked"]
    with open_store() as st:
        assert [st.get_run(x.id)["task"] for x in (a, b, c)] == [t, None, None]


def test_evidence_must_have_ended(comp, capsys):
    from kx.runs import tracking
    act = tracking.begin("still going", None)
    try:
        code, err = run_kx("finding", "add", "it works", "--evidence", act.run.name, capsys=capsys)
        assert code == 1 and f"kx wait {act.run.id}" in err["fix"]
        code, f = run_kx("finding", "add", "it works so far", "--evidence", act.run.name, "--partial", capsys=capsys)
        assert code == 0 and "[partial:" in f["text"]
    finally:
        tracking.end(act, "crashed later")
    code, st = run_kx("status", capsys=capsys)
    assert any(x["kind"] == "finding_evidence" and x["ref"] == f["id"] for x in st["needs_you"])


def test_stale_active_tasks_show(comp, capsys):
    from kx.ledger.store import open_store
    t = run_kx("task", "add", "forgotten", capsys=capsys)[1]["id"]
    run_kx("task", "start", t, capsys=capsys)
    with open_store() as st:
        st.update("tasks", {"created": "2026-01-01T00:00:00+00:00", "updated": "2026-01-01T00:00:00+00:00"}, id=t)
    code, st = run_kx("status", capsys=capsys)
    assert any(x["kind"] == "stale_task" and x["ref"] == t for x in st["needs_you"])
