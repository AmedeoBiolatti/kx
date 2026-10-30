"""Commands: spawn, workers, wait, stop (and the hidden worker-run the runner uses)."""

from __future__ import annotations

import argparse
import os
import shlex
import time
from pathlib import Path

from kx.cli.common import Out
from kx.ledger import config
from kx.ledger.config import KxError
from kx.ledger.store import Store, age_s
from kx.views.text import ago, bold, dim, dur, table, trunc
from kx.workers.runner import run
from kx.workers.spawn import live, spawn, stop
from kx.workers.streams import replay

# status items that do not end a `kx wait`: they are advice, not events
QUIET = ("no_reference", "sync", "stale_task", "protocol_churn")


def cmd_spawn(a):
    with Store(config.load(), auto_backup=True) as st:
        res = spawn(st, a.worker, a.task, model=a.model, effort=a.effort, foreground=a.foreground, dry_run=a.dry_run,
                    force=a.force)
    if res.get("dry_run"):
        return Out(res, " ".join(shlex.quote(x) for x in res["argv"]))
    text = f"worker {res['worker']} started on {res['task']} ({res['id']})\n  worktree {res['worktree']}"
    if "pid" in res:
        text += f"\n  follow it: kx workers --show {res['id']} · stop it: kx stop {res['id']}"
    return Out(res, text)

def cmd_workers(a):
    with Store(config.load()) as st:
        if a.show:
            w = st.one("SELECT * FROM workers WHERE id LIKE ? ORDER BY started DESC LIMIT 1", (a.show + "%",))
            if w is None:
                raise KxError(f"no worker session {a.show!r}", fix="kx workers --all")
            w = dict(w)
            rs = replay(w)
            stderr = ""
            if w["transcript"]:
                p = Path(w["transcript"]).with_suffix(".stderr")
                stderr = p.read_text(errors="replace")[-2000:] if p.exists() else ""
            d = {**w, "actions": [{"t": ts, "what": x} for ts, x in rs.actions[-a.n:]], "errors": rs.errors[-10:],
                 "stderr_tail": stderr}
            state = "running" if not w["finished"] else f"ended {ago(w['finished'])} ago, exit {w['code']}" + \
                (f" ({w['stop']})" if w["stop"] else "")
            lines = [bold(f"{w['agent']} on {w['task']}") + dim(f"  {w['id']} · {w['harness']} {w['model'] or ''} · {state}"),
                     dim(f"  {w['turns'] or 0} turns · {((w['tokens_in'] or 0) + (w['tokens_out'] or 0)):,} tokens"
                         + (f" · ${w['cost_usd']:.2f} API-equivalent" if w["cost_usd"] else "")
                         + (f" · plan 5h {w['plan_5h'] * 100:.0f}%" if w["plan_5h"] is not None else "")),
                     dim(f"  worktree {w['worktree']} · transcript {w['transcript']}"), ""]
            lines += [f"  {trunc(x['what'], 200)}" for x in d["actions"]]
            if d["errors"]:
                lines += ["", bold("errors")] + [f"  {trunc(e, 200)}" for e in d["errors"]]
            if w["result"]:
                lines += ["", bold("last message"), "  " + w["result"].strip()[-2000:].replace("\n", "\n  ")]
            return Out(d, "\n".join(lines))
        running = live(st)
        recent = [] if not a.all else [dict(r) for r in st.all("SELECT * FROM workers WHERE finished IS NOT NULL "
                                                                "ORDER BY started DESC LIMIT ?", (a.n,))]
    rows = [[w["id"], w["agent"], w['task'], "running" if not w["finished"] else f"exit {w['code']}",
             dur(age_s(w["started"])), f"{((w['tokens_in'] or 0) + (w['tokens_out'] or 0)):,}",
             w["stop"] or w["last_action"] or ""] for w in running + recent]
    return Out({"running": running, "recent": recent},
               table(rows, ["id", "worker", "task", "state", "age", "tokens", "doing / why it stopped"]) if rows else
               "no workers running" + ("" if a.all else " (--all: recent ones)"))

def cmd_stop(a):
    with Store(config.load()) as st:
        res = stop(st, a.id)
    return Out(res, f"stopped {res['worker']} ({res['id']})")

def _wait_for(a):
    """kx wait TARGET ...: until one of them has happened, or can no longer happen."""
    from kx import wait
    from kx.cli.common import argv
    began = time.monotonic()
    deadline = began + a.timeout * 60
    ts = wait.targets(a.targets, a.until or (), a.stall * 60)
    settled = {}  # target name -> its event; with --all, each target is checked until it has one
    while True:
        new = [ev for t in ts if t.name not in settled and (ev := t.check())]
        settled.update((ev["target"], ev) for ev in new)
        events = list(settled.values())
        if events and (not a.all or len(settled) == len(ts)):
            met = all(ev["met"] for ev in events) if a.all else any(ev["met"] for ev in events)
            text = "\n".join(wait.line(ev) for ev in events)
            if a.all and len(events) > 1:
                n_met = sum(ev["met"] for ev in events)
                text += f"\nall {len(events)} settled: {n_met} as waited for" + ("" if met else f", {len(events) - n_met} not")
            return Out({"met": met, "events": events, "waited_s": round(time.monotonic() - began)}, text,
                       0 if met else wait.UNMET)
        left = deadline - time.monotonic()
        if left <= 0:
            again = "kx " + " ".join(shlex.quote(x) for x in argv())
            pending = [t.name for t in ts if t.name not in settled]
            text = "\n".join(wait.line(ev) for ev in events)
            return Out({"met": False, "events": events, "pending": pending, "why": "timeout", "again": again},
                       (text + "\n" if text else "") + f"nothing more after {a.timeout:g} min: "
                       f"{', '.join(pending)} still pending\n  wait again: {again}", 1)
        time.sleep(min(a.poll, left))


