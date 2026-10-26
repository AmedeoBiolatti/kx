---
name: kx-explain
description: Explain this competition's work to the person, with diagrams. What the current solution is and how it works, how it got there, why one run beats another, what a task or finding established. Gathers the facts from kx (--json) and the code, then answers in prose with Mermaid diagrams. Use when asked to explain, summarize or walk through the solution, a run, a task or a finding, or to write or update docs/solution.md.
---

<!-- Written by kx init, which rewrites this folder: put your own skills in other folders. -->

# Explaining the work, with diagrams

kx holds the facts (runs, scores, parents, tasks, findings, submissions); the code holds how it works. Your job is
the story that connects them, told so the person can check it.

## 1. Pin down the question

Default: "what is our solution and how did we get here", for the person you work with. Other questions this answers:
how does run R work, why does A beat B, what did task T establish, what do we know about X. Ask only if it changes
what you gather.

"The current solution" is two runs, which may differ: the best under the reference protocol (`kx board --json`,
`rows[0]`) and the one behind the latest scored submission (`kx subs --json`, `submissions[].run_id`). Say which
you explain, and when they differ, why.

## 2. Gather the facts (every number from kx, never from memory)

| to know | run | fields |
|---|---|---|
| the competition, the reference protocol | `kx context --json` | `competition`, `scoring`, `reference`, `best`, `findings`, `tasks` |
| best runs, how sure | `kx board --json` | `rows[]`: `run`, `value`, `se`, `n`, `vs_best.verdict` |
| what was submitted, how it scored | `kx subs --json` | `submissions[]`: `run_id`, `public`, `private`, `status`, `created` |
| one run | `kx show R --json` | `hypothesis`, `config`, `command`, `git_sha`, `parent`, `children`, `task`, `scores[]`, `notes`, `tags`, `metrics`, `curves` |
| how it got here | `kx show` again on each `parent`, until there is none (15 steps at most) | |
| what changed from A to B | their `config` (compare the dicts), `git diff --stat A_SHA B_SHA` then the files that matter | |
| is B really better | `kx compare A B --json` | `comparisons[]`: `verdict`, `delta`, `z`, `method`, `protocol` |
| why it was tried, what was learned | `kx task show T --json`; `kx finding list --all --json`; `kx search WORDS --json` | task `body`, `notes`, `outcome`, `findings`; finding `text`, `evidence` (runs), `status` |
| how training went | `kx show R --json --curves` | `curve_points` (already cut to their shape), `curve_times` |
| the runs around it | `kx runs -n 300 --config --json` | `config`, `metrics`, `seed`, `git_sha`, `command`, `created` |

Links are often missing: many runs record no `parent` and are not linked to their task. Rebuild the story from what
is there, in this order: a finding's `evidence` (the runs a conclusion rests on), a task's `notes` and `outcome`
(they name runs and commits), `kx search` on the task id or a word of the idea (hypotheses mention them), the same
`command` or script, time order, and `git log --oneline A_SHA..B_SHA`. Draw what you inferred as inferred (dashed)
and say so.

## 3. Read how it works

From the run's `command` to its entry script, then the code it uses, at the run's commit (`git show SHA:path`, so
later edits do not mislead you). Find: what goes in (data, features), the model, how it is trained, what comes out,
and the protocol it is scored under (`src/comp/evaluate.py` or the protocol's spec in `kx protocol list --json`).
Explain at the person's level: they know machine learning, not necessarily this code. Name files with paths.

## 4. Check before you say it

- Every number comes from kx output, with its protocol and its ± se; scores under different protocols are never
  compared.
- "Better" or "worse" only from a `kx compare` verdict; "within noise" is said as such.
- Local against public: from `kx subs`, with the gap.
- Runs and findings by full id (`r0012-fluent-sun`, `F3-bold-comet`): other machines may have the same number.
- Established (findings with evidence, tasks done with a result) is told apart from believed (notes, hypotheses,
  your reading of the code).

## 5. Diagrams

Pick the one to three that answer the question; patterns and syntax are in [diagrams.md](diagrams.md).

| question | diagram |
|---|---|
| how did we get here | lineage: runs left to right, each edge says what changed, the score on each node |
| how does it work | pipeline: data → features → model → output, from the code, with file names |
| what did we learn | map: tasks → findings → the runs they rest on, dead ends marked |
| where did the time go | timeline of tasks and runs by day (when the person asks about effort) |

The score over time is already drawn by the dashboard (`kx dashboard`, Status tab): point to it instead of
redrawing it. Keep a diagram to about 15 nodes: group the rest ("8 speed runs") and say what is grouped.

## 6. Answer

1. The answer in one paragraph.
2. The diagram(s).
3. The evidence: a short table (run, what changed, score ± se under which protocol, verdict).
4. What is uncertain, and what would settle it.

Point to the dashboard for exploring: `kx dashboard`, Training tab, `#training/compare=A,B` shows two runs side
by side. Write or update `docs/solution.md` only when asked, or when it exists and has fallen behind (say so); it
is in git: leave the commit to the person. For something to share with others, offer a short video (skill
kx-video).
