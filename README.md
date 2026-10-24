# kx

The ledger and Kaggle interface of a competition: runs, scores, tasks, findings and submissions, shared by you, the
agent you talk to in your terminal, and background workers. kx records, checks and talks to Kaggle; it never decides
what to work on and never runs anything on its own.

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
└── .kx/                    git-ignored: store/ (ledger, runs, artifacts, data) and worktrees/
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
- `kx task start T7` links the runs you start to T7 until it is done.
- The dashboard's Training tab works like wandb: the runs that log curves on the left (state, when, how long, host,
  score; filter, or order by the curves they share), and the curves of up to 8 shown runs overlaid on the right,
  grouped by prefix (`train/`, `val_`), then their scores with `kx compare`'s verdict against a baseline, metrics, and
  what differs in config and code. `#training/compare=r12,r15` links to it; ticking runs in the Runs tab or
  "Compare…" on a run opens it with them.

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
work the same way. `kx sync` fetches results and a leaderboard snapshot; `kx subs` lists submissions with today's
slots (and the ladder, simulations); `kx subs S12` shows one with its games.

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

## Develop

```bash
uv run pytest
```

```
src/kx/
  __init__.py   the Python API competition code imports (kx.run, kx.Protocol, ...)
  ledger/       comp.yaml (config) and the SQLite ledger (store)
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
to hosts you configure, and, for mailbox hosts, to the storage you name. The Google Drive app it uses through
rclone is described in [PRIVACY.md](PRIVACY.md).

MIT licensed ([LICENSE](LICENSE)).
