"""The video's look and building blocks: text helpers, colors, the voice-timed scene, and the visuals kx explanations
use (a run lineage, scores with their error bars, training curves). This copy is the video's own: change anything.

Text goes through Pango (Text), not LaTeX: Tex and MathTex need a LaTeX install, and so do Manim's own axis numbers
(DecimalNumber), which is why the axes here write their ticks themselves. Use MathTex only for formulas.
"""
import json
import math
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from manim import *

from voice_util import VOICE_DIR, clip_hash, estimate, load_manifest

HERE = Path(__file__).parent
DATA = json.loads((HERE / "data.json").read_text())

FONT, MONO = "DejaVu Sans", "DejaVu Sans Mono"
C_TEXT, C_DIM = GREY_A, GREY_C
C_RUN, C_BEST, C_GOOD, C_BAD = BLUE_C, YELLOW_C, GREEN_C, RED_C
SERIES = [BLUE_C, ORANGE, GREEN_C, PURPLE_B, TEAL_C, PINK, GOLD_C, MAROON_B]

LABELS = {}  # run id -> the label to show for it; prep_data.py's are cut from the hypothesis, often too long


# ----------------------------------------------------------------------------- text

def text(s, scale=0.5, color=C_TEXT, weight=NORMAL, max_width=13.0, font=FONT):
    """One block of text, shrunk to `max_width` when wider; "\\n" breaks lines."""
    t = Text(str(s), font=font, color=color, weight=weight, line_spacing=0.8).scale(scale)
    if t.width > max_width:
        t.scale_to_fit_width(max_width)
    return t


def wrap(s, chars=24, lines=2):
    """Words into at most `lines` lines of about `chars` characters ("…" when cut)."""
    out, cur = [], ""
    for w in str(s).split():
        if cur and len(cur) + 1 + len(w) > chars:
            out.append(cur)
            cur = w
            if len(out) == lines:
                return "\n".join(out[:-1] + [out[-1].rstrip(".,;:") + "…"])
        else:
            cur = f"{cur} {w}".strip()
    return "\n".join(out + ([cur] if cur else []))


def title(s, scale=0.7):
    t = text(s, scale, WHITE, BOLD).to_edge(UP, buff=0.4)
    rule = Line(LEFT, RIGHT, stroke_width=1.5, color=GREY_B).set_width(t.width + 1.0).next_to(t, DOWN, buff=0.15)
    return VGroup(t, rule)


def caption(s, scale=0.42, color=C_TEXT):
    return text(s, scale, color).to_edge(DOWN, buff=0.35)


def mono(s, scale=0.34, color=WHITE):
    return text(s, scale, color, font=MONO)


def fmt(v, digits=3):
    """A number the way a reader takes it in: 0.803, 1.2K, 3.4M."""
    if v is None:
        return "–"
    a = abs(v)
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e4)):
        if a >= size:
            return f"{v / (size if unit != 'K' else 1e3):.{digits}g}{unit}"
    return f"{v:.{digits}g}"


def score_str(value, se=None):
    return "no score" if value is None else fmt(value) + (f" ± {se:.2g}" if se else "")


def label(run):
    return LABELS.get(run["run"], run.get("label") or run["run"])


def short_id(run_id):
    return run_id.split("-")[0]  # r0788-silver-trail -> r0788


def passes(points):
    """A curve's points split where the step goes back: a resumed run or a sweep logged again starts a new pass."""
    out = [[]]
    for p in points:
        if out[-1] and p[0] < out[-1][-1][0]:
            out.append([])
        out[-1].append(p)
    return [q for q in out if q]


def nice_ticks(lo, hi, n=4, cover=False):
    """Round ticks across lo..hi; with `cover`, from the tick at or below lo to the one at or above hi."""
    if hi == lo:
        lo, hi = lo - 1, hi + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = (math.floor if cover else math.ceil)(lo / step - 1e-9) * step
    last = math.ceil(hi / step - 1e-9) * step if cover else hi
    return [round(first + i * step, 12) for i in range(int((last - first) / step + 1e-9) + 1)]


# ----------------------------------------------------------------------------- narration

_MANIFEST = load_manifest()
STRICT = os.environ.get("KX_VIDEO_STRICT") == "1"  # the final render: every clip must exist and be current
_told = []


