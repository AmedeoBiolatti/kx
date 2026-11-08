"""The Training page, as a wandb workspace. On the left, the runs that log curves (r.step), found by words, status,
tag, protocol or task and ticked to show, each in its color in every chart (a run keeps its place, so its color);
with API 4 also their scores side by side and the charts' settings, and the left stays in place while the charts
scroll (NextCode 0.1.6). On the right, one x axis for all (step, time since the run started, or a count the runs log
such as examples seen), smoothing and log scale, then the charts side by side, a curve each, grouped by prefix
(train/, val/) into sections that fold away, their hover line and zoom shared, each removed from its title row. Then
the ticked runs' scores with `kx compare`'s verdict against one of them, their metrics and what differs in config
and code."""

from __future__ import annotations

import math
import re

from kx.ledger.config import KxError
from kx.views.nextcode import Ctx, footer, header, run_kx, states
from kx.views.nextcode.widgets import (AMBER, HOST, bars, button, cell, chart, checkbox, checklist, col, ema, grid, label,
                                       mark, md, pad, row, section as fold, select, sidebar, table, text_input, thin, tiles,
                                       trunc)

LISTED = 16
SIDE = 380  # px: the sidebar (API 4)
HEIGHTS = {"s": 180, "m": 240, "l": 320}  # NextCode keeps 102 px of a chart for its title, ticks and labels
STATUSES = ("any", "running", "done", "failed")
SECTION = re.compile(r"^(train|training|val|valid|validation|eval|test|dev)(?=[_.\-])", re.I)
# What NextCode's widgets take up, in px (measured). API 3 has no grid nor sidebar: the run list and the charts are
# two columns of a row, which aligns their bottoms, so the shorter is padded to the other's height. Text widgets
# (markdown, labels) are kept out of both: in a column of a row they are measured at no width, a word a line.
TALL = {"field": 28, "input": 26, "check": 28, "button": 28, "chart": 26, "line": 22.6}  # inline labels, no legends
SIDE_GAP, MAIN_GAP = 8, 12
WIDTHS = {"2": 470, "3": 320, "4": 240}  # a chart's width, by how many a row
LABEL = 40  # characters of a run's line in the sidebar


def max_runs() -> int:
    """Runs shown at once: as many as a chart's series with API 3 (6); API 4 takes 12, but six stay apart by color."""
    return min(6, HOST.limits["series"])


