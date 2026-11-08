"""NextCode's native widgets, for the extension API of the NextCode that runs kx (HOST).

Views describe what they show fully, as extension API 4 has it (NextCode 0.1.6, its docs/extensions.md): series
styles and colors, point labels, axis formats, column widths, clickable and tickable rows, icons in cells, tabs,
grids, sidebars, folding sections, controls in a chart's title row. These functions draw that with what the host
takes, down to API 3 (NextCode 0.1.3 to 0.1.5), where some of it is drawn with workarounds and the rest left out.
`problems` checks a panel against NextCode's limits before it is sent, as NextCode does
(crates/extensions/src/panels.rs).
"""

from __future__ import annotations

import math
import os
import re
import zlib
from datetime import datetime, timezone

LIMITS = {3: {"series": 6, "values": 4096, "text": 64 * 1024, "widgets": 128, "depth": 8, "state": 16 * 1024,
              "output": 192 * 1024, "cells": 1024, "chart_height": 500},
          4: {"series": 12, "values": 16384, "text": 256 * 1024, "widgets": 256, "depth": 12, "state": 64 * 1024,
              "output": 900 * 1024, "cells": 4096, "chart_height": 600}}
ICONS = ("check", "cross", "clock", "circle", "dot", "minus", "warning", "comment", "person")
# NextCode's palette (charts.rs): 0 blue, 1 orange, 2 teal, 3 amber, 4 pink, 5 green, 6 purple, 7 red, 8 cyan,
# 9 brown, 10 olive, 11 grey
BLUE, AMBER, GREEN, RED, GREY = 0, 3, 5, 7, 11


class Host:
    """What the NextCode that runs kx takes. NextCode puts it in its tools' environment: NEXT_CODE_VERSION (after
    0.1.4) and NEXT_CODE_EXTENSION_API (API 4: grids, sidebars, tabs, sections, stat tiles, sized, clickable and
    tickable tables, series styles, log and date axes, chart controls and sync, larger limits). An older host
    refuses fields it doesn't know, so each is sent only where it is taken."""

    def __init__(self, api: int = 3, version: tuple[int, ...] | None = None):
        self.set(api, version)

    def set(self, api: int, version: tuple[int, ...] | None = None) -> Host:
        self.api, self.version = (4 if api >= 4 else 3), version
        self.limits = LIMITS[self.api]
        self.chart_width = version is not None  # a chart's "width" (after 0.1.4)
        # 0.1.6: icons in table cells (marks); a root row of columns, one growing, fills the panel and each column
        # scrolls on its own (API 4 before it lays the same row out as a sidebar, the panel scrolling as a whole)
        self.marks = self.api >= 4 and version is not None and version >= (0, 1, 6)
        return self

    @classmethod
    def from_env(cls, env=os.environ) -> Host:
        try:
            api = int(env.get("NEXT_CODE_EXTENSION_API") or 3)
        except ValueError:
            api = 3
        v = env.get("NEXT_CODE_VERSION")
        return cls(api, tuple(int(x) for x in re.findall(r"\d+", v)[:3]) if v else None)


HOST = Host.from_env()


# --- layout


def col(id_: str, *children, gap: int = 8, width: int | None = None, grow: bool = False) -> dict:
    """width (px) or grow: its share of a row (API 4; with API 3 a column is as wide as what it holds)."""
    w = {"type": "column", "id": id_, "children": [c for c in children if c], "gap": gap}
    if HOST.api >= 4:
        w.update({k: v for k, v in (("width", width and max(120, min(1600, width))), ("grow", grow or None)) if v})
    return w


def row(id_: str, *children, gap: int = 8, align: str | None = None) -> dict:
    """align (API 4): start, center or end (the default: fields line up with buttons)."""
    w = {"type": "row", "id": id_, "children": [c for c in children if c], "gap": gap}
    if align and HOST.api >= 4:
        w["align"] = align
    return w


def sidebar(id_: str, side: list, main: list, *, width: int = 340, gap: int = 20, right: bool = False) -> dict:
    """A column of fixed width beside one taking the rest (API 4). As the panel's root, NextCode 0.1.6 fills the
    panel with it and scrolls each column on its own, so the side stays in place beside long content. right: the
    side on the right. Ids: `{id_}-side`, `{id_}-main`."""
    s, m = col(f"{id_}-side", *side, width=width), col(f"{id_}-main", *main, grow=True, gap=12)
    return row(id_, *((m, s) if right else (s, m)), gap=gap)


