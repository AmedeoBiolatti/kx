"""The storyboard as scenes, one idea each, in order: render_all.py renders every `class S<n>_<Name>`. These four are a
starting point built on data.json (the question, the lineage, the score, the curves): keep what serves the story,
rewrite the rest. Every number shown comes from DATA.
"""
from common import *  # noqa: F403

C, BEST, BASE, CMP = DATA["competition"], DATA["best"], DATA.get("baseline"), DATA.get("comparison")


class S1_Question(VoiceScene):
    def construct(self):
        head = title(C["title"])
        what = text(f"{C['metric'].replace('_', ' ')} · {C['direction']}imise · {C['kind']}", 0.4, C_DIM).next_to(head, DOWN, buff=0.4)
        with self.voice("q_task"):
            self.play(FadeIn(head, shift=0.3 * DOWN), run_time=1.2)
            self.play(Write(what), run_time=1.2)
        question = text("Where do we stand, and how did we get here?", 0.6, WHITE)
        with self.voice("q_question"):
            self.play(FadeIn(question, shift=0.2 * UP), run_time=1.2)


class S2_Lineage(VoiceScene):
    def construct(self):
        head = title("How we got here")
        self.add(head)
        lin = Lineage(DATA["lineage"], highlight=BEST["run"])
        room = head.get_bottom()[1] + 3.7 - 0.6  # from under the title to the bottom edge of the frame
        if lin.height > room:
            lin.scale_to_fit_height(room)
        lin.next_to(head, DOWN, buff=0.6)
        with self.voice("lin_start"):
            self.play(FadeIn(lin.boxes[0], scale=0.9), run_time=1.0)
        with self.voice("lin_steps") as dur:
            steps = [AnimationGroup(GrowArrow(a), FadeIn(b, shift=0.2 * RIGHT)) for a, b in zip(lin.arrows, lin.boxes[1:])]
            if steps:
                self.play(LaggedStart(*steps, lag_ratio=0.6), run_time=max(1.0, dur - 0.5))
        with self.voice("lin_best"):
            self.play(Indicate(lin.boxes[-1], color=C_BEST, scale_factor=1.06), run_time=1.5)


class S3_Score(VoiceScene):
    def construct(self):
        head = title("How good is it")
        self.add(head)
        items = [{"name": short_id(r["run"]), "value": r["score"], "se": r["se"], "color": color}
                 for r, color in ((BASE, C_DIM), (BEST, C_BEST)) if r and r["score"] is not None]
        with self.voice("score_compare"):
            if items:
                plot = ScorePlot(items).next_to(head, DOWN, buff=1.0)
                self.play(Create(plot.axis), run_time=0.8)
                self.play(LaggedStart(*[FadeIn(row) for row in plot.rows], lag_ratio=0.5), run_time=1.6)
            else:
                self.play(Write(text(score_str(BEST["score"], BEST["se"]), 0.9, C_BEST)), run_time=1.2)
        verdict = CMP["verdict"] if CMP else "not compared"
        color = {"better": C_GOOD, "worse": C_BAD}.get(verdict, C_DIM)
        note = (f"{verdict}: {CMP['method']}, z {CMP['z']:.1f}" if CMP and CMP.get("z") is not None else verdict)
        with self.voice("score_verdict"):
            self.play(FadeIn(caption(note, color=color), shift=0.2 * UP), run_time=1.0)


class S4_Curves(VoiceScene):
    def construct(self):
        head = title("How training went")
        self.add(head)
        runs = [(r, c) for r, c in ((BASE, C_DIM), (BEST, C_BEST)) if r and DATA["curves"].get(r["run"])]
        metric = next(iter(DATA["curves"][runs[-1][0]["run"]]), None) if runs else None
        if metric is None:
            with self.voice("curves_show"):
                self.play(Write(text("These runs log no curves (r.step in kx.run).", 0.45)), run_time=1.5)
            return
        series = [(short_id(r["run"]), passes(DATA["curves"][r["run"]][metric])[-1], c)  # its latest pass
                  for r, c in runs if metric in DATA["curves"][r["run"]]]
        plot = CurvePlot(series, x_label="step", y_label=metric).next_to(head, DOWN, buff=0.5)
        with self.voice("curves_show"):
            self.play(FadeIn(VGroup(*[m for m in plot if m not in plot.lines.values()])), run_time=1.0)
            self.play(*[Create(line) for line in plot.lines.values()], run_time=2.5)
        with self.voice("curves_read"):
            self.play(Indicate(plot.legend), run_time=1.2)
