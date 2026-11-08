# Request 3 to NextCode: one panel with tabs, and a training workspace like wandb's

**Done: NextCode 0.1.6** (extension API 4, NextCode PR #14; the panes of a sidebar root, PR #15), and kx
uses all of it. Kept as the record of why; with API 3 hosts kx still draws the workarounds below.

kx's NextCode extension is now one panel, **Dashboard**, with the web dashboard's pages as tabs (Overview, Runs,
Training, Tasks, Findings, Submissions). Its Training tab works like a wandb workspace: runs and filters on the left
(ticked runs are the ones shown), and on the right the charts side by side, one curve each, with the y axis picked
above each chart. With API 3 kx gets there through workarounds, checked on screen with NextCode's `--automation`
mode:

- The sidebar and the charts are two columns of a row. The row aligns their bottoms, so kx estimates both columns'
  heights and pads the shorter one with a label of empty lines.
- A chart is about 320 px wide, so kx lays charts out in rows of 2–4.
- Legends are shortened to fit.
- Large x values are scaled down so the ticks fit.
- A short last row of charts goes under the sidebar.
- A run keeps its color only because kx adds a one-point placeholder series for a run that lacks a curve.

The section "Layout bugs found" below lists what forced these workarounds. Fixing those bugs, plus 1–3 here, would
remove all of the workarounds.

It builds on [REQUEST-2.md](REQUEST-2.md). From that one, in order of need: **1** (a fixed `color` a series, so a
run keeps its color in every chart), **4** (the grid) and **3** (the limits). **6** (opening another panel) is no
longer needed, because the pages are tabs of one panel now. The rest of REQUEST-2 is still wanted.

kx already draws 1–3 the way they are described here when `API = 4` (`src/kx/views/nextcode/widgets.py`), as it
does for REQUEST-2. 4 and 8 need nothing from kx, and kx will use 5–7 once they exist.

## 1. Tabs

```json
{"type": "tabs", "id": "nav", "value": "training",
 "options": [{"value": "overview", "label": "Overview"}, {"value": "training", "label": "Training"}]}
```

A tab strip with the current tab marked. Clicking a tab sends `{"kind": "change", "value": "runs"}`, as a select
does. The tabs don't hold children: the extension draws the page under them. Today the tabs are buttons, and the
open tab is bold text in their place.

## 2. A sidebar: `width` and `grow` on columns in a row

```json
{"type": "row", "id": "workspace", "gap": 16, "children": [
  {"type": "column", "id": "runs", "width": 340, "children": [...]},
  {"type": "column", "id": "charts", "grow": true, "children": [{"type": "grid", ...}]}]}
```

- `width`: a fixed width in px (120–1200).
- `grow`: the column takes the width the row has left, and the row stops wrapping it under the others.
- A chart or grid inside such a column fills the column, not the whole editor.

wandb's layout is the run list on the left and the charts filling the rest. Today a chart is always full width, so
in a row it wraps onto its own line.

## 3. A table with ticks and color swatches

```json
{"type": "table", "id": "pick", "columns": ["", "run", "status", "score", "hypothesis"],
 "widths": [24, 64, 80, 120, 0], "rows": [...],
 "check": true, "checked": [0, 1], "disabled": [5, 6], "swatches": [0, 1, null, null, null, null, null],
 "max_height": 300}
```

- `check`: a checkbox at the start of every row. Ticking or unticking a row sends
  `{"kind": "check-row", "index": 3, "value": true}`.
- `checked`: the rows that are ticked. `disabled`: rows that can't be ticked (here, because six are already).
- `swatches`: one entry a row, a palette index (the colors of REQUEST-2 #1) or `null`. It is drawn as a small color
  square next to the box, so the list doubles as the charts' legend.
- `widths` and `max_height` work as in REQUEST-2 #5.

Today each run is a checkbox with all of its columns run together into one label, and nothing ties it to its color
in the charts.

## 4. Hover shows every series at that x

When the pointer is over a line chart, show a vertical line at the nearest x, and a tooltip with each series' name,
color and y at that x (its nearest point), sorted by y. Today hover shows the single nearest point
(`name · #index · x= y=`), so finding which run is which takes a click per line.

Optional: charts with the same `"sync": "<group>"` share that hover line and their zoom.

## 5. Controls in a chart's title bar

```json
{"type": "chart", "id": "chart-0", "title": "val/loss", "height": 220, "chart": {...},
 "controls": [{"type": "select", "id": "y-0", "label": "", "value": "val/loss", "options": [...], "disabled": false},
              {"type": "button", "id": "del-0", "text": "×", "disabled": false}]}
```

Up to 4 small selects, buttons or checkboxes, drawn in the chart's title row next to its zoom buttons. They send
the usual events and count as widgets of the panel. This is the y axis picker of a chart. Today it is a row above
the chart that takes as much room as the title does.

## 6. Collapsible sections

```json
{"type": "section", "id": "sec-val", "title": "val · 8 charts", "open": true, "children": [...]}
```

Clicking the title sends `{"kind": "change", "value": false}`, and the extension leaves out the children of a
closed section. In wandb, charts are grouped by prefix (train/, val/) into sections the user folds away.

## 7. Limits

On top of REQUEST-2 #3:

- 256 widgets a panel (it is 128). Twelve charts, each with its picker, plus a run list of sixteen rows, plus the
  tabs, already come to 113.
- 64 KiB of panel state (it is 16 KiB), now that one panel keeps the state of six pages.
- 1 MiB of process output when the process's response is `"response": "stdout"` (it is 256 KiB), the same as the
  response limit.

## 8. Keep the scroll position when the root is replaced

Ticking a run or changing a chart's y axis replaces the whole root. If the editor tab's scroll offset is not kept
already (I couldn't check this on screen), it should be, so that the view doesn't jump back to the top under the
pointer. Widget ids are stable, so the scroll can be kept by the id of the scroll container.

## Layout bugs found (API 3, NextCode 0.1.3, checked with `--automation` and Pinpoint bounds)

1. **Text in a column inside a row is measured at no width.** A `markdown` or `label` widget, or a `text-input`'s
   `label`, inside a `column` that is a child of a `row` makes that column far taller than what it shows. Each word
   counts as a line: one text input labelled "Find runs" adds 181 px, and one sidebar measured 2077 px for 967 px of
   widgets. The row then aligns the next column to that phantom bottom. Labels of selects and checkboxes are fine.
2. **A chart's width is its content's.** In a row, `w_full` doesn't apply, so a chart is as wide as its hint line
   (about 320 px) or its legend on one line, whichever is wider. The legend never wraps, so six run names make a
   chart about 700 px wide.
3. **X ticks overflow and break the layout.** Ticks are written as `885000.00`. When the five ticks are wider than
   the plot, that chart and the widgets after it in the same row or column are laid out outside their own boxes:
   Pinpoint puts a select's field 140 px below the select, and a plot outside its chart. Repro: a row of three
   charts in a column, x from 0 to 885000.
4. **A short row beside a sidebar is laid out wrong.** In a column that is the second child of a row, a row of
   two chart cells after rows of three is drawn out of its boxes, the same as in 3. The same JSON outside the outer
   row is fine. A chart alone in a row also gets a cramped plot (150 of 320 px).
5. **Empty labels still take a line.** A select or text input with `"label": ""` still reserves the label's line.
6. **A bar chart with one label repeats it.** Ticks are placed at fractional x, so the one label is shown under
   every tick.
7. **Emoji are drawn in one color.** `🟦` in a checkbox label is grey, so it can't serve as a run's color swatch.
