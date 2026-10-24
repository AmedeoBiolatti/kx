"""Starting and stopping background workers: `kx spawn T7 --worker codex-1` runs an agent harness on one task, in
its own worktree, through a detached runner (kx.workers.runner). Also which workers are alive (`live`)."""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path


from kx.board import tasks
from kx.guard import actors, budgets
from kx.ledger.config import KxError
from kx.ledger.store import Store, now, pid_alive
from kx.runs import git
from kx.workers.runner import _terminate, _worker_actor, _wrap_task, run

API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY")


RUN_VARS = ("KX_RUN_ID", "KX_RUN_DIR", "KX_TASK", "KX_AGENT")


HARNESSES = {
    "claude": ["claude", "-p", "{prompt}", "--allowedTools", "Bash(kx:*)", "--model", "{model}", "--permission-mode", "auto",
               "--output-format", "stream-json", "--verbose", "--add-dir", "{store}"],
    "codex": ["codex", "exec", "--json", "-m", "{model}", "-c", "model_reasoning_effort={effort}",
              "-c", "sandbox_workspace_write.network_access={network}", "--sandbox", "{sandbox}",
              "--add-dir", "{store}", "--add-dir", "{git_admin}", "--add-dir", "{git_objects}",
              "--add-dir", "{git_task_refs}", "--add-dir", "{git_task_logs}", "{prompt}"],
}


PROMPT = """You are {agent}, a background worker on a Kaggle competition worked with kx. Your task is {task}.
First read {context} (your task, the reference protocol, the best runs, open tasks, recent findings, the rules) and
docs/kx.md. Work only on {task}, in this directory: a git worktree on its own branch. Commit your work as you go.
Record every experiment with kx (kx exec / kx.run), scored under the reference protocol. Report progress with
`kx task note {task} "..."`. Write conclusions as findings (kx finding add ... --evidence rN).
Finish with `kx task done {task} --result positive|negative|inconclusive --outcome "what was learned"`; negative
results count. If you cannot go on without the person driving (a submission or a push to send: kx printed its
command; or a decision is needed), run `kx task wait {task} --on "<that command, or what you need>"` and end.
Your session ends when you end your turn: wait for long runs inside it (sleep and check kx runs) instead of
ending with "I'll wait"."""


def harness_template(w: dict) -> list[str]:
    """The harness's command line: a worker entry's own `argv:` (comp.yaml), else the built-in one for its harness."""
    if w.get("argv"):
        return shlex.split(w["argv"]) if isinstance(w["argv"], str) else [str(x) for x in w["argv"]]
    return list(HARNESSES[w["harness"]])


def fill(template: list[str], values: dict) -> list[str]:
    """Substitute placeholders argument by argument; `--flag {x}` with x empty drops the flag too."""
    out = []
    for part in template:
        names = re.findall(r"\{([a-z_]+)\}", part)
        resolved = re.sub(r"\{([a-z_]+)\}", lambda m: "" if values.get(m.group(1)) in (None, "") else str(values[m.group(1)]), part)
        if names and resolved == "" and out and out[-1].startswith("-"):
            out.pop()
            continue
        out.append(resolved)
    return out


def limit(argv: list[str], w: dict) -> list[str]:
    """`memory: 5G` / `cpus: 8` in a worker's comp.yaml entry: run it in a systemd scope with those hard caps, so a
    worker that runs out of memory is the one killed, not the machine."""
    props = []
    if w.get("memory"):
        # OOMPolicy=continue: when one of the worker's programs runs out of memory only that program is killed;
        # the default (stop) would also take the agent down, so it could not notice and adapt
        props += ["-p", f"MemoryMax={w['memory']}", "-p", "MemorySwapMax=0", "-p", "OOMPolicy=continue"]
    if w.get("cpus"):
        props += ["-p", f"CPUQuota={int(float(w['cpus']) * 100)}%"]
    if not props:
        return argv
    import shutil
    if shutil.which("systemd-run") is None:
        print("kx: warning: systemd-run not found, the worker runs without its memory/cpu caps", file=sys.stderr)
        return argv
    return ["systemd-run", "--user", "--scope", "-q", *props, "--", *argv]


