"""Ledgers of kx before the logs (<store>/ledger.sqlite, numbered rows) into this machine's history, once.

Every row becomes a log entry at the time it was made, in <log>/<user>@<machine>+import-<id>.jsonl. Runs, tasks,
findings and submissions keep their number and get a name (the same name each time the same ledger is read):
r0012 is r0012-fluent-sun. Importing this store's own old ledger renames the run folders to the new ids; another
store's runs give their kx files (logs, curves, tables) and its submitted files.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import socket
import sqlite3
from pathlib import Path

from kx.ledger import names
from kx.ledger.config import KxError
from kx.ledger.store import ENTITIES, Store, author, dumps, entity_key, loads, now, pid_alive


def find(path: Path) -> Path | None:
    """The old ledger in a file, a store folder or a competition repo."""
    path = Path(path).expanduser()
    if path.is_file():
        return path
    if (path / "comp.yaml").is_file():
        from kx.ledger import config
        path = config.load(path).store
    return path / "ledger.sqlite" if (path / "ledger.sqlite").is_file() else None


def _seed(src, slug: str) -> str:
    first = []
    for t, col in (("runs", "created"), ("tasks", "created"), ("findings", "ts")):
        try:
            row = src.execute(f"SELECT MIN({col}) FROM {t}").fetchone()
            first.append(str(row[0]))
        except sqlite3.Error:
            first.append("")
    return f"{slug}|" + "|".join(first)


def import_old(store: Store, db: Path) -> dict:
    db = Path(db)
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        have = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "runs" not in have or "n" in {c[1] for c in src.execute("PRAGMA table_info(runs)")}:
            raise KxError(f"{db} is not a ledger of kx before the logs")
        seed = _seed(src, store.comp.slug)
        tag = hashlib.sha256(seed.encode()).hexdigest()[:8]
        log = store.comp.log_dir / f"{store.writer}+import-{tag}.jsonl"
        if any(store.comp.log_dir.glob(f"*+import-{tag}.jsonl")):
            raise KxError(f"{db} is already imported here ({next(store.comp.log_dir.glob(f'*+import-{tag}.jsonl')).name})")
        in_place = db.parent.resolve() == store.comp.store.resolve()
        rows = {t: [dict(r) for r in src.execute(f"SELECT * FROM {t}" + (" ORDER BY id" if t in ENTITIES else ""))]
                for t in ("runs", "protocols", "scores", "notes", "tags", "findings", "finding_tags", "tasks", "task_notes",
                          "artifacts", "submissions", "submission_episodes", "submission_scores", "pushes", "lb_snapshots",
                          "usage", "settings") if t in have}
    finally:
        src.close()
    if in_place:  # a run going on here writes to its folder, which the import renames
        alive = [f"r{r['id']:04d}" for r in rows["runs"] if r["status"] == "running"
                 and r["host"] == socket.gethostname() and pid_alive(r["pid"], r["pid_start"])
                 and not (db.parent / "runs" / f"r{r['id']:04d}" / "remote.json").exists()]
        if alive:
            raise KxError(f"{', '.join(alive)} still running here: the import renames run folders",
                          fix="wait for them to end (or stop them), then import")
    keys: dict[str, dict[int, str]] = {}
    for t in ENTITIES:
        used = {r[0] for r in store.all(f"SELECT name FROM {t}")}
        keys[t] = {}
        for r in rows.get(t, []):
            name = names.stable(f"{seed}|{t}|{r['id']}", used)
            used.add(name)
            keys[t][r["id"]] = entity_key(t, r["id"], name)

    def ref(t, v):
        return keys[t].get(v) if isinstance(v, int) else v

    def text_refs(v):
        """run:r12, T3, task:T3, submission:S4 in what a task waits on; r0012 and S4 as a usage ref."""
        def sub(pat, t, text):  # groups: what stays before, the old ref (kept when unknown), its number
            return re.sub(pat, lambda m: m.group(1) + keys[t].get(int(m.group(3)), m.group(2)), text)
        if not v:
            return v
        v = sub(r"(\brun:)(r?(\d+))\b", "runs", v)
        v = sub(r"(\bsubmission:)(S?(\d+))\b", "submissions", v)
        v = sub(r"(^(?:task:)?)(T(\d+))\b", "tasks", v)
        v = sub(r"(^)(r0*(\d+))$", "runs", v)
        return sub(r"(^)(S(\d+))$", "submissions", v)

    entries = []

    def add(t, row, at, by=None):
        entries.append({"at": at or now(), "by": by or author(), "t": t, "op": "ins", "row": row})

    def entity(t, r):
        return {**{k: v for k, v in r.items() if k != "uid"}, "id": keys[t][r["id"]], "n": r["id"],
                "name": keys[t][r["id"]].split("-", 1)[1], "writer": store.writer}

    runs_at = {}
    for r in rows.get("runs", []):
        d = entity("runs", r) | {"task": ref("tasks", r["task"]), "parent": ref("runs", r["parent"])}
        runs_at[r["id"]] = r["created"]
        add("runs", d, r["created"], r["author"])
    for r in rows.get("protocols", []):
        add("protocols", r, r["created"])
    for r in rows.get("scores", []):
        if r["run_id"] in keys["runs"]:
            add("scores", r | {"run_id": keys["runs"][r["run_id"]]}, r["created"])
    for r in rows.get("notes", []):
        if r["run_id"] in keys["runs"]:
            add("notes", {k: v for k, v in r.items() if k != "id"} | {"run_id": keys["runs"][r["run_id"]]}, r["ts"], r["author"])
    for r in rows.get("tags", []):
        if r["run_id"] in keys["runs"]:
            add("tags", {"run_id": keys["runs"][r["run_id"]], "tag": r["tag"]}, runs_at.get(r["run_id"]))
    fnd_at = {}
    for r in rows.get("findings", []):
        ev = loads(r["evidence"]) or []
        d = entity("findings", r) | {"evidence": dumps([ref("runs", e) for e in ev]), "task": ref("tasks", r["task"]),
                                     "superseded_by": ref("findings", r["superseded_by"])}
        fnd_at[r["id"]] = r["ts"]
        add("findings", d, r["ts"], r["author"])
    for r in rows.get("finding_tags", []):
        if r["finding_id"] in keys["findings"]:
            add("finding_tags", {"finding_id": keys["findings"][r["finding_id"]], "tag": r["tag"]}, fnd_at.get(r["finding_id"]))
    for r in rows.get("tasks", []):
        deps = loads(r["depends"]) or []
        d = entity("tasks", r) | {"depends": dumps([ref("tasks", x) for x in deps]) if deps else None,
                                  "waiting_on": text_refs(r["waiting_on"])}
        add("tasks", d, r["created"], r["created_by"])
    for r in rows.get("task_notes", []):
        if r["task"] in keys["tasks"]:
            add("task_notes", {k: v for k, v in r.items() if k != "id"} | {"task": keys["tasks"][r["task"]]}, r["ts"], r["author"])
    for r in rows.get("artifacts", []):
        if r["run_id"] in keys["runs"]:
            m = re.search(r"(artifacts/[0-9a-f]+/[^/]+)$", r["path"] or "")
            add("artifacts", r | {"run_id": keys["runs"][r["run_id"]], "path": m.group(1) if m else r["path"]},
                r["created"], r["actor"])
    sub_at = {}
    for r in rows.get("submissions", []):
        d = entity("submissions", r) | {"run_id": ref("runs", r["run_id"]), "task": ref("tasks", r["task"])}
        sub_at[r["id"]] = r["created"]
        add("submissions", d, r["created"], r["actor"])
    for t in ("submission_episodes", "submission_scores"):
        for r in rows.get(t, []):
            if r["submission_id"] in keys["submissions"]:
                add(t, r | {"submission_id": keys["submissions"][r["submission_id"]]},
                    r.get("created") or r.get("ts") or sub_at.get(r["submission_id"]))
    for r in rows.get("pushes", []):
        if r["run_id"] in keys["runs"]:
            add("pushes", {k: v for k, v in r.items() if k != "id"} | {"run_id": keys["runs"][r["run_id"]]}, r["created"])
    for r in rows.get("lb_snapshots", []):
        add("lb_snapshots", r, r["ts"])
    for r in rows.get("usage", []):
        add("usage", r | {"ref": text_refs(r["ref"])}, r["ts"], r["actor"])
    for r in rows.get("settings", []):
        if r["key"] == "reference":
            add("settings", r, now())
    entries.sort(key=lambda e: e["at"])  # the import's own order is the history's
    n = store.log_to(log, entries)
    folders = 0
    old_runs = db.parent / "runs"
    for old, key in keys["runs"].items():
        d = old_runs / f"r{old:04d}"
        if not d.is_dir():
            continue
        if in_place:
            d.rename(store.run_dir(key))
        else:
            from kx.ledger.sync import records
            for rel in records(d):
                q = store.run_dir(key) / rel
                q.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(d / rel, q)
        folders += 1
    if not in_place:
        from kx.ledger.sync import copy_artifacts
        copy_artifacts(store, db.parent / "artifacts")
    counts = {t: len(keys[t]) for t in ENTITIES if keys[t]}
    return {"source": str(db), "log": log.name, "entries": n, "rows": len(entries), "ids": counts,
            "run_folders": folders, "in_place": in_place}


def summary(res: dict) -> str:
    what = ", ".join(f"{n} {t}" for t, n in res["ids"].items())
    return (f"imported {res['source']}: {what} ({res['rows']} rows) into {res['log']}; "
            f"{res['run_folders']} run folders {'renamed' if res['in_place'] else 'copied (kx files only)'}")

