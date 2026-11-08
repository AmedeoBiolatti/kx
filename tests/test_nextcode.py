"""kx nextcode panel: the NextCode panel's responses, inside the limits NextCode checks, for extension API 3 (NextCode
0.1.3 to 0.1.5) and API 4 (NextCode 0.1.6, which also draws icons in table cells)."""

import json
import re

import pytest

import kx
from kx.cli.main import main as kx_main
from kx.views import nextcode
from kx.views.nextcode import widgets

from conftest import scored_run, run_kx

# API 3's widget fields (NextCode refuses others), and the optional ones NextCode 0.1.4 added
FIELDS3 = {
    "row": {"children", "gap"}, "column": {"children", "gap"}, "markdown": {"text"}, "label": {"text"},
    "button": {"text", "disabled"}, "text-input": {"label", "value", "placeholder", "disabled"},
    "checkbox": {"label", "value", "disabled"}, "select": {"label", "value", "options", "disabled"},
    "progress": {"label", "value"}, "table": {"columns", "rows"}, "chart": {"title", "chart", "height"},
    "scroll": {"child", "height"},
}
OPTIONAL3 = {"chart": {"legend", "width"}, "checkbox": {"color"}, "select": {"label_inline"}, "text-input": {"label_inline"}}
# API 4's (extension_api/src/lib.rs): required, optional
FIELDS4 = {
    "row": ({"children"}, {"gap", "align"}), "column": ({"children"}, {"gap", "width", "grow"}),
    "grid": ({"children"}, {"min_width", "gap", "columns"}), "section": ({"title", "children"}, {"open"}),
    "scroll": ({"child", "height"}, set()), "space": ({"size"}, set()), "label": ({"text"}, set()),
    "markdown": ({"text"}, set()), "stat": ({"label", "value"}, {"detail", "color"}), "button": ({"text"}, {"disabled"}),
    "text-input": (set(), {"label", "label_inline", "value", "placeholder", "disabled"}),
    "checkbox": ({"value"}, {"label", "disabled", "color"}),
    "select": ({"value", "options"}, {"label", "label_inline", "disabled"}), "tabs": ({"value", "options"}, {"disabled"}),
    "progress": ({"value"}, {"label"}),
    "table": ({"columns", "rows"}, {"widths", "selectable", "selected", "max_height", "check", "checked", "disabled",
                                    "swatches", "marks"}),
    "chart": ({"chart"}, {"title", "height", "legend", "width", "controls", "sync"}),
}
AXES = {"x_label", "y_label", "x_scale", "y_scale", "x_format", "y_format"}


@pytest.fixture(params=[3, 4], ids=["api3", "api4"])
def api(request):
    """The NextCode kx draws for: API 3 (0.1.4), or API 4 (0.1.6)."""
    was = (widgets.HOST.api, widgets.HOST.version)
    widgets.HOST.set(request.param, (0, 1, 4) if request.param == 3 else (0, 1, 6))
    yield request.param
    widgets.HOST.set(*was)


@pytest.fixture
def api3():
    was = (widgets.HOST.api, widgets.HOST.version)
    yield widgets.HOST.set(3, None)
    widgets.HOST.set(*was)


@pytest.fixture
def api4():
    was = (widgets.HOST.api, widgets.HOST.version)
    yield widgets.HOST.set(4, (0, 1, 6))
    widgets.HOST.set(*was)


def panel(capsys, invocation) -> dict:
    """Runs the command as the extension does (no --json: one compact line) and checks what NextCode would."""
    capsys.readouterr()
    code = kx_main(["nextcode", "panel", "--invocation", json.dumps(invocation)])
    out = capsys.readouterr().out
    assert code == 0 and out.count("\n") == 1
    r = json.loads(out)
    assert len(out.encode()) <= nextcode.max_bytes() and set(r) == {"panels"}
    [p] = r["panels"]
    want = invocation.get("id") or invocation.get("panel")
    assert p["id"] == want and p["refresh_ms"] >= 1000 and len(json.dumps(p["state"])) <= widgets.HOST.limits["state"]
    assert widgets.problems(p["root"]) is None
    for w in walk(p["root"]):
        fields(w)
    assert "error" not in ids(p), ids(p)["error"]["text"]
    return p


