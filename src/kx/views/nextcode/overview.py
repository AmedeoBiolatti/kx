"""The Overview panel: `kx status` with the score over time, reported metrics and the leaderboard, as the web
dashboard's Status tab. A point clicked on a chart opens its run below the charts; a row of the tasks or submissions
opens it in its own page."""

from __future__ import annotations

from kx.views.nextcode import Ctx, Go, comp, footer, header, states
from kx.views.nextcode.widgets import (BLUE, GREEN, GREY, HOST, RED, chart, col, esc, grid, label, md, progress, select,
                                       stat, table, thin, tick, trunc)


def view(ctx: Ctx) -> tuple[dict, dict]:
    from kx.ledger.store import Store
    from kx.views import status
    from kx.views.dashboard import history, leaderboard
    c = comp()
    with Store(c) as st:
        d = status.status(st)
    hist = history(c.root, ctx.value("protocol", "") or None)
    lbd = leaderboard(c.root, None)
    state = {"protocol": hist["protocol"], "metric": ctx.value("metric", ""), "run": ctx.state.get("run"), "x0": {},
             "tasks": [], "subs": []}
    for table_, page in (("tasks", "tasks"), ("subs", "subs")):  # a row: that task or submission, in its page
        if (key := ctx.row(table_, ctx.state.get(table_) or [])) is not None:
            raise Go(page, open=key)
    n = 250 if ctx.tight else 1000
    pts = [p for p in hist["points"] if isinstance(p["value"], (int, float)) and isinstance(p["best"], (int, float))]
    for name in ("scores", "metric"):  # a point clicked: its run, in the chart as it was drawn
        if (hit := ctx.selected(name)) and (shown := window(points_of(pts, name, state["metric"]), ctx.state, name, n)):
            state["run"] = shown[hit[1]]["run"] if 0 <= hit[1] < len(shown) else state["run"]
    if ctx.clicked("close-run"):
        state["run"] = None
    if ctx.clicked("train-run") and state["run"]:
        raise Go("training", runs=[state["run"]])
    charts = [*scores(hist, d["results"]["reference"], pts, state, n), *metrics(pts, state, n // 2), *board(lbd)]
    from kx.views.nextcode.runs import run_card
    body = [header(d["competition"]["slug"], competition_line(d["competition"])
                   + (" · click a point on a chart to open its run" if charts and HOST.api >= 4 else "")),
            needs(d["needs_you"]), stats(d), grid("charts", *charts, min_width=440),
            col("card", *run_card(state["run"], ctx.tight)) if state["run"] else None,
            *running(d["running"]), *submissions(d["submissions"]["recent"], state), *task_list(d["tasks"], ctx.tight, state),
            *budget_list(d["budgets"]), footer()]
    state["_live"] = bool(d["running"]["runs"] or d["running"]["workers"])
    return col("root", *body, gap=12), state


def competition_line(c: dict) -> str:
    left = f" · {c['days_left']:.0f} days left" if c.get("days_left") is not None and c["days_left"] >= 0 else \
        (" · ended" if c.get("days_left") is not None else "")
    return f"{c['kind']} competition · {esc(c['metric'])} ({c['direction']}){left}"


def needs(items: list[dict]) -> dict:
    if not items:
        return md("needs", "✓ Nothing needs you right now.")
    from kx.views.text import dur
    lines = [f"- {esc(n['what'])}" + (f" · {dur(n['age_s'])}" if n.get("age_s") is not None else "")
             + f"  `{tick(n['fix'])}`" for n in items[:12]]
    more = [f"- … {len(items) - 12} more: `kx status`"] if len(items) > 12 else []
    return md("needs", f"### Needs you · {len(items)}\n\n" + "\n".join(lines + more))


def stats(d: dict) -> dict:
    from kx.views.text import dur, num
    res, sub = d["results"], d["submissions"]
    ref, best, sl, lb = res["reference"], (res["best"] or [None])[0], sub["slots"], sub["lb"]
    pub = [x["public"] for x in sub["recent"] if x.get("public") is not None]
    runs, workers, tc = d["running"]["runs"], d["running"]["workers"], d["tasks"]["counts"]
    full = bool(sl.get("limit")) and sl["used"] >= sl["limit"]
    cells = [
        ("best", "Best local score", (num(best["value"]) + (f" ±{num(best['se'], 2)}" if best["se"] is not None else ""))
         if best else "–", f"{best['run']} · {ref['id']}" if best else (ref["id"] or "no scored runs yet"), BLUE if best else None),
        ("public", "Public leaderboard", (num(max(pub), 3) if pub else "–") + (f" #{lb['our_rank']}" if lb and lb.get("our_rank") else ""),
         f"of {lb['n_teams']:,} teams" if lb and lb.get("n_teams") else "kx sync to fetch", 1 if pub else None),
        ("slots", "Submissions today", f"{sl['used']}" + (f" of {sl['limit']}" if sl.get("limit") else ""),
         ("none left · " if full else "") + f"resets in {dur(sl.get('reset_in_s'))}", (RED if full else GREEN) if sl.get("limit") else None),
        ("running", "Running now", str(len(runs) + len(workers)),
         f"{len(runs)} runs · {len(workers)} workers · {tc['active']} active, {tc['open']} open tasks", BLUE if runs or workers else GREY),
    ]
    if HOST.api < 4:  # tiles would stack a screen high: a table, a column each
        return table("stats", [c[1] for c in cells], [[c[2] for c in cells], [c[3] for c in cells]], widths=[200] * len(cells))
    return grid("stats", *[stat(f"stat-{k}", name, value, sub_, color) for k, name, value, sub_, color in cells],
                min_width=200, gap=12)


# --- charts. x is the run number, so a point is a run; `window` keeps what was drawn, for a click to find its run


def points_of(pts: list[dict], chart_id: str, metric: str) -> list[dict]:
    if chart_id == "scores":
        return pts
    return [p for p in pts if isinstance((p.get("metrics") or {}).get(metric), (int, float))]


def window(pts: list[dict], state: dict, chart_id: str, n: int) -> list[dict]:
    """The points the chart showed: from the first run it showed (state x0, a run id), else the last n."""
    x0 = (state.get("x0") or {}).get(chart_id)
    if isinstance(x0, str) and (at := next((i for i, p in enumerate(pts) if p["run"] == x0), None)) is not None:
        return pts[at:at + n]
    return pts[-n:]


def hover(p: dict) -> str:
    return trunc(f"{p['run']} · {p.get('hypothesis') or ''}", 60)


def boxed(id_: str, heading: str, hint: str, pick: dict | None, c: dict | None, empty: str = "", title: str = "") -> dict:
    """A chart with its picker: (API 4) the picker in the chart's title row, the title the heading (or title); else a
    heading and its hint above the picker and the chart. With no chart: the heading, the picker and why."""
    if c and HOST.api >= 4:
        c["title"] = title or heading
        if pick:
            pick["label"] = ""
            c["controls"] = [pick]
        return c
    return col(f"{id_}-box", md(f"{id_}-title", f"### {esc(heading)}" + (f"\n\n{hint}" if hint else "")), pick,
               c or (label(f"{id_}-empty", empty) if empty else None))


def scores(hist: dict, ref: dict, pts: list[dict], state: dict, n: int) -> list[dict]:
    if not hist["protocols"]:
        return []
    pick = select("protocol", "Protocol", hist["protocol"] or "",
                  [(p["id"], f"{p['id']}{' (reference)' if p['id'] == ref['id'] else ''} · {p['n']} runs")
                   for p in hist["protocols"]]) if len(hist["protocols"]) > 1 else None
    shown = pts[-n:]
    if not shown:
        return [boxed("scores", "Score over time", "", pick, None, "No scored runs under this protocol.")]
    state["x0"]["scores"] = shown[0]["run"]
    x = lambda p: p["n"]  # noqa: E731
    c = chart("scores", hist["protocol"], kind="scatter", x_label="run", y_label="score", height=260, series=[
        {"name": "best so far", "style": "step", "color": 0, "points": [(x(p), p["best"], hover(p)) for p in shown]},
        {"name": "each run", "style": "points", "color": 1, "points": [(x(p), p["value"], hover(p)) for p in shown]}])
    more = f" · last {len(shown)} of {len(pts)}" if len(shown) < len(pts) else ""
    return [boxed("scores", "Score over time", f"click a point to open its run{more}", pick, c,
                  title="Score over time" + ("" if pick else f" · {hist['protocol']}") + more)]


def metrics(pts: list[dict], state: dict, n: int) -> list[dict]:
    """A number the scored runs log (r.log), over runs: any of them, picked."""
    counts = {}
    for p in pts:
        for k, v in (p.get("metrics") or {}).items():
            if isinstance(v, (int, float)):
                counts[k] = counts.get(k, 0) + 1
    names = sorted(k for k, c in counts.items() if c >= 2)
    if not names:
        return []
    metric = state["metric"] if state["metric"] in names else ""
    state["metric"] = metric
    pick = select("metric", "Metric", metric or "-", [("-", "Pick a reported metric…"), *((k, f"{k} · {counts[k]} runs") for k in names)])
    if not metric:
        return [boxed("metric", "Reported metrics", f"{len(names)} numbers the scored runs log", pick, None)]
    shown = points_of(pts, "metric", metric)[-n:]
    state["x0"]["metric"] = shown[0]["run"]
    c = chart("metric", metric, kind="scatter", x_label="run", y_label=metric, height=260, series=[
        {"name": metric, "style": "points", "color": 2, "points": [(p["n"], p["metrics"][metric], hover(p)) for p in shown]}])
    return [boxed("metric", "Reported metric", "", pick, c)]


def board(d: dict) -> list[dict]:
    """Our public scores and the leaderboard over time (kx sync snapshots), and local against public score."""
    from kx.ledger.store import parse_ts
    t = lambda ts: parse_ts(ts).timestamp() if parse_ts(ts) else None  # noqa: E731
    snaps = [s for s in d["snapshots"] if t(s["ts"])]
    series = [{"name": "our submissions", "style": "points", "color": 1,
               "points": [(t(s["created"]), s["public"], trunc(f"{s['id']} · {s['run'] or 'external'} · {s['message'] or ''}", 60))
                          for s in d["submissions"] if t(s["created"])]}]
    for key, name, color, style in (("top", "top", 0, "line"), ("our_score", "us on the board", 2, "line"),
                                    ("gold", "gold", 3, "dashed"), ("silver", "silver", 4, "dashed"), ("bronze", "bronze", 5, "dashed")):
        if len([s for s in snaps if s.get(key) is not None]) >= 2:
            series.append({"name": name, "style": style, "color": color,
                           "points": [(t(s["ts"]), s[key]) for s in snaps if s.get(key) is not None]})
    out = []
    if series[0]["points"] or len(series) > 1:
        c = chart("lb", "public leaderboard", series=thin_series(series, 300), kind="line", x_label="date", y_label="public score",
                  x_format="date", height=260)
        out.append(boxed("lb", "Leaderboard", "our public scores and the board at each kx sync", None, c))
    if d["pairs"]:
        pairs = [{"name": "runs", "style": "points", "color": 1,
                  "points": [(p["local"], p["public"], f"{p['run']} · {trunc(p['hypothesis'], 40)}") for p in d["pairs"]]}]
        if d["fit"]:
            xs = [p["local"] for p in d["pairs"]]
            f = d["fit"]
            pairs.append({"name": f"fit (r {f['r']:.2f})" if f.get("r") is not None else "fit", "style": "line", "color": 0,
                          "points": [(x, f["intercept"] + f["slope"] * x) for x in (min(xs), max(xs))]})
        out.append(boxed("pairs", "Local against public", esc(d["protocol"]), None,
                         chart("pairs", d["protocol"], pairs, kind="scatter", x_label="local", y_label="public", height=260),
                         title=f"Local against public · {d['protocol']}"))
    return out


def thin_series(series: list[dict], n: int) -> list[dict]:
    return [{**s, "points": thin(s["points"], n)} for s in series]


# --- lists


def running(r: dict) -> list[dict]:
    runs, workers = r["runs"], r["workers"]
    if not runs and not workers:
        return []
    from kx.views.text import dur
    out = [md("running-title", f"### Running · {len(runs) + len(workers)}")]
    for x in runs[:10]:
        where = f" @{x['host']}" if x.get("host") else ""
        line = " · ".join(s for s in [f"{x['run']}{where}", f"{x['percent']:.0f}%" if x["percent"] is not None else "running",
                                      f"eta {dur(x['eta_s'])}" if x.get("eta_s") else "", dur(x["elapsed_s"]),
                                      x.get("phase") or "", trunc(x["hypothesis"], 100)] if s)
        out.append(progress(f"run-{x['run']}", line, (x["percent"] or 0) / 100))
    if len(runs) > 10:
        out.append(label("running-more", f"… {len(runs) - 10} more: the Runs panel"))
    if workers:
        out.append(table("workers", ["worker", "task", "tokens", "doing"],
                         [[w["agent"], w["task"] or "", f"{(w.get('tokens_in') or 0) + (w.get('tokens_out') or 0):,}",
                           trunc(w.get("last_action"), 100)] for w in workers[:20]], widths=[110, 60, 90, 0]))
    return out


def submissions(subs: list[dict], state: dict) -> list[dict]:
    from kx.views.text import ago, num
    out = [md("subs-title", "### Latest submissions" + (" · click one to open it" if HOST.api >= 4 and subs else ""))]
    if not subs:
        return out + [label("subs-empty", "No submissions yet.")]
    state["subs"] = [x["id"] for x in subs]
    rows = [[x["id"], x["run"] or "external", states.sub(x), num(x["public"], 4), f"{ago(x['created'])} ago",
             f"{x['decided']} games · {x['winrate'] * 100:.1f}% won" if x.get("winrate") is not None
             else trunc(x.get("error"), 80)] for x in subs]
    return out + [table("subs", ["sub", "run", "status", "public", "when", ""], rows, widths=[50, 70, 110, 80, 80, 0],
                        selectable=True)]


def task_list(t: dict, tight: bool, state: dict) -> list[dict]:
    c = t["counts"]
    out = [md("tasks-title", f"### Tasks · {c['active']} active · {c['waiting']} waiting · {c['open']} open")]
    tl = (t["active"] + t["ready"])[:12 if tight else 40]
    if not tl:
        return out + [label("tasks-empty", "No active or ready tasks.")]
    state["tasks"] = [x["id"] for x in tl]
    rows = [[x["id"], trunc(x["title"], 90), states.task(x),
             trunc(f"waits on {x['waiting_on']}" if x["status"] == "waiting" else (x.get("last_note") or {}).get("text"),
                   60 if tight else 160)] for x in tl]
    return out + [table("tasks", ["task", "title", "state", "latest"], rows, widths=[50, 320, 90, 0], selectable=True)]


def budget_list(bs: list[dict]) -> list[dict]:
    if not bs:
        return []
    from kx.guard.budgets import fmt
    out = [md("budgets-title", "### Budgets")]
    for i, b in enumerate(bs[:16]):
        scope = f" · {b['scope']}" if b["scope"] != "team" else ""
        out.append(progress(f"budget-{i}", f"{b['key'].replace('_', ' ')}{scope}: {fmt(b['metric'], b['used'])} / "
                                           f"{fmt(b['metric'], b['cap'])}", b["used"] / b["cap"] if b["cap"] else 0.0))
    return out
