"""The three ways a competition can be scored (comp.yaml `mode`), and what each means for the tools.

`kind` says how you submit (file / code / simulation). `mode` says what a score is:

  predict   the public score is a sample of the private one, from the same distribution. Local CV is the
            instrument, the leaderboard a few noisy probes; the risk is overfitting the public part.
  optimize  public == private: a fixed set of problems, the metric is exact. A run's score is deterministic and
            the leaderboard is an oracle; the work is per-problem (instances), not statistics.
  rl        a rating (ELO-like) against a changing field. A submission's score moves while it plays; local
            evaluation is a gauntlet against opponents; the leaderboard is a trajectory, not a number.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Mode:
    name: str
    public: str  # what kaggle:public means
    private: str  # what kaggle:private means
    guidance: tuple[str, ...]  # shown in kx context


PREDICT = Mode(
    "predict",
    public="Kaggle public leaderboard (a sample of the test set)",
    private="Kaggle private leaderboard (the rest of the test set, revealed at the end)",
    guidance=(
        "Trust local CV over the public leaderboard: public is a small noisy sample, and chasing it overfits.",
        "Submissions are scarce feedback: submit to calibrate CV against the public score, not to search; keep diverse "
        "strong runs, the final ones are chosen for the private score.",
        "A gap between CV and public larger than CV noise means leakage, shift or a metric mismatch: find out why.",
    ),
)
OPTIMIZE = Mode(
    "optimize",
    public="Kaggle leaderboard (the exact metric on the fixed problem set; same as private)",
    private="Kaggle private score (identical to public in this competition)",
    guidance=(
        "Scores are exact: a strictly better value is better, there is no noise to average out.",
        "Evaluate locally on the real problem set; the leaderboard is the same number, so submit only for a check.",
        "Work per problem: record per-instance scores (breakdown), find which problems are lost, specialise there.",
    ),
)
RL = Mode(
    "rl",
    public="Kaggle rating of the submission (moves with every episode it plays)",
    private="Kaggle rating at the end of the competition",
    guidance=(
        "A submission's rating keeps moving: wait for enough games before judging it, and read them (kx subs S12): "
        "losses against specific opponents say more than the rating.",
        "Compare agents by head-to-head win rate against a pool of earlier agents (a gauntlet), not by rating.",
        "Only the last few submissions play (submission.active): a new one retires the oldest. Let it play "
        "min_episodes games first (kx submit refuses until then unless --early; kx subs shows the ladder).",
    ),
)
BY_NAME = {m.name: m for m in (PREDICT, OPTIMIZE, RL)}


def get(name: str) -> Mode:
    return BY_NAME[name]


def infer(evaluation: str, kind: str) -> tuple[str, str]:
    """(mode, why) from the competition's evaluation text. Always printed for review, so keep it honest."""
    import re
    t = evaluation or ""
    if kind == "simulation" or (re.search(r"\bepisodes?\b", t, re.I) and re.search(r"rating|skill|elo", t, re.I)):
        return "rl", "Evaluation page talks about episodes and ratings"
    if re.search(r"public and private (?:leaderboards?|scores?) (?:are|will be) (?:the same|identical)"
                 r"|same (?:problems|instances|inputs) (?:for|on) (?:both )?(?:public|the public)"
                 r"|no (?:hidden|private) (?:test|data|set)|fixed set of (?:problems|instances)", t, re.I):
        return "optimize", "Evaluation page says the public and private problem sets are the same"
    return "predict", "default: a hidden test set split into public and private parts"
