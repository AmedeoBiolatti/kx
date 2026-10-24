"""The task board: open -> active -> done | dropped, or waiting on something. Plain records, no leases."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from kx.ledger.config import KxError
from kx.ledger.store import Store, author, dumps, loads, now
from kx.runs import git

RESULTS = ("positive", "negative", "inconclusive")
OPEN = ("open", "active", "waiting")
BRANCH = re.compile(r"^task/T(\d+)\b")


def tid(n: int) -> str:
    return f"T{n}"


def parse_id(ref) -> int:
    s = str(ref).strip().upper().lstrip("#")
    if s.startswith("T"):
        s = s[1:]
    if not s.isdigit():
        raise KxError(f"not a task id: {ref!r}", fix="use e.g. T7 (kx task list)")
    return int(s)


def get(store: Store, ref) -> dict:
    n = parse_id(ref)
    row = store.one("SELECT * FROM tasks WHERE id=?", (n,))
    if row is None:
        raise KxError(f"no task T{n}", fix="kx task list --all")
    d = dict(row)
    d["depends"] = loads(d["depends"]) or []
    return d


def current(root: Path | None = None, store: Store | None = None, actor: str | None = None) -> int | None:
    """The task this process works on: KX_TASK (a worker's), else a task/T<n>-... branch, else the one task `actor`
    has started (kx task start) and not finished. None when that is ambiguous."""
    if os.environ.get("KX_TASK"):
        try:
            return parse_id(os.environ["KX_TASK"])
        except KxError:
            return None
    g = git.info(root)
    m = BRANCH.match((g or {}).get("branch") or "")
    if m:
        return int(m.group(1))
    if store is not None and actor:
        active = store.all("SELECT id FROM tasks WHERE status='active' AND owner=?", (actor,))
        if len(active) == 1:
            return active[0]["id"]
    return None


def slug(title: str, n: int = 32) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:n].strip("-") or "task"


def note(store: Store, ref, text: str) -> dict:
    t = get(store, ref)
    store.x("INSERT INTO task_notes (task, ts, author, text) VALUES (?,?,?,?)", (t["id"], now(), author(), text))
    store.x("UPDATE tasks SET updated=? WHERE id=?", (now(), t["id"]))
    return t


def notes(store: Store, ref, limit: int | None = None) -> list[dict]:
    n = parse_id(ref)
    rows = store.all("SELECT * FROM task_notes WHERE task=? ORDER BY id DESC" + (" LIMIT ?" if limit else ""),
                     (n, limit) if limit else (n,))
    return [dict(r) for r in reversed(rows)]


def add(store: Store, title: str, body: str = "", priority: int = 0, depends=()) -> dict:
    deps = [parse_id(d) for d in depends or []]
    for d in deps:
        get(store, d)
    n = store.x("INSERT INTO tasks (title, body, priority, depends, created, created_by, updated) VALUES (?,?,?,?,?,?,?)",
                (title.strip(), body or None, priority, dumps(deps) if deps else None, now(), author(), now())).lastrowid
    return get(store, n)


def edit(store: Store, ref, *, title=None, body=None, priority=None, depends=None) -> dict:
    t = get(store, ref)
    sets, params = [], []
    for col, val in (("title", title), ("body", body), ("priority", priority)):
        if val is not None:
            sets.append(f"{col}=?")
            params.append(val)
    if depends is not None:
        deps = [parse_id(d) for d in depends]
        if t["id"] in deps:
            raise KxError("a task cannot depend on itself")
        sets.append("depends=?")
        params.append(dumps(deps) if deps else None)
    if not sets:
        raise KxError("nothing to change", fix="pass --title, --body, --priority or --depends")
    store.x(f"UPDATE tasks SET {', '.join(sets)}, updated=? WHERE id=?", (*params, now(), t["id"]))
    return get(store, t["id"])


def blockers(store: Store, t: dict) -> list[str]:
    """Dependencies not done yet."""
    out = []
    for d in t["depends"]:
        row = store.one("SELECT status FROM tasks WHERE id=?", (d,))
        if row is None or row["status"] != "done":
            out.append(f"T{d} ({row['status'] if row else 'missing'})")
    return out


def start(store: Store, ref, *, owner: str | None = None) -> dict:
    """Mark a task active, owned by `owner` (default: whoever runs this); runs they start are linked to it."""
    t = get(store, ref)
    if t["status"] in ("done", "dropped"):
        raise KxError(f"T{t['id']} is {t['status']}", fix=f"kx task reopen T{t['id']}")
    who = owner or author()
    if t["status"] == "active" and t["owner"] and t["owner"] != who:
        raise KxError(f"T{t['id']} is active with {t['owner']}", fix=f"kx task show T{t['id']}")
    store.x("UPDATE tasks SET status='active', owner=?, waiting_on=NULL, updated=? WHERE id=?", (who, now(), t["id"]))
    return get(store, t["id"])


def make_worktree(store: Store, t: dict) -> tuple[Path, str]:
    """<repo>/.kx/worktrees/T<n>-<slug> on branch task/T<n>-<slug>, from the main checkout's HEAD (kx spawn)."""
    from kx.ledger.config import worktrees_dir
    main = store.comp.main_root
    name = f"T{t['id']}-{slug(t['title'])}"
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
    store.x("UPDATE tasks SET status='waiting', waiting_on=?, updated=? WHERE id=?", (on, now(), t["id"]))
    if text:
        note(store, t["id"], text)
    return get(store, t["id"])


def finish(store: Store, ref, result: str, outcome: str) -> dict:
    t = get(store, ref)
    if result not in RESULTS:
        raise KxError(f"result must be one of {', '.join(RESULTS)}")
    if not outcome.strip():
        raise KxError("an outcome is required: what was learned, including negative results")
    store.x("UPDATE tasks SET status='done', result=?, outcome=?, waiting_on=NULL, updated=? WHERE id=?",
            (result, outcome.strip(), now(), t["id"]))
    return get(store, t["id"])


def reopen(store: Store, ref) -> dict:
    t = get(store, ref)
    store.x("UPDATE tasks SET status='open', owner=NULL, waiting_on=NULL, updated=? WHERE id=?", (now(), t["id"]))
    return get(store, t["id"])


def listing(store: Store, all_: bool = False) -> list[dict]:
    where = "" if all_ else f" WHERE status IN ({','.join('?' * len(OPEN))})"
    rows = store.all("SELECT * FROM tasks" + where + " ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'waiting' THEN 1 "
                     "WHEN 'open' THEN 2 ELSE 3 END, priority DESC, id", () if all_ else OPEN)
    out = []
    for r in rows:
        d = dict(r)
        d["depends"] = loads(d["depends"]) or []
        d["blocked_by"] = blockers(store, d) if d["status"] in OPEN else []
        last = store.one("SELECT ts, text FROM task_notes WHERE task=? ORDER BY id DESC LIMIT 1", (d["id"],))
        d["last_note"] = dict(last) if last else None
        out.append(d)
    return out


def waiting_resolved(store: Store, t: dict) -> str | None:
    """Why a waiting task can go on now (what it waited for has happened), or None."""
    on = t.get("waiting_on") or ""
    m = re.search(r"\bT(\d+)\b", on)
    if m and on.startswith(("task:", "T")):
        r = store.one("SELECT status FROM tasks WHERE id=?", (int(m.group(1)),))
        if r and r["status"] in ("done", "dropped"):
            return f"T{m.group(1)} is {r['status']}"
    m = re.search(r"run:r?(\d+)", on)
    if m:
        r = store.one("SELECT status FROM runs WHERE id=?", (int(m.group(1)),))
        if r and r["status"] != "running":
            return f"run r{int(m.group(1)):04d} is {r['status']}"
    m = re.search(r"submission:S?(\d+)", on)
    if m:
        r = store.one("SELECT status, public FROM submissions WHERE id=?", (int(m.group(1)),))
        if r and (r["status"] in ("complete", "error") or r["public"] is not None):
            return f"S{m.group(1)} is {r['status']}"
    return None


def merge(store: Store, ref) -> str:
    """Merge the task branch into the main checkout's current branch (no fast-forward) and remove its worktree."""
    t = get(store, ref)
    if not t["branch"]:
        raise KxError(f"T{t['id']} has no branch", fix="it was worked on in the main checkout: nothing to merge")
    main = store.comp.main_root
    g = git.info(main)
    if g and [c for c in g["changes"] if not c.endswith((" data", " data/"))]:
        raise KxError(f"{main} has uncommitted changes", fix="commit or stash them first")
    wt = Path(t["worktree"]) if t["worktree"] else None
    if wt and wt.exists():
        w = git.info(wt)
        if w and w["changes"]:
            raise KxError(f"{wt} has uncommitted changes", fix=f"commit them on {t['branch']} first")
    r = subprocess.run(["git", "merge", "--no-ff", "-m", f"Merge T{t['id']}: {t['title']}", t["branch"]], cwd=main,
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
    store.x("UPDATE tasks SET worktree=NULL, updated=? WHERE id=?", (now(), t["id"]))
    return out.strip()
