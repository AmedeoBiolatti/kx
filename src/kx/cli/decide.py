"""Commands: kx alone, brief, approve, answer, notify. What the person does without learning kx: `kx` shows what
happened and walks them through what needs them; their agent does the same with approve and answer."""

from __future__ import annotations

import argparse
import contextlib
import os
import shlex
import sys
from pathlib import Path

from kx.board import decide, tasks
from kx.cli.common import Out, S
from kx.guard import actors
from kx.ledger import config
from kx.ledger.config import KxError
from kx.views.text import bold, dim, kv

GATED = (("submit",), ("kernel", "push"), ("dataset", "push"), ("gc",))  # they preview without --yes themselves


def cmd_approve(a):
    from kx.cli.main import call
    actors.not_worker("approving what a task waits on", fix="workers leave the command to their person: kx task wait")
    with S() as st:
        t = tasks.get(st, a.id)
        argv = decide.command(t["waiting_on"]) if t["status"] == "waiting" else None
        if argv is None:
            on = t["waiting_on"]
            raise KxError(f"{t['id']} waits on no command kx approve runs"
                          + (f" (it waits on: {on})" if t["status"] == "waiting" else f" (it is {t['status']})"),
                          fix=f'kx answer {t["id"]} "..."' if t["status"] == "waiting" else f"kx task show {t['id']}")
    words, tid, asked = decide.verb(argv), t["id"], t["waiting_on"]
    yes = f"kx approve {tid} --yes"
    # where the worker ran it: its worktree (a dataset's PATH, the task its branch names), else here
    where = Path(t["worktree"]) if t["worktree"] and Path(t["worktree"]).is_dir() else None
    here = contextlib.chdir(where) if where else contextlib.nullcontext()
    if not a.yes:  # what it would do: the command's own preview
        if words not in GATED:
            raise KxError(f"not run (preview): kx {shlex.join(argv)}", code=3, fix=yes,
                          data={"preview": {"task": tid, "command": f"kx {shlex.join(argv)}"}, "command": yes})
        try:
            with here:
                out = call(argv)
        except KxError as e:
            if e.code == 3:
                raise KxError(str(e), code=3, fix=yes, data={**(e.data or {}), "command": yes}) from None
            raise
        return Out(out.data, ((out.text or "").rsplit("\n  delete them:", 1)[0]) + f"\n  approve: {yes}", 3)
    with here:
        out = call(argv + (["--yes"] if words in GATED else []))
    with S() as st:
        if tasks.get(st, tid)["waiting_on"] == asked:  # the command did not settle it itself (gc, task merge)
            decide.mark(st, tid, f"ran `{asked}`, which this task waited on",
                        f"{decide.ANSWERED} {actors.current().name} ran kx {shlex.join(argv[:len(words) + 1])}")
        t = tasks.get(st, tid)
        resumed = _resume(st, t) if a.resume else None
    text = (out.text if out and out.text else f"ran kx {shlex.join(argv)}") + "\n" + _after(t, resumed)
    return Out({"task": t, "output": out.data if out else None, "resumed": resumed}, text)


def cmd_answer(a):
    actors.not_worker("answering a task", fix="say it in your task's notes: kx task note")
    with S() as st:
        t = decide.answer(st, a.id, " ".join(a.text))
        resumed = _resume(st, t) if a.resume else None
    return Out({"task": t, "resumed": resumed}, _after(t, resumed))


def _resume(st, t: dict) -> dict | None:
    from kx.workers.spawn import spawn
    who = decide.resumer(st, t["id"])
    if not who:
        raise KxError(f"no worker to resume {t['id']} with (none worked on it, or it is running)",
                      fix=f"kx spawn {t['id']} -w NAME")
    return spawn(st, who, t["id"])


def _after(t: dict, resumed: dict | None) -> str:
    on = t["waiting_on"] or ""
    line = (f"{t['id']} can go on: {decide.resolved(on)}" if decide.resolved(on) is not None
            else f"{t['id']} now waits on {on}" if t["status"] == "waiting" else f"{t['id']} is {t['status']}")
    if resumed:
        return line + f"\nworker {resumed['worker']} resumes it ({resumed['id']})"
    return line


def cmd_brief(a):
    from kx.views import brief
    if a.hook:  # a Claude Code session starting: the person's brief, or nothing (a worker, not a competition, an error)
        if os.environ.get("KX_AGENT"):
            return None
        try:
            with S() as st:
                b = brief.brief(st)
                brief.mark(st)
        except Exception:  # noqa: BLE001 - a hook never stops a session
            return None
        return Out(b, brief.hook_text(b))
    with S() as st:
        since = None
        if a.hours is not None:
            from datetime import datetime, timedelta, timezone
            since = (datetime.now(timezone.utc) - timedelta(hours=a.hours)).isoformat(timespec="seconds")
        b = brief.brief(st, since)
        if a.mark:
            actors.not_worker("marking what the person has seen")
            b["marked"] = brief.mark(st)
    return Out(b, brief.render(b))


def cmd_notify(a):
    from kx import notify
    if a.test:
        c = notify.config()
        if not c:
            raise KxError("notices are off on this machine",
                          fix=f"notify: {{ntfy: https://ntfy.sh/A-LONG-RANDOM-TOPIC}} in {_machine_file()} "
                              "(desktop notices need notify-send and a display)")
        res = {}
        title, body = "kx · test", "Notices from kx reach you here."
        if c.get("desktop"):
            res["desktop"] = notify.desktop(title, body)
        if c.get("ntfy"):
            res["ntfy"] = notify.ntfy(c["ntfy"], title, body, None)
        if c.get("command"):
            res["command"] = notify.command(c["command"], title, body, None)
        text = "\n".join(f"{k}: {'sent' if v else 'FAILED'}" for k, v in res.items())
        return Out(res, text, 0 if all(res.values()) else 1)
    with S() as st:
        lines = notify.check(st)
    return Out({"sent": lines}, "\n".join(lines) if lines else "nothing new to tell (or notices are off: kx notify --test)")