def fields(w: dict):
    """Only the fields the host takes."""
    have = set(w) - {"type", "id"}
    if widgets.HOST.api < 4:
        assert FIELDS3[w["type"]] <= have <= FIELDS3[w["type"]] | OPTIONAL3.get(w["type"], set()), w
    else:
        need, may = FIELDS4[w["type"]]
        assert need <= have <= need | may, w
        if "marks" in w:
            assert widgets.HOST.marks
            assert all(set(m) <= {"icon", "color", "label"} and m["icon"] in widgets.ICONS for r in w["marks"] for c in r for m in c)
    if w["type"] != "chart":
        return
    spec = w["chart"]
    if spec["kind"] == "bar":
        assert {"kind", "labels", "series"} <= set(spec) <= {"kind", "labels", "series", "x_label", "y_label",
                                                            *(["y_format"] if widgets.HOST.api >= 4 else [])}
        assert all({"name", "values"} <= set(s) <= {"name", "values", "color"} for s in spec["series"])
        return
    assert spec["kind"] in ("line", "scatter")
    axes = AXES if widgets.HOST.api >= 4 else {"x_label", "y_label"}
    assert {"kind", "series"} <= set(spec) <= {"kind", "series"} | axes, spec.keys()
    point = {"x", "y", "label"} if widgets.HOST.api >= 4 else {"x", "y"}
    series = {"name", "points", "color", "style"} if widgets.HOST.api >= 4 else {"name", "points", "color"}
    assert all({"name", "points"} <= set(s) <= series and all({"x", "y"} <= set(q) <= point for q in s["points"])
               for s in spec["series"])


def walk(w):
    yield w
    for c in w.get("children", []) + ([w["child"]] if "child" in w else []) + w.get("controls", []):
        yield from walk(c)


def sh(x):
    """Run ids as the tests write them: r0003 for r0003-calm-lotus (a v3 id carries a name), in any strings inside."""
    if isinstance(x, str):
        return re.sub(r"\b(r\d{4,})-[a-z]+(?:-[a-z]+)*", r"\1", x)
    if isinstance(x, list):
        return [sh(v) for v in x]
    if isinstance(x, dict):
        return {sh(k): sh(v) for k, v in x.items()}
    return x


def full(run: str) -> str:
    """The id of the run a test calls r0003."""
    from kx.ledger import config
    from kx.ledger.store import Store
    with Store(config.load()) as st:
        return st.resolve(run)


def page(p: dict, raw: bool = False) -> dict:
    """The state of the page open (its run ids shortened, unless raw)."""
    st = p["state"]["pages"][p["state"]["page"]]
    return st if raw else sh(st)


def ids(p: dict) -> dict:
    return {w["id"]: w for w in walk(p["root"])}


def open_(capsys, name):
    return panel(capsys, {"kind": "open-panel", "id": name})


def event(capsys, p, widget, kind, inputs=None, **payload):
    return panel(capsys, {"kind": "ui-event", "panel": p["id"], "revision": 1, "state": p["state"], "inputs": inputs or {},
                          "event": {"widget": widget, "kind": {"kind": kind, **payload}}})


def go(capsys, p, to, inputs=None):
    """A tab picked."""
    if widgets.HOST.api >= 4:
        return event(capsys, p, "nav", "change", {**(inputs or {}), "nav": to}, value=to)
    return event(capsys, p, f"nav-{to}", "click", inputs)


def picks(p) -> dict:
    """The Training page's run list: run -> (ticked, color, can't be ticked)."""
    if widgets.HOST.api >= 4:
        t = ids(p)["pick"]
        return {sh(r): (i in t["checked"], t["swatches"][i], i in t["disabled"]) for i, r in enumerate(page(p, True)["listed"])}
    w = ids(p)
    return {sh(r): (w[f"pick-{r}"]["value"], w[f"pick-{r}"].get("color"), w[f"pick-{r}"]["disabled"]) for r in page(p, True)["listed"]}


def tick(capsys, p, run, on, inputs=None):
    run = full(run)
    if widgets.HOST.api >= 4:
        return event(capsys, p, "pick", "check-row", inputs, index=page(p, True)["listed"].index(run), value=on)
    return event(capsys, p, f"pick-{run}", "change", inputs, value=on)


def charts(p, kind="line") -> list[dict]:
    return [x for x in walk(p["root"]) if x["type"] == "chart" and x["chart"]["kind"] == kind]


def titled(p, title) -> dict:
    return next(c for c in charts(p) if c["title"].rstrip("\xa0") == title)


def marks(table: dict, i: int, j: int) -> list[str]:
    """The icons before a cell (NextCode 0.1.6)."""
    m = table.get("marks") or []
    return [x["icon"] for x in (m[i][j] if i < len(m) and j < len(m[i]) else [])]