class VoiceScene(Scene):
    """A scene timed by its narration: `with self.voice(key): ...` plays the clip and, when the block's animations end
    early, holds until it is over (plus `pad`). Without the clip yet (no gen_voice.py), a silent draft is timed from
    the words of the line, so the layout can be checked before any voice exists."""

    def voice_duration(self, key):
        info = _MANIFEST.get(key)
        if info is not None and info["hash"] == clip_hash(key) and (VOICE_DIR / f"{key}.wav").exists():
            return info["duration"], VOICE_DIR / f"{key}.wav"
        if STRICT:
            raise RuntimeError(f"narration clip '{key}' missing or stale: run gen_voice.py")
        if not _told:
            _told.append(key)
            print("draft: clips missing or stale, timed from their words and silent (gen_voice.py makes them)",
                  file=sys.stderr)
        return estimate(key), None

    @contextmanager
    def voice(self, key, pad=0.35):
        dur, wav = self.voice_duration(key)
        if wav is not None:
            self.add_sound(str(wav))
        t0 = self.renderer.time
        yield dur
        rest = dur + pad - (self.renderer.time - t0)
        if rest > 1 / 30:
            self.wait(rest)


# ----------------------------------------------------------------------------- kx visuals

class Lineage(VGroup):
    """Runs oldest first, as boxes joined by arrows, `per_row` to a row; rows snake (the second runs right to left),
    so the step from one row to the next is a short arrow down. A box: the run's short id, its label, its score;
    failed runs red, `highlight` yellow. `.boxes[i]` and `.arrows[i]` (from box i to box i+1) animate it step by step."""

    def __init__(self, runs, highlight=None, per_row=4, box_width=2.9, gap=0.55, max_width=13.0, **kwargs):
        super().__init__(**kwargs)
        self.boxes, self.arrows = VGroup(), VGroup()
        contents = []
        for r in runs:
            color = C_BAD if r.get("status") == "failed" else C_BEST if r["run"] == highlight else C_RUN
            score = "failed" if r.get("status") == "failed" else score_str(r.get("score"), r.get("se"))
            contents.append((color, VGroup(mono(short_id(r["run"]), 0.34, color),
                                           text(wrap(label(r), 24, 2), 0.27, C_TEXT, max_width=box_width - 0.3),
                                           text(score, 0.27, color)).arrange(DOWN, buff=0.12)))
        height = max(c.height for _, c in contents) + 0.35
        for color, content in contents:
            box = RoundedRectangle(width=box_width, height=height, corner_radius=0.12, stroke_color=color,
                                   stroke_width=2.5, fill_color=color, fill_opacity=0.08)
            self.boxes.add(VGroup(box, content.move_to(box)))
        step = box_width + gap
        for i, b in enumerate(self.boxes):
            row, col = divmod(i, per_row)
            col = col if row % 2 == 0 else per_row - 1 - col
            b.move_to([col * step, -row * (height + gap * 1.2), 0])
        rows = VGroup(*self.boxes)
        for i in range(len(self.boxes) - 1):
            a, b = self.boxes[i], self.boxes[i + 1]
            if abs(a.get_y() - b.get_y()) < 1e-3:
                start, end = (a.get_right(), b.get_left()) if b.get_x() > a.get_x() else (a.get_left(), b.get_right())
            else:
                start, end = a.get_bottom(), b.get_top()
            self.arrows.add(Arrow(start, end, buff=0.06, stroke_width=3, color=GREY_B, tip_length=0.16,
                                  max_tip_length_to_length_ratio=0.5))
        self.add(rows, self.arrows)
        if self.width > max_width:
            self.scale_to_fit_width(max_width)


