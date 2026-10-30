# kx

The ledger and Kaggle interface of a competition: runs, scores, tasks, findings and submissions, shared by you, the
agent you talk to in your terminal, background workers, your other machines and the people you work with. kx
records, checks and talks to Kaggle; it never decides what to work on and never runs anything on its own.

```bash
kx status      # what is running, what needs you (with the command that fixes it), results, submissions, budgets
kx context     # what an agent reads first: its task, the reference protocol, best runs, findings, rules
kx dashboard   # the same on a local web page (http://localhost:8765)
```

Every command takes `--json`. Exit codes: `0` ok · `1` error (with a `fix:` line) · `2` usage · `3` needs
confirmation · `4` over a budget or Kaggle limit · `5` not comparable · `6` what `kx wait` waited for can no longer
happen.

## Setup

You need git, [uv](https://docs.astral.sh/uv/), the Kaggle CLI logged in (`uv tool install kaggle`, then
`kaggle auth login`), and rclone if you share a project between machines or use mailbox hosts.

```bash
git clone https://github.com/AmedeoBiolatti/kx && uv tool install --editable kx    # the kx command
mkdir <competition-slug> && cd $_       # next to kx: competition repos sit beside it
kx init                 # git repo, comp.yaml, docs, code skeleton, pyproject.toml, the ledger, data
uv sync && git add -A && git commit -m init
kx doctor               # Kaggle CLI and login, git, data, docs, the workers' harnesses
```