def cmd_wait(a):
    if a.targets:
        return _wait_for(a)
    if a.until:
        raise KxError("--until needs a run to wait for", fix="kx wait r12 --until 'val_auc>=0.9'", code=2)
    from kx.views.status import needs_you
    deadline = time.monotonic() + a.timeout * 60

    def snapshot():
        with Store(config.load()) as st:
            ws = {w["id"]: w["agent"] for w in live(st)}
            rs = {r["id"]: r["hypothesis"] for r in st.all("SELECT id, hypothesis FROM runs WHERE status='running'")}
            nd = {(x["kind"], x["ref"]): x for x in needs_you(st) if x["kind"] not in QUIET}
        return ws, rs, nd
    ws0, rs0, nd0 = snapshot()
    if not ws0 and not (a.runs and rs0):
        return Out({"changed": [], "why": "nothing running"}, "nothing is running: nothing to wait for")
    while time.monotonic() < deadline:
        time.sleep(a.poll)
        ws, rs, nd = snapshot()
        changed = [{"kind": "worker_finished", "id": i, "worker": n} for i, n in ws0.items() if i not in ws]
        if a.runs:
            changed += [{"kind": "run_finished", "run": i, "hypothesis": h} for i, h in rs0.items() if i not in rs]
        changed += [{"kind": "needs_you", **x} for k, x in nd.items() if k not in nd0]
        if changed:
            lines = []
            with Store(config.load()) as st:
                for c in changed:
                    if c["kind"] == "worker_finished":
                        w = st.one("SELECT * FROM workers WHERE id=?", (c["id"],))
                        c.update(task=w['task'], code=w["code"], stop=w["stop"], result=(w["result"] or "")[-600:])
                        lines.append(f"worker {w['agent']} on {w['task']} ended (exit {w['code']}"
                                     + (f", {w['stop']}" if w["stop"] else "") + f"): {trunc(w['result'] or '', 300)}")
                    elif c["kind"] == "run_finished":
                        r = st.one("SELECT status FROM runs WHERE id=?", (c["run"],))
                        c["status"] = r["status"] if r else None
                        lines.append(f"{c['run']} {c['status']}: {trunc(c['hypothesis'] or '', 200)}")
                    else:
                        lines.append(f"needs you: {c['what']}  ({c['fix']})")
            return Out({"changed": changed}, "\n".join(lines))
    return Out({"changed": [], "why": "timeout"}, f"nothing changed in {a.timeout:g} min", 1)

def cmd_worker_run(a):
    comp = config.load()
    return Out(None, None, run(comp, a.id, env=dict(os.environ)))


def register(cmd, group, action):
    s = cmd("spawn", cmd_spawn, "start a background worker on a task, in its own worktree")
    s.add_argument("task")
    s.add_argument("--worker", "-w", required=True, help="a name from comp.yaml workers:")
    s.add_argument("--model")
    s.add_argument("--effort")
    s.add_argument("--foreground", action="store_true", help="run it here, printing what it does")
    s.add_argument("--dry-run", action="store_true", help="print the command line, start nothing")
    s.add_argument("--force", action="store_true", help="even if the task is active, blocked, or max_workers is reached")
    s = cmd("workers", cmd_workers, "running workers (--all: recent ones; --show ID: what it did)")
    s.add_argument("--all", action="store_true")
    s.add_argument("--show")
    s.add_argument("-n", type=int, default=25)
    s = cmd("wait", cmd_wait, "block until a run ends or reaches --until, a submission is scored, a kernel finishes "
                              "(exit 0; 6 when it failed or stalled instead); with no target, until a worker ends or "
                              "something new needs you")
    s.add_argument("targets", nargs="*", metavar="TARGET",
                   help="r12 (a run), group:NAME (its runs: kx exec --group), S4 (a submission), kernel:r12 (the kernel "
                        "pushed for r12): returns on the first (--all: once each has)")
    s.add_argument("--all", action="store_true", help="return once every target has ended, met or not (exit 0 only "
                   "when all were as waited for)")
    s.add_argument("--until", action="append", metavar="COND",
                   help="for runs: NAME OP NUMBER on a metric it logs or a score (val_auc>=0.91, loss>5, cv5>=0.8, "
                        "score>=0.8 for the reference protocol); repeatable, any one ends the wait")
    s.add_argument("--stall", type=float, default=30,
                   help="minutes a running run may go without progress, metrics or output (0: no limit)")
    s.add_argument("--timeout", type=float, default=120, help="minutes (exit 1 when nothing changed)")
    s.add_argument("--poll", type=float, default=30, help="seconds between checks (Kaggle: at most every minute)")
    s.add_argument("--runs", action="store_true", help="with no target: also return when a running run finishes")
    s = cmd("stop", cmd_stop, "stop a running worker")
    s.add_argument("id")
    s = cmd("worker-run", cmd_worker_run, argparse.SUPPRESS)
    s.add_argument("id")