def safe(name: str) -> str:
    """A curve's name in a widget id."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:80]


def view(ctx: Ctx) -> tuple[dict, dict]:
    listing = run_kx(["runs", "-n", "5000", "--curves"])
    logging = [r for r in listing if r.get("curves")]
    by_id = {r["run"]: r for r in listing}
    state = {"q": ctx.value("q", ""), "task": ctx.value("task", "-"), "status": ctx.value("status", "any"),
             "tag": ctx.value("tag", "-"), "proto": ctx.value("proto", "-"), "x": ctx.value("x", "step"),
             "smooth": ctx.value("smooth", "0"), "scale": ctx.value("scale", "linear-"), "size": ctx.value("size", "s"),
             "per": ctx.value("per", "3"), "cq": ctx.value("cq", ""), "allcfg": ctx.value("allcfg", False),
             "base": ctx.value("base", ""), "bars": ctx.value("bars", "score")}
    for k, ok, default in (("size", HEIGHTS, "s"), ("per", ("2", "3", "4"), "3"), ("status", STATUSES, "any"),
                           ("scale", ("linear", "linear-", "log", "log-"), "linear-")):
        if state[k] not in ok:
            state[k] = default
    tasks = sorted({r["task"] for r in logging if r.get("task")}, reverse=True)
    if state["task"] != "-" and state["task"] not in tasks:
        state["task"] = "-"
    facts = ledger_facts(state)
    found = [r["run"] for r in logging if finds(r, state, facts)]
    shown = picked(ctx, [r["run"] for r in logging], found, by_id)
    runs = []
    for ref in shown:
        try:
            runs.append(run_kx(["show", ref, "--curves"]))
        except KxError:
            pass
    state["runs"] = [r["run"] for r in runs]
    if state["base"] not in state["runs"]:  # the first scored, so that there are verdicts
        state["base"] = next((r["run"] for r in runs if r.get("scores")), state["runs"][0] if runs else "")

    side, side_h, items = run_list(ctx, found, tasks, facts, state, by_id, len(logging))
    state["listed"] = [k for k, *_ in items]
    if runs:
        main, below, main_h, state["charts"], state["drawn"], settings = curves(ctx, runs, state, ctx.state, by_id, facts)
        side += [w_ for w_, _ in settings]
        side_h += sum(h_ for _, h_ in settings)
    else:
        main = [button("empty", "Tick runs on the left to see their curves." if logging
                       else "No run logs curves yet (r.step in kx.run).", disabled=True)]
        below, main_h, state["charts"], state["drawn"] = [], TALL["button"], ctx.state.get("charts"), []
    said = state.pop("said", "")
    head = header("Training", " · ".join(x for x in (
        f"{len(runs)} runs shown (up to {max_runs()})", f"{len(found)} of {len(logging)} that log curves found",
        said, f"compared against {state['base']}" if len(runs) > 1 else "") if x))
    state["_live"] = any(r["status"] == "running" for r in runs)
    below += comparison(runs, state, ctx.tight) if runs else []
    if HOST.api >= 4:  # wandb's: the runs on the left, in place, the charts filling the rest
        return sidebar("root", side, [head, *main, *below, footer()], width=SIDE), state
    work = row("workspace", col("side", *side, pad("side-pad", main_h - side_h - SIDE_GAP), gap=SIDE_GAP),
               col("main", *main, pad("main-pad", side_h - main_h - MAIN_GAP), gap=MAIN_GAP), gap=20)
    return col("root", head, work, *below, footer(), gap=12), state


def ledger_facts(state: dict) -> dict:
    """What the run listing lacks: the tags (with how many runs), the protocols scored (with how many), and under the
    protocol picked, each run's score; the runs of the tag picked."""
    from kx.ledger.store import Store
    from kx.views.nextcode import comp
    with Store(comp()) as st:
        tags = [(r[0], r[1]) for r in st.all("SELECT tag, COUNT(*) n FROM tags GROUP BY tag ORDER BY n DESC LIMIT 30")]
        protos = [(r[0], r[1]) for r in st.all("SELECT protocol_id, COUNT(DISTINCT run_id) n FROM scores GROUP BY protocol_id "
                                                "ORDER BY n DESC LIMIT 30")]
        tagged = {r[0] for r in st.all("SELECT run_id FROM tags WHERE tag=?", (state["tag"],))} if state["tag"] != "-" else None
        scored = {r[0]: (r[1], r[2]) for r in st.all("SELECT run_id, value, se FROM scores WHERE protocol_id=? ORDER BY id",
                                                           (state["proto"],))} if state["proto"] != "-" else None
    if state["tag"] not in ("-", *(t for t, _ in tags)):
        state["tag"], tagged = "-", None
    if state["proto"] not in ("-", *(p for p, _ in protos)):
        state["proto"], scored = "-", None
    return {"tags": tags, "protos": protos, "tagged": tagged, "scored": scored}


def finds(r: dict, state: dict, facts: dict) -> bool:
    """A run the filters find: its words (id, status, hypothesis, note, author, host, task, curves' names), status,
    task, tag and protocol."""
    blob = " ".join(str(r.get(k) or "") for k in ("run", "status", "hypothesis", "note", "author", "host")).lower() \
        + (f" {r['task']}".lower() if r.get("task") else "") + " " + " ".join(r.get("curves") or []).lower()
    return (all(w in blob for w in state["q"].lower().split())
            and (state["task"] == "-" or str(r.get("task")) == state["task"])
            and (state["status"] == "any" or r["status"] == state["status"])
            and (facts["tagged"] is None or r["run"] in facts["tagged"])
            and (facts["scored"] is None or r["run"] in facts["scored"]))


def picked(ctx: Ctx, logging: list[str], found: list[str], by_id: dict) -> list[str]:
    """The runs shown after this event: ticked or unticked in the list, the newest found, none, or sent here (Show in
    Training)."""
    st = ctx.state
    runs = [r for r in (st.get("runs") or []) if isinstance(r, str)] if "runs" in st else logging[:3]
    sent = [r for r in ctx.args.get("runs") or [] if isinstance(r, str) and r in by_id]
    n = max_runs()
    if sent:
        runs = sent if ctx.args.get("only") else [r for r in runs if r not in sent][: n - len(sent[:n])] + sent
    if hit := ctx.toggled("pick", st.get("listed") or []):
        key, on = hit
        runs = [r for r in runs if r != key] + ([key] if on else [])
    if ctx.clicked("pick-none"):
        runs = []
    if ctx.clicked("pick-found"):
        runs = found[:n]
    return list(dict.fromkeys(runs))[:n]