class ScorePlot(VGroup):
    """Scores as dots with their ± se whiskers on one axis, a row per item: honest about overlap, with no bar that
    starts at an arbitrary floor. items: [{"name", "value", "se", "color"}]."""

    def __init__(self, items, width=9.0, row=1.0, **kwargs):
        super().__init__(**kwargs)
        items = [it for it in items if it.get("value") is not None]
        los = [it["value"] - 2 * (it.get("se") or 0) for it in items]
        his = [it["value"] + 2 * (it.get("se") or 0) for it in items]
        lo, hi = min(los), max(his)
        pad = (hi - lo) * 0.05 or abs(hi) * 0.05 or 1.0
        ticks = nice_ticks(lo - pad, hi + pad, n=5, cover=True)  # the axis spans every whisker
        lo, hi = ticks[0], ticks[-1]
        X = lambda v: (v - lo) / (hi - lo) * width - width / 2  # noqa: E731
        axis = Line([X(lo), 0, 0], [X(hi), 0, 0], color=GREY_B, stroke_width=2)
        tick_marks = VGroup(*[VGroup(Line([X(t), 0.07, 0], [X(t), -0.07, 0], color=GREY_B),
                                     text(fmt(t), 0.24, C_DIM).move_to([X(t), -0.3, 0])) for t in ticks])
        self.rows = VGroup()
        for k, it in enumerate(items):
            y = (len(items) - k) * row
            color, v, se = it.get("color", C_RUN), it["value"], it.get("se") or 0
            whisker = VGroup(Line([X(v - se), y, 0], [X(v + se), y, 0], color=color, stroke_width=4),
                             *[Line([X(e), y - 0.12, 0], [X(e), y + 0.12, 0], color=color, stroke_width=3)
                               for e in (v - se, v + se)]) if se else VGroup()
            dot = Dot([X(v), y, 0], radius=0.09, color=color)
            name = text(it["name"], 0.3, color).next_to([X(lo), y, 0], LEFT, buff=0.35)
            value = text(score_str(v, se), 0.26, color).next_to(dot, UP, buff=0.18)
            self.rows.add(VGroup(name, whisker, dot, value))
        self.axis = VGroup(axis, tick_marks)
        self.add(self.axis, self.rows)
        self.move_to(ORIGIN)


class CurvePlot(VGroup):
    """Training curves on one pair of axes. series: [(name, [[x, y], ...], color)]. A curve whose steps go back is
    drawn as one line per pass (pass passes(pts)[-1] for the latest only). `.lines[name]` is each curve, to draw with
    Create; ticks are Text (no LaTeX)."""

    def __init__(self, series, x_label="step", y_label="", width=9.5, height=4.4, **kwargs):
        super().__init__(**kwargs)
        xs = [p[0] for _, pts, _ in series for p in pts]
        ys = [p[1] for _, pts, _ in series for p in pts]
        xt, yt = nice_ticks(min(xs), max(xs)), nice_ticks(min(ys), max(ys), cover=True)
        x0, x1 = min(xt[0], min(xs)), max(xt[-1], max(xs))
        y0, y1 = min(yt[0], min(ys)), max(yt[-1], max(ys))
        self.axes = Axes(x_range=[x0, x1, (x1 - x0) / 4], y_range=[y0, y1, (y1 - y0) / 4], x_length=width,
                         y_length=height, tips=False,
                         axis_config={"include_numbers": False, "include_ticks": False, "color": GREY_B,
                                      "stroke_width": 2})
        ax = self.axes
        labels = VGroup(*[text(fmt(t), 0.24, C_DIM).next_to(ax.c2p(t, y0), DOWN, buff=0.15) for t in xt if x0 <= t <= x1],
                        *[text(fmt(t), 0.24, C_DIM).next_to(ax.c2p(x0, t), LEFT, buff=0.15) for t in yt if y0 <= t <= y1])
        grid = VGroup(*[DashedLine(ax.c2p(x0, t), ax.c2p(x1, t), color=GREY_D, stroke_width=1, dash_length=0.06)
                        for t in yt if y0 < t <= y1])
        names = VGroup(*([text(x_label, 0.26, C_DIM).next_to(ax.c2p(x1, y0), DOWN, buff=0.45).align_to(ax, RIGHT)] if x_label else []),
                       *([text(y_label, 0.26, C_DIM).next_to(ax, UP, buff=0.15).align_to(ax, LEFT)] if y_label else []))
        self.lines = {}
        legend = VGroup()
        for name, pts, color in series:
            line = VGroup(*[VMobject(stroke_color=color, stroke_width=3).set_points_as_corners([ax.c2p(x, y) for x, y in q])
                            for q in passes(pts) if len(q) > 1])
            self.lines[name] = line
            legend.add(VGroup(Line(ORIGIN, 0.35 * RIGHT, color=color, stroke_width=4), text(name, 0.26, color))
                       .arrange(RIGHT, buff=0.12))
        legend.arrange(DOWN, aligned_edge=LEFT, buff=0.1).next_to(ax, UP, buff=0.2).align_to(ax, RIGHT)
        self.add(grid, ax, labels, names, legend, *self.lines.values())
        self.legend = legend