def curves_run(hyp, n=30, scale=1.0, **kw):
    with kx.run(hyp, config={"lr": 0.1 * scale, "depth": 6}, **kw) as r:
        for i in range(n):
            r.step(i, **{"train/loss": scale / (i + 1), "val/loss": scale * 1.1 / (i + 1), "lr": 0.1, "seen": i * 32})
        r.score(0.6 + scale / 10, kx.Protocol("cv3", metric="auc", folds="f1"), breakdown={"f0": 0.6, "f1": 0.6 + scale / 5})
    return r


def test_the_host_comes_from_nextcodes_environment():
    old = widgets.Host.from_env({})
    assert (old.api, old.version, old.marks, old.chart_width) == (3, None, False, False)
    h = widgets.Host.from_env({"NEXT_CODE_VERSION": "0.1.4"})
    assert (h.api, h.version, h.marks, h.chart_width) == (3, (0, 1, 4), False, True)
    h = widgets.Host.from_env({"NEXT_CODE_VERSION": "0.1.5", "NEXT_CODE_EXTENSION_API": "4"})
    assert (h.api, h.marks, h.limits["widgets"]) == (4, False, 256)  # API 4 before marks
    h = widgets.Host.from_env({"NEXT_CODE_VERSION": "0.1.6", "NEXT_CODE_EXTENSION_API": "5"})
    assert (h.api, h.marks) == (4, True)
    assert widgets.Host.from_env({"NEXT_CODE_EXTENSION_API": "x"}).api == 3


def test_overview_shows_status_and_scores_and_a_clicked_point_opens_its_run(comp, capsys, api):
    for i in range(5):
        scored_run(f"run {i}", {"f0": 0.5 + i / 50 * (-1) ** i, "f1": 0.6})
    scored_run("other protocol", {"f0": 0.7}, protocol=kx.Protocol("holdout", metric="auc"))
    run_kx("task", "add", "try *lgbm* dart", capsys=capsys)
    p = open_(capsys, "overview")
    w = ids(p)
    assert "demo" in w["title"]["text"] and w["refresh"]["type"] == "button"
    assert w["tasks"]["rows"][0][1] == "try *lgbm* dart"  # table text as is, not markdown
    best, each = w["scores"]["chart"]["series"]
    assert [q["y"] for q in each["points"]] == [0.55, 0.54, 0.57, 0.52, 0.59]
    assert [q["y"] for q in best["points"]] == [0.55, 0.55, 0.57, 0.57, 0.59]  # the best so far, at each run
    if api >= 4:
        assert best["style"] == "step" and sh(each["points"][2]["label"]) == "r0003 · run 2"  # a dot says its run
        assert w["scores"]["controls"] == [w["protocol"]] and w["stat-best"]["value"].startswith("0.59")
    p = event(capsys, p, "scores", "chart-select", series=1, index=2)
    assert page(p)["run"] == "r0003" and "r0003" in ids(p)["card-title"]["text"]
    assert ids(p)["card-scores"]["rows"][0][0].startswith("cv3@")
    p = event(capsys, p, "close-run", "click")
    assert page(p)["run"] is None and "card" not in ids(p)
    ref = page(p)["protocol"]
    other = next(o["value"] for o in ids(p)["protocol"]["options"] if o["value"] != ref)
    p = event(capsys, p, "protocol", "change", {"protocol": other}, value=other)
    assert page(p)["protocol"] == other and ids(p)["protocol"]["value"] == other
    if api < 4:
        assert ids(p)["scores"]["title"].rstrip("\xa0") == other
    p = panel(capsys, {"kind": "refresh", "panel": "overview", "revision": 2, "state": p["state"], "inputs": {}})
    assert ids(p)["protocol"]["value"] == other


def test_overview_rows_open_their_task_or_submission(comp, capsys, api4):
    code, t = run_kx("task", "add", "try dart", capsys=capsys)
    p = open_(capsys, "kx")
    w = ids(p)
    assert w["tasks"]["selectable"] and marks(w["tasks"], 0, 2) == ["circle"]  # ready
    p = event(capsys, p, "tasks", "select-row", index=0)
    assert p["state"]["page"] == "tasks" and page(p)["task"] == t["id"] and ids(p)["nav"]["value"] == "tasks"
    root = p["root"]  # beside the list, which keeps its place
    assert widgets.is_sidebar(root) and [c["id"] for c in root["children"]] == ["root-main", "root-side"]
    assert root["children"][0]["children"][0]["id"] == "top" and "card-title" in {x["id"] for x in walk(root["children"][1])}
    assert ids(p)["tasks"]["columns"] == ["task", "title", "state"]