def run_list(ctx: Ctx, found: list[str], tasks: list[int], facts: dict, state: dict, by_id: dict,
             total: int) -> tuple[list, float, list]:
    """The sidebar: the filters, the checklist (ticked runs first, in their colors' order, then the newest found) and
    the ticked runs' scores, whose legend gives their colors. (widgets, their height, the checklist's items)."""
    from kx.views.text import ago
    from kx.views.text import num as fmt
    titles = {}
    if tasks:
        try:
            titles = {t["id"]: t["title"] for t in run_kx(["task", "list", "--all"])}
        except KxError:
            pass
    on = state["runs"]
    rest = [r for r in found if r not in on]
    listed = rest[: max(0, (8 if ctx.tight else LISTED) - len(on))]
    full = len(on) >= max_runs()

    def score_of(r):
        if facts["scored"] is not None:
            v = facts["scored"].get(r["run"])
            return fmt(v[0], 4) if v else ""
        return fmt(r["score"]["value"], 4) if r.get("score") else ""  # the value: its ± would be cut
    items = []
    for i, ref in enumerate(on + listed):
        r = by_id.get(ref, {"run": ref, "status": "?", "created": None})
        ticked = i < len(on)
        if HOST.api >= 4:  # a table: its columns sized, a cell cut to its width (hover shows the row whole)
            cells = [f"{i + 1}" if ticked else "", ref, states.run(r, words=False), score_of(r),
                     ago(r["created"]) if r.get("created") else "", r.get("hypothesis") or ""]
        else:
            cells = [f"{i + 1}" if ticked else "", ref, "" if r["status"] == "done" else r["status"], score_of(r),
                     ago(r["created"]) if r.get("created") else ""]
            line = " · ".join(c for c in cells if c)  # the sidebar is as wide as its widest line: about 300 px
            cells.append(trunc(r.get("hypothesis"), max(8, LABEL - len(line) - 3)))
        items.append((ref, cells, ticked, i if ticked else None, full and not ticked))
    newest = found[:max_runs()]
    widgets = [
        text_input("q", "", state["q"], "find: words, T12, a curve"),  # no label: a text input's is measured at no width
        select("status", "Status", state["status"], [(s, s) for s in STATUSES], inline=True),
        select("tag", "Tag", state["tag"], [("-", "any"), *((t, f"{t} · {n}") for t, n in facts["tags"])], inline=True) if facts["tags"] else None,
        select("proto", "Scored under", state["proto"], [("-", "any protocol"), *((p, f"{p} · {n}") for p, n in facts["protos"])], inline=True)
        if facts["protos"] else None,
        select("task", "Task", state["task"], [("-", "any"), *((t, f"{t} · {titles.get(t, '')}") for t in tasks)], inline=True) if tasks else None,
        row("pick-actions", button("pick-found", f"Show newest {len(newest)}") if newest and newest != on else None,
            button("pick-none", "Untick all") if on else None, gap=8) if (newest and newest != on) or on else None,
        checklist("pick", ["#", "run", "", "score", "age", "hypothesis"], items, scroll_=False, height=640,
                  widths=[24, 56, 24, 64, 36, 0]) if items and HOST.api >= 4
        else checklist("pick", ["", "run", "status", "score", "age", "hypothesis"], items, scroll_=False) if items
        else button("pick-empty", "No run matches", disabled=True)]
    widgets = [w for w in widgets if w]
    h = sum({"select": TALL["field"], "text-input": TALL["input"], "row": TALL["button"], "button": TALL["button"]}
            .get(w["type"], (TALL["check"] + 2) * len(items) - 2) + SIDE_GAP for w in widgets) - SIDE_GAP
    return widgets, h, items


