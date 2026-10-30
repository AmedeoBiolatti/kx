"""A project's rules on where its runs happen and where its files live (comp.yaml, so they hold on every machine):

    runs:
      on: [colab, lambda]     # only these hosts; kx exec without --on uses the first
    storage:
      keep: here              # remote runs leave nothing on their host: every file comes back, then the host's copy goes
      max_gb: 200             # the store here (runs, artifacts, data, backups)

A rule refuses up front, like a budget (exit 4), with what to do instead. Like the rest of kx it guards against
mistakes: a command run without kx is not stopped.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from kx.ledger.config import KxError

GB = 2**30
SIZE_CACHE_S = 600  # the store's size is measured at most this often: on a slow disk a big store takes 10-30 s
SIZE_FILE = ".kx-size.json"  # in the store: the last measure, shared by every kx process
KEEPS = ("host", "here")


def validate(runs: dict, storage: dict, where) -> None:
    on = runs.get("on")
    if on is not None and (not isinstance(on, list) or not on or not all(isinstance(h, str) and h for h in on)):
        raise KxError(f"{where}: runs.on must be a list of host names", fix="runs: {on: [colab, lambda]}")
    if unknown := sorted(set(runs) - {"on"}):
        raise KxError(f"{where}: unknown runs option(s) {', '.join(unknown)}", fix="runs: {on: [HOST, ...]}")
    if storage.get("keep", "host") not in KEEPS:
        raise KxError(f"{where}: storage.keep must be here or host", fix="storage: {keep: here}")
    cap = storage.get("max_gb")
    if cap is not None and (not isinstance(cap, (int, float)) or isinstance(cap, bool) or cap <= 0):
        raise KxError(f"{where}: storage.max_gb must be a positive number (GB)")
    if unknown := sorted(set(storage) - {"keep", "max_gb"}):
        raise KxError(f"{where}: unknown storage option(s) {', '.join(unknown)}", fix="storage: {keep: here, max_gb: 200}")


# -- where runs happen -------------------------------------------------------------------------------------------------

def run_hosts(comp) -> list[str]:
    return list(comp.runs.get("on") or [])


def default_host(comp) -> str | None:
    """The host a run goes to when none is given: the first of runs.on."""
    on = run_hosts(comp)
    return on[0] if on else None


def check_host(comp, host: str | None) -> None:
    """Refuse a run here (host None) or on a host outside runs.on."""
    on = run_hosts(comp)
    if not on or host in on:
        return
    where = f"on {host}" if host else "on this machine"
    raise KxError(f"this project's runs happen on {', '.join(on)} (comp.yaml runs.on), not {where}", code=4,
                  fix=f"kx exec --on {on[0]} -- ...  (Python: run the script under it, kx.run attaches)")


# -- where files live --------------------------------------------------------------------------------------------------

def keep_here(comp) -> bool:
    return comp.storage.get("keep") == "here"


def cap_bytes(comp) -> int | None:
    cap = comp.storage.get("max_gb")
    return int(cap * GB) if cap else None


def tree_size(path: Path) -> int:
    """Bytes under path (apparent size, symlinks not followed)."""
    total, stack = 0, [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _measure(path: Path) -> int:
    if shutil.which("du"):  # several times faster than walking it from Python
        try:
            p = subprocess.run(["du", "-sb", str(path)], capture_output=True, text=True, timeout=600)
            if p.returncode == 0 and p.stdout.split():
                return int(p.stdout.split()[0])
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return tree_size(path)


def store_size(comp, fresh: bool = False) -> int:
    """The store's size in bytes: measured at most every SIZE_CACHE_S (or now, with fresh)."""
    f = comp.store / SIZE_FILE
    if not fresh:
        try:
            hit = json.loads(f.read_text())
            if time.time() - hit["at"] < SIZE_CACHE_S:
                return int(hit["bytes"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    n = _measure(comp.store)
    try:
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"bytes": n, "at": time.time()}))
        tmp.replace(f)
    except OSError:
        pass
    return n


def forget_size(comp) -> None:
    """After deleting (kx gc): the next look measures again."""
    (comp.store / SIZE_FILE).unlink(missing_ok=True)


def largest_runs(comp, n: int = 5) -> list[tuple[str, int]]:
    runs = [(d.name, tree_size(d)) for d in comp.runs_dir.iterdir() if d.is_dir()] if comp.runs_dir.exists() else []
    return sorted(runs, key=lambda x: -x[1])[:n]


def room(comp) -> int | None:
    """Bytes the store may still take (None: no cap)."""
    cap = cap_bytes(comp)
    return None if cap is None else cap - store_size(comp)


def check_room(comp, need: int = 0, what: str = "this") -> None:
    """Refuse what would take the store over storage.max_gb (need: bytes it adds, when known)."""
    cap = cap_bytes(comp)
    if cap is None:
        return
    used = store_size(comp)
    if used + need <= cap and used < cap:
        return
    used = store_size(comp, fresh=True)  # about to refuse: on a measure of now, not of minutes ago
    if used + need <= cap and used < cap:
        return
    big = ", ".join(f"{r} {b / GB:.1f} GB" for r, b in largest_runs(comp) if b >= GB // 10)
    raise KxError(f"the store would go over its cap: {used / GB:.1f} GB used"
                  + (f" + {need / GB:.1f} GB for {what}" if need else f", refusing {what}") + f" > {cap / GB:g} GB "
                  "(comp.yaml storage.max_gb)" + (f"\n  largest runs: {big}" if big else ""), code=4,
                  fix="kx gc (frees the files of discarded and old failed runs), or raise storage.max_gb")


def usage(comp) -> dict | None:
    cap = cap_bytes(comp)
    if cap is None:
        return None
    used = store_size(comp)
    return {"used_gb": round(used / GB, 2), "max_gb": cap / GB, "left_gb": round((cap - used) / GB, 2)}