Each machine names itself in `~/.config/kx/machine.yaml`: `name: laptop` (default: its host name; two machines
must not share one), and `share: gdrive:kx` to share projects with your other machines and people (see
[The ledger](#the-ledger-machines-and-people)). `git pull` in the kx folder updates kx; then `kx init` in each
competition repo updates its `docs/kx.md` (the rules agents read) and its skills.

A competition repo then looks like this:

```
<competition-slug>/
├── README.md  AGENTS.md  CLAUDE.md  comp.yaml  pyproject.toml
├── docs/competition.md     the Kaggle pages (kx meta)
├── docs/kx.md              the rules for whoever works here
├── .agents/skills/         kx's skills: explain, catch up, video (.claude/skills/ links to them)
├── src/comp/evaluate.py    the evaluation protocol: the one place a score is defined
├── scripts/                entry points, run with kx exec
├── notebooks/
├── kernel/  or  agent/     what Kaggle runs (code competitions / simulations)
├── data -> .kx/store/data
└── .kx/                    git-ignored: store/ (log/, its cache, runs, artifacts, data) and worktrees/
```

Everything for a competition is in its folder: deleting it deletes everything. `store:` in comp.yaml puts the
store elsewhere (another disk). Never `git clean -x` in a competition repo: it would delete `.kx/`.

Only `kx init` starts a ledger (and `kx import`, which brings one in). On a machine that shares nothing, any other
command that finds none where comp.yaml says (the store moved, its disk is not mounted, `.kx/` was deleted) stops and
says so, with the newest backup to restore, instead of going on as a project with no history. A machine with a
`share:` goes on, saying so: `kx sync` brings the history back.

## Runs and scores

```python
import kx
PROTOCOL = kx.Protocol("cv5", metric="auc", data=kx.data_version(), folds="v1", noise="cv")

with kx.run("neutral-loss features", config=cfg, seed=0, tags=["algorithm"]) as r:
    r.progress(epoch, total=epochs, loss=loss)
    r.score(mean(fold_scores.values()), PROTOCOL, breakdown=fold_scores)
    r.artifact("submission.csv", src="submission.csv")
```

A run also has `r.step(i, **metrics)` (training curves), `r.log(**metrics)` (summary values), `r.note(text)` and
`r.log_table(name, rows)`; `kx.score(R, value, PROTOCOL)` scores an earlier run, `kx.run_dir(R)` finds its files.

Any other command: `kx exec -H "hypothesis" [--probe] [--gpus 1] -- ./train.sh` (it writes `scores.json` in
`$KX_RUN_DIR`; a tqdm bar becomes its progress). `--detach` returns once the run has started and leaves it running
in its own session (stop it with `kill PID`: it is recorded as failed); then `kx wait R`.

- Scores compare only under the same protocol id (`name@fingerprint`). One protocol is the **reference**
  (`kx protocol use NAME`); `board`, `status` and `context` rank under it.
- Experiments need committed code; `--probe` runs on uncommitted code and saves the patch.
- `kx runs` · `show R` · `board` · `compare A B` · `search WORDS` · `note` · `tag` · `path` · `fetch` · `reap` · `gc`
- Runs are `r0012-fluent-sun`: a number and a name, like wandb's (tasks `T7-quiet-otter`, findings `F3-…`,
  submissions `S5-…`). `r12`, `12` or `fluent-sun` find it.
- Several runs side by side, with what you want of each, instead of code that reads their files:

  ```bash
  kx runs r1133..r1150 --cols score,cv5.se,config.lr,val_auc,summary.json:by_opponent.tusk
  kx compare r1133 r1135 r1137 group:sweep      # each against r1133, one table
  ```

  A column is a score (`score` for the reference protocol, a protocol's name, `cv5.se`, `cv5.breakdown.f0`), the
  config (`config.lr`), a logged metric's latest value, or a path into a JSON file the run wrote (`FILE:PATH`; a
  `.jsonl` file's last record). A run that lacks it shows `–`; a column no run has comes back with the names that
  are there.
- `kx task start T7` links the runs you start to T7 until it is done.
- To compare runs well, put every setting in `config=` and the seed in `seed=`, and log curves with a counter
  beside them (`r.step(i, loss=..., samples=n)`).

## Dashboard

`kx dashboard` serves a local page (Status, Training, Runs, Tasks, Findings, Submissions) over the same data as
`--json`.

- **Training** works like wandb: the runs that log curves on the left (state, when, how long, host, score; filter,
  or order by the curves they share), and the curves of up to 8 shown runs overlaid on the right, grouped by prefix
  (`train/`, `val_`), then their scores with `kx compare`'s verdict against a baseline, metrics, and what differs in
  config and code. x is the step, the time since start, or a counter the runs log (samples, epochs), which lines up
  runs that count steps differently; drag across a chart to zoom (double-click goes back). "Mean of repeats" draws
  runs with the same code, command and config but for the seed as one line with their range shaded ("+N repeats"
  on a run adds them). `#training/compare=r12,r15` links to it; ticking runs in the Runs tab or "Compare…" on a run
  opens it with them.
- **Runs** works like mlflow: every run with columns of its config keys and reported metrics (pick them under
  Columns), sorted by any column, and any two numbers plotted against each other (a setting against the score).
- Curves come from the whole of `metrics.jsonl`, cut for drawing to the first, last, lowest and highest point of
  each stretch of the run and both sides of every restart (`kx show R --curves` gives the same); a running run's
  chart fetches only what is new, every 5 seconds.

## Explaining the work

kx keeps the facts; explaining them is the agent's job, with three skills `kx init` puts in the repo
(`.agents/skills/` for Codex, linked from `.claude/skills/` for Claude Code). They add no kx commands: they say
which ones to run and how to put the answer together, so what the agent tells you can be checked.

- **kx-explain**: the current solution (or a run, a task, a finding): how it works, how it got there, how sure we
  are, with Mermaid diagrams (lineage, pipeline, what we learned). "Explain our solution", "why is r15 better than
  r12".
- **kx-catch-up**: what happened since a time, and what needs you: "catch me up since Friday".
- **kx-video**: a short narrated explainer in the style of 3Blue1Brown (Manim, an offline Kokoro voice), every
  number from kx: storyboard first, silent drafts whose frames the agent looks at, then voice and a 1080p render.
  It ships a template (a run lineage, scores with error bars, training curves) and needs its own environment
  (Manim with Cairo and Pango; the skill says how, with or without sudo).

Explanations follow the links runs record: a run that builds on another should name it (`parent=`).

## wandb and MLflow

Code that already logs to wandb or MLflow needs no change: under `kx exec` (here or `--on` a host) and inside
`kx.run`, what it logs is recorded in its kx run too.

| the code calls | kx records |
|---|---|
| `wandb.init(config=..., tags=..., notes=...)`, `wandb.config.update` | the run's config (a `seed` in it becomes the run's seed), tags as `wandb_tags`, notes as a note |
| `wandb.log({...})`, `run.log(..., step=)` | curve points: nested dicts as `a/b`, numbers only; without `step`, wandb's own counter (`commit=False` stays on the step) |
| `mlflow.log_param(s)`, autolog's params | the run's config, `"4"` back to `4` |
| `mlflow.log_metric(s)`, autolog's metrics | curve points; MLflow logs a metric without a step at 0, so kx counts that metric's points instead |
| the run's end (`wandb.finish`, `mlflow.end_run`, or the process exiting) | the last value of each metric (and wandb's summary) as the run's metrics; a metric logged once is a metric, not a curve |

