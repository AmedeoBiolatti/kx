"""What the person does without learning kx: decisions settled in one step (kx approve / kx answer), the brief of
what changed since they last looked, notices, kx alone, and what the dashboard and the panel may run for them."""

import json
import re
import sys
from datetime import datetime, timedelta, timezone

import pytest

from kx.cli.main import main as kx_main
from kx.ledger import config
from kx.ledger.store import Store, author

from conftest import run_kx, scored_run, submittable


def worker_asks(monkeypatch, capsys, *argv, task: str):
    """A worker runs a gated command, is refused, and parks its task on it, as docs/kx.md tells it to."""
    monkeypatch.setenv("KX_AGENT", "w1")
    monkeypatch.setenv("KX_TASK", task)
    run_kx("task", "start", task, capsys=capsys)
    code, err = run_kx(*argv, capsys=capsys)
    assert code == 3
    run_kx("task", "wait", task, "--on", err["command"], capsys=capsys)
    monkeypatch.delenv("KX_AGENT")
    monkeypatch.delenv("KX_TASK")
    return err["command"]


def needs(capsys, kind=None):
    code, st = run_kx("status", capsys=capsys)
    return [x for x in st["needs_you"] if kind is None or x["kind"] == kind]


def test_approve_previews_sends_and_the_task_waits_on_the_score(comp, fake_kaggle, capsys, monkeypatch):
    r = submittable(comp)
    code, t = run_kx("task", "add", "submit the baseline", capsys=capsys)
    tid = t["id"]
    worker_asks(monkeypatch, capsys, "submit", r.name, "-m", "baseline", "--yes", task=tid)
    [item] = needs(capsys, "task_waits_on_you")
    assert item["who"] == "decision" and item["asker"] == "w1"
    assert [a["label"] for a in item["actions"]] == ["Send", "Decline"]
    assert item["actions"][0] == {"label": "Send", "argv": ["approve", tid, "--yes"], "preview": ["approve", tid],
                                  "primary": True}
    code, prev = run_kx("approve", tid, capsys=capsys)  # the command's own preview, nothing sent
    assert code == 3 and prev["preview"]["run"] == r.name and prev["fix"] == f"kx approve {tid} --yes"
    assert not fake_kaggle.sent
    code, res = run_kx("approve", tid, "--yes", capsys=capsys)
    assert code == 0 and len(fake_kaggle.sent) == 1
    assert res["task"]["status"] == "waiting" and res["task"]["waiting_on"].startswith("submission:S1-")
    code, task = run_kx("task", "show", tid, capsys=capsys)
    assert re.match(r"S1-[a-z-]+ sent", task["notes"][-1]["text"])
    run_kx("sync", "--no-lb", capsys=capsys)  # Kaggle scored it: the task can go on
    [go] = needs(capsys, "resume_task")
    assert re.fullmatch(rf"{tid} waited for submission:(S1-[a-z-]+): \1 is complete, it can go on", go["what"])
    code, subs = run_kx("subs", "S1", capsys=capsys)
    assert subs["task"] == t["id"]


def test_running_the_command_by_hand_settles_the_task(comp, fake_kaggle, capsys, monkeypatch):
    """Before: the task kept saying "waits on you" after the person had sent the submission."""
    r = submittable(comp)
    code, t = run_kx("task", "add", "submit it", capsys=capsys)
    worker_asks(monkeypatch, capsys, "submit", r.name, "-m", "from worker", "--yes", task=t["id"])
    code, res = run_kx("submit", r.name, "-m", "my own words", "--yes", capsys=capsys)
    assert code == 0 and res["settled"] == [t["id"]]
    assert not needs(capsys, "task_waits_on_you")
    code, task = run_kx("task", "show", t["id"], capsys=capsys)
    assert task["waiting_on"].startswith("submission:S1-")


def test_answer_declines_a_command_or_replies_to_a_question(comp, fake_kaggle, capsys, monkeypatch):
    r = submittable(comp)
    code, t = run_kx("task", "add", "submit it", capsys=capsys)
    tid = t["id"]
    worker_asks(monkeypatch, capsys, "submit", r.name, "-m", "x", "--yes", task=tid)
    code, res = run_kx("answer", tid, "within", "noise:", "try", "dart", "first", capsys=capsys)
    assert code == 0 and not fake_kaggle.sent
    [go] = needs(capsys, "resume_task")
    assert go["what"].startswith(f"{tid} can go on: ") and "declined: within noise: try dart first" in go["what"]
    code, task = run_kx("task", "show", tid, capsys=capsys)
    assert task["notes"][-1]["text"].startswith("not run: `kx submit")
    code, q = run_kx("task", "add", "folds", capsys=capsys)
    run_kx("task", "wait", q["id"], "--on", "person: 5 or 10 folds?", capsys=capsys)
    item = next(x for x in needs(capsys, "task_waits_on_you") if x["ref"] == q["id"])
    assert [a["label"] for a in item["actions"]] == ["Answer"] and item["fix"] == f'kx answer {q["id"]} "..."'
    code = kx_main(["answer", q["id"], "--json", "--", "-10, the folds are small"])  # text that looks like an option
    res = json.loads(capsys.readouterr().out)
    assert code == 0 and res["task"]["waiting_on"] == f"answered: {author()} replied: -10, the folds are small"
    code, cx = run_kx("context", "--task", q["id"], capsys=capsys)  # the answer reaches whoever resumes it
    assert any("-10, the folds are small" in n["text"] for n in cx["task"]["notes"])
    assert any("-10, the folds are small" in n["text"] for n in cx["from_person"])


