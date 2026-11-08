# Request 4 to NextCode: compact charts and controls

**Done: NextCode 0.1.4** (as optional API 3 fields; kx uses them all).

Small changes for the kx Training tab, which shows a dozen charts beside a run list. Each one removes something
that kx can't avoid drawing with API 3. Each item is a few lines in `crates/ui/src/extension_views.rs` and
`extension_views/charts.rs`. If you implement only part of REQUESTS 2–4, start with these items, in this order.

## 1. Charts without a legend

```json
{"type": "chart", "id": "c", "title": "val/loss", "height": 140, "legend": false, "chart": {...}}
```

`legend` defaults to `true`. With `false`, the row of color squares under the plot isn't drawn. kx gives the runs'
colors once, in the run list (2), and today names the series `1`…`6` only to keep a legend that can't be turned
off on one line.

Also drop the hint line "Focus chart: +/− zoom, 0 resets; click a point or bar to select" under every chart (or
show it once in the panel, or as a tooltip of the zoom buttons). Twelve charts repeat it twelve times.

## 2. A color swatch on a checkbox

```json
{"type": "checkbox", "id": "pick-r0958", "label": "r0958 · 2d23h · T88 short matched…", "value": true, "disabled": false,
 "color": 0}
```

`color` is a palette index: the charts' color for that series index (and REQUEST-2's `color`). It is drawn as a
small square between the box and the label. Then the run list is the legend: a run's line has its color. Color emoji
can't stand in, because they render in one color.

## 3. No label, no label line

A `select` or `text-input` with `"label": ""` still reserves the label's line (about 25 px), so a row of selects is
twice as tall as their fields. With an empty label, draw only the field. Better still, allow a label inside the
field, as a prefix:

```json
{"type": "select", "id": "x", "label": "X axis", "label_inline": true, ...}
```

which is drawn as `X axis: step ▾` in one field-high line.

## 4. No title line above an editor panel

An extension panel in an editor tab shows its manifest title ("Dashboard") on a line above the root, although the
tab already says it. Leave it out for `"placement": "editor"`, or accept `"header": false` on the manifest's panel.

## Also needed: series colors

kx keeps a run's color across charts by adding a one-point series for a run that lacks a curve, hidden under another
series' first point, because API 3 colors a series by its place. REQUEST-2 #1 (`color` on a series) removes the
need for that placeholder.