wandb is switched off while it is captured (`mode="disabled"`: no service, nothing sent), unless `KX_WANDB=also`.
MLflow keeps its own copy where `MLFLOW_TRACKING_URI` points, and without one in the run's dir (`mlflow.db`, or
`mlruns/` without SQLAlchemy), never in the repo. Images, tables and artifacts stay with the library. `KX_CAPTURE=off`
turns capture off. Each library is an adapter in `kx/capture/` speaking the same four things (config, step, summary,
note): another library is another adapter.

## Remote runs

Run on another machine (a laptop on the same network, a rented GPU) over ssh; the ledger stays here.

```yaml
# ~/.config/kx/hosts.yaml (or comp.yaml hosts:)
lambda: {ssh: ubuntu@203.0.113.7, gpus: 1, usd_per_hour: 1.29}
laptop: {ssh: me@laptop.local}
```

```bash
kx remote check lambda                          # ssh (key auth), rsync, git, uv, bubblewrap, disk, GPU
kx exec --on lambda -H "bigger model" -- python scripts/train.py
kx exec --on lambda --detach -- ...             # returns at once: kx remote follow R · sync · kill R
```

kx sends the run as a job folder (its `run.sh`, the commit's code, a probe's patch), with the kx sources and the data
when the host lacks that version; `run.sh` builds the environment from `uv.lock` (once per lock) and the command runs
in its own session, so a dropped connection does not stop it. The run dir comes back every few seconds (logs,
progress, metrics, files up to 32 MB) and the run is recorded when it ends. Bigger files (checkpoints) stay on the
host: `kx show` and the dashboard list them, and `kx fetch R [PATH ...]` copies them here. On the host there is no
ledger: `kx.run`/`r.score` write files that kx records here.

A host gets the competition's own files, once per data version, and nothing else of the data folder: a run that reads
a derived cache asks for it, `kx exec --on HOST --needs derived/fp-v1 -- ...`, which sends it when missing or
changed. On the host the run is confined with bubblewrap when it has it (`kx remote check` says): everything
read-only, the home hidden, writable only the run's dir, the data's `derived/`, `~/.cache` and a private /tmp
(`confine: false` on a host turns that off).
Ctrl-C stops following, not the run. A paid host charges wall time × `usd_per_hour` to the `cloud_usd` budget.
`kx status` lists remote runs nobody has synced for 30 minutes.

Your own machines are easiest over Tailscale: on the other one, install Tailscale, log in with your account and run
`sudo tailscale up --ssh`; then here `kx remote add NAME` finds it, checks it, counts its GPUs, adds it to
hosts.yaml and sets it up. It needs neither kx nor a Kaggle login (kx ships what it needs). Set the Tailscale ssh rule
for your own devices to `accept`: in the default
`check` mode it asks for a browser login every 12 h, which kx cannot answer. A Windows machine runs kx inside WSL2:
Tailscale in WSL (`--hostname NAME-wsl`), then `kx remote add NAME-wsl`.

A host's environment has every extra of the project. `torch: cu126` on a host replaces the lock's torch with the
same version from that PyTorch index, in its own environment: for a GPU the lock's build does not cover (Maxwell to
Volta need cu126, whose last release is torch 2.14).

A machine kx cannot reach at all (no ssh, no Tailscale) takes runs through a **mailbox**: an rclone location both
machines reach, such as a Google Drive folder. Only outbound calls are made, on both sides.

```yaml
farbox: {kind: mailbox, mailbox: gdrive:kx, gpus: 1}
```

