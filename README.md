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
confirmation · `4` over a budget or Kaggle limit · `5` not comparable.

## Setup

```bash
git clone https://github.com/AmedeoBiolatti/kx && uv tool install --editable kx    # the kx command
mkdir <competition-slug> && cd $_       # next to kx: competition repos sit beside it
kx init                 # git repo, comp.yaml, docs, code skeleton, pyproject.toml, data
uv sync && git add -A && git commit -m init
kx doctor
```

A competition repo then looks like this:

```
<competition-slug>/
├── README.md  AGENTS.md  CLAUDE.md  comp.yaml  pyproject.toml
├── docs/competition.md     the Kaggle pages (kx meta)
├── docs/kx.md              the rules for whoever works here
├── src/comp/evaluate.py    the evaluation protocol: the one place a score is defined
├── scripts/                entry points, run with kx exec
├── notebooks/
├── kernel/  or  agent/     what Kaggle runs (code competitions / simulations)
├── data -> .kx/store/data
└── .kx/                    git-ignored: store/ (log/, its cache, runs, artifacts, data) and worktrees/
```

Everything for a competition is in its folder: deleting it deletes everything. `store:` in comp.yaml puts the
store elsewhere (another disk). Never `git clean -x` in a competition repo: it would delete `.kx/`.

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
`$KX_RUN_DIR`; a tqdm bar becomes its progress).

- Scores compare only under the same protocol id (`name@fingerprint`). One protocol is the **reference**
  (`kx protocol use NAME`); `board`, `status` and `context` rank under it.
- Experiments need committed code; `--probe` runs on uncommitted code and saves the patch.
- `kx runs` · `show R` · `board` · `compare A B` · `search WORDS` · `note` · `tag` · `path` · `fetch` · `reap`
- Runs are `r0012-fluent-sun`: a number and a name, like wandb's (tasks `T7-quiet-otter`, findings `F3-…`,
  submissions `S5-…`). `r12`, `12` or `fluent-sun` find it.
- `kx task start T7` links the runs you start to T7 until it is done.
- The dashboard's Training tab works like wandb: the runs that log curves on the left (state, when, how long, host,
  score; filter, or order by the curves they share), and the curves of up to 8 shown runs overlaid on the right,
  grouped by prefix (`train/`, `val_`), then their scores with `kx compare`'s verdict against a baseline, metrics, and
  what differs in config and code. `#training/compare=r12,r15` links to it; ticking runs in the Runs tab or
  "Compare…" on a run opens it with them. x can be the step, the time since start, or a counter the runs log
  (samples, epochs), which lines up runs that count steps differently; drag across a chart to zoom (double-click
  goes back). "Mean of repeats" draws runs with the same code, command and config but for the seed as one line
  with their range shaded ("+N repeats" on a run adds them).
- Curves come from the whole of `metrics.jsonl`, cut for drawing to the first, last, lowest and highest point of
  each stretch of the run and both sides of every restart (`kx show R --curves` gives the same); a running run's
  chart fetches only what is new, every 5 seconds.
- The Runs tab works like mlflow's: every run with columns of its config keys and reported metrics (pick them under
  Columns), sorted by any column, and any two numbers plotted against each other (a setting against the score).

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

## Tasks and findings

```bash
kx task add "try dart boosting" --body "..." -p 2 [--depends T3]
kx task start T7                       # yours; the runs you start are linked to it
kx task note T7 "fold 3 still worse"
kx task wait T7 --on run:r12           # parked on run:rN, submission:SN, T3, a command for the person, or text
kx task done T7 --result negative --outcome "dart did not help: -0.002"
kx task merge T7                       # a worker's task: merge its branch into main, remove the worktree

kx finding add "fold 3 leaks through user_id" --evidence r12 r15 --tag data
kx finding withdraw F3 · kx finding supersede F3 --by F7
```

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
  remote/       hosts (ssh, mailbox), run.sh, kx exec --on (ship, launch, copy back, record), the mailbox agent
  board/        tasks and findings
  kaggle/       the Kaggle CLI client, competition pages, data, submissions, kernels, ladder
  guard/        actors, budgets, and the gate on actions that leave the machine
  workers/      spawn (launch, stop, liveness), runner (the run loop), streams (harness output)
  sandbox.py    machine roles (workspace, executor) and the bubblewrap sandbox workers run in
  views/        kx status / kx context, the dashboard, text rendering
  setup/        kx init (scaffold, docs/kx.md template)
  cli/          main.py (parser, dispatch) and one module of commands per area above
tests/          one file per area
```

The design and why kx works this way: [docs/v2.md](docs/v2.md); where it is going: [docs/collab.md](docs/collab.md).
The older autopilot version is tag `v1`.

## Privacy

kx keeps everything on your machines: the ledger, runs and data. It talks only to Kaggle (through the kaggle CLI),
to hosts you configure, and to the storage you name for mailbox hosts and for `share:` (the logs and run records of
the projects you sync). The Google Drive app it uses through
rclone is described in [PRIVACY.md](PRIVACY.md).

MIT licensed ([LICENSE](LICENSE)).