def confine(argv: list[str], comp, w: dict, worktree: Path | None) -> list[str]:
    """The harness inside kx's sandbox (kx.sandbox): it, and anything it runs, may write only the competition.
    `confine: false` in a worker's entry opts out where a person allows it; a workspace never runs one unconfined."""
    from kx import sandbox
    if w.get("confine") is False and sandbox.role() != "workspace":
        return argv
    if not sandbox.available():
        if sandbox.role() == "workspace":
            raise KxError("this machine is a workspace and bubblewrap (bwrap) is not usable here: workers would run "
                          "unconfined", fix="install bubblewrap (apt install bubblewrap)")
        print("kx: warning: bubblewrap (bwrap) is not usable here: the worker runs unconfined", file=sys.stderr)
        return argv
    return sandbox.wrap(argv, sandbox.competition_paths(comp), cwd=worktree)


def git_write_paths(worktree: Path) -> dict:
    """What a sandboxed Codex needs to commit on its task branch: its worktree's admin dir, the objects, task refs."""
    def git(*a):
        return subprocess.run(["git", *a], cwd=worktree, capture_output=True, text=True).stdout.strip()
    admin, common = git("rev-parse", "--absolute-git-dir"), git("rev-parse", "--path-format=absolute", "--git-common-dir")
    if not admin or not common:
        return {}
    refs, logs = Path(common) / "refs" / "heads" / "task", Path(common) / "logs" / "refs" / "heads" / "task"
    refs.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    return {"git_admin": admin, "git_objects": str(Path(common) / "objects"), "git_task_refs": str(refs), "git_task_logs": str(logs)}


def worker_env(comp, name: str, task_id: int, w: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in RUN_VARS}
    if w.get("billing", "subscription") == "subscription":  # never fall back to per-token billing
        env = {k: v for k, v in env.items() if k not in API_KEY_VARS}
    venv = comp.main_root / ".venv"
    if venv.is_dir():
        env.update(VIRTUAL_ENV=str(venv), UV_PROJECT_ENVIRONMENT=str(venv), PATH=f"{venv / 'bin'}{os.pathsep}{env.get('PATH', '')}")
    env.update(KX_AGENT=name, KX_TASK=f"T{task_id}")
    return env


def live(store: Store) -> list[dict]:
    """Workers still running; ones whose runner died are closed on the way."""
    out, host = [], socket.gethostname()
    for w in store.all("SELECT * FROM workers WHERE finished IS NULL"):
        if w["host"] == host and pid_alive(w["pid"]) is False and pid_alive(w["harness_pid"]) is not True:
            try:  # a transcript written in the last 2 minutes means something is still running: do not trust the pids alone
                if time.time() - Path(w["transcript"]).stat().st_mtime < 120:
                    out.append(dict(w))
                    continue
            except (OSError, TypeError):
                pass
            why = f"runner died (runner pid {w['pid']} and harness pid {w['harness_pid']} gone)"
            store.x("UPDATE workers SET finished=?, code=COALESCE(code, -1), stop=COALESCE(stop, ?) WHERE id=?",
                    (now(), why, w["id"]))
            _wrap_task(store, dict(w), "its kx runner died")
            continue
        out.append(dict(w))
    return out


