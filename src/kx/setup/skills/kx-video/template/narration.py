"""The script: one voice clip per key; a scene plays one with `with self.voice("key"):` and lasts as long as it, so
editing a line re-times the video (after `python gen_voice.py`; until then drafts are timed from the words).

Numbers come from data.json (prep_data.py), never typed: re-run prep_data.py and the narration follows the ledger.
Write for the ear: short sentences, one idea each, numbers rounded the way people say them. Kokoro reads
[word](/IPA/) as written, for names it gets wrong.
"""
import json
from pathlib import Path

DATA = json.loads((Path(__file__).parent / "data.json").read_text())

VOICE = "af_heart"  # Kokoro: af_heart, af_bella, am_michael, bf_emma, bm_george, ... (first letter: the language)
SPEED = 1.05


def said(x, digits=2):
    """A number as it is said: 0.64, not 0.640625."""
    return "no score" if x is None else f"{x:.{digits}g}"


C, BEST, BASE, CMP = DATA["competition"], DATA["best"], DATA.get("baseline"), DATA.get("comparison")
STEPS = len(DATA["lineage"]) - 1
VERDICT = {"better": "a real improvement", "worse": "a real loss", "within noise": "within the noise",
           "inconsistent": "not consistent from fold to fold"}

NARRATION = {
    # S1 the question
    "q_task": f"This is {C['title']}. Every entry is judged on {C['metric'].replace('_', ' ')}.",
    "q_question": "Here is where our solution stands, and how it got there.",

    # S2 the lineage
    "lin_start": "It started from a single run.",
    "lin_steps": (f"Each run after it changed one thing: {STEPS} steps in all." if STEPS > 1 else
                  "The next run changed one thing." if STEPS == 1 else "It is a run of its own, with no ancestors."),
    "lin_best": "And this is the run we have now.",

    # S3 the score
    "score_compare": ("Against the run it came from, it scores "
                      f"{said(BEST['score'])}, where that one scored {said(BASE['score'])}." if BASE and BEST["score"] is not None
                      and BASE["score"] is not None else "Its score is the one to beat."),
    "score_verdict": (f"Over the same folds, that difference is {VERDICT.get(CMP['verdict'], CMP['verdict'])}."
                      if CMP else "The two were never scored the same way, so their numbers are not compared."),

    # S4 the curves
    "curves_show": "Here is how training went for both.",
    "curves_read": "Lower is better for a loss: watch where the lines part.",
}
