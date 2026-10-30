"""Commands: budget."""

from __future__ import annotations

from kx.cli.common import Out, S
from kx.guard import actors, budgets, rules
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
        store = rules.usage(st.comp)
    if not rows and not other and not store:
        return Out({"caps": [], "uncapped": [], "store": None}, "no budgets set (comp.yaml budgets:) and nothing used yet")
    lines = []
    if rows:
        lines.append(table([[r["scope"], r["key"], f"{budgets.fmt(r['metric'], r['used'])} / {budgets.fmt(r['metric'], r['cap'])}",
                             budgets.fmt(r["metric"], r["left"]) + " left", r["frees_up"]] for r in rows],
                           ["scope", "budget", "used / cap", "left", "frees up"]))
    if other:
        lines.append(dim("no cap: " + ", ".join(f"{o['metric']} {budgets.fmt(o['metric'], o['used'])} {budgets.WINDOWS[o['window']]}"
                                                 for o in other)))
    if store:
        lines.append(f"store: {store['used_gb']:g} / {store['max_gb']:g} GB (storage.max_gb), {store['left_gb']:g} GB left"
                     + ("; kx gc frees what discarded and failed runs made" if store["left_gb"] < 0.1 * store["max_gb"] else ""))
    return Out({"caps": rows, "uncapped": other, "store": store}, "\n".join(lines))


def register(cmd, group, action):
    cmd("budget", cmd_budget, "budgets: used, caps, when they free up")