def test_runs_filter_and_open_a_run(comp, capsys, api):
    for i in range(4):
        scored_run(f"lgbm {i}" if i % 2 else f"xgb {i}", {"f0": 0.6 + i / 100})
    curves_run("with curves")
    p = open_(capsys, "runs")
    assert [sh(r[0]) for r in ids(p)["runs"]["rows"]] == ["r0005", "r0004", "r0003", "r0002", "r0001"]
    assert ids(p)["runs"]["rows"][0][1] == "done"
    if api >= 4:
        assert marks(ids(p)["runs"], 0, 1) == ["check"]
    p = event(capsys, p, "q", "submit", {"q": "lgbm"})
    assert [sh(r[0]) for r in ids(p)["runs"]["rows"]] == ["r0004", "r0002"]
    p = event(capsys, p, "curves", "change", {"q": "", "curves": True}, value=True)
    assert [sh(r[0]) for r in ids(p)["runs"]["rows"]] == ["r0005"]
    p = event(capsys, p, "open", "submit", {"open": "5", "curves": True})
    w = ids(p)
    assert page(p)["run"] == "r0005" and "with curves" in w["card-title"]["text"]
    assert {r[1] for r in w["card-values"]["rows"]} >= {"lr", "depth"}
    assert w["card-curve-0"]["chart"]["series"][0]["points"][0] == {"x": 0, "y": 1.0}
    if api >= 4:  # the run beside the list
        assert widgets.is_sidebar(p["root"]) and w["runs"]["columns"] == ["run", "status", "score", "hypothesis"]
    p = event(capsys, p, "runs", "select-row", {"curves": True}, index=0)  # a row
    assert page(p)["run"] == "r0005"
    p = event(capsys, p, "open", "submit", {"open": "r0999"})
    assert "r0999" in ids(p)["card-title"]["text"]  # no such run: said in its card, the panel stays
    p = event(capsys, p, "close-run", "click")
    assert page(p)["run"] is None and not widgets.is_sidebar(p["root"])


def test_one_panel_has_every_page_as_a_tab(comp, capsys, api):
    scored_run("lgbm", {"f0": 0.6})
    curves_run("with curves")
    code, t = run_kx("task", "add", "try curves", capsys=capsys)
    p = open_(capsys, "kx")
    w = ids(p)
    assert p["state"]["page"] == "overview" and "demo" in w["title"]["text"] and "refresh" in w
    if api >= 4:
        assert w["nav"]["type"] == "tabs" and w["nav"]["value"] == "overview"
    else:
        assert w["nav-overview"]["type"] == "markdown" and {"nav-runs", "nav-training", "nav-subs"} <= set(w)
    p = go(capsys, p, "runs")
    assert p["state"]["page"] == "runs" and (api >= 4 or ids(p)["nav-overview"]["type"] == "button")
    p = event(capsys, p, "q", "submit", {"q": "lgbm"})
    assert [sh(r[0]) for r in ids(p)["runs"]["rows"]] == ["r0001"]
    p = go(capsys, p, "tasks", {"q": "lgbm"})  # the form of the page left does not reach the next
    assert p["state"]["page"] == "tasks" and ids(p)["tasks"]["rows"]
    p = go(capsys, p, "runs")  # a page comes back as it was left
    assert page(p)["q"] == "lgbm" and [sh(r[0]) for r in ids(p)["runs"]["rows"]] == ["r0001"]
    p = panel(capsys, {"kind": "refresh", "panel": "kx", "revision": 2, "state": p["state"], "inputs": {}})
    assert p["state"]["page"] == "runs" and p["refresh_ms"] == nextcode.refresh_ms()  # nothing runs: seldom
    p = event(capsys, p, "open", "submit", {"open": "r2"})
    p = event(capsys, p, "train-run", "click")  # a run's card: Show in Training
    assert p["state"]["page"] == "training" and page(p)["runs"] == ["r0002"]


