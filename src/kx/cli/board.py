"""Commands: task, finding."""

from __future__ import annotations

import sys

from kx.board import findings, tasks
from kx.cli.common import Out, S, task_here
from kx.guard import actors
from kx.ledger.config import KxError
from kx.views.text import ago, bold, dim, table, trunc


def _task_text(t: dict, notes: list[dict] | None = None) -> str:
    head = bold(f"{t['id']} {t['title']}") + dim(f"  {t['status']}" + (f" · {t['result']}" if t["result"] else "")
                                                   + (f" · {t['owner']}" if t["owner"] else "") + f" · p{t['priority']}")
    lines = [head]
    if t.get("waiting_on"):
        lines.append(f"  waiting on: {t['waiting_on']}")
    if t["depends"]:
        lines.append(f"  depends on: {', '.join(t['depends'])}")
    if t["branch"]:
        lines.append(dim(f"  branch {t['branch']}" + (f" · worktree {t['worktree']}" if t["worktree"] else "")))
    if t["body"]:
        lines += ["", "  " + t["body"].strip().replace("\n", "\n  ")]
    if t["outcome"]:
        lines += ["", bold("outcome"), "  " + t["outcome"].strip().replace("\n", "\n  ")]
    if notes:
        lines += ["", bold("notes")] + [f"  {ago(n['ts'])} ago, {n['author']}: {n['text']}" for n in notes]
    return "\n".join(lines)


def cmd_task(a):
    with S() as st:
        act = a.action
        if act == "list":
            rows = tasks.listing(st, all_=a.all)
            trows = []
            for t in rows:
                state = t["status"] + (f"/{t['result']}" if t["result"] else "")
                why = (t["waiting_on"] if t["status"] == "waiting" else
                       f"after {', '.join(t['blocked_by'])}" if t["blocked_by"] else
                       (t["last_note"]["text"] if t["last_note"] else ""))
                trows.append([t['id'], f"p{t['priority']}", state, t["owner"] or "", trunc(t["title"], 60), why])
            return Out(rows, table(trows, ["task", "p", "state", "owner", "title", "note"]) if trows else "no open tasks")
        if act == "add":
            t = tasks.add(st, a.title, a.body or "", a.priority, a.depends or ())
            return Out(t, f"added {t['id']}: {t['title']}")
        if act == "relink":
            plan = tasks.relink_plan(st)
            n = sum(len(v) for v in plan.values())
            data = {"tasks": plan, "runs": n, "linked": a.yes and n > 0}
            if not n:
                return Out(data, "no unlinked run names a task")
            text = "\n".join(f"  {k}: {len(v)} run(s), {v[0]}" + (f" .. {v[-1]}" if len(v) > 1 else "")
                             for k, v in sorted(plan.items()))
            if not a.yes:
                return Out(data, f"{n} run(s) linked to no task start with one:\n{text}\n  link them: kx task relink --yes", 3)
            with st.transaction():
                for k, v in plan.items():
                    for r in v:
                        st.update("runs", {"task": k}, id=r, task=None)
            return Out(data, f"linked {n} run(s) to the task their hypothesis starts with:\n{text}")
        t_id = a.id
        if act == "show":
            t = tasks.get(st, t_id)
            n = tasks.notes(st, t_id)
            runs = [dict(r) for r in st.all("SELECT id, status, hypothesis FROM runs WHERE task=? ORDER BY n, id", (t["id"],))]
            fnd = [dict(f) for f in st.all("SELECT id, status, text FROM findings WHERE task=?", (t["id"],))]
            text = _task_text(t, n)
            if runs:
                text += "\n\n" + bold("runs") + "\n" + table([[r["id"], r["status"], r["hypothesis"] or ""] for r in runs], indent=2)
            if fnd:
                text += "\n\n" + bold("findings") + "\n" + "\n".join(f"  {f['id']} [{f['status']}] {trunc(f['text'], 120)}" for f in fnd)
            return Out({**t, "notes": n, "runs": runs, "findings": fnd}, text)
        if act == "start":
            t = tasks.start(st, t_id)
            others = [x for x in tasks.active_for(st, None) if x != t["id"]]
            return Out(t, f"{t['id']} is active ({t['owner']}): your runs are linked to it until you finish it or start "
                          "another" + (dim(f"\n  also active for you: {', '.join(others)} (kx task done / wait / reopen)")
                                       if others else ""))
        if act == "note":
            t = tasks.note(st, t_id, " ".join(a.text))
            return Out(t, f"noted on {t['id']}")
        if act == "wait":
            t = tasks.wait(st, t_id, a.on, a.note or "")
            return Out(t, f"{t['id']} waits on {a.on}")
        if act == "done":
            t = tasks.finish(st, t_id, a.result, a.outcome)
            hint = f"\n  merge its branch: kx task merge {t['id']}" if t["worktree"] and not actors.current().worker else ""
            return Out(t, f"{t['id']} done ({t['result']}){hint}")
        if act == "reopen":
            t = tasks.reopen(st, t_id)
            return Out(t, f"{t['id']} is open again")
        if act == "edit":
            t = tasks.edit(st, t_id, title=a.title, body=a.body, priority=a.priority, depends=a.depends)
            return Out(t, _task_text(t))
        if act == "merge":
            actors.not_worker("merging into the main checkout")
            out = tasks.merge(st, t_id)
            return Out({"output": out}, out)
    raise KxError(f"unknown task action {act}")


