"""Commands: status, context, dashboard."""

from __future__ import annotations

from pathlib import Path

from kx.board import tasks
from kx.cli.common import Out, S, task_here
from kx.guard import actors
from kx.ledger import config
from kx.ledger.config import KxError
from kx.ledger.store import Store
from kx.views.text import bold


def cmd_status(a):
    from kx.views import status
    if a.all:
        ws = config.workspace_of(config.load()) if _in_comp() else Path.cwd()
        comps, data, texts = config.competitions(ws), [], []
        for root in comps:
            try:
                with Store(config.load(root)) as st:
                    d = status.status(st)
                d["competition"]["folder"] = root.name
                data.append(d)
                texts.append(status.render_status(d, short=True))
            except KxError as e:
                data.append({"competition": {"slug": root.name}, "error": str(e)})
                texts.append(bold(root.name) + f"\n  {e}\n")
        if not comps:
            raise KxError(f"no competitions (folders with comp.yaml) in {ws}")
        return Out(data, "\n".join(texts))
    with S() as st:
        d = status.status(st)
    return Out(d, status.render_status(d))


def _in_comp() -> bool:
    try:
        config.find_root()
        return True
    except KxError:
        return False


def cmd_context(a):
    from kx.views import status
    with S() as st:
        t = tasks.parse_id(a.task) if a.task else task_here()
        cx = status.context(st, actors.current(), t)
    return Out(cx, status.render_context(cx))


def cmd_dashboard(a):
    from kx.views import dashboard
    dashboard.serve(a.port, a.open)
    return None


def register(cmd, group, action):
    s = cmd("status", cmd_status, "what is running, what needs you, results, submissions, budgets")
    s.add_argument("--all", action="store_true", help="every competition in the workspace")
    s = cmd("context", cmd_context, "what an agent needs before it starts (task, reference protocol, best runs, rules)")
    s.add_argument("--task", help="the task to describe (default: KX_TASK or the task branch)")
    s = cmd("dashboard", cmd_dashboard, "local web page: status, runs, tasks, findings, submissions")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open", action="store_true", help="open it in the browser")