def test_training_overlays_curves_and_compares_with_one_run(comp, capsys, api):
    curves_run("small", scale=1.0)
    curves_run("big", scale=2.0)
    p = open_(capsys, "training")
    assert page(p)["runs"] == ["r0002", "r0001"]  # a first visit: the newest that log curves
    assert [picks(p)[r][:2] for r in ("r0002", "r0001")] == [(True, 0), (True, 1)]  # colors: the charts'
    assert sorted(c["title"].rstrip("\xa0") for c in charts(p)) == ["lr", "train/loss", "val/loss"]  # seen counts up: an x axis instead
    loss = titled(p, "train/loss")
    assert [sh(s["name"]) for s in loss["chart"]["series"]] == ["r0002 0.06667", "r0001 0.03333 -50.0%"]  # last value, change
    assert [s["color"] for s in loss["chart"]["series"]] == [0, 1] and loss["legend"] is False  # colors: the list's swatches
    assert len(loss["chart"]["series"][0]["points"]) == 29  # the first step, far above the rest: an outlier, ignored
    q = event(capsys, p, "scale", "change", {"scale": "linear"}, value="linear")
    assert len(titled(q, "train/loss")["chart"]["series"][0]["points"]) == 30
    q = event(capsys, q, "x", "change", {"x": "pct"}, value="pct")  # runs of different lengths side by side
    loss = titled(q, "train/loss")
    assert loss["chart"]["x_label"] == "% of the run" and [p_["x"] for p_ in loss["chart"]["series"][0]["points"]][::29] == [0, 100]
    [bars] = charts(p, "bar")  # the runs side by side
    assert [s["values"] for s in bars["chart"]["series"]] == [[0.8], [0.7]] and ids(p)["bars"]["value"] == "score"
    p = event(capsys, p, "bars", "change", {"bars": "train/loss"}, value="train/loss")
    [bars] = charts(p, "bar")
    assert [s["values"][0] for s in bars["chart"]["series"]] == pytest.approx([2 / 30, 1 / 30], rel=1e-5)
    assert ids(p)["bars"]["value"] == "train/loss"
    assert api >= 4 or bars["title"].rstrip("\xa0") == "last train/loss"
    w = ids(p)
    assert {o["value"] for o in w["x"]["options"]} == {"step", "pct", "time", "c:seen"}
    assert page(p)["drawn"] == ["lr", "train/loss", "val/loss"]  # by section
    scores = w["scores"]["rows"]
    assert scores[0][0].startswith("cv3@") and scores[0][2].split()[0] in ("0.7000", "0.7")
    assert any(r[0] == "seen (last)" for r in scores)
    if api >= 4:  # kx compare's verdict, and what differs, as icons
        assert marks(w["scores"], 0, 2) in (["check"], ["cross"], ["dot"], ["warning"], ["circle"])
        assert [r for r in w["config"]["rows"] if r[0] == "lr"] == [["lr", "0.2", "0.1"]]
        i = next(i for i, r in enumerate(w["config"]["rows"]) if r[0] == "lr")
        assert (marks(w["config"], i, 1), marks(w["config"], i, 2)) == ([], ["dot"])
    else:
        assert [r for r in w["config"]["rows"] if r[0] == "lr"] == [["lr", "0.2", "≠ 0.1"]]
    p = event(capsys, p, "base", "change", {"base": full("r0001")}, value=full("r0001"))  # compare against the other
    lr = next(r for r in ids(p)["config"]["rows"] if r[0] == "lr")
    assert lr == (["lr", "0.2", "0.1"] if api >= 4 else ["lr", "≠ 0.2", "0.1"])
    p = event(capsys, p, "smooth", "change", {"smooth": "0.9", "scale": "log", "x": "time"}, value="0.9")
    loss = titled(p, "train/loss")
    if api >= 4:
        assert loss["chart"]["y_scale"] == "log" and loss["chart"]["x_format"] == "duration" and loss["chart"]["x_label"] == "time"
    else:
        assert loss["chart"]["y_label"] == "log10" and loss["chart"]["x_label"] == "seconds"
    p = event(capsys, p, "x", "change", {"smooth": "0", "scale": "linear", "x": "c:seen"}, value="c:seen")
    loss = titled(p, "train/loss")
    assert loss["chart"]["x_label"] == "seen" and [q["x"] for q in loss["chart"]["series"][0]["points"][:3]] == [0, 32, 64]
    first = page(p)["drawn"][0]
    if api >= 4:  # its ×, in its title row
        p = event(capsys, p, f"del-0-{first}", "click")
    else:
        p = event(capsys, p, "remove-chart", "change", value=first)
    assert page(p)["drawn"] == ["train/loss", "val/loss"] and page(p)["charts"] == page(p)["drawn"]
    p = event(capsys, p, "add-chart", "change", value="seen")  # any curve, the counts too
    assert page(p)["drawn"] == ["train/loss", "val/loss", "seen"]
    p = event(capsys, p, "cq", "submit", {"cq": "loss"})  # the charts of the curves named so
    assert page(p)["charts"] is None and sorted(page(p)["drawn"]) == ["train/loss", "val/loss"]
    p = tick(capsys, p, "r0002", False)  # untick
    assert page(p)["runs"] == ["r0001"] and picks(p)["r0002"][0] is False
    p = tick(capsys, p, "r0002", True)  # tick again: last, in the next color
    assert page(p)["runs"] == ["r0001", "r0002"]
    p = event(capsys, p, "q", "submit", {"q": "small"})
    assert [k for k in page(p)["listed"]] == ["r0001", "r0002"]  # ticked ones stay listed
    p = event(capsys, p, "pick-found", "click", {"q": "small"})  # the newest found, alone
    assert page(p)["runs"] == ["r0001"]
    p = event(capsys, p, "pick-none", "click")
    assert page(p)["runs"] == [] and "empty" in ids(p)