def bars_chart(runs: list[dict], what: str, last: dict, by_id: dict, facts: dict, proto: str | None, height: int,
               width: int | None, pick: dict | None = None) -> dict | None:
    """The runs side by side, a bar each in the charts' colors: their scores (under the protocol picked, else each's
    own), or the last value of a curve; none when no run has one. pick: what they show, in the title row (API 4)."""
    vals = []
    for r in runs:
        if what == "score":
            info = by_id.get(r["run"], {})
            v = (facts["scored"] or {}).get(r["run"], (None,))[0] if proto else (info.get("score") or info.get("other_score") or {}).get("value")
        else:
            v = ((last[r["run"]].get(what) or [(None, None)])[-1])[1]
        vals.append(v if isinstance(v, (int, float)) and math.isfinite(v) and abs(v) <= 1e12 else None)
    title = f"score{' · ' + proto.split('@')[0] if proto else ''}" if what == "score" else f"last {what}"
    if pick:
        pick["label"] = ""
        title = "Runs side by side"
    return bars(f"bars-{safe(what)}", title, [{"name": f"{r['run']} {v:.4g}", "color": i, "value": v}
                                              for i, (r, v) in enumerate(zip(runs, vals)) if v is not None],
                height=height, width=width, controls=[pick] if pick else None)


# --- curves


def passes(points: list, times: list | None) -> list[list[tuple]]:
    """A curve logged again and again (steps restart): each pass, (step, value, time)."""
    out = [[]]
    for i, p in enumerate(points):
        if i and p[0] < points[i - 1][0]:
            out.append([])
        out[-1].append((p[0], p[1], times[i] if times and i < len(times) else None))
    return out


def counter(points: list) -> bool:
    """A count that only goes up (examples seen, tokens): an x axis, or a number for the table, rather than a chart."""
    vs = [p[1] for p in points]
    return len(vs) >= 3 and all(float(v).is_integer() for v in vs) and all(b >= a for a, b in zip(vs, vs[1:])) and vs[-1] > vs[0]


def section(name: str) -> str:
    if "/" in name:
        return name.split("/", 1)[0]
    m = SECTION.match(name)
    return m.group(1).lower() if m else ""