def spawn(store: Store, name: str, task_ref, *, model: str | None = None, effort: str | None = None,
          foreground: bool = False, dry_run: bool = False, force: bool = False) -> dict:
    actors.not_worker("starting workers")
    comp = store.comp
    if name not in comp.workers:
        raise KxError(f"no worker {name!r} in comp.yaml", fix="configured: " + (", ".join(comp.workers) or
                      "none; add e.g. `workers: {codex-1: {harness: codex, model: gpt-6-luna}}` to comp.yaml"))
    w = dict(comp.workers[name])
    t = tasks.get(store, task_ref)
    if t["status"] in ("done", "dropped"):
        raise KxError(f"T{t['id']} is {t['status']}", fix=f"kx task reopen T{t['id']}")
    if t["status"] == "active" and t["owner"] and not force:
        raise KxError(f"T{t['id']} is active with {t['owner']}", fix="--force to start a worker on it anyway")
    if (b := tasks.blockers(store, t)) and not force:
        raise KxError(f"T{t['id']} depends on tasks not done: {', '.join(b)}", fix="--force to start it anyway")
    running = live(store)
    if any(r["agent"] == name for r in running):
        raise KxError(f"worker {name} is already running", fix="kx workers")
    if len(running) >= comp.max_workers and not force:
        raise KxError(f"{len(running)} worker(s) running, max_workers is {comp.max_workers}", code=4,
                      fix="wait for one to end (kx workers), or raise max_workers in comp.yaml")
    budgets.check(store, _worker_actor(name), {"tokens": 1})
    harness = w["harness"]
    model = model or w.get("model")
    values = {"agent": name, "task": f"T{t['id']}", "model": model, "effort": effort or w.get("effort", "medium"),
              "store": str(comp.store), "sandbox": w.get("sandbox", "workspace-write"),
              "network": "false" if w.get("network") is False else "true"}
    wid = uuid.uuid4().hex[:12]
    if dry_run:
        values.update(worktree="<worktree>", context="<worktree>/.kx/context.md")
        values["prompt"] = PROMPT.format(**{k: values[k] for k in ("agent", "task", "context")})
        return {"worker": name, "task": f"T{t['id']}", "argv": limit(confine(fill(harness_template(w), values), comp, w, None), w),
                "dry_run": True}
    wt, branch = tasks.make_worktree(store, t)
    store.x("UPDATE tasks SET branch=?, worktree=? WHERE id=?", (branch, str(wt), t["id"]))
    tasks.start(store, t["id"], owner=name)
    from kx.views import status
    ctx = wt / ".kx" / "context.md"
    ctx.parent.mkdir(exist_ok=True)
    ctx.write_text(status.render_context(status.context(store, _worker_actor(name), t["id"])))
    git.exclude("/.kx/", comp.main_root)
    values.update(worktree=str(wt), context=str(ctx), **(git_write_paths(wt) if harness == "codex" else {}))
    values["prompt"] = PROMPT.format(**{k: values[k] for k in ("agent", "task", "context")})
    argv = limit(confine(fill(harness_template(w), values), comp, w, wt), w)
    tdir = comp.store / "workers"
    tdir.mkdir(exist_ok=True)
    transcript = tdir / f"{wid}.jsonl"
    store.x("INSERT INTO workers (id, agent, harness, model, task, host, worktree, argv, started, updated, transcript, last_action)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (wid, name, harness, model, t["id"], socket.gethostname(), str(wt),
                                                 json.dumps(argv), now(), now(), str(transcript), "starting"))
    env = worker_env(comp, name, t["id"], w)
    if foreground:
        code = run(comp, wid, env=env, echo=True)
        return {"id": wid, "worker": name, "task": f"T{t['id']}", "worktree": str(wt), "code": code}
    with open(tdir / f"{wid}.runner.log", "ab") as log:  # the child keeps its own copy of the handle
        p = subprocess.Popen([sys.executable, "-m", "kx", "worker-run", wid], cwd=wt, env=env, stdin=subprocess.DEVNULL,
                             stdout=log, stderr=log, start_new_session=True)
    store.x("UPDATE workers SET pid=? WHERE id=?", (p.pid, wid))
    return {"id": wid, "worker": name, "task": f"T{t['id']}", "worktree": str(wt), "pid": p.pid, "transcript": str(transcript)}


def stop(store: Store, ref: str) -> dict:
    actors.not_worker("stopping workers")
    rows = [w for w in live(store) if w["id"].startswith(ref) or w["agent"] == ref]
    if not rows:
        raise KxError(f"no running worker {ref!r}", fix="kx workers")
    w = rows[0]
    store.x("UPDATE workers SET stop='asked' WHERE id=?", (w["id"],))
    if w["harness_pid"]:
        _terminate(w["harness_pid"])
    return {"id": w["id"], "worker": w["agent"], "stopped": True}

