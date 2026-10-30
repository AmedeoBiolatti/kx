"""kx gc: free the store by deleting the files of runs nobody needs: those tagged discarded, failed ones older than a
few days, or the runs named. What kx recorded stays: the ledger (hypothesis, config, scores, notes), the run's own
records (curves, scores.json, notes, progress, logged tables, the code of a probe) and the end of its logs. Only what
the run made goes, here and on its host (for the runs this machine launched). Runs a submission or a push came from,
and the best run, are kept whole.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kx.ledger.store import Store, parse_ts

KEEP_FILES = {"metrics.jsonl", "progress.json", "notes.jsonl", "scores.json", "logged_metrics.json", "attach.json",
              "objects.jsonl", "remote.json", "run.sh", "job.json"}   # kx's records of the run, at its top level
KEEP_DIRS = {"code", "objects", ".remote", "env"}
LOGS = ("stdout.log", "stderr.log")
LOG_TAIL = 256 * 1024  # what is left of a log


def _protected(store: Store) -> dict[str, str]:
    from kx.runs.protocol import best_run
    out = {r["run_id"]: f"{r['id']} was submitted from it" for r in
           store.all("SELECT id, run_id FROM submissions WHERE run_id IS NOT NULL")}
    out.update({r["run_id"]: f"a {r['kind']} was pushed from it" for r in
                store.all("SELECT run_id, kind FROM pushes") if r["run_id"] not in out})
    best = best_run(store)
    if best is not None:
        out.setdefault(best, "the best run under the reference protocol")
    return out


def _freeable(d: Path) -> tuple[list[Path], int]:
    """The paths gc deletes in a run dir, and the bytes that frees (the logs' part beyond their tail included)."""
    from kx.guard.rules import tree_size
    paths, total = [], 0
    if not d.is_dir():
        return [], 0
    for e in d.iterdir():
        if e.name in KEEP_FILES or (e.is_dir() and e.name in KEEP_DIRS):
            continue
        if e.name in LOGS:
            total += max(0, e.stat().st_size - LOG_TAIL)
            continue
        paths.append(e)
        total += tree_size(e) if e.is_dir() and not e.is_symlink() else e.lstat().st_size
    return paths, total


def plan(store: Store, runs=(), failed_days: float = 7) -> dict:
    """What gc would delete: {"runs": [{run, why, bytes}], "kept": [{run, why}], "bytes"}."""
    protected = _protected(store)
    picked: dict[str, str] = {}
    if runs:
        for ref in runs:
            picked[store.resolve(ref)] = "named"
    else:
        for r in store.all("SELECT run_id FROM tags WHERE tag='discarded'"):
            picked[r["run_id"]] = "discarded"
        cutoff = datetime.now(timezone.utc) - timedelta(days=failed_days)
        for r in store.all("SELECT id, finished FROM runs WHERE status='failed'"):
            t = parse_ts(r["finished"])
            if t and t < cutoff:
                picked.setdefault(r["id"], f"failed {(datetime.now(timezone.utc) - t).days} days ago")
    out, kept = [], []
    for run_id, why in sorted(picked.items()):
        row = store.get_run(run_id)
        if row["status"] == "running":
            kept.append({"run": run_id, "why": "still running"})
        elif run_id in protected:
            kept.append({"run": run_id, "why": protected[run_id]})
        else:
            paths, n = _freeable(store.run_dir(run_id))
            from kx.remote.execute import info, ours
            inf = info(store, run_id) or {}  # a host copy goes only with the machine that launched the run
            on_host = inf.get("host_copy") != "removed" and bool(inf.get("on_host")) and ours(store, row)
            if n or on_host:
                out.append({"run": run_id, "why": why, "bytes": n, "files": len(paths),
                            **({"on_host": inf["host"]} if on_host else {})})
    return {"runs": out, "kept": kept, "bytes": sum(r["bytes"] for r in out)}


def collect(store: Store, p: dict) -> dict:
    """Delete what plan() listed. Returns {"freed", "runs", "host_errors"}."""
    from kx.guard import rules
    from kx.remote import execute
    freed, errors = 0, []
    for item in p["runs"]:
        run_id = store.resolve(item["run"])
        d = store.run_dir(run_id)
        paths, n = _freeable(d)
        for path in paths:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        for name in LOGS:
            log = d / name
            if log.exists() and log.stat().st_size > LOG_TAIL:
                with log.open("rb") as f:
                    f.seek(-LOG_TAIL, os.SEEK_END)
                    tail = f.read()
                log.write_bytes(b"[kx gc: the start of this log was deleted]\n" + tail)
        freed += n
        host_gone = False
        if item.get("on_host"):
            inf = execute.info(store, run_id)
            try:
                execute._host(inf).transport.remove(inf)
                inf.update(host_copy="removed", on_host=[])
                execute._save(d, inf)
                host_gone = True
            except Exception as e:  # noqa: BLE001 - an unreachable host keeps its copy; gc goes on
                errors.append(f"{item['run']} on {inf['host']}: {str(e).splitlines()[0]}")
        store.add_note(run_id, f"kx gc: deleted {item['files']} file(s) of what it made, {n / 2**20:,.0f} MB ({item['why']})"
                       + (f", and its copy on {item['on_host']}" if host_gone else ""))
    rules.forget_size(store.comp)
    return {"freed": freed, "runs": [r["run"] for r in p["runs"]], "host_errors": errors}