def test_training_is_a_workspace_with_a_sidebar_and_sections(comp, capsys, api4):
    curves_run("small", scale=1.0)
    curves_run("big", scale=2.0)
    p = open_(capsys, "kx")
    p = go(capsys, p, "training")
    root = p["root"]  # the panel's root: the sidebar beside the charts, each scrolling on its own (NextCode 0.1.6)
    assert widgets.is_sidebar(root) and [c["id"] for c in root["children"]] == ["root-side", "root-main"]
    side, main = root["children"]
    assert side["width"] == 380 and main["children"][0]["id"] == "top"  # the tabs above the charts
    assert {"q", "pick", "bars-score", "cq", "add-chart"} <= {x["id"] for x in walk(side)}
    w = ids(p)
    assert [w[s]["title"] for s in ("sec-", "sec-train", "sec-val")] == ["other · 1 chart", "train · 1 chart", "val · 1 chart"]
    loss = titled(p, "val/loss")
    assert loss["sync"] == "curves" and [c["id"] for c in loss["controls"]] == ["del-2-val_loss"]
    assert marks(w["pick"], 0, 2) == ["check"] and w["pick"]["rows"][0][2] == ""  # done: the icon alone
    p = event(capsys, p, "sec-val", "change", {"sec-val": False, "sec-train": True, "sec-": True}, value=False)
    assert page(p)["closed"] == ["val"] and ids(p)["sec-val"]["open"] is False and ids(p)["sec-val"]["children"] == []
    assert "chart-2-val_loss" not in ids(p) and page(p)["drawn"] == ["lr", "train/loss", "val/loss"]
    p = panel(capsys, {"kind": "refresh", "panel": "kx", "revision": 3, "state": p["state"], "inputs": {}})
    assert ids(p)["sec-val"]["children"] == []  # stays folded
    p = event(capsys, p, "sec-val", "change", {"sec-val": True}, value=True)
    assert page(p)["closed"] == [] and "chart-2-val_loss" in ids(p)


def test_marks_only_where_the_host_draws_them(comp, capsys):
    curves_run("one")
    was = (widgets.HOST.api, widgets.HOST.version)
    try:
        widgets.HOST.set(4, (0, 1, 5))  # API 4 before 0.1.6: the icons' labels as text
        p = open_(capsys, "runs")
        assert "marks" not in ids(p)["runs"] and ids(p)["runs"]["rows"][0][1] == "done"
        p = open_(capsys, "training")
        assert "marks" not in ids(p)["pick"] and ids(p)["pick"]["rows"][0][2] == "done"
    finally:
        widgets.HOST.set(*was)


def test_six_runs_of_many_curves_fit(comp, capsys, api):
    for i in range(7):
        with kx.run(f"run {i}") as r:
            for s in range(2000 if i else 10):
                r.step(s, **{f"m{k}": (k + 1) / (s + 1) + i for k in range(20)})
    p = open_(capsys, "training")
    for r in ("r0004", "r0003", "r0002"):
        p = tick(capsys, p, r, True)
    assert len(page(p)["runs"]) == 6 and picks(p)["r0001"][2]  # six at most: the rest can't be ticked
    assert all(len(c["chart"]["series"]) == 6 for c in charts(p))
    assert len(charts(p)) == (12 if api >= 4 else 11)  # and the runs' bars: four rows of 3 (API 4: in the sidebar)


def test_a_task_shows_its_runs_in_training(comp, capsys, api):
    curves_run("not of the task")
    code, t = run_kx("task", "add", "curves", capsys=capsys)
    run_kx("task", "start", t["id"], capsys=capsys)  # the runs that follow are the task's
    curves_run("one")
    curves_run("two")
    p = open_(capsys, "tasks")
    p = event(capsys, p, "open", "submit", {"open": t["id"]})
    p = event(capsys, p, "train-task", "click")
    assert p["state"]["page"] == "training" and page(p)["runs"] == ["r0002", "r0003"]
    assert ids(p)["task"]["type"] == "select"


