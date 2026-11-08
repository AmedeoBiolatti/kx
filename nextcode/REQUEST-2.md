# Request 2 to NextCode: charts, tables and panels that read like a dashboard

**Done: NextCode 0.1.6** (extension API 4, NextCode PR #14), and kx uses all of it, except #6 (opening
another panel), which kx no longer needs since its pages are tabs of one panel. Kept as the record of why.

For the kx panels (Overview, Runs, Training, Tasks, Findings, Submissions), and any extension that shows data. They
work on API 3; these are what keeps them from reading as well as kx's web dashboard. None is specific to kx. In order
of how much they change.

## 1. Series styles, point labels and fixed colors

A line or scatter chart where each series says how it is drawn:

```json
{"kind": "line", "x_label": "run", "y_label": "auc", "series": [
  {"name": "best so far", "style": "step", "points": [{"x": 3, "y": 0.81}, {"x": 9, "y": 0.83}]},
  {"name": "each run", "style": "points", "color": 1,
   "points": [{"x": 3, "y": 0.81, "label": "r0003 · lgbm baseline"}, {"x": 4, "y": 0.79, "label": "r0004 · dart"}]},
  {"name": "bronze", "style": "dashed", "points": [{"x": 1, "y": 0.8}, {"x": 9, "y": 0.8}]}
]}
```

- `style`: `line`, `step` (holds a value until the next point's x), `dashed` or `points`. Default: `line` in a line
  chart, `points` in a scatter chart. The score over time is a step line of the best so far over a dot per run; a
  leaderboard has its medal lines dashed.
- `label` on a point: shown on hover instead of `#index` (and in the selection info). A dot is a run, and its name
  is what the person wants to know. Labels count as panel text.
- `color`: a palette index (0–11). A run keeps its color across the six charts it appears in, also when it is
  missing from some of them. Default: the series' index.

## 2. Axes

On line and scatter charts:

- `x_scale`, `y_scale`: `linear` (default) or `log` (positive values only; losses).
- `x_format`: `number` (default), `date` (x is Unix seconds: ticks and hover as dates) or `duration` (x is seconds:
  `45s`, `12m`, `3h20`; time since a run started).

## 3. Limits

- 12 series a chart (6 runs, each raw and smoothed).
- 16,384 chart values a panel (12 training curves of 6 runs at about 200 points each).
- 256 KiB of panel text (point labels: 2,000 dots of a score history at about 60 characters).

## 4. A grid

```json
{"type": "grid", "id": "curves", "min_width": 360, "gap": 12, "children": [...]}
```

As many equal columns as fit, each at least `min_width` px; children fill them in order. Charts are full width
today, so a dozen training curves are a long scroll: in a grid they are three or four rows. Also for stat tiles.

## 5. Tables: widths, one-line cells, row selection

```json
{"type": "table", "id": "runs", "columns": ["run", "status", "score", "hypothesis"], "widths": [70, 80, 90, 0],
 "selectable": true, "selected": 2, "max_height": 600, "rows": [...]}
```

- `widths`: px per column; `0` shares what is left. Columns are 150 px each today: an id is too wide, a hypothesis
  too narrow and wraps into a tall row.
- A cell longer than its column is cut to one line with `…`; hover shows it whole.
- `selectable`: clicking a row, or Enter on it, sends `{"kind": "select-row", "index": 2}`; `selected` highlights
  one. A run, a task or a finding opens from its row (today: by typing its id).
- `max_height`: 120–1200 px (today 320), for a table that is the panel's main content.

## 6. Opening another panel

```json
{"panels": [...], "open": [{"panel": "training", "args": {"runs": ["r0012", "r0015"]}}]}
```

Any response may open (or reveal) another panel the extension declares, which is invoked with
`{"kind": "open-panel", "id": "training", "args": {...}, "state": <its state, or null when it was not open>}`.
From a run in Runs, **Compare in Training** opens the Training panel with that run added.

## Not needed

Colors for table cells or badges, per-cell markdown, editable tables, more chart kinds.