def inliers(series: list[dict]) -> list[dict]:
    """The points within the bulk of a chart's values (2nd to 98th percentile of all its series, widened by a quarter
    on each side), as wandb's "ignore outliers": a loss's first steps don't flatten the rest of the curve."""
    ys = sorted(p[1] for x in series for p in x["points"])
    if len(ys) < 20:
        return series
    lo, hi = ys[len(ys) // 50], ys[-1 - len(ys) // 50]
    lo, hi = lo - (hi - lo) / 4, hi + (hi - lo) / 4
    out = [{**x, "points": [p for p in x["points"] if lo <= p[1] <= hi]} for x in series]
    return [x for x in out if x["points"]]


def matches(name: str, q: str) -> bool:
    """Words all in the name; -word: not in it."""
    n = name.lower()
    return all((w[1:] not in n) if w.startswith("-") and len(w) > 1 else (w in n) for w in q.lower().split())


def curves(ctx: Ctx, runs: list[dict], state: dict, st: dict, by_id: dict, facts: dict) -> tuple[list, list, float, list | None, list, list]:
    """The x axis and smoothing, then the charts side by side: (widgets, a last row to put under the sidebar, the
    widgets' height, the charts picked (None: the filter's), the charts drawn, the charts' settings for the sidebar as
    (widget, its height))."""
    from kx.views.text import num
    last = {r["run"]: {k: passes(pts, (r.get("curve_times") or {}).get(k))[-1] for k, pts in (r.get("curve_points") or {}).items()
                       if pts} for r in runs}
    have, counts = {}, set()
    for r in runs:
        for k, ps in last[r["run"]].items():
            have.setdefault(k, []).append(r["run"])
            if counter([p[:2] for p in ps]):
                counts.add(k)
    plain = sorted((k for k in have if k not in counts), key=lambda k: (-len(have[k]), section(k), k))
    every = plain + sorted(counts)
    limit = 6 if ctx.tight else 12

    picks = st.get("charts") if isinstance(st.get("charts"), list) else None
    drawn = [k for k in st.get("drawn") or [] if isinstance(k, str)]
    if ctx.clicked("cq") or ctx.clicked("charts-auto"):  # a filter typed, or Reset: the charts of the filter's curves
        picks = None
    if ctx.clicked("charts-auto"):
        state["cq"] = ""
    if (v := ctx.changed("add-chart")) and v in have:
        picks = [k for k in drawn if k != v] + [v]
    if (v := ctx.changed("remove-chart")) and v in drawn:
        picks = [k for k in drawn if k != v]
    if (hit := ctx.pressed("del-")) and (j := hit.split("-", 1)[0]).isdigit() and int(j) < len(drawn) \
            and hit == f"{j}-{safe(drawn[int(j)])}":  # a chart's ×, in its title row
        picks = [k for k in drawn if k != drawn[int(j)]]
    per = int(state["per"])
    if picks is None:
        named = [k for k in plain if matches(k, state["cq"])]
        drawn = named[:limit]
        extra = 1 if len(runs) > 1 and HOST.api < 4 else 0  # the runs' bars, in the grid (API 4: in the sidebar)
        if len(named) > limit and len(drawn) + extra > per:  # some are left out anyway: whole rows
            drawn = drawn[: len(drawn) - (len(drawn) + extra) % per]
    else:
        picks = [k for k in dict.fromkeys(picks) if isinstance(k, str)][:limit]
        drawn = [k for k in picks if k in have]

    xs = [("step", "step"), ("pct", "% of each run"), ("time", "time since the start"), *((f"c:{k}", k) for k in sorted(counts))]
    if state["x"] not in [v for v, _ in xs]:
        state["x"] = "step"
    hits = [k for k in plain if matches(k, state["cq"])]
    height = HEIGHTS[state["size"]]
    state["said"] = f"{len(drawn)} of {len(every)} curves charted" + (f" ({len(hits) - len(drawn)} more named so)"
                                                                      if picks is None and len(hits) > len(drawn) else "")
    f = TALL["field"] + SIDE_GAP
    top = row("chart-settings", select("x", "X axis", state["x"], xs, inline=True),
              select("smooth", "Smoothing", state["smooth"], [("0", "none"), *((v, v) for v in ("0.6", "0.8", "0.9", "0.97"))], inline=True),
              select("scale", "Y", state["scale"], [("linear", "linear"), ("linear-", "linear, outliers ignored"), ("log", "log"),
                                                    ("log-", "log, outliers ignored")], inline=True), gap=12)
    if state["bars"] not in ("score", *every):
        state["bars"] = "score"
    side = [(text_input("cq", "", state["cq"], "charts of curves named… (Enter)"), TALL["input"] + SIDE_GAP),
            (select("add-chart", "Add a chart", "-", [("-", f"one of {len(every)} curves…"), *((k, k) for k in every if k not in drawn)], inline=True)
             if len(drawn) < limit else None, f),
            (select("remove-chart", "Remove a chart", "-", [("-", f"one of {len(drawn)}…"), *((k, k) for k in drawn)], inline=True)
             if drawn and HOST.api < 4 else None, f),  # API 4: a chart's × in its title row
            (select("bars", "Bars of the runs", state["bars"], [("score", "score"), *((k, f"last {k}") for k in every)], inline=True)
             if len(runs) > 1 and HOST.api < 4 else None, f),
            (select("size", "Chart size", state["size"], [("s", "small"), ("m", "medium"), ("l", "large")], inline=True), f),
            (select("per", "Charts a row", state["per"], [(str(n), str(n)) for n in (2, 3, 4)], inline=True), f),
            (button("charts-auto", "Reset charts") if picks is not None or state["cq"] else None, TALL["button"] + SIDE_GAP)]
    side = [(w_, h_) for w_, h_ in side if w_]
    out, h = [top], TALL["field"] + MAIN_GAP
    proto = state["proto"] if state["proto"] != "-" else None
    if len(runs) > 1 and HOST.api >= 4:  # the runs side by side, under their list: their colors' legend, with values
        pick = select("bars", "", state["bars"], [("score", "score"), *((k, f"last {k}") for k in every)])
        side.insert(0, (bars_chart(runs, state["bars"], last, by_id, facts, proto, 180, None, pick), 0))
        side = [(w_, h_) for w_, h_ in side if w_]
    if not have:
        out.append(button("curves-empty", "These runs log no curves: their scores and config are below.", disabled=True))
        return out, [], h + TALL["button"], picks, [], side

    groups = {}  # API 4: the charts by prefix (train/, val/), each group a section that folds away
    for k in drawn:
        groups.setdefault(section(k), []).append(k)
    sec = {}  # a group's section id (unique, the same for the same groups)
    for g in groups:
        sec[g] = next(i for n in range(1, 99) if (i := f"sec-{safe(g)}" + (f"-{n}" if n > 1 else "")) not in sec.values())
    closed = {g for g in st.get("closed") or [] if isinstance(g, str)}
    for g in groups:
        if isinstance(v := ctx.inputs.get(sec[g]), bool):  # a section folded or opened
            closed.discard(g) if v else closed.add(g)
    folds = HOST.api >= 4 and len(groups) > 1
    state["closed"] = sorted(closed)
    shown = [k for k in drawn if not folds or section(k) not in closed]
    budget = max(20, min(400, int(HOST.limits["values"] * 0.85) // max(1, len(shown) * len(runs))))
    w, mode = float(state["smooth"]), state["x"]
    t0 = {r: min((p[2] for ps in curves_.values() for p in ps if p[2] is not None), default=None) for r, curves_ in last.items()}
    xname = mode[2:] if mode.startswith("c:") else None
    x_label = "time" if mode == "time" else "% of the run" if mode == "pct" else xname or "step"
    scale, robust = state["scale"].rstrip("-"), state["scale"].endswith("-")
    base = state["base"]
    cells, charts_ = [], {}
    for j, k in enumerate(drawn):
        if k not in shown:
            continue
        series, names = [], []
        at_base = (last[base].get(k) or [None])[-1] if base in last else None
        for i, r in enumerate(runs):
            ps = last[r["run"]].get(k)
            pts = []
            if ps and mode == "time":
                pts = [(t - t0[r["run"]], v) for _, v, t in ps if t is not None and t0[r["run"]] is not None]
            elif ps and xname:
                at = {s: v for s, v, _ in last[r["run"]].get(xname) or []}
                pts = [(at[s], v) for s, v, _ in ps if s in at]
            elif ps and mode == "pct":  # runs of different lengths, side by side
                span = (ps[-1][0] - ps[0][0]) or 1
                pts = [(100 * (s - ps[0][0]) / span, v) for s, v, _ in ps]
            elif ps:
                pts = [(s, v) for s, v, _ in ps]
            if not pts:
                continue
            if w and len(pts) > 3:
                pts = list(zip([p[0] for p in pts], ema([p[1] for p in pts], w)))
            v = ps[-1][1]
            change = delta(v, at_base[1], r["run"] == base).strip() if at_base and len(runs) > 1 else ""
            names.append(f"{r['run']} {num(v, 4)}" + (f" {change}" if change else ""))
            series.append({"name": names[-1], "style": "line", "color": i, "points": thin(pts, budget)})  # its color in the list
        if robust:
            series = inliers(series)
        key = f"{j}-{safe(k)}"
        c = chart(f"chart-{key}", k, series, kind="line", x_label=x_label, y_label="", height=height, legend=False,
                  width=None if HOST.api >= 4 else WIDTHS[state["per"]], x_format="duration" if mode == "time" else "number",
                  y_scale=scale, sync="curves", controls=[button(f"del-{key}", "×")] if HOST.api >= 4 else None) if names else None
        if HOST.api >= 4:
            charts_[k] = c or label(f"nodata-{j}", f"No run shown logs {k} on this x axis.")
            continue
        cell_ = col(f"cell-{key}", c or button(f"nodata-{j}", f"No run shown logs {trunc(k, 30)} on this x axis", disabled=True),
                    None if c else pad(f"nodata-pad-{j}", height + TALL["chart"] - TALL["button"] - 4), gap=4)
        cells.append((cell_, height + TALL["chart"]))
    if HOST.api >= 4:
        if folds:
            out += [fold(sec[g], f"{g or 'other'} · {len(ks)} chart{'s' if len(ks) > 1 else ''}", g not in closed,
                         grid(f"grid-{sec[g]}", *(charts_[k] for k in ks if k in charts_), min_width=200, columns=per))
                    for g, ks in groups.items()]
        else:
            out.append(grid("grid", *charts_.values(), min_width=200, columns=per))
        return out, [], 0, picks, drawn, side
    if len(runs) > 1:  # the runs compared: a bar each
        if bars_ := bars_chart(runs, state["bars"], last, by_id, facts, proto, height, WIDTHS[state["per"]]):
            cells.insert(0, (col("cell-bars", bars_), height + TALL["chart"]))
    inside, below = tiles("grid", [c for c, _ in cells], per, gap=12)
    out += inside
    h += sum(max(t for _, t in cells[i:i + per]) + MAIN_GAP for i in range(0, per * len(inside), per)) - MAIN_GAP
    return out, below, h, picks, drawn, side


# --- scores, metrics, config


def comparison(runs: list[dict], state: dict, tight: bool) -> list[dict]:
    from kx.views.nextcode.runs import flatten
    from kx.views.text import num, score
    b = next(i for i, r in enumerate(runs) if r["run"] == state["base"])
    base, out = runs[b], []
    verdicts = []
    for r in runs:
        try:
            cmp = run_kx(["compare", base["run"], r["run"]]) if r is not base else {"comparisons": []}
            verdicts.append({c["protocol"]: c for c in cmp["comparisons"]})
        except KxError:
            verdicts.append({})
    ref = (run_kx(["protocol", "list"]).get("reference") or {}).get("id")
    protos = sorted({s["protocol"] for r in runs for s in r["scores"]}, key=lambda p: (p != ref, p))
    rows = []
    for p in protos:
        cells = [p + (" · reference" if p == ref else "")]
        for i, r in enumerate(runs):
            s = next((x for x in r["scores"] if x["protocol"] == p), None)
            cells.append("–" if not s else states.verdict(score(s["value"], s["se"]), verdicts[i].get(p), base["run"]))
        rows.append(cells)
    nums = []
    for r in runs:
        n = {k: v for k, v in (r.get("metrics") or {}).items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        n.update({f"{k} (last)": pts[-1][1] for k, pts in (r.get("curve_points") or {}).items() if pts})
        nums.append(n)
    for k in sorted({k for n in nums for k in n}, key=lambda k: (k.endswith(" (last)"), k)):
        rows.append([k] + ["–" if n.get(k) is None else num(n[k]) + delta(n[k], nums[b].get(k), i == b) for i, n in enumerate(nums)])
    cols = ["", *(f"{i + 1} · {r['run']}" + (" · base" if i == b else "") for i, r in enumerate(runs))]
    picker = select("base", "Compare against", base["run"], [(r["run"], f"{r['run']} · {trunc(r.get('hypothesis'), 80)}") for r in runs]) \
        if len(runs) > 1 else None
    if rows:
        out += [row("scores-head", md("scores-title", "### Scores, metrics and last values" + (
                    f"\n\neach run against {base['run']}: `kx compare`'s verdict, the change in %" if len(runs) > 1 else "")), picker, gap=16),
                table("scores", cols, rows[: (40 if tight else 1024 // len(cols))], widths=[260] + [0] * len(runs), max_height=520)]
    attrs = [("git", lambda r: r.get("git_sha") and r["git_sha"][:10] + (" (dirty)" if r.get("git_dirty") else "")),
             ("branch", lambda r: r.get("git_branch")),
             ("command", lambda r: r["command"] if isinstance(r["command"], str) else " ".join(r["command"] or [])),
             ("host", lambda r: (r.get("remote") or {}).get("host")), ("by", lambda r: r.get("author")),
             ("task", lambda r: r.get("task")), ("tags", lambda r: ", ".join(r.get("tags") or []) or None)]
    flat = [flatten(r.get("config") or {}) for r in runs]
    lines = [(k, [f(r) for r in runs]) for k, f in attrs] + [(k, [c.get(k) for c in flat]) for k in sorted({k for c in flat for k in c})]
    differ = [(k, vs) for k, vs in lines if any(v != vs[b] for v in vs)]
    show = [(k, vs) for k, vs in lines if any(v is not None for v in vs)] if state["allcfg"] or len(runs) == 1 else differ
    out.append(row("config-head", md("config-title", "### Config and code\n\n" + (f"{len(differ)} of {len(lines)} differ" if len(runs) > 1
                                                                                  else f"{len(flat[0])} config keys")),
                   checkbox("allcfg", "all rows", state["allcfg"]) if len(runs) > 1 else None, gap=16))
    def differs(v, i, vs):
        text = trunc("–" if v is None else v, 200)
        if i == b or v == vs[b]:
            return text
        return cell(text, mark("dot", f"differs from {base['run']}", AMBER)) if HOST.marks else f"≠ {text}"
    if show:
        out.append(table("config", cols, [[k] + [differs(v, i, vs) for i, v in enumerate(vs)]
                                          for k, vs in show][: (30 if tight else 1024 // len(cols))],
                         widths=[200] + [0] * len(runs), max_height=520))
    else:
        out.append(label("config-same", "Same config and code everywhere."))
    return out


def delta(v, base, is_base: bool) -> str:
    if is_base or base is None or v == base:
        return ""
    if base:
        return f"  {(v - base) / abs(base) * 100:+.1f}%"
    return f"  {v - base:+.3g}"