def test_tasks_findings_and_submissions(comp, capsys, api):
    code, t = run_kx("task", "add", "try lgbm dart", "--body", "because *trees*", capsys=capsys)
    run_kx("task", "note", t["id"], "halfway there", capsys=capsys)
    run_kx("finding", "add", "dart beats gbdt by 0.01", "--tag", "algorithm", capsys=capsys)
    run_kx("finding", "add", "fold 3 is odd", "--tag", "data", capsys=capsys)
    p = open_(capsys, "tasks")
    assert ids(p)["tasks"]["rows"][0][:3] == [t["id"], "try lgbm dart", "ready"]
    p = event(capsys, p, "open", "submit", {"open": t["id"]})
    w = ids(p)
    assert "try lgbm dart" in w["card-title"]["text"] and "\\*trees\\*" in w["card-body"]["text"] and "halfway" in w["card-notes"]["text"]
    p = event(capsys, p, "close", "click")
    assert "card" not in ids(p) and "card-title" not in ids(p)
    p = event(capsys, p, "tasks", "select-row", index=0)
    assert page(p)["task"] == t["id"]
    f = open_(capsys, "findings")
    assert "dart beats" in ids(f)["findings-0"]["text"] and "fold 3" in ids(f)["findings-0"]["text"]
    f = event(capsys, f, "tag", "change", {"tag": "data"}, value="data")
    assert "fold 3" in ids(f)["findings-0"]["text"] and "dart" not in ids(f)["findings-0"]["text"]
    s = open_(capsys, "subs")
    assert "subs-empty" in ids(s)


def test_a_big_ledger_falls_back_to_less_and_then_says_so(comp, capsys, monkeypatch, api):
    for i in range(30):
        code, t = run_kx("task", "add", f"task {i} " + "x" * 200, capsys=capsys)
        run_kx("task", "start", t["id"], capsys=capsys)
    for i in range(12):
        scored_run(f"run {i} " + "y" * 300, {"f0": 0.5 + i / 100})
    full = open_(capsys, "overview")
    assert len(ids(full)["tasks"]["rows"]) == 30
    monkeypatch.setattr(nextcode, "MAX_BYTES", nextcode.size({"panels": [full]}) - 1)  # too big: shows less
    assert len(ids(open_(capsys, "overview"))["tasks"]["rows"]) == 12
    monkeypatch.setattr(nextcode, "MAX_BYTES", 600)  # too big even then: says so
    capsys.readouterr()
    kx_main(["nextcode", "panel", "--invocation", json.dumps({"kind": "open-panel", "id": "overview"})])
    assert "too much to show" in capsys.readouterr().out


def test_outside_a_competition_every_panel_says_so(tmp_path, monkeypatch, capsys, api):
    monkeypatch.chdir(tmp_path)
    for name in ("kx", "overview", "runs", "training", "tasks", "findings", "subs"):
        capsys.readouterr()
        kx_main(["nextcode", "panel", "--invocation", json.dumps({"kind": "open-panel", "id": name})])
        [p] = json.loads(capsys.readouterr().out)["panels"]
        assert p["id"] == name and "no comp.yaml" in ids(p)["error"]["text"] and widgets.problems(p["root"]) is None


def test_a_bad_invocation_is_a_usage_error(comp, capsys):
    assert kx_main(["nextcode", "panel", "--invocation", "{nope"]) == 1
    assert "not JSON" in capsys.readouterr().err