def is_sidebar(w: dict) -> bool:
    """A row of columns, one growing: what NextCode 0.1.6 lays out as the panel's panes when it is the root."""
    kids = w.get("children") or []
    return (w.get("type") == "row" and bool(kids) and all(c["type"] == "column" for c in kids)
            and any(c.get("grow") for c in kids))


def grid(id_: str, *children, min_width: int = 360, gap: int = 12, columns: int | None = None) -> dict | None:
    """API 4: as many equal columns as fit at min_width each, or exactly `columns` of them."""
    children = [c for c in children if c]
    if not children:
        return None
    if HOST.api >= 4:
        return {"type": "grid", "id": id_, "children": children, "min_width": max(120, min(1600, min_width)), "gap": gap,
                **({"columns": max(1, min(12, columns))} if columns else {})}
    return col(id_, *children, gap=gap)


def section(id_: str, title: str, open_: bool, *children) -> dict:
    """A titled group that folds away (API 4): clicking its title sends `change` with the new `open`, a boolean
    input as a checkbox's. Closed, its children are left out. With API 3: a checkbox with the title, the children
    under it."""
    kids = [c for c in children if c] if open_ else []
    if HOST.api >= 4:
        return {"type": "section", "id": id_, "title": title, "children": kids, **({} if open_ else {"open": False})}
    return col(f"{id_}-box", checkbox(id_, f"{'▾' if open_ else '▸'} {title}", open_), *kids)


def scroll(id_: str, child: dict, height: int) -> dict:
    return {"type": "scroll", "id": id_, "child": child, "height": max(32, min(600, height))}


def tabs(id_: str, value: str, options: list[tuple[str, str]]) -> dict:
    """A tab strip; picking one sends `change` (API 4). With API 3: a button a tab (`{id_}-{value}`), the open one as
    bold text."""
    if HOST.api >= 4:
        return {"type": "tabs", "id": id_, "value": value, "options": [{"value": v, "label": t} for v, t in options]}
    return row(id_, *(md(f"{id_}-{v}", f"**{esc(t)}**") if v == value else button(f"{id_}-{v}", t) for v, t in options),
               gap=4)


def pad(id_: str, px: float) -> dict | None:
    """Empty space px high, or nothing (one widget: a label of empty lines, 22.6 px each in NextCode)."""
    n = round(px / 22.6)
    return label(id_, "\n" * (n - 1)) if n >= 1 else None


def tiles(id_: str, children: list[dict], per_row: int, gap: int = 12) -> tuple[list[dict], list[dict]]:
    """Children side by side, per_row a row (API 4: a grid as wide as it can be): (the rows, a last row left over to
    put outside a sidebar's row). With API 3 a chart is about 320 px wide, and has its plot only when it is alone in
    a column: a cell is a column holding a column holding the chart. In a column beside another (a sidebar), a row
    shorter than the rows above it is laid out wrong (its cells drawn out of their boxes), so such a row is given
    back apart, to go under both."""
    if HOST.api >= 4:  # the grid sizes the cells and the charts fill them
        g = grid(id_, *children, min_width=200, gap=gap, columns=per_row)
        return ([g] if g else []), []
    rows = [row(f"{id_}-{zlib.crc32('|'.join(c['id'] for c in part).encode()):08x}", *part, gap=gap)
            for part in (children[i:i + per_row] for i in range(0, len(children), per_row))]
    if len(rows) > 1 and len(rows[-1]["children"]) < per_row:
        return rows[:-1], rows[-1:]
    return rows, []


# --- content


def stat(id_: str, label_: str, value: str, detail: str = "", color: int | None = None) -> dict:
    """A headline number with its name and a line under it (API 4); color: a palette index, drawn as a bar."""
    return {"type": "stat", "id": id_, "label": label_, "value": value,
            **({"detail": detail} if detail else {}), **({"color": color % 12} if color is not None else {})}


def md(id_: str, text: str) -> dict:
    return {"type": "markdown", "id": id_, "text": text}


def label(id_: str, text: str) -> dict:
    return {"type": "label", "id": id_, "text": text}


def button(id_: str, text: str, disabled: bool = False) -> dict:
    return {"type": "button", "id": id_, "text": text, "disabled": disabled}


def text_input(id_: str, label_: str, value: str = "", placeholder: str = "", inline: bool = False) -> dict:
    """inline: the label inside the field, `Label: value` (NextCode 0.1.4); "" draws the field alone."""
    return {"type": "text-input", "id": id_, "label": label_, "value": value[:4096], "placeholder": placeholder,
            "disabled": False, **({"label_inline": True} if inline and label_ else {})}


