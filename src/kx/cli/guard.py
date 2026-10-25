"""Commands: budget."""

from __future__ import annotations

from kx.cli.common import Out, S
from kx.guard import actors, budgets
from kx.views.text import dim, table


def cmd_budget(a):
    with S() as st:
        rows = budgets.report(st, actors.current())
        other = []
        for m in budgets.METRICS:
            for w in budgets.WINDOWS:
                if not any(r["metric"] == m and r["window"] == w for r in rows):
                    u = budgets.used(st, m, w)
                    if u:
                        other.append({"metric": m, "window": w, "used": u})
    if not rows and not other:
        return Out({"caps": [], "uncapped": []}, "no budgets set (comp.yaml budgets:) and nothing used yet")
    lines = []
    if rows:
        lines.append(table([[r["scope"], r["key"], f"{budgets.fmt(r['metric'], r['used'])} / {budgets.fmt(r['metric'], r['cap'])}",
                             budgets.fmt(r["metric"], r["left"]) + " left", r["frees_up"]] for r in rows],
                           ["scope", "budget", "used / cap", "left", "frees up"]))
    if other:
        lines.append(dim("no cap: " + ", ".join(f"{o['metric']} {budgets.fmt(o['metric'], o['used'])} {budgets.WINDOWS[o['window']]}"
                                                 for o in other)))
    return Out({"caps": rows, "uncapped": other}, "\n".join(lines))


def register(cmd, group, action):
    cmd("budget", cmd_budget, "budgets: used, caps, when they free up")
