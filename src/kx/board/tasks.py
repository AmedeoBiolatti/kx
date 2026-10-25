"""The task board: open -> active -> done | dropped, or waiting on something. Plain records, no leases."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from kx.ledger.config import KxError
from kx.ledger.store import Store, author, dumps, loads, now, owner
from kx.runs import git

RESULTS = ("positive", "negative", "inconclusive")
OPEN = ("open", "active", "waiting")
BRANCH = re.compile(r"^task/(T\d+[\w~-]*)")  # task/<task id>-<title slug> (kx spawn); older: task/T<n>-<slug>


def get(store: Store, ref) -> dict:
    d = dict(store.one("SELECT * FROM tasks WHERE id=?", (store.resolve(ref, "tasks"),)))
    d["depends"] = loads(d["depends"]) or []
    return d


def current(store: Store, root: Path | None = None, actor: str | None = None) -> str | None:
    """The task this process works on: KX_TASK (a worker's), else a task/T<n>-... branch, else the one task `actor`
    has started (kx task start) and not finished. None when that is ambiguous."""
    if os.environ.get("KX_TASK"):
        try:
            return store.resolve(os.environ["KX_TASK"], "tasks")
        except KxError:
            return None
    branch = (git.info(root) or {}).get("branch") or ""
    m = BRANCH.match(branch)
    if m:
        row = store.one("SELECT id FROM tasks WHERE branch=?", (branch,))
        if row:
            return row["id"]
        # else the id is the longest start of the branch's words that is a number and a name (the name and the slug
        # look alike); never the number alone: another machine's task branch, before its task came with kx sync,
        # would be this machine's task of that number
        parts = m.group(1).split("-")
        for k in range(len(parts), 2, -1):
            try:
                return store.resolve("-".join(parts[:k]), "tasks")
            except KxError:
                continue
        return None
    if actor:
        mine = store.owners(actor)
        active = store.all(f"SELECT id FROM tasks WHERE status='active' AND owner IN ({','.join('?' * len(mine))})",
                           mine)
        if len(active) == 1:
            return active[0]["id"]
    return None


def slug(title: str, n: int = 32) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:n].strip("-") or "task"


def note(store: Store, ref, text: str, by: str | None = None) -> dict:
    t = get(store, ref)
    with store.transaction():
        store.insert("task_notes", {"task": t["id"], "ts": now(), "author": by or author(), "text": text})
        store.update("tasks", {"updated": now()}, id=t["id"])
    return t


def notes(store: Store, ref, limit: int | None = None) -> list[dict]:
    key = store.resolve(ref, "tasks")
    rows = store.all("SELECT * FROM task_notes WHERE task=? ORDER BY ts DESC, id DESC" + (" LIMIT ?" if limit else ""),
                     (key, limit) if limit else (key,))
    return [dict(r) for r in reversed(rows)]


def add(store: Store, title: str, body: str = "", priority: int = 0, depends=()) -> dict:
    deps = [store.resolve(d, "tasks") for d in depends or []]
    key = store.new("tasks", {"title": title.strip(), "body": body or None, "priority": priority,
                              "depends": dumps(deps) if deps else None, "created": now(), "created_by": author(),
                              "updated": now()})
    return get(store, key)


def edit(store: Store, ref, *, title=None, body=None, priority=None, depends=None) -> dict:
    t = get(store, ref)
    values = {col: val for col, val in (("title", title), ("body", body), ("priority", priority)) if val is not None}
    if depends is not None:
        deps = [store.resolve(d, "tasks") for d in depends]
        if t["id"] in deps:
            raise KxError("a task cannot depend on itself")
        values["depends"] = dumps(deps) if deps else None
    if not values:
        raise KxError("nothing to change", fix="pass --title, --body, --priority or --depends")
    store.update("tasks", {**values, "updated": now()}, id=t["id"])
    return get(store, t["id"])


def blockers(store: Store, t: dict) -> list[str]:
    """Dependencies not done yet."""
    out = []
    for d in t["depends"]:
        row = store.one("SELECT status FROM tasks WHERE id=?", (d,))
        if row is None or row["status"] != "done":
            out.append(f"{d} ({row['status'] if row else 'missing'})")
    return out


def start(store: Store, ref, *, owner: str | None = None) -> dict:
    """Mark a task active, owned by `owner` (an actor name, default: whoever runs this; recorded with this machine:
    codex-1@laptop); runs they start are linked to it."""
    t = get(store, ref)
    if t["status"] in ("done", "dropped"):
        raise KxError(f"{t['id']} is {t['status']}", fix=f"kx task reopen {t['id']}")
    name = owner or author()
    who = _owner(name)
    if t["status"] == "active" and t["owner"] and not store.owns(t["owner"], name):
        raise KxError(f"{t['id']} is active with {t['owner']}",
                      fix=f"kx task show {t['id']}  (kx task reopen {t['id']} first to take it over)")
    # the claim holds only while nobody else has the task: two machines claiming at once, the earlier one keeps it
    # (by when they claimed it: `claimed`)
    store.update("tasks", {"status": "active", "owner": who, "waiting_on": None, "claimed": now(), "updated": now()},
                 id=t["id"], owner=t["owner"])
    return get(store, t["id"])


def _owner(name: str) -> str:
    return owner(name)


def held(t: dict) -> dict:
    """The claim a change to a task is made under: replayed after another machine's earlier claim, it changes nothing
    (the machine that lost the task cannot finish it for the one that has it)."""
    return {"owner": t["owner"]} if t["status"] in ("active", "waiting") and t["owner"] else {}


def make_worktree(store: Store, t: dict) -> tuple[Path, str]:
    """<repo>/.kx/worktrees/T<n>-<slug> on branch task/T<n>-<slug>, from the main checkout's HEAD (kx spawn)."""
    from kx.ledger.config import worktrees_dir
    main = store.comp.main_root
    name = f"{t['id']}-{slug(t['title'])}"
    path, branch = worktrees_dir(main) / name, f"task/{name}"
    if path.exists():
        return path, branch
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = subprocess.run(["git", "rev-parse", "--verify", "--quiet", branch], cwd=main, capture_output=True).returncode == 0
    cmd = (["git", "worktree", "add", str(path), branch] if exists else
           ["git", "worktree", "add", "-b", branch, str(path), "HEAD"])
    r = subprocess.run(cmd, cwd=main, capture_output=True, text=True)
    if r.returncode != 0:
        raise KxError(f"git worktree add failed: {(r.stdout + r.stderr).strip()}")
    from kx.kaggle import data
    try:
        data.link(store.comp, path)
    except OSError:
        pass
    return path, branch


def wait(store: Store, ref, on: str, text: str = "") -> dict:
    t = get(store, ref)
    store.update("tasks", {"status": "waiting", "waiting_on": on, "updated": now()}, id=t["id"], **held(t))
    if text:
        note(store, t["id"], text)
    return get(store, t["id"])


def finish(store: Store, ref, result: str, outcome: str) -> dict:
    t = get(store, ref)
    if result not in RESULTS:
        raise KxError(f"result must be one of {', '.join(RESULTS)}")
    if not outcome.strip():
        raise KxError("an outcome is required: what was learned, including negative results")
    store.update("tasks", {"status": "done", "result": result, "outcome": outcome.strip(), "waiting_on": None,
                           "updated": now()}, id=t["id"], **held(t))
    return get(store, t["id"])


def reopen(store: Store, ref) -> dict:
    t = get(store, ref)
    store.update("tasks", {"status": "open", "owner": None, "waiting_on": None, "claimed": None, "updated": now()},
                 id=t["id"])
    return get(store, t["id"])


def listing(store: Store, all_: bool = False) -> list[dict]:
    where = "" if all_ else f" WHERE status IN ({','.join('?' * len(OPEN))})"
    rows = store.all("SELECT * FROM tasks" + where + " ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'waiting' THEN 1 "
                     "WHEN 'open' THEN 2 ELSE 3 END, priority DESC, n, id", () if all_ else OPEN)
    out = []
    for r in rows:
        d = dict(r)
        d["depends"] = loads(d["depends"]) or []
        d["blocked_by"] = blockers(store, d) if d["status"] in OPEN else []
        last = store.one("SELECT ts, text FROM task_notes WHERE task=? ORDER BY ts DESC, id DESC LIMIT 1", (d["id"],))
        d["last_note"] = dict(last) if last else None
        out.append(d)
    return out


WAITS = (("tasks", re.compile(r"^(?:task:)?(T[\w~-]+)")), ("runs", re.compile(r"\brun:([\w~-]+)")),
         ("submissions", re.compile(r"\bsubmission:([\w~-]+)")))


def waiting_resolved(store: Store, t: dict) -> str | None:
    """Why a waiting task can go on now (what it waited for has happened), or None."""
    on = t.get("waiting_on") or ""
    for table, pat in WAITS:
        m = pat.search(on)
        if not m:
            continue
        try:
            key = store.resolve(m.group(1), table)
        except KxError:
            continue
        r = store.one(f"SELECT * FROM {table} WHERE id=?", (key,))
        if table == "tasks" and r["status"] in ("done", "dropped") or table == "runs" and r["status"] != "running" \
                or table == "submissions" and (r["status"] in ("complete", "error") or r["public"] is not None):
            return f"{key} is {r['status']}"
    return None


def merge(store: Store, ref) -> str:
    """Merge the task branch into the main checkout's current branch (no fast-forward) and remove its worktree."""
    t = get(store, ref)
    if not t["branch"]:
        raise KxError(f"{t['id']} has no branch", fix="it was worked on in the main checkout: nothing to merge")
    main = store.comp.main_root
    g = git.info(main)
    if g and [c for c in g["changes"] if not c.endswith((" data", " data/"))]:
        raise KxError(f"{main} has uncommitted changes", fix="commit or stash them first")
    wt = Path(t["worktree"]) if t["worktree"] else None
    if wt and wt.exists():
        w = git.info(wt)
        if w and w["changes"]:
            raise KxError(f"{wt} has uncommitted changes", fix=f"commit them on {t['branch']} first")
    r = subprocess.run(["git", "merge", "--no-ff", "-m", f"Merge {t['id']}: {t['title']}", t["branch"]], cwd=main,
                       capture_output=True, text=True)
    if r.returncode != 0:
        subprocess.run(["git", "merge", "--abort"], cwd=main, capture_output=True)
        raise KxError(f"merge of {t['branch']} failed (aborted): {(r.stdout + r.stderr).strip()[-1500:]}",
                      fix=f"resolve by hand: git merge {t['branch']}")
    out = r.stdout.strip()
    if wt and wt.exists():
        rm = subprocess.run(["git", "worktree", "remove", str(wt)], cwd=main, capture_output=True, text=True)
        out += "\n" + (f"removed {wt}" if rm.returncode == 0 else f"kept {wt}: {rm.stderr.strip()}")
    if not (wt and wt.exists()):  # the branch is merged and nothing checks it out any more: delete it (-d: merged only)
        if subprocess.run(["git", "branch", "-d", t["branch"]], cwd=main, capture_output=True).returncode == 0:
            out += f"\ndeleted branch {t['branch']}"
    store.update("tasks", {"worktree": None, "updated": now()}, id=t["id"])
    return out.strip()
