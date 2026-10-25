"""The worker runner (`kx worker-run ID`, started by `kx spawn`): it runs the harness to the end, keeps its stream as
the transcript, charges tokens to the budget as they stream and stops the harness at a cap. When the harness ends it
commits what the worker left uncommitted and reopens the task if it was not finished. Nothing is retried."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from kx.guard import actors, budgets
from kx.ledger.config import KxError
from kx.ledger.store import Store, now, pid_alive
from kx.runs import git
from kx.workers.streams import State, parse_line

UPDATE_EVERY_S = 5


RESULT_GRACE_S = 60


def _worker_actor(name: str) -> actors.Actor:
    return actors.Actor(name, True)


def run(comp, wid: str, *, env: dict | None = None, echo: bool = False) -> int:
    """Run the worker's harness to the end (the body of `kx worker-run ID`)."""
    store = Store(comp)
    w = dict(store.one("SELECT * FROM workers WHERE id=?", (wid,)))
    argv = json.loads(w["argv"])
    st = State(w["harness"])
    me = _worker_actor(w["agent"])
    transcript = Path(w["transcript"])
    store.x("UPDATE workers SET pid=? WHERE id=?", (os.getpid(), wid))
    try:
        with open(transcript.with_suffix(".stderr"), "ab") as err:  # the child keeps its own copy of the handle
            p = subprocess.Popen(argv, cwd=w["worktree"], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=err, start_new_session=True, text=True, bufsize=1)
    except OSError as e:
        store.x("UPDATE workers SET finished=?, code=127, stop=? WHERE id=?", (now(), f"cannot start: {e}", wid))
        _wrap_task(store, w, f"its harness could not start ({e})")
        store.close()
        return 127
    store.x("UPDATE workers SET harness_pid=? WHERE id=?", (p.pid, wid))
    charged, last, stopped = 0, 0.0, None
    watchdog = None
    with open(transcript, "a") as out:
        for line in p.stdout:
            out.write(line)
            out.flush()
            parse_line(line, st)
            if st.result is not None and watchdog is None and w["harness"] == "claude":
                # the agent ended its turn; a headless harness keeps running while commands it left in the background
                # do: give them a grace period, then stop them (the prompt says to wait inside the session)
                watchdog = threading.Timer(RESULT_GRACE_S, _after_result, (store.comp.db_path, wid, p.pid))
                watchdog.daemon = True
                watchdog.start()
            if echo:
                print(f"[{w['agent']}] {st.last_action[:160]}", file=sys.stderr)
            if time.monotonic() - last >= UPDATE_EVERY_S:
                last = time.monotonic()
                charged, stopped = _update(store, wid, st, me, charged, p, stopped)
    code = p.wait()
    if watchdog is not None:
        watchdog.cancel()
    charged, stopped = _update(store, wid, st, me, charged, None, stopped)
    row = store.one("SELECT stop FROM workers WHERE id=?", (wid,))
    store.x("UPDATE workers SET finished=?, code=?, result=?, stop=COALESCE(stop, ?) WHERE id=?",
            (now(), code, (st.result or st.text or "")[-4000:], stopped, wid))
    _commit_leftovers(Path(w["worktree"]), w["agent"], w["task"])
    why = None
    if (row["stop"] or stopped):
        why = f"it was stopped ({row['stop'] or stopped})"
    elif code != 0:
        why = f"its harness exited with code {code}"
    _wrap_task(store, w, why or "the session ended without kx task done/wait", last=st.result or st.text)
    store.close()
    return code


def _update(store: Store, wid: str, st: State, me, charged: int, p, stopped):
    store.x("UPDATE workers SET updated=?, turns=?, tokens_in=?, tokens_out=?, cost_usd=?, plan_5h=?, last_action=? WHERE id=?",
            (now(), st.turns, st.tokens_in, st.tokens_out, st.cost_usd, st.plan_5h, st.last_action, wid))
    if st.tokens > charged:
        budgets.charge(store, me, "tokens", st.tokens - charged, wid)
        charged = st.tokens
    if p is not None and stopped is None:
        try:
            budgets.check(store, me, {"tokens": 1})
        except KxError as e:
            stopped = "budget: " + str(e)[:300]
            store.x("UPDATE workers SET stop=? WHERE id=?", (stopped, wid))
            _terminate(p.pid)
    return charged, stopped


def _after_result(db_path, wid: str, pid: int):
    if pid_alive(pid) is True:
        import sqlite3
        db = sqlite3.connect(db_path, timeout=60, isolation_level=None)
        db.execute("UPDATE workers SET stop=COALESCE(stop, ?) WHERE id=?",
                   (f"ended its turn; background commands still running after {RESULT_GRACE_S}s were stopped", wid))
        db.close()
        _terminate(pid)


def _terminate(pid: int):
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    for _ in range(50):
        if pid_alive(pid) is False:
            return
        time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _commit_leftovers(wt: Path, agent: str, task: str):
    g = git.info(wt)
    if not g or not [c for c in g["changes"] if c[3:].rstrip("/") not in ("data", ".kx")]:
        return
    subprocess.run(["git", "add", "-A"], cwd=wt, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", f"kx: work left uncommitted by {agent} on {task}"], cwd=wt, capture_output=True)


def _wrap_task(store: Store, w: dict, why: str, last: str | None = None):
    """A task the worker left active goes back to open, with why and its last words, so it shows in kx status."""
    t = store.one("SELECT status, owner FROM tasks WHERE id=?", (w["task"],))
    if t is None or t["status"] != "active" or not store.owns(t["owner"], w["agent"]):
        return
    text = f"worker {w['agent']} stopped: {why}." + (f" Last message: {last.strip()[:1500]}" if last else "")
    with store.transaction():
        store.insert("task_notes", {"task": w["task"], "ts": now(), "author": "kx", "text": text})
        store.update("tasks", {"status": "open", "owner": None, "updated": now()}, id=w["task"], owner=t["owner"])