def test_approve_is_for_the_person_and_for_commands(comp, capsys, monkeypatch):
    code, t = run_kx("task", "add", "x", capsys=capsys)
    code, err = run_kx("approve", t["id"], capsys=capsys)
    assert code == 1 and "waits on no command" in err["error"]
    run_kx("task", "wait", t["id"], "--on", "kx exec -- rm -rf /", capsys=capsys)  # not one kx approve runs
    code, err = run_kx("approve", t["id"], "--yes", capsys=capsys)
    assert code == 1 and "waits on no command" in err["error"]
    monkeypatch.setenv("KX_AGENT", "w1")
    code, err = run_kx("answer", t["id"], "fine", capsys=capsys)
    assert code == 1 and "person driving" in err["error"]


def test_brief_says_what_changed_since_the_person_last_looked(comp, capsys):
    old = scored_run("baseline", {"f0": 0.80, "f1": 0.80})
    hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    with Store(config.load(comp)) as st:
        st.update("runs", {"finished": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")}, id=old.id)
        st.set_setting("seen", {author(): hour_ago})
    new = scored_run("bigger model", {"f0": 0.90, "f1": 0.92})
    code, t = run_kx("task", "add", "dart", capsys=capsys)
    run_kx("task", "done", t["id"], "--result", "negative", "--outcome", "dart did not help", capsys=capsys)
    code, b = run_kx("brief", capsys=capsys)
    c = b["changes"]
    assert c["runs"]["done"] == 1 and c["best"]["run"] == new.name and c["best"]["was"]["run"] == old.name
    assert [x["run"] for x in c["scored"]] == [new.name] and c["scored"][0]["rank"] == 1
    assert c["tasks"][0]["outcome"] == "dart did not help" and c["tasks_added"][0]["title"] == "dart"
    kx_main(["brief"])
    text = capsys.readouterr().out
    assert "SINCE YOU LOOKED" in text and f"new best {new.name}" in text and "dart did not help" in text
    code, b = run_kx("brief", "--mark", capsys=capsys)
    code, again = run_kx("brief", capsys=capsys)
    assert b["marked"] and again["since"] == b["marked"]


def test_kx_alone_is_the_brief(comp, capsys):
    code, b = run_kx(capsys=capsys)
    assert code == 0 and b["competition"] == "demo" and "changes" in b and "needs_you" in b


def test_the_session_hook_briefs_the_person_not_a_worker(comp, capsys, monkeypatch):
    assert kx_main(["brief", "--hook"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("kx brief for demo") and "SINCE YOU LOOKED" in out and "NEEDS YOU" in out
    monkeypatch.setenv("KX_AGENT", "w1")
    assert kx_main(["brief", "--hook"]) == 0 and capsys.readouterr().out == ""
    monkeypatch.delenv("KX_AGENT")
    monkeypatch.chdir(comp.parent)  # not a competition: nothing, and no error
    assert kx_main(["brief", "--hook"]) == 0 and capsys.readouterr().out == ""


def test_walk_settles_decisions_one_by_one(comp, fake_kaggle, capsys, monkeypatch):
    from kx.cli import decide
    r = submittable(comp)
    code, t = run_kx("task", "add", "submit it", capsys=capsys)
    worker_asks(monkeypatch, capsys, "submit", r.name, "-m", "x", "--yes", task=t["id"])
    answers = iter(["1"])  # 1: Send; then Enter to anything else
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers, ""))
    decide.walk(needs(capsys))
    out = capsys.readouterr().out
    assert len(fake_kaggle.sent) == 1 and "[1/1]" in out and f"{t['id']} now waits on submission:S1-" in out
    assert r.name in out  # the preview came first


def test_notices_tell_each_thing_once(comp, fake_kaggle, capsys, monkeypatch, tmp_path):
    from kx import notify
    r = submittable(comp)
    code, t = run_kx("task", "add", "submit it", capsys=capsys)
    sent = []
    worker_asks(monkeypatch, capsys, "submit", r.name, "-m", "x", "--yes", task=t["id"])
    with Store(config.load(comp)) as st:
        lines = notify.check(st, send=lambda title, body, link: sent.append((title, body)) or True)
        assert any("waits on you: w1 asks to submit" in line for line in lines)
        assert sent[0][0] == "kx · demo: 1 thing needs you"
        assert notify.check(st, send=lambda *a: sent.append(a) or True) == [] and len(sent) == 1
    # through machine.yaml, where kx records something (a worker parking its task)
    out = tmp_path / "notice.txt"
    from kx import sandbox
    sandbox.MACHINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    sandbox.MACHINE_FILE.write_text("notify:\n  desktop: false\n  command: " + json.dumps(
        [sys.executable, "-c", f"import sys, pathlib; pathlib.Path({str(out)!r}).write_text(sys.argv[1] + '|' + sys.argv[2])"]) + "\n")
    monkeypatch.delenv("KX_NOTIFY")
    code, t2 = run_kx("task", "add", "a question", capsys=capsys)
    run_kx("task", "wait", t2["id"], "--on", "person: which folds?", capsys=capsys)
    assert out.read_text().startswith("kx · demo: 1 thing needs you|") and "which folds?" in out.read_text()
    code, res = run_kx("notify", "--test", capsys=capsys)
    assert code == 0 and res == {"command": True}


def test_what_the_dashboard_may_run(comp):
    from kx.views.actions import _safe, allowed
    assert allowed(["approve", "T1", "--yes"]) and allowed(["answer", "T1", "-rf: no"]) and allowed(["reap"])
    assert allowed(["task", "add", "try dart", "--body", "-why", "-p", "2"]) and allowed(["spawn", "T3", "-w", "codex-1"])
    assert allowed(["note", "r0012", "looks like leakage"]) and allowed(["brief", "--mark"])
    for bad in (["exec", "--", "rm", "-rf", "/"], ["submit", "r1", "--yes"], ["approve", "T1; rm"], ["answer", "T1", " "],
                ["gc", "--yes"], ["task", "add", "x", "--depends", "T1"], "reap", [], [1]):
        assert not allowed(bad), bad
    assert _safe(["answer", "T1", "-rf"]) == ["answer", "T1", "--", "-rf"]
    assert _safe(["task", "add", "-t", "--body", "-b", "-p", "2"]) == ["task", "add", "--body=-b", "-p", "2", "--", "-t"]


def test_a_new_task_and_notes_from_the_dashboard_actions(comp, capsys):
    from kx.views.actions import execute
    r = scored_run("baseline", {"f0": 0.8, "f1": 0.8})
    res = execute(["task", "add", "-check fold 3", "--body", "-it leaks?", "-p", "2"])
    assert res["code"] == 0 and res["data"]["title"] == "-check fold 3" and res["data"]["body"] == "-it leaks?"
    with Store(config.load(comp)) as st:  # the person looks at it a while after it ended
        st.update("runs", {"finished": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")}, id=r.id)
    execute(["note", r.name, "-this looks like leakage"])
    code, cx = run_kx("context", capsys=capsys)
    assert any(n["text"] == "-this looks like leakage" and n["ref"] == r.name for n in cx["from_person"])
    with pytest.raises(Exception):
        execute(["exec", "--", "true"])


def test_init_writes_the_session_hook_once(comp):
    from kx.setup.scaffold import HOOK, write_agent_files
    write_agent_files(comp)
    write_agent_files(comp)
    s = json.loads((comp / ".claude" / "settings.json").read_text())
    hooks = [h for m in s["hooks"]["SessionStart"] for h in m["hooks"]]
    assert [h["command"] for h in hooks] == [HOOK] and "Bash(kx:*)" in s["permissions"]["allow"]
    assert "kx approve T7" in (comp / "AGENTS.md").read_text()


def test_the_dashboard_settles_a_decision_with_the_token_only(comp, fake_kaggle, capsys, monkeypatch):
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from kx.views.dashboard import Handler

    r = submittable(comp)
    code, t = run_kx("task", "add", "submit it", capsys=capsys)
    tid = t["id"]
    worker_asks(monkeypatch, capsys, "submit", r.name, "-m", "x", "--yes", task=tid)
    Handler.comps, Handler.default, Handler.token = {"demo": comp}, "demo", "secret"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://localhost:{httpd.server_address[1]}/api"

    def post(argv, token="secret"):
        req = urllib.request.Request(f"{base}/do?comp=demo", data=json.dumps({"argv": argv}).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-KX-Token": token})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)
    try:
        assert post(["approve", tid, "--yes"], token="guess")[0] == 403
        code, d = post(["exec", "--", "true"])
        assert code == 400 and "not something the dashboard runs" in d["error"]
        code, d = post(["approve", tid])  # the preview the page shows before Send
        assert code == 200 and d["code"] == 3 and d["preview"]["run"] == r.name and not fake_kaggle.sent
        code, d = post(["approve", tid, "--yes"])
        assert code == 200 and d["code"] == 0 and len(fake_kaggle.sent) == 1 and "now waits on submission:S1" in d["text"]
        code, b = 200, json.load(urllib.request.urlopen(f"{base}/brief?comp=demo", timeout=10))
        assert b["changes"]["submissions"][0]["sent"] is True
        code, d = post(["answer", tid, "x"])  # it waits on the score now, not on the person: still answerable
        assert code == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