```bash
kx remote setup farbox          # puts kx and the data in the mailbox, prints the agent's start command
# on farbox (rclone configured with the same remote, python3):
mkdir -p ~/kx-remote/kx && rclone cat gdrive:kx/_kx/kx.tar.gz | tar -xz -C ~/kx-remote/kx
python3 ~/kx-remote/kx/src/kx/remote/agent.py farbox --mailbox gdrive:kx
kx remote check farbox          # the agent's last report: machine, GPUs, disk
kx exec --on farbox -- python scripts/train.py     # then follow, sync and kill as with any host
```

The mailbox has a folder per competition (`<slug>/data/`, `<slug>/runs/<run>/out/`), the kx sources in `_kx/kx.tar.gz` and what
the machines exchange in `_hosts/<host>/` (heartbeat, jobs, kill notes). kx leaves each run as a job; the agent
(standard library only) takes it, sets it up as over ssh, runs it one at a time
(`--max-runs`), and copies its `out/` to the mailbox every few seconds (files over 32 MB when it ends). A run waits
while no agent is running; `kx remote kill` leaves a note the agent acts on at its next look. Expect updates every
10 to 30 s. For Google Drive, `rclone config` the remote with the `drive.file` scope on both machines (or copy the
config: it holds the login, so keep it private, e.g. a Colab notebook secret). Both machines must go through rclone
and the same Google app: with `drive.file`, files written by another app, such as Colab's own Drive mount, are
invisible to it. Give rclone your own Google app (rclone's documentation: "Making your own client_id"): its shared
one is rate-limited, which turns a one-second call into a minute.

## The ledger: machines and people

The ledger is a set of logs, `.kx/store/log/<user>@<machine>.jsonl`: every change (a run started, a score, a note, a
task claimed) is a line in the log of the machine where it happened, and only that machine writes to it. The SQLite
file next to it is a cache of the logs, rebuilt when another machine's log arrives: deleting it loses nothing of the
ledger (only this machine's list of workers and when it last synced). Changes replay in the order they were made as far
as the machines knew (a logical clock), not by their clocks: a change made after seeing another machine's row comes
after it.

Machines and people share a project through a folder all of them reach with rclone (Google Drive, S3, a server):

```yaml
# ~/.config/kx/machine.yaml
share: gdrive:kx        # <share>/<slug>/: everyone's logs and run records
name: laptop            # this machine in the logs (default: its host name); user: defaults to your login
```

```bash
git push && kx sync     # machine A: code to GitHub, history to the shared folder
git clone ... && cd <competition> && kx init --no-data && kx data && kx sync     # machine B, the first time
```

`kx sync` puts this machine's logs and run records (logs, curves, tables, the files it submitted; one archive per
run, sent when its files change) in the shared folder and takes everyone else's, then syncs with Kaggle (`--ledger`:
only the first part). Logs only grow and each has one writer, so a sync never merges or overwrites anything: two
machines working at the same time both keep their work. No copy of a log replaces a longer one, here or in the shared
folder: a machine that lost its store (a new clone, `.kx/` deleted) gets its log and run records back at its next
sync, and if it wrote before that, what it wrote is kept beside them (`<user>@<machine>+fork-<time>.jsonl`; what
both copies have counts once). Their ids do not clash: two runs both numbered 12 have different names (`r12` then
means this machine's, and kx says that another machine has one; write full ids where others read them). In the rare
case two machines made the very same id, this machine's keeps it here and the other shows with a suffix
(`r0012-fluent-sun~3f2a`, the sync says so); either form finds the same run on every machine. Run outputs stay where
they were made: a remote run's on its host (`kx fetch`), a local run's on its machine.

Each machine needs its own `name:`. Two stores writing under one name (two rented boxes both called `ubuntu`) are
refused at the sync, until one gets another name; what it wrote under the old one moves to the new one. Renaming a
machine is fine: the logs, tasks, runs and budgets under its old name stay its own.

A task's owner is who claimed it and where: `me@laptop`, `codex-1@gpubox` (the same worker name from comp.yaml on two
machines is two owners). Two claims of one task (`kx task start`, `kx spawn`) made without seeing each other: the
earlier by the clock keeps it, and a `kx task done` or `wait` the other machine made under its claim changes nothing
(a task one of them finished first stays done). The machine that lost the claim hears of it
at its sync, and `kx status` lists it with the command that stops its worker.