def test_charts_drawn_with_what_api_3_has(api3):
    day = 86400
    c = widgets.chart("c", "t", [{"name": "s", "style": "step", "points": [(0, 1.0), (2, 3.0), (5, 2.0)]}])
    assert [(q["x"], q["y"]) for q in c["chart"]["series"][0]["points"]] == [(0, 1.0), (2, 1.0), (2, 3.0), (5, 3.0), (5, 2.0)]
    c = widgets.chart("c", "t", [{"name": "s", "points": [(1_700_000_000, 1), (1_700_000_000 + 3 * day, 2)]}], x_format="date")
    assert [q["x"] for q in c["chart"]["series"][0]["points"]] == [0, 3] and c["chart"]["x_label"].startswith("days since 2023-11-14")
    c = widgets.chart("c", "t", [{"name": "s", "points": [(1, 100.0), (2, 0.0), (3, float("nan")), (4, 0.01)]}], y_scale="log")
    assert [q["y"] for q in c["chart"]["series"][0]["points"]] == [2.0, -2.0]
    assert widgets.chart("c", "t", [{"name": "s", "points": [(1, float("inf"))]}]) is None
    fit = widgets.chart("c", "t", [{"name": "fit", "style": "line", "points": [(0, 0), (1, 1)]},
                                   {"name": "best", "style": "step", "points": [(0, 0), (1, 1)]}], kind="scatter")
    assert [len(x["points"]) for x in fit["chart"]["series"]] == [24, 2]  # a straight line dotted; steps stay points
    assert len(widgets.thin(list(range(1000)), 10)) == 10 and widgets.thin(list(range(1000)), 10)[-1] == 999
    boxed = widgets.chart("c", "t", [{"name": "s", "points": [(0, 1)]}], controls=[widgets.button("x", "×")])
    assert [w["id"] for w in boxed["children"]] == ["c-tools", "c"]  # controls: a row above the chart
    assert widgets.section("s", "val", True, widgets.label("l", "x"))["children"][0]["type"] == "checkbox"
    t = widgets.table("t", ["a", "b"], [[widgets.cell("", widgets.mark("check", "done")), widgets.cell("5", widgets.mark("dot", "x"))]])
    assert t["rows"] == [["done", "5"]] and "marks" not in t  # icons' labels stand in for an empty text


def test_charts_side_by_side_with_api_3(api3):
    c = widgets.chart("c", "t", [{"name": "s", "points": [(0, 1.0), (5000, 2.0)]}], x_label="step")
    assert c["chart"]["x_label"] == "step (thousands)" and [q["x"] for q in c["chart"]["series"][0]["points"]] == [0, 5]
    cells = [widgets.col(f"cell-{i}", widgets.label(f"l{i}", "x")) for i in range(7)]
    inside, below = widgets.tiles("grid", cells, 3)
    assert [len(r["children"]) for r in inside] == [3, 3] and [len(r["children"]) for r in below] == [1]
    inside, below = widgets.tiles("grid", cells[:2], 3)  # a row alone stays beside the sidebar
    assert [len(r["children"]) for r in inside] == [2] and below == []


def test_widgets_with_api_4(api4):
    c = widgets.chart("c", "t", [{"name": "s", "style": "step", "color": 14, "points": [(0, 1.0, "r1"), (5000, 2.0)]}],
                      x_format="date", y_scale="log", controls=[widgets.button("x", "×"), None], sync="g", legend=False)
    assert c["chart"]["series"][0] == {"name": "s", "style": "step", "color": 2,
                                       "points": [{"x": 0, "y": 1.0, "label": "r1"}, {"x": 5000, "y": 2.0}]}
    assert (c["chart"]["x_format"], c["chart"]["y_scale"], c["sync"], len(c["controls"])) == ("date", "log", "g", 1)
    t = widgets.table("t", ["a", "b", "c"], [["1", widgets.cell("", widgets.mark("check", "done", 5)), "x"], ["2", "", ""]],
                      selectable=True, selected=7)
    assert t["marks"] == [[[], [{"icon": "check", "color": 5, "label": "done"}]]] and "selected" not in t  # out of range
    s = widgets.section("s", "val", False, widgets.label("l", "x"))
    assert s == {"type": "section", "id": "s", "title": "val", "children": [], "open": False}
    assert widgets.problems(widgets.col("r", c, t, s)) is None
    bad = widgets.table("t", ["a"], [[widgets.cell("", *[widgets.mark("dot", "")] * 13)]])
    assert len(bad["marks"][0][0]) == 12  # at most 12 a cell


def test_training_filters_and_keeps_each_run_in_its_color(comp, capsys, api):
    curves_run("tagged", tags=["sweep"])
    with kx.run("other curves") as r:
        for i in range(5):
            r.step(i, other=i / 10)
    curves_run("failed one", scale=2.0)
    p = open_(capsys, "training")
    assert page(p)["runs"] == ["r0003", "r0002", "r0001"]
    loss = titled(p, "train/loss")
    assert [(sh(s["name"].split()[0]), s["color"]) for s in loss["chart"]["series"]] == [("r0003", 0), ("r0001", 2)]  # r0002 lacks it
    p = event(capsys, p, "tag", "change", {"tag": "sweep"}, value="sweep")
    assert [k for k in page(p)["listed"]] == ["r0003", "r0002", "r0001"]  # ticked runs stay listed
    p = event(capsys, p, "pick-found", "click", {"tag": "sweep"})
    assert page(p)["runs"] == ["r0001"]
    p = event(capsys, p, "status", "change", {"tag": "-", "status": "failed"}, value="failed")
    assert page(p)["listed"] == ["r0001"] and "pick-found" not in ids(p)  # no failed run