def checkbox(id_: str, label_: str, value: bool, disabled: bool = False, color: int | None = None) -> dict:
    """color: a palette index, drawn as a swatch before the label, the charts' color for it (NextCode 0.1.4)."""
    return {"type": "checkbox", "id": id_, "label": label_, "value": bool(value), "disabled": bool(disabled),
            **({"color": color % 12} if color is not None else {})}


def select(id_: str, label_: str, value: str, options: list[tuple[str, str]], inline: bool = False) -> dict | None:
    """options: (value, label), at most 32 (the current value kept among them). inline: as text_input's."""
    seen, opts = set(), []
    for v, text in options:
        if v and v not in seen:
            seen.add(v)
            opts.append((v, text))
    if not opts:
        return None
    if value not in seen:
        value = opts[0][0]
    keep = opts[:32] if any(v == value for v, _ in opts[:32]) else [o for o in opts if o[0] == value] + opts[:31]
    return {"type": "select", "id": id_, "label": label_, "value": value, "disabled": False,
            "options": [{"value": v, "label": trunc(text, 120) or v} for v, text in keep],
            **({"label_inline": True} if inline and label_ else {})}


def progress(id_: str, label_: str, value: float) -> dict:
    return {"type": "progress", "id": id_, "label": label_, "value": min(1.0, max(0.0, value or 0.0))}


# --- tables


def mark(icon: str, label_: str, color: int | None = None) -> dict:
    """An icon before a table cell's text (NextCode 0.1.6): check, cross, clock, circle, dot, minus, warning, comment
    or person, in a palette color (else muted); label_ says what it means (hover, and the text where icons aren't
    drawn)."""
    assert icon in ICONS, icon
    return {"icon": icon, **({"color": color % 12} if color is not None else {}), **({"label": label_} if label_ else {})}


def cell(text, *marks: dict | None) -> tuple:
    """A table cell with icons before its text. Where icons aren't drawn, their labels stand in for an empty text."""
    return ("" if text is None else str(text), [m for m in marks if m])


def _cells(rows: list[list], n_cols: int) -> tuple[list[list[str]], list]:
    """Rows of cells (text, or cell()): their texts, and their marks where the host draws them (else [])."""
    texts, marks = [], []
    for r in rows:
        tr, mr = [], []
        for c in r:
            if isinstance(c, tuple):
                text, ms = c
                tr.append(text if HOST.marks or text else " ".join(m.get("label", "") for m in ms))
                mr.append(ms[:12] if HOST.marks else [])
            else:
                tr.append("" if c is None else str(c))
                mr.append([])
        texts.append(tr)
        while mr and not mr[-1]:  # "shorter rows and cells have none"
            mr.pop()
        marks.append(mr)
    while marks and not marks[-1]:
        marks.pop()
    return texts, marks