def _machine_file():
    from kx import sandbox
    return sandbox.MACHINE_FILE


# -- kx alone ---------------------------------------------------------------------------------------------------------

def cmd_home(a):
    """kx alone: the brief; on a terminal, the person then settles what needs them, one by one."""
    from kx.views import brief
    config.find_root()  # outside a competition: kx says so, with how to start one
    interactive = (sys.stdin.isatty() and sys.stdout.isatty() and not a.json and not os.environ.get("KX_AGENT"))
    with S() as st:
        b = brief.brief(st)
        if interactive:
            brief.mark(st)
    if not interactive:
        return Out(b, brief.render(b))
    print(brief.render(b, needs=False))
    walk(b["needs_you"])
    return None


def walk(items: list[dict]):
    from kx.views import actions
    from kx.views.brief import needs_text
    decisions = [x for x in items if x["who"] == "decision"]
    chores = [x for x in items if x["who"] == "chore" and x["actions"]]
    if not decisions and not chores:
        print(needs_text(items))
        return
    print(needs_text(items))
    for i, x in enumerate(decisions, 1):
        acts = x["actions"]
        print("\n" + bold(f"[{i}/{len(decisions)}] {x['what']}"))
        if not acts:
            print(dim(f"  {x['fix']}"))
            if _ask("  Enter: next · q: stop > ").lower() == "q":
                return
            continue
        prev = next((p for p in acts if p.get("preview")), None)
        if prev:
            print(dim("  preparing the preview…"))
            try:
                r = actions.execute(prev["preview"])
                _show(r)
            except KxError as e:
                print(f"  {e}" + (f"\n  fix: {e.fix}" if e.fix else ""))
        menu = "  " + " · ".join(f"{j}: {p['label']}" for j, p in enumerate(acts, 1)) + " · Enter: skip · q: stop > "
        choice = _ask(menu).lower()
        if choice == "q":
            return
        if not choice.isdigit() or not 1 <= int(choice) <= len(acts):
            continue
        p = acts[int(choice) - 1]
        argv = list(p["argv"])
        if p.get("input"):
            text = _ask(f"  {p['input']}: ")
            if not text or text == "q":
                print(dim("  nothing said: skipped"))
                continue
            argv.append(text)
        try:
            r = actions.execute(argv)
            print("  " + (r.get("text") or "done").replace("\n", "\n  "))
        except KxError as e:
            print(f"  {e}" + (f"\n  fix: {e.fix}" if e.fix else ""))
            continue
        if p.get("resume") and _ask(f"  Resume it with {p['resume'][-1]}? [Y/n] ").lower() in ("", "y", "yes"):
            try:
                r = actions.execute(p["resume"])
                print("  " + (r.get("text") or "started").replace("\n", "\n  "))
            except KxError as e:
                print(f"  {e}" + (f"\n  fix: {e.fix}" if e.fix else ""))
    if chores:
        cmds = list(dict.fromkeys(tuple(c["actions"][0]["argv"]) for c in chores))
        print("\n" + bold("Chores") + dim(": " + " · ".join("kx " + " ".join(c) for c in cmds)))
        if _ask("  Do them now? [Y/n] ").lower() in ("", "y", "yes"):
            for c in cmds:
                try:
                    r = actions.execute(list(c))
                    print("  " + (r.get("text") or "done").replace("\n", "\n  "))
                except KxError as e:
                    print(f"  kx {' '.join(c)}: {e}")


def _ask(prompt: str) -> str:
    """What the person typed; "q" when they closed the input (Ctrl-D, Ctrl-C)."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return "q"


def _show(r: dict):
    if r.get("preview") is not None:  # the artifact's path here says nothing its sha does not
        print(kv({k: v for k, v in r["preview"].items() if not (k == "target" and str(v).startswith("/"))}, indent=4))
        for p in r.get("problems") or []:
            print(f"    problem: {p}")
    elif r.get("text"):
        print("    " + r["text"].replace("\n", "\n    "))


def register(cmd, group, action):
    s = cmd("brief", cmd_brief, "what happened since you last looked, and what needs you (kx alone, on a terminal: "
                                "settle it there)")
    s.add_argument("--hours", type=float, help="since this many hours ago, instead of since you last looked")
    s.add_argument("--mark", action="store_true", help="you have seen it: the next brief starts from now")
    s.add_argument("--hook", action="store_true", help=argparse.SUPPRESS)  # the SessionStart hook kx init writes
    s = cmd("approve", cmd_approve, "run the kx command a task waits on, as you (a preview; --yes runs it)")
    s.add_argument("id", help="the task, e.g. T7")
    s.add_argument("-y", "--yes", action="store_true", help="run it")
    s.add_argument("--resume", action="store_true", help="then start the worker that asked again on the task")
    s = cmd("answer", cmd_answer, "reply to what a task waits on (to a command: it is not run, and why)")
    s.add_argument("id", help="the task, e.g. T7")
    s.add_argument("text", nargs="+")
    s.add_argument("--resume", action="store_true", help="then start the worker that asked again on the task")
    s = cmd("notify", cmd_notify, "send what is new as a notice now (kx does when it records something); "
                                  "--test: try each channel")
    s.add_argument("--test", action="store_true")