def _finding_line(f: dict) -> str:
    tags = f" [{', '.join(f['tags'])}]" if f["tags"] else ""
    ev = f" (evidence {', '.join(f['evidence'])})" if f["evidence"] else ""
    return f"{f['id']} {f['status']}{tags}: {f['text']}{dim(ev)}"


def cmd_finding(a):
    with S() as st:
        act = a.action
        if act == "add":
            f = findings.add(st, a.text, a.evidence or (), a.tag or (), st.resolve(a.task, "tasks") if a.task else task_here(st),
                             partial=a.partial)
            unknown = [t for t in f["tags"] if st.comp.tags and t not in st.comp.tags]
            if unknown:
                print(f"kx: note: {', '.join(unknown)} not in comp.yaml tags", file=sys.stderr)
            return Out(f, f"added {f['id']}")
        if act == "list":
            fs = findings.listing(st, all_=a.all, tag=a.tag)
            return Out(fs, "\n".join(_finding_line(f) for f in fs) or "no findings")
        if act == "show":
            f = findings.get(st, a.id)
            extra = ""
            if f["review_note"] or f["superseded_by"]:
                extra = dim(f"\n  {f['reviewed_by']}: {f['review_note'] or ''}" + (f" (superseded by F{f['superseded_by']})"
                                                                                    if f["superseded_by"] else ""))
            return Out(f, _finding_line(f) + extra)
        if act in ("withdraw", "supersede"):
            status = {"withdraw": "withdrawn", "supersede": "superseded"}[act]
            f = findings.review(st, a.id, status, a.note or "", getattr(a, "by", None))
            return Out(f, _finding_line(f))
    raise KxError(f"unknown finding action {act}")


def register(cmd, group, action):
    g = group("task", "the task board")
    s = action(g, "list", cmd_task, "open tasks (--all: every task)")
    s.add_argument("--all", action="store_true")
    s = action(g, "add", cmd_task, "add a task")
    s.add_argument("title")
    s.add_argument("--body")
    s.add_argument("--priority", "-p", type=int, default=0)
    s.add_argument("--depends", nargs="+")
    for name, help_ in (("show", "a task with its notes, runs and findings"), ("reopen", "open a done task again"),
                        ("merge", "merge the task branch into the main checkout and remove its worktree")):
        s = action(g, name, cmd_task, help_)
        s.add_argument("id")
    s = action(g, "start", cmd_task, "mark a task active (yours) and current: your runs are linked to it")
    s.add_argument("id")
    s = action(g, "relink", cmd_task, "link the runs linked to no task to the task their hypothesis starts with "
                                      "(\"T161 ...\"; preview, --yes links)")
    s.add_argument("-y", "--yes", action="store_true")
    s = action(g, "note", cmd_task, "add a progress line")
    s.add_argument("id")
    s.add_argument("text", nargs="+")
    s = action(g, "wait", cmd_task, "park a task on something (run:rN, submission:SN, T7, a command for your person, or text)")
    s.add_argument("id")
    s.add_argument("--on", required=True)
    s.add_argument("--note")
    s = action(g, "done", cmd_task, "finish a task with its result and what was learned")
    s.add_argument("id")
    s.add_argument("--result", required=True, choices=tasks.RESULTS)
    s.add_argument("--outcome", required=True)
    s = action(g, "edit", cmd_task, "change a task")
    s.add_argument("id")
    s.add_argument("--title")
    s.add_argument("--body")
    s.add_argument("--priority", "-p", type=int)
    s.add_argument("--depends", nargs="*")

    g = group("finding", "conclusions backed by runs")
    s = action(g, "add", cmd_finding, "record a finding")
    s.add_argument("text")
    s.add_argument("--evidence", nargs="+")
    s.add_argument("--tag", action="append")
    s.add_argument("--task")
    s.add_argument("--partial", action="store_true", help="evidence may be still running (the finding says so)")
    s = action(g, "list", cmd_finding, "open findings (--all: every one)")
    s.add_argument("--all", action="store_true")
    s.add_argument("--tag")
    s = action(g, "show", cmd_finding, "one finding")
    s.add_argument("id")
    s = action(g, "withdraw", cmd_finding, "withdraw a finding")
    s.add_argument("id")
    s.add_argument("--note")
    s = action(g, "supersede", cmd_finding, "replace a finding by a newer one")
    s.add_argument("id")
    s.add_argument("--by", required=True)
    s.add_argument("--note")
