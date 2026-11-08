# Request 5 to NextCode: chart chrome, width, ticks and markers; a panel that moves

**Done: NextCode 0.1.6** (extension API 4, NextCode PR #14): all six. Kept as the record of why.

REQUEST-4 is done (NextCode 0.1.4), and kx uses all of it. Four things are left on the kx Training tab's charts, all in
`crates/ui/src/extension_views/charts.rs`.

## 1. Smaller zoom buttons, at the top right

The `+`, `−` and `Reset` buttons are full-size buttons (34, 34 and 62 px wide, 26 px high) placed right after the
title. Make them small icon buttons (about 18 px: `+`, `−`, `⟲`), aligned to the top-right corner of the chart. Or
show them only while the chart is hovered or focused. On a 140 px chart the buttons are as tall as a fifth of the
plot.

## 2. A chart keeps its width without a legend

A chart used to be as wide as its hint line (about 320 px). Without the hint and the legend, a chart is as wide as
its title row, about 200 px, so its plot is cramped. kx pads chart titles with no-break spaces to get a chosen
width, which is also why the buttons now sit at the right. Either of these would replace the padding:

- `"width": 320` on a chart (px; the chart fills it), or
- REQUEST-2's `grid`, or REQUEST-3's `grow` on columns in a row, with charts filling their cell.

## 3. Tick labels on their gridlines

The y labels are five `text_xs` divs in a fixed 64 px column with `justify_between`
(`extension_views/charts.rs`, `render_chart`). So the first label's top touches the plot's top and the last one's
bottom its bottom, the middle three sit between gridlines rather than on them, and all of them are left-aligned
64 px from the axis. Instead:

- Place each label at its gridline's height (`1 - tick/4` of the plot), centered on it vertically.
- Right-align the labels against the axis, with a few px of gap, and make the column as wide as the widest label
  (or about 40 px) instead of 64.
- The same for the x labels: center each under its gridline. Today `justify_between` puts the first label's left
  edge at the axis and the last one's right edge at the end.

## 4. No markers on lines

`draw()` paints a 2.5 px square on every datum, lines included, so a 200-point curve turns into a row of beads. In
a line chart, draw a marker only:

- for an isolated point: a series with one point, or a point whose neighbours in its series are both missing or
  clipped away (zoomed out of view), so that it would otherwise not show at all;
- for the point hovered or selected (the 4 px marker, as today).

Scatter charts keep their markers. (REQUEST-2's `style: "points" | "line"` would give the extension the choice;
this default is the one that fits training curves.)

## 5. The "Running…" line moves the panel (a bug in 0.1.4)

Without its title row, an editor panel still shows a row with "Running…" while the extension runs
(`extension_views.rs`: `.when(self.show_title || self.is_busy(), …)`). That row comes and goes, so the whole
panel moves down by a line and back at every refresh and after every click. Draw the indicator so that it takes no
space: an overlay in the panel's top-right corner, or the tab's own spinner. Until then kx refreshes a page every
60 s instead of 10, unless something the page shows is running.

## 6. Round ticks, short numbers, no empty label lines

A chart puts five ticks at the quarters of its data range and writes each with two decimals: `1.00 125.75 250.50
375.25 500.00`, `11264.00 94129.50 …`. Instead:

- Put the ticks at round values in the range (1, 2 or 5 × 10ⁿ apart, about 4–6 of them), with the gridlines on
  them.
- Write the numbers without trailing zeros and with a suffix past a thousand: `0 100 200 300 400 500`,
  `25k 50k`, `1.2M`, `0.001`, `1e-6`. Hover keeps the full value.
- An empty `x_label` or `y_label` takes no line. A 180 px chart has about 102 px of chrome today (the title row,
  an empty y label line, the ticks and the x label), so its plot is 104 px.