def table(id_: str, columns: list[str], rows: list[list], *, widths: list[int] | None = None, selectable: bool = False,
          selected: int | None = None, max_height: int | None = None) -> dict:
    """A cell is text or cell() (icons before it). widths: px a column (0: the rest); selectable: clicking a row sends
    `select-row` (API 4). With API 3 columns are 150 px each, so long cells are cut shorter here."""
    texts, marks = _cells(rows[: HOST.limits["cells"] // max(1, len(columns))], len(columns))
    w = {"type": "table", "id": id_, "columns": columns, "rows": texts}
    if HOST.api >= 4:
        w.update({k: v for k, v in (("widths", widths), ("selectable", selectable or None),
                                    ("selected", selected if selectable and selected is not None and selected < len(texts) else None),
                                    ("max_height", max_height and max(120, min(1200, max_height))),
                                    ("marks", marks or None)) if v is not None})
    else:
        w["rows"] = [[trunc(c, 70) for c in r] for r in texts]
    return w


def checklist(id_: str, columns: list[str], items: list[tuple], height: int = 280, scroll_: bool = True,
              widths: list[int] | None = None) -> dict:
    """items: (key, cells, checked, color, disabled). A table with a box and a color swatch a row (API 4: ticking
    sends `{"kind": "check-row", "index", "value"}`); with API 3 a checkbox a row (`{id_}-{key}`), its cells as one
    line, in a scroll."""
    if HOST.api >= 4:
        texts, marks = _cells([cells for _, cells, *_ in items], len(columns))
        return {"type": "table", "id": id_, "columns": columns, "rows": texts,
                "check": True, "checked": [i for i, it in enumerate(items) if it[2]],
                "disabled": [i for i, it in enumerate(items) if it[4]], "swatches": [it[3] for it in items],
                "max_height": max(120, min(1200, height)), **({"widths": widths} if widths else {}),
                **({"marks": marks} if marks else {})}
    texts, _ = _cells([cells for _, cells, *_ in items], len(columns))
    boxes = col(f"{id_}-list", *(checkbox(f"{id_}-{key}", trunc(" · ".join(c for c in line if c), 150), on, off, color)
                                 for (key, _, on, color, off), line in zip(items, texts)), gap=2)
    return scroll(id_, boxes, min(height, 26 * len(items) + 12)) if scroll_ and len(items) > 6 else col(id_, boxes)


# --- charts


def chart(id_: str, title: str, series: list[dict], *, kind: str = "line", x_label: str = "", y_label: str = "",
          x_format: str = "number", y_format: str = "number", y_scale: str = "linear", height: int = 220,
          legend: bool = True, width: int | None = None, controls: list | None = None, sync: str | None = None) -> dict | None:
    """series: {"name", "points": [(x, y) or (x, y, label)], "style": line|step|dashed|points|area, "color": palette
    index}. Non-finite values are dropped; a series left empty is too, and with none left there is no chart.
    legend=False: none under the plot (the colors are given elsewhere, a run list's swatches). width: px wanted.
    controls (API 4): up to 4 selects, buttons or checkboxes in the chart's title row; with API 3 a row above it.
    sync (API 4): charts of one group share their hover line and zoom."""
    out = []
    for s in series:
        pts = [p for p in s["points"] if finite(p[0]) and finite(p[1]) and (y_scale != "log" or p[1] > 0)]
        if pts:
            out.append({**s, "points": pts})
    if not out:
        return None
    height = max(120, min(HOST.limits["chart_height"], height))
    controls = [c for c in controls or [] if c][:4]
    if HOST.api >= 4:
        spec = {"kind": kind, "x_label": x_label, "y_label": y_label, "series": [
            {"name": s["name"], **({"style": s["style"]} if s.get("style") else {}),
             **({"color": s["color"] % 12} if s.get("color") is not None else {}),
             "points": [{"x": num(p[0]), "y": num(p[1]), **({"label": p[2]} if len(p) > 2 and p[2] else {})}
                        for p in s["points"]]} for s in out[: HOST.limits["series"]]]}
        spec.update({k: v for k, v, default in (("x_format", x_format, "number"), ("y_format", y_format, "number"),
                                                ("y_scale", y_scale, "linear")) if v != default})
        return {"type": "chart", "id": id_, "title": title, "height": height, "chart": spec, **({} if legend else {"legend": False}),
                **({"width": max(180, min(1600, width))} if width else {}), **({"controls": controls} if controls else {}),
                **({"sync": sync} if sync else {})}
    # API 3: one style a chart (the kind's), no labels, linear axes and plain numbers; colors (NextCode 0.1.4)
    title = title if HOST.chart_width else widen(title, width)
    xs = [p[0] for s in out for p in s["points"]]
    shift, scale, x_label = 0.0, 1.0, x_label
    if x_format == "date":
        shift, scale = min(xs), 86400.0
        x_label = f"days since {datetime.fromtimestamp(shift, timezone.utc):%Y-%m-%d}"
    elif x_format == "duration":
        scale = 3600.0 if max(xs) > 7200 else 60.0 if max(xs) > 120 else 1.0
        x_label = {3600.0: "hours", 60.0: "minutes", 1.0: "seconds"}[scale]
    if x_format == "number" and kind == "line":  # NextCode writes ticks as 885000.00: past 1000 they overflow the
        top = max(abs(x) for x in xs)  # chart, and it then draws the charts after it out of place
        for div, word in ((1e6, "millions"), (1e3, "thousands")):
            if top >= div:
                scale, x_label = div, f"{x_label} ({word})".strip()
                break
    ty = (lambda v: math.log10(v)) if y_scale == "log" else (lambda v: v * 100) if y_format == "percent" else (lambda v: v)
    y_label = f"log10 {y_label}".strip() if y_scale == "log" else f"{y_label} (%)".strip() if y_format == "percent" else y_label
    spec_series = []
    for s in out[: HOST.limits["series"]]:
        pts = s["points"]
        if s.get("style") == "step" and kind == "line":  # hold each value until the next point
            pts = [q for a, b in zip(pts, pts[1:] + [None]) for q in ([a] if b is None else [a, (b[0], a[1])])]
        elif s.get("style") in ("line", "dashed") and kind == "scatter" and len(pts) == 2:
            (x0, y0), (x1, y1) = pts[0][:2], pts[1][:2]  # a straight line in a scatter chart: dotted
            pts = [(x0 + (x1 - x0) * i / 23, y0 + (y1 - y0) * i / 23) for i in range(24)]
        spec_series.append({"name": s["name"], **({"color": s["color"] % 12} if s.get("color") is not None else {}),
                            "points": [{"x": num((p[0] - shift) / scale), "y": num(ty(p[1]))} for p in pts]})
    c = {"type": "chart", "id": id_, "title": title, "height": height, **({} if legend else {"legend": False}),
         **({"width": max(180, min(1600, width))} if width and HOST.chart_width else {}),
         "chart": {"kind": kind, "x_label": x_label, "y_label": y_label, "series": spec_series}}
    return col(f"{id_}-with", row(f"{id_}-tools", *controls), c, gap=4) if controls else c


def bars(id_: str, title: str, series: list[dict], *, height: int = 220, width: int | None = None, legend: bool = False,
         controls: list | None = None, y_format: str = "number") -> dict | None:
    """One bar a series ({"name", "value", "color"}), side by side; None when no value is a finite number."""
    out = [s for s in series if finite(s.get("value"))]
    if not out:
        return None
    height = max(120, min(HOST.limits["chart_height"], height))
    controls = [c for c in controls or [] if c][:4]
    spec = {"kind": "bar", "labels": [" "], "x_label": "", "y_label": "",
            "series": [{"name": s["name"], **({"color": s["color"] % 12} if s.get("color") is not None else {}),
                        "values": [num(s["value"] * (100 if y_format == "percent" and HOST.api < 4 else 1))]}
                       for s in out[: HOST.limits["series"]]]}
    if HOST.api >= 4:
        if y_format != "number":
            spec["y_format"] = y_format
        return {"type": "chart", "id": id_, "title": title, "height": height, "legend": legend, "chart": spec,
                **({"width": max(180, min(1600, width))} if width else {}), **({"controls": controls} if controls else {})}
    c = {"type": "chart", "id": id_, "height": height, "legend": legend, "chart": spec,
         **({"title": title, **({"width": max(180, min(1600, width))} if width else {})} if HOST.chart_width
            else {"title": widen(title, width)})}
    return col(f"{id_}-with", row(f"{id_}-tools", *controls), c, gap=4) if controls else c


def widen(title: str, width: int | None) -> str:
    """A chart's title made about width px wide: in API 3 a chart without a legend is as wide as its title row (the
    title, then the zoom buttons, about 150 px), so the title is padded with no-break spaces (about 3.3 px each)."""
    if not width:
        return title
    return title + " " * max(0, round((width - 150 - 7 * len(title)) / 3.3))


def thin(points: list, n: int) -> list:
    """At most n points, evenly spaced, the first and the last kept."""
    if len(points) <= n or n < 2:
        return points[: max(n, 0)] if n < 2 else points
    step = (len(points) - 1) / (n - 1)
    return [points[round(i * step)] for i in range(n)]


def ema(values: list[float], w: float) -> list[float]:
    """Smoothed like TensorBoard (and the web dashboard): debiased, so the start is not pulled toward zero."""
    m = n = 0.0
    out = []
    for v in values:
        m, n = w * m + (1 - w) * v, w * n + (1 - w)
        out.append(m / n)
    return out


# --- text


def esc(s) -> str:
    """Text from the ledger shown as itself in markdown (a hypothesis with `*` or `_` is not emphasis)."""
    return re.sub(r"([\\`*_\[\]<>#|~])", r"\\\1", " ".join(str(s or "").split()))


def tick(s) -> str:
    """Text inside an inline code span: backticks would end it."""
    return " ".join(str(s or "").split()).replace("`", "'")


def trunc(s, n: int) -> str:
    from kx.views.text import trunc as cut
    return cut(s, n)


def finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and abs(v) <= 1e12


def num(v: float) -> float | int:
    return v if isinstance(v, int) else float(f"{v:.6g}")


# --- NextCode's checks (extensions/src/panels.rs), so a panel it would refuse is never sent


def problems(root: dict) -> str | None:
    lim = HOST.limits
    ids, totals = set(), {"text": 0, "values": 0}

    def text(*parts):
        totals["text"] += sum(len(str(p).encode()) for p in parts)

    def options(w):
        opts = w["options"]
        text(w.get("label", ""), w["value"], *(o["value"] + o["label"] for o in opts))
        if not 1 <= len(opts) <= 32 or w["value"] not in {o["value"] for o in opts} or len({o["value"] for o in opts}) < len(opts):
            return f"{w['type']} {w['id']}: bad options"
        return None

    def table_(w):
        cols, rows = w["columns"], w["rows"]
        if not 1 <= len(cols) <= 16 or len(rows) * len(cols) > lim["cells"] or any(len(r) != len(cols) for r in rows):
            return f"table {w['id']} too big or ragged"
        text(*cols, *(c for r in rows for c in r))
        if w.get("widths") and (len(w["widths"]) != len(cols) or any(x and not 16 <= x <= 1200 for x in w["widths"])):
            return f"table {w['id']}: bad widths"
        if w.get("selected") is not None and not (w.get("selectable") and 0 <= w["selected"] < len(rows)):
            return f"table {w['id']}: bad selected row"
        if (w.get("checked") or w.get("disabled")) and not w.get("check"):
            return f"table {w['id']}: checked without check"
        if any(not 0 <= i < len(rows) for i in (w.get("checked") or []) + (w.get("disabled") or [])):
            return f"table {w['id']}: bad row index"
        if w.get("swatches") and len(w["swatches"]) != len(rows):
            return f"table {w['id']}: a swatch a row"
        marks = w.get("marks") or []
        if len(marks) > len(rows) or any(len(r) > len(cols) for r in marks) \
                or any(len(c) > 12 for r in marks for c in r) or sum(len(c) for r in marks for c in r) > 4096:
            return f"table {w['id']}: bad marks"
        text(*(m.get("label", "") for r in marks for c in r for m in c))
        return None

    def chart_(w):
        text(w.get("title", ""))
        spec = w["chart"]
        if not 120 <= w["height"] <= lim["chart_height"] or not 180 <= w.get("width", 180) <= 1600:
            return f"chart {w['id']}: bad size"
        if len(w.get("controls") or []) > 4 or any(c["type"] not in ("select", "button", "checkbox") for c in w.get("controls") or []):
            return f"chart {w['id']}: bad controls"
        if w.get("sync"):
            text(w["sync"])
        series = spec.get("series") or []
        if spec["kind"] != "histogram" and not 1 <= len(series) <= lim["series"]:
            return f"chart {w['id']}: {len(series)} series"
        text(spec.get("x_label", ""), spec.get("y_label", ""), *spec.get("labels", []))
        for s in series:
            text(s["name"])
            if "values" in s:  # a bar chart's
                if len(s["values"]) != len(spec["labels"]):
                    return f"chart {w['id']}: bars and labels differ"
                totals["values"] += len(s["values"])
                continue
            if not s["points"]:
                return f"chart {w['id']}: an empty series"
            text(*(p.get("label", "") for p in s["points"]))
            totals["values"] += len(s["points"])
        return None

    def walk(w, depth):
        if depth > lim["depth"]:
            return f"deeper than {lim['depth']}"
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", w["id"]) or w["id"] in ids:
            return f"bad or repeated id {w['id']!r}"
        ids.add(w["id"])
        text(w["id"])
        t = w["type"]
        problem = None
        if t in ("label", "markdown", "button"):
            text(w["text"])
        elif t == "section":
            text(w["title"])
        elif t == "stat":
            text(w["label"], w["value"], w.get("detail", ""))
        elif t == "text-input":
            text(w.get("label", ""), w.get("value", ""), w.get("placeholder", ""))
        elif t in ("checkbox", "progress"):
            text(w.get("label", ""))
        elif t in ("select", "tabs"):
            problem = options(w)
        elif t == "table":
            problem = table_(w)
        elif t == "chart":
            problem = chart_(w)
        if problem:
            return problem
        kids = w.get("children") or ([w["child"]] if "child" in w else []) or w.get("controls") or []
        return next((p for c in kids if (p := walk(c, depth + 1))), None)

    problem = walk(root, 1)
    if problem:
        return problem
    if len(ids) > lim["widgets"]:
        return f"{len(ids)} widgets"
    if totals["values"] > lim["values"]:
        return f"{totals['values']} chart values"
    if totals["text"] > lim["text"]:
        return f"{totals['text']} bytes of text"
    return None