`kx export [FILE]` writes the whole history in one file (every log here, this machine's run records); `kx import
FILE` takes one in, to move a project without a shared folder or to add a second project for the same competition.
`kx import` alone brings in this store's ledger from kx before the logs (`ledger.sqlite`), once: every run, task,
finding and submission keeps its number and gets a name (`r0012` becomes `r0012-<name>`), and the run folders are
renamed to match. `kx import PATH` does the same for another store or repo, taking its runs' kx files.

### Moving a project from kx v2

A v2 project keeps its ledger in `.kx/store/ledger.sqlite`; v3 never changes that file, so it stays as a fallback.

1. Let remote runs in flight finish, and stop the agents and workers on every machine: what v2 writes after the
   import does not reach v3.
2. Install v3 on every machine (`git pull` in the kx folder; `kx` on PATH is what workers call), each with its own
   `name:` in `~/.config/kx/machine.yaml`.
3. On one machine only, the one with the fullest ledger: `kx import` in the repo, then `kx init` (rewrites
   `docs/kx.md` for v3) and commit it. Compare `kx status` with what v2 showed.
4. `kx sync` there, then on the other machines.
5. Restart mailbox hosts' agents (`kx remote setup HOST` prints the start command).

## Tasks and findings

```bash
kx task add "try dart boosting" --body "..." -p 2 [--depends T3]
kx task start T7                       # yours and current: the runs you start are linked to it
kx exec --task T7 -- ...               # one run for another task (or start its hypothesis with "T7 ...")
kx task relink [--yes]                 # link runs with no task to the task their hypothesis starts with
kx task note T7 "fold 3 still worse"
kx task wait T7 --on run:r12           # parked on run:rN, submission:SN, T3, a command for the person, or text
kx task done T7 --result negative --outcome "dart did not help: -0.002"
kx task merge T7                       # a worker's task: merge its branch into main, remove the worktree

kx finding add "fold 3 leaks through user_id" --evidence r12 r15 --tag data
kx finding withdraw F3 · kx finding supersede F3 --by F7
```

A run is linked to the task it is for: a worker's, the one its branch is for, the one its hypothesis starts with
("T7 ..."), or else the one you started last that is still active (a run linked to none while several are active
says so). Evidence must be runs that have ended: a running one is refused (`kx wait rN` first, or `--partial`,
which the finding then says), a failed one is accepted with a note. `kx status` lists, one line each kind: tasks
active for over 3 days with nothing new, findings whose evidence failed after they were written, and protocols that
got 3 or more versions in 7 days (run settings put in the spec split scores into groups that do not compare; a run
scored under a version other than its name's most-scored one gets a note saying so, and so does its result line).

## Kaggle

`kx submit R -m "..." --why "..." --expect "..."` builds an immutable artifact, smoke-tests it, checks slots,
duplicates and (rl) the ladder, and shows a preview; `--yes` sends it. `kx kernel push R` and `kx dataset push R PATH`
work the same way. `kx sync` brings the other machines' history, then fetches results and a leaderboard snapshot
(only what changed goes into the log); `kx subs` lists submissions with today's slots, counted from everyone's (and the
ladder, simulations); `kx subs S12` shows one with its games.

A dataset kx uploads carries `kx-dataset.json` (its run). `kx kernel push` refuses while an attached dataset kx uploaded
is still processing on Kaggle (the kernel would get its previous version), and fills `"__KX_PUSH__"` in the kernel
code with `{"run", "git_sha", "datasets": {ref: run}}`, so the kernel can check it got the right data.

A worker (`KX_AGENT` set) does not send: with `--yes` kx refuses (exit 3) and prints the command. The worker records
it with `kx task wait T7 --on "kx submit ... --yes"`; `kx status` shows it to you, and you run it.

## Waiting

`kx wait TARGET ...` blocks until what it names has happened, and also when it can no longer happen; an agent runs it
in the background and is woken when it exits, instead of polling.

```bash
kx exec --detach -H "bigger model" -- python scripts/train.py      # r12 started in the background
kx wait r12                                  # it ended: exit 0 done, 6 failed
kx wait r12 --until 'val_auc>=0.91'          # a metric it logs reached a value (or a score: cv5>=0.9, score>=0.9)
kx wait r12 --until 'val_auc>=0.91' --until 'loss>5'      # any one of them
kx submit r12 -m "..." --yes && kx wait S4   # scored on Kaggle: exit 0, with the public score; 6 if it errored
kx kernel push r12 --yes && kx wait kernel:r12            # the pushed kernel version completed
kx wait r12 S4                               # the first of them
kx wait --all group:sweep                    # every run of kx exec --group sweep (or --all r12 r13 ...)
```

A run that ended says what it came to, so there is nothing to ask afterwards: its score, its rank under the reference
protocol, and `kx compare`'s verdict against its parent (`--parent`) or else the best other run:

```
r0042 done
  cv5@3fa1 0.8134 ±0.0021 · #2 of 41 · vs r0012 (best): -0.0031, z=-1.20 ≈ within noise
```

`kx exec` ends with the same line.

Exit 0 when it happened (with `--all`: all of them). Exit 6 when it can no longer happen: the run failed, ended without reaching `--until`
(kx says the last values, or which metrics it does log), lost its process, or went `--stall` minutes (default 30)
without progress, metrics or output; or the submission or kernel errored. Exit 1 after `--timeout` minutes
(default 120), with where each target is (a run's progress and ETA, its latest metrics, the last line it printed)
and the same command to wait again: a look at a long run is `kx wait R --timeout 20`, not reading its files. Each
check reads the state afresh, so a wait keeps nothing: rerun
it after a timeout or a lost session and it answers at once if it has already happened. Remote runs are copied back
on each check (`kx remote sync`). Kaggle is asked at most once a minute, backing off to every 5 minutes, and not when
anyone (another wait, `kx sync`) has just asked. A harness that cannot run a command in the background and wake
the agent when it exits (Codex) runs the wait in the foreground with a long `--timeout`: kx returns as soon as the
run ends, fails or stalls, so there is nothing to gain from short sleeps between looks. With no target, `kx wait` blocks until a worker ends or something new
needs you.

## Workers

```yaml
# comp.yaml
workers:
  codex-1: {harness: codex, model: gpt-6-luna, effort: medium, memory: 5G, cpus: 8}
  sonnet-1: {harness: claude, model: sonnet, memory: 5G}
max_workers: 2
```

`kx spawn T7 -w codex-1` creates the task's worktree, writes `.kx/context.md` and starts the harness in the background
with `KX_AGENT=codex-1`, inside a systemd scope when `memory`/`cpus` are set (an out-of-memory program is killed, not
the agent). Tokens are charged to the budget as they stream and the worker is stopped at its cap. When it ends, kx
commits what it left uncommitted and reopens the task if it was not finished. Nothing is retried automatically.

`kx workers [--all] [--show ID]` · `kx stop ID` · `kx wait` (block until a worker ends or something needs you).
Workers start inside bubblewrap: they, and anything they run, may write only the competition (its repo,
worktrees and store), their own state (`~/.claude`, `~/.codex`), caches and a private /tmp; logins they never need
(rclone, gh, cloud CLIs) are hidden. `confine: false` in a worker's entry opts out (not on a workspace). Codex's own
sandbox hides the GPU: give GPU work to Claude workers. A worker entry's `argv:` replaces its harness's command
line.

A machine's role, in `~/.config/kx/machine.yaml`: `role: both` (the default), `workspace` (people and agents
think here; no project code runs: `kx exec` and `kx.run` go to an executor, `default_host: NAME` picks it; the
submission smoke test runs sandboxed), or `executor`. See [docs/collab.md](docs/collab.md) for where this is going.

## Budgets

```yaml
budgets:
  submissions_per_day: 3
  kaggle_gpu_hours_per_week: 25     # kernel pushes with a GPU (submission.kernel_gpu_hours each, default 1)
  gpu_hours_per_day: 12             # local: kx exec --gpus N, wall time × N
  cloud_usd_per_week: 50            # kx exec --on a host with usd_per_hour
  tokens_per_day: 30000000          # all workers together
  workers:
    codex-1: {tokens_per_day: 10000000}
```

Caps are checked before the action; over one, the action is refused with when it frees up (exit 4): wait, or raise
the cap. `kx budget` shows usage.

comp.yaml is shared by everyone, so each cap counts what its metric is paid from: submissions the team's (Kaggle's
team slots), `kaggle_gpu_hours`, `tokens` and `cloud_usd` a person's (all their machines with the same `user:` in
machine.yaml), `gpu_hours` this machine's GPUs. `budgets: {scope: {tokens: team}}` changes one. A worker's own cap
(`budgets.workers`) counts that worker on this machine. Another machine's usage counts once it has synced.

## Where runs happen, where files live

Rules of the project, in comp.yaml (so they hold on every machine that works on it):

```yaml
runs:
  on: [colab, lambda]     # only these hosts: kx exec without --on uses the first, other hosts and this machine refuse
storage:
  keep: here              # remote runs leave nothing on their host
  max_gb: 200             # the store here: runs, artifacts, data, backups
```

`runs.on` refuses (exit 4) a run anywhere else: `kx exec --on` another host, a local `kx exec`, a script calling
`kx.run(...)` here. `storage.keep: here` changes how a remote run ends: every file comes back, files over 32 MB
too; once kx has checked that all of them are here whole, it deletes the host's copy (a mailbox host's: from the
mailbox, and its agent deletes its own copy at its next look). What cannot come back (the host is unreachable, the
store has no room) stays on the host, listed by `kx show`, and the next `kx remote sync R` tries again.
`storage.max_gb` refuses (exit 4) a new run, `kx fetch` and `kx data` once the store would go over it, naming the
largest runs. `kx budget` shows `store: 143 / 200 GB`; `kx status` says when it is nearly full.

`kx gc` frees space: it previews, and with `--yes` deletes, what runs nobody needs made (those tagged `discarded`,
failed ones older than `--failed-days 7`, or the runs named), here and on their host. What kx recorded stays (the
ledger, scores, notes, curves, logged tables, the end of the logs); runs a submission or a push came from, and the
best run, are kept whole. Workers cannot pass `--yes`.

Like the rest of kx these rules guard against mistakes: a command run without kx is not stopped.

## Develop

```bash
uv run pytest
```

```
src/kx/
  __init__.py   the Python API competition code imports (kx.run, kx.Protocol, ...)
  ledger/       comp.yaml (config), the logs and their SQLite cache (store), ids (names), kx sync and export
                (sync), old ledgers in (port)
  runs/         kx.run, kx exec, protocols, compare, progress and curves, run objects
  capture/      what code logs to wandb / MLflow, recorded in its run (one adapter per library, an import hook)
  remote/       hosts (ssh, mailbox), run.sh, kx exec --on (ship, launch, copy back, record), the mailbox agent
  board/        tasks and findings
  kaggle/       the Kaggle CLI client, competition pages, data, submissions, kernels, ladder
  guard/        actors, budgets, the project's rules (runs.on, storage), the gate on actions that leave the machine
  workers/      spawn (launch, stop, liveness), runner (the run loop), streams (harness output)
  sandbox.py    machine roles (workspace, executor) and the bubblewrap sandbox workers run in
  wait.py       what kx wait waits for: runs (ended, --until, stalled), submissions, kernel versions
  views/        kx status / kx context, the dashboard, text rendering
  setup/        kx init (scaffold, docs/kx.md template)
  cli/          main.py (parser, dispatch) and one module of commands per area above
tests/          one file per area
```

How the shared ledger works is in [The ledger](#the-ledger-machines-and-people) and
[docs/collab.md](docs/collab.md) (also where kx is going); why kx is a ledger rather than an autopilot is in
[docs/v2.md](docs/v2.md).
The older autopilot version is tag `v1`.

## Privacy

kx keeps everything on your machines: the ledger, runs and data. It talks only to Kaggle (through the kaggle CLI),
to hosts you configure, and to the storage you name for mailbox hosts and for `share:` (the logs and run records of
the projects you sync). The Google Drive app it uses through
rclone is described in [PRIVACY.md](PRIVACY.md).

MIT licensed ([LICENSE](LICENSE)).
