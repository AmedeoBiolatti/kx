---
name: kx-catch-up
description: Tell the person what happened in this competition since a time (default the last 24 hours, or since they last asked) - new best runs, failures, findings, tasks finished or stuck, submissions and their scores, workers and other machines - ending with what needs their decision. Gathers it from kx (--json). Use when asked what happened, where things stand, to catch them up, or at the start of a session after a break.
---

<!-- Written by kx init, which rewrites this folder: put your own skills in other folders. -->

# Catching the person up

One screen they can read in a minute, most important first, every claim checkable in kx.

## 1. Since when

A time the person gave ("since Friday", "since the last submission"), else since they last asked for a catch-up in
this conversation, else the last 24 hours. Say which you used.

## 2. Bring in the other machines' work

If this machine shares its ledger (`share:` in `~/.config/kx/machine.yaml`), run `kx sync --ledger` first: it sends
and brings the logs, nothing else. A worker (`KX_AGENT` set) cannot: say the summary covers this machine's view.

## 3. Gather (keep what is newer than the time)

| what | run | fields |
|---|---|---|
| what needs the person now | `kx status --json` | `needs_you[]`: `what`, `fix`; `running` |
| runs started or finished | `kx runs -n 500 --json` | `created`, `finished`, `status`, `author`, `host`, `task`, `hypothesis`, `score` |
| did the best change | `kx board --json` | rows whose run started after the time; `vs_best.verdict` |
| why a run failed | `kx show R --json` | `error` (its first line is usually enough) |
| what was learned | `kx finding list --all --json` | `ts`, `text`, `evidence`, `status` |
| tasks | `kx task list --all --json` | `updated`, `status`, `result`, `outcome`, `last_note`, `waiting_on` |
| submissions | `kx subs --json` | `created`, `updated`, `status`, `public`, `run_id`, `error` |
| workers | `kx workers --all --json` | `running[]`, `recent[]`: what each did and how it ended |
| code | `git log --since=TIME --oneline` | commits, by branch |

## 4. Write it

1. **Headline**: did the best change (from what to what, under which protocol, the `kx compare` verdict), and the
   one other thing that matters most (a submission's score, a stuck worker, a budget nearly spent).
2. **What ran**: counts by outcome; the failures that matter with the first line of their error; the runs that
   moved a score.
3. **What was learned**: new findings in one line each, with their evidence runs; withdrawn ones too.
4. **Tasks**: finished (with their result), started, waiting (on what).
5. **Submissions**: sent, scored, slots left today.
6. **Needs you**: each item of `needs_you` with the exact command that resolves it, and any decision the above
   raises (a promising run to submit, a direction to stop).

When much happened, add one diagram: what ran by task and day, as a Mermaid `gantt` or a small `flowchart` (patterns
in the kx-explain skill's `diagrams.md`). Full ids everywhere (`r0012-fluent-sun`). Leave out what did not change.
For more on any item, the person can ask for an explanation (skill kx-explain).
