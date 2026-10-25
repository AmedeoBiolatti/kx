"""Sharing a project's ledger between machines and people: `kx sync` through a folder every machine reaches with
rclone (Google Drive for now; any rclone remote works: S3, a server over sftp, a local disk).

    <share>/<slug>/log/<user>@<machine>.jsonl     each machine's log: only that machine writes it
    <share>/<slug>/records/<user>@<machine>/<run>.tar.gz   the kx files of each of its runs (logs, curves, tables)
                                                          and the files it submitted

A sync puts this machine's logs there and takes everyone else's: logs only grow and each has one writer, so nothing is
ever merged or overwritten, and no copy replaces a longer one (a machine that lost its store gets its log back); the
cache is brought up to them. Run outputs stay where they were made (remote runs' on
their host: kx fetch); `kx export` / `kx import` move the same history as one file.

    share: gdrive:kx      # ~/.config/kx/machine.yaml; without it, the folder of the mailbox hosts, if there is one
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from kx.ledger.config import KxError
from kx.ledger.store import Store, age_s, complete, now, read_log

RUN_FILES = ("stdout.log", "stderr.log", "metrics.jsonl", "progress.json", "remote.json", "run.sh", "provenance.json",
             "packages.txt")
RUN_DIRS = ("env", "objects", "code", ".remote")
MAX_FILE = 32 * 2**20  # a runaway log stays behind
LOST_KEEP_S = 3 * 86400  # how long kx status says a claim was lost


def location(comp) -> str | None:
    """<share>/<slug>, or None when this machine shares nothing."""
    from kx import sandbox
    from kx.remote import hosts
    from kx.remote.agent import join
    root = sandbox.machine().get("share")
    if not root:
        boxes = sorted({h.mailbox for h in hosts.load(comp).values() if h.kind == "mailbox"})
        root = boxes[0] if len(boxes) == 1 else None
    return join(root, comp.slug) if root else None


def _rclone(*args, **kw):
    from kx.remote.mailbox import rclone
    return rclone(*args, **kw)


def records(d: Path) -> list[str]:
    """A run's own records, relative to its folder: what kx wrote there."""
    rels = [*RUN_FILES, *(p.relative_to(d).as_posix() for n in RUN_DIRS if (d / n).is_dir() for p in sorted((d / n).rglob("*")))]
    return [r for r in rels if (d / r).is_file() and not (d / r).is_symlink() and (d / r).stat().st_size <= MAX_FILE]


def run_records(store: Store, run_id: str) -> list[tuple[Path, str]]:
    """(file, path in the archive) for one run: its kx files and the files it submitted."""
    d = store.run_dir(run_id)
    out = [(d / rel, f"runs/{run_id}/{rel}") for rel in (records(d) if d.is_dir() else [])]
    for a in store.all("SELECT path FROM artifacts WHERE run_id=?", (run_id,)):
        p = store.comp.store / a["path"]
        if p.is_file() and p.stat().st_size <= MAX_FILE:
            out.append((p, a["path"]))
            if (p.parent / "manifest.json").is_file():
                out.append((p.parent / "manifest.json", str(Path(a["path"]).parent / "manifest.json")))
    return out


def own_runs(store: Store) -> list:
    """This machine's runs (under its name now and the ones it had): id, writer."""
    return store.all(f"SELECT id, writer FROM runs WHERE writer IN ({','.join('?' * len(store.writers))}) ORDER BY n",
                     store.writers)


def own_records(store: Store) -> list[tuple[Path, str]]:
    """(file, path in the archive) for this machine's runs and the files they submitted."""
    return [f for r in own_runs(store) for f in run_records(store, r["id"])]


def pack(dest: Path, files: list[tuple[Path, str]]) -> Path:
    with tarfile.open(dest, "w:gz") as tf:
        for p, arc in files:
            tf.add(p, arc)
    return dest


def _extract(archive: Path, dest: Path):
    with tarfile.open(archive) as tf:
        try:
            tf.extractall(dest, filter="data")
        except TypeError:  # a Python without extraction filters (before 3.10.12 / 3.11.4)
            tf.extractall(dest)


def take_records(store: Store, root: Path, writer: str | None = None, *, own: bool = False) -> int:
    """Another machine's run records (root/runs/<run>/, root/artifacts/) into this store: never over this machine's
    runs (own: this machine's own records back, for runs whose folder is gone), never over a file already in
    artifacts/ (they are content-addressed)."""
    n = 0
    for d in sorted((root / "runs").iterdir()) if (root / "runs").is_dir() else []:
        key = store.local_id(d.name, writer) if writer else d.name
        row = store.one("SELECT writer FROM runs WHERE id=?", (key,))
        if row is None or store.mine(row["writer"] or "") and not (own and not store.run_dir(key).exists()):
            continue
        dest = store.run_dir(key)
        for p in sorted(d.rglob("*")):
            if p.is_file():
                q = dest / p.relative_to(d)
                q.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, q)
        n += 1
    copy_artifacts(store, root / "artifacts")
    return n


def copy_artifacts(store: Store, src: Path):
    """Submitted files (content-addressed: <sha12>/<name>) into this store, where missing."""
    for p in sorted(src.rglob("*")) if src.is_dir() else []:
        q = store.comp.store / "artifacts" / p.relative_to(src)
        if p.is_file() and not q.exists():
            q.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, q)


def _digest(files: list[tuple[Path, str]]) -> str:
    h = hashlib.sha256()
    for p, arc in files:
        st = p.stat()
        h.update(f"{arc} {st.st_size} {st.st_mtime_ns}\n".encode())
    return h.hexdigest()[:16]


def _sha(p: Path, n: int | None = None) -> str:
    """sha256 of a file, or of its first n bytes."""
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while n is None or n > 0:
            b = f.read(1 << 20 if n is None else min(1 << 20, n))
            if not b:
                break
            h.update(b)
            if n is not None:
                n -= len(b)
    return h.hexdigest()


def _starts(a: Path, b: Path) -> bool:
    """Whether b begins with all of a's complete lines: a log b that only grew from a (a line a was still writing,
    b may have otherwise)."""
    n = complete(a)
    return n <= b.stat().st_size and _sha(a, n) == _sha(b, n)


def _common(a: Path, b: Path) -> int:
    """How many bytes of complete lines two logs start with alike."""
    x, y = a.read_bytes(), b.read_bytes()
    n = 0
    for la, lb in zip(x.split(b"\n")[:-1], y.split(b"\n")[:-1]):
        if la != lb:
            break
        n += len(la) + 1
    return n


def _after(p: Path, n: int) -> list[dict]:
    return [e for _, e in read_log(p, n)[0]]


def _put(src: Path, dest: Path):
    tmp = dest.with_name(dest.name + ".part")
    shutil.copy2(src, tmp)
    os.replace(tmp, dest)


def _own_logs(store: Store, loc: str, state: dict) -> list[str]:
    """Before this machine's logs go up, each one in the shared folder must be where the one here started: logs only
    grow. One here that is missing or behind (the store was deleted, a backup restored) takes the shared one; one that
    went another way (lost, then written to before a sync) is kept beside it as <writer>+fork-<time>, so neither
    history is lost (an entry in both is applied once). The shared log of a name this machine had (machine.yaml
    name: changed) that grew elsewhere is another machine's: what this one wrote under it and never sent moves to
    its name now. Two machines writing as one name at once: refused. Returns what it did; the cache is rebuilt when
    it did anything."""
    logs = store.comp.log_dir
    p = _rclone("lsf", f"{loc}/log", "--files-only", "--format", "ps",
                *(f"--include={w}{x}" for w in store.writers for x in (".jsonl", "+*.jsonl")), check=False)
    if p.returncode != 0:
        if "not found" in p.stderr:
            return []  # nothing shared yet
        raise KxError(f"rclone lsf {loc}/log: {p.stderr.strip()[-600:]}", fix="rclone config  (is the remote set up?)")
    sent, done, back = state.setdefault("sent", {}), [], []
    staging = store.comp.store / ".share" / "own"
    for line in sorted(p.stdout.splitlines()):
        name, _, size = line.strip().rpartition(";")
        if not name.endswith(".jsonl"):
            continue
        size, here, last = int(size), logs / name, sent.get(name)
        if last and last["size"] == size and here.exists() and here.stat().st_size >= size and _sha(here, size) == last["sha"]:
            continue  # what this machine sent last is still there, and the log here goes on from it
        staging.mkdir(parents=True, exist_ok=True)
        got = staging / name
        _rclone("copyto", f"{loc}/log/{name}", str(got), timeout=1800)
        if here.exists() and _starts(got, here):
            got.unlink()
        else:
            back.append((name, got))
    if not back:
        return done
    with store.transaction():  # no process here appends to a log meanwhile, and none reads the cache half rebuilt
        for name, got in back:
            here, stem = logs / name, name[:-len(".jsonl")]
            if here.exists() and complete(here) and not _starts(here, got):
                n = _common(here, got)
                ours, theirs = _after(here, n), _after(got, n)
                if stem in store.former:
                    done.append(_rehome(store, here, got, n, state))
                    continue
                if stem == store.writer and ours and theirs and \
                        max(str(e.get("at")) for e in theirs) > min(str(e.get("at")) for e in ours):
                    raise KxError(f"another machine writes as {stem}: the shared log and the one here both grew since "
                                  f"they parted (this one since {min(str(e.get('at')) for e in ours)})",
                                  fix="give this machine its own name: name: <name> in ~/.config/kx/machine.yaml, then "
                                      "kx sync (what it wrote moves to that name)")
                fork = logs / f"{stem}+fork-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl"
                here.rename(fork)
                done.append(f"{name} here went another way than the shared one (was this store lost and used again "
                            f"before a sync?): the shared one is back, the one here is kept as {fork.name}")
            else:
                done.append(f"{name}: taken back from the shared folder (the one here was "
                            f"{'behind' if here.exists() and here.stat().st_size else 'missing'})")
            _put(got, here)
            got.unlink()
        store.rebuild()
    return done


def _moved(e: dict, old: str, new: str) -> dict:
    """An entry written under the name `old`, as `new` would have: the writer, and the owners on that machine."""
    oh, nh = old.split("@", 1)[1], new.split("@", 1)[1]

    def fix(v):
        if v == old:
            return new
        if isinstance(v, str) and re.fullmatch(rf"[^@\s]+@{re.escape(oh)}", v):
            return v[: -len(oh)] + nh
        return v
    return {**e, **{part: {k: fix(v) for k, v in e[part].items()} for part in ("row", "set", "where")
                    if isinstance(e.get(part), dict)}}


def _rehome(store: Store, here: Path, got: Path, n: int, state: dict) -> str:
    """In a transaction: the log of a name this machine had grew elsewhere (another machine has that name). The
    entries this machine wrote under it after the two parted, never sent, go to its log now; the shared one is
    taken as that machine's."""
    old, new, cur = here.stem, store.writer, store.log_path
    moved = [_moved(e, old, new) for e in _after(here, n)]
    lines = "".join(json.dumps(e, default=str) + "\n" for e in moved)
    if cur.name in state.get("sent", {}):  # its log now is out already: they go after it, in a later time
        lines = "".join(json.dumps({**e, "c": store._tick()}, default=str) + "\n" for e in moved)
        with open(cur, "a") as f:
            f.write(lines)
    else:  # before it: they were written first
        rest = cur.read_text() if cur.exists() else ""
        tmp = cur.with_name(cur.name + ".part")
        tmp.write_text(lines + rest)
        os.replace(tmp, cur)
    _put(got, here)
    got.unlink()
    store.forget_name(old)
    state.get("sent", {}).pop(here.name, None)
    return (f"{old} is another machine's too: {len(moved)} entr{'y' if len(moved) == 1 else 'ies'} this machine "
            f"wrote under it moved to {new}")


def _upload(store: Store, loc: str, state: dict):
    """This machine's logs up, as they were at one moment: their complete lines, read under the lock (no line is
    being written, a half line a crash left is cut off first)."""
    out = store.comp.store / ".share" / "up"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    with store.transaction():
        for p in sorted(store.comp.log_dir.glob("*.jsonl")):
            if store.mine(p.stem):
                n = complete(p)
                with open(p, "rb") as f, open(out / p.name, "wb") as g:
                    g.write(f.read(n))
    _rclone("copy", str(out), f"{loc}/log", timeout=1800)
    for p in out.glob("*.jsonl"):
        state.setdefault("sent", {})[p.name] = {"size": p.stat().st_size, "sha": _sha(p)}
    shutil.rmtree(out, ignore_errors=True)


def _their_logs(store: Store, loc: str) -> tuple[list[str], list[str]]:
    """Everyone else's logs from the shared folder into log/, only where one goes on from the one here: a shorter or
    different copy (a stale upload, a machine that lost its store) never replaces what this machine has. Returns
    (writers pulled, warnings)."""
    logs = store.comp.log_dir
    inbox = store.comp.store / ".share" / "log"
    inbox.mkdir(parents=True, exist_ok=True)
    p = _rclone("copy", f"{loc}/log", str(inbox), *(f"--exclude={w}{x}" for w in store.writers for x in (".jsonl", "+*.jsonl")),
                check=False, timeout=1800)
    if p.returncode != 0 and "not found" not in p.stderr:
        raise KxError(f"rclone copy from {loc}/log: {p.stderr.strip()[-600:]}", fix="rclone config  (is the remote set up?)")
    pulled, warn = [], []
    for got in sorted(inbox.glob("*.jsonl")):
        if store.mine(got.stem):
            continue
        here = logs / got.name
        if not here.exists():
            _put(got, here)
            pulled.append(got.stem)
        elif got.stat().st_size > here.stat().st_size:
            if _starts(here, got):
                _put(got, here)
                pulled.append(got.stem)
            else:
                warn.append(f"the shared log of {got.stem} is not a continuation of the one here: kept this one "
                            f"(its machine's next kx sync puts its own history back)")
        elif got.stat().st_size < here.stat().st_size and not _starts(got, here):
            warn.append(f"the shared log of {got.stem} differs from the one here: kept this one")
    return pulled, warn


def _held(store: Store) -> dict[str, str]:
    """Tasks this machine's people and workers hold: {task: owner}."""
    here = tuple("@" + w.split("@", 1)[1] for w in store.writers)
    return {r["id"]: r["owner"] for r in store.all("SELECT id, owner FROM tasks WHERE status IN ('active', 'waiting') "
                                                    "AND owner IS NOT NULL") if r["owner"].endswith(here)}


def _lost(store: Store, before: dict[str, str]) -> list[dict]:
    """Claims of this machine that another machine's earlier claim (or a reopen) took, kept for kx status."""
    lost = []
    for task, was in before.items():
        r = store.one("SELECT owner, status FROM tasks WHERE id=?", (task,))
        if r is not None and r["owner"] != was:
            lost.append({"task": task, "was": was, "now": r["owner"], "status": r["status"], "at": now()})
    if lost:
        keep = [x for x in store.setting("lost_claims") or [] if (age_s(x["at"]) or 0) < LOST_KEEP_S]
        store.set_setting("lost_claims", keep + lost)
    return lost


def _records_up(store: Store, loc: str, state: dict) -> int:
    """This machine's run records, one archive per run (<share>/records/<writer>/<run>.tar.gz), only the runs whose
    files changed since the last sync. Returns how many went."""
    me, done = store.writer, state.setdefault("runs", {})
    with tempfile.TemporaryDirectory(prefix="kx-records-", dir=store.comp.store) as tmp:
        out = Path(tmp)
        for r in own_runs(store):  # each under the name it was made with: the id it has there
            files = run_records(store, r["id"])
            digest = _digest(files)
            if files and done.get(r["id"]) != digest:
                (out / r["writer"]).mkdir(exist_ok=True)
                pack(out / r["writer"] / f"{r['id']}.tar.gz", files)
                done[r["id"]] = digest
        sent = sorted(out.glob("*/*.tar.gz"))
        if sent:
            mb = sum(a.stat().st_size for a in sent) / 2**20
            print(f"kx: sending the records of {len(sent)} run(s) ({mb:.2g} MB)", file=sys.stderr)
            _rclone("copy", str(out), f"{loc}/records", timeout=1800)
    if not state.get("split"):  # the one archive of every run, from before: superseded
        _rclone("deletefile", f"{loc}/records/{me}.tar.gz", check=False)
        state["split"] = True
    return len(sent)


def _records_down(store: Store, loc: str, state: dict, *, own: bool = False) -> list[str]:
    """Everyone else's run records (and with `own`, this machine's, for runs whose folder is gone), each archive taken
    once per version."""
    inbox = store.comp.store / ".records"
    inbox.mkdir(exist_ok=True)
    for w in store.writers if own else ():
        _rclone("copy", f"{loc}/records/{w}", str(inbox / w), check=False, timeout=1800)
    p = _rclone("copy", f"{loc}/records", str(inbox), *(f"--exclude={w}{x}" for w in store.writers for x in (".tar.gz", "/**")),
                check=False, timeout=1800)
    if p.returncode != 0 and "not found" not in p.stderr:
        raise KxError(f"rclone copy from {loc}/records: {p.stderr.strip()[-600:]}")
    seen, down = state.get("seen") or {}, set()
    for a in sorted(inbox.glob("*.tar.gz")) + sorted(inbox.glob("*/*.tar.gz")):  # one archive (before), then per run
        rel = a.relative_to(inbox).as_posix()
        writer = rel.split("/", 1)[0] if "/" in rel else rel[: -len(".tar.gz")]
        mark = f"{a.stat().st_size}:{a.stat().st_mtime_ns}"
        mine_ = store.mine(writer)
        if seen.get(rel) == mark and not mine_ or mine_ and not own:
            continue
        with tempfile.TemporaryDirectory(prefix="kx-records-", dir=store.comp.store) as tmp:
            _extract(a, Path(tmp))
            take_records(store, Path(tmp), writer, own=mine_)
        if mine_:
            a.unlink()
        else:
            seen[rel] = mark
            down.add(writer)
    for w in store.writers if own else ():
        shutil.rmtree(inbox / w, ignore_errors=True)
    state["seen"] = seen
    return sorted(down)


def sync(store: Store, *, up_only: bool = False) -> dict:
    """This machine's logs and run records up, everyone else's down, the cache brought up to them. Logs only grow:
    none replaces a longer one, here or in the shared folder. up_only: just this machine's logs (what it wrote
    since)."""
    loc = location(store.comp)
    if loc is None:
        return {"share": None}
    state = store.setting("share") or {}
    restored = _own_logs(store, loc, state)
    held = _held(store)
    ids = _renamed(store)
    cache = "rebuilt" if restored else None
    _upload(store, loc, state)
    if up_only:
        store.set_setting("share", state)
        return {"share": loc, "restored": restored}
    pulled, warnings = _their_logs(store, loc)
    cache = store.catch_up() or cache
    lost = _lost(store, held)
    renamed = [f"{key} of {writer} is {local} here (another machine's row has that id; write {local} for it)"
               for key, writer, local in sorted(_renamed(store) - ids)]
    sent = _records_up(store, loc, state)
    down = _records_down(store, loc, state, own=bool(restored))
    store.set_setting("share", state)
    return {"share": loc, "pulled": pulled, "cache": cache, "records_sent": sent, "records_taken": down,
            "restored": restored, "warnings": warnings, "lost_claims": lost, "renamed": renamed}


def _renamed(store: Store) -> set:
    return {tuple(r) for r in store.all("SELECT id, writer, local FROM ids WHERE writer<>'*' AND local<>id")}


def describe(res: dict) -> str:
    if res.get("error"):
        return f"ledger: not synced: {res['error']}" + (f"\n  fix: {res['fix']}" if res.get("fix") else "")
    if not res.get("share"):
        return "ledger: this machine only (share: in ~/.config/kx/machine.yaml shares it)"
    parts = [f"ledger: in sync with {res['share']}"]
    if res.get("pulled"):
        parts.append("new from " + ", ".join(res["pulled"]))
    if res.get("records_taken"):
        parts.append("run records from " + ", ".join(res["records_taken"]))
    lines = ["; ".join(parts)]
    lines += [f"kx: {x}" for x in res.get("restored") or []]
    lines += [f"kx: {x}" for x in res.get("warnings") or []]
    lines += [f"kx: {x}" for x in res.get("renamed") or []]
    lines += [f"kx: {x['task']} is no longer {x['was']}'s: "
              + (f"{x['now']} {x['status']} it" if x.get("status") in ("done", "dropped") else
                 f"{x['now'] or 'reopened'} has it (an earlier claim)")
              + "; stop what works on it here (kx workers)" for x in res.get("lost_claims") or []]
    return "\n".join(lines)


def export(store: Store, dest: Path) -> dict:
    """The whole history in one file: every log here, this machine's run records and submitted files."""
    files = own_records(store)
    store.archive(dest, files)
    return {"path": str(dest), "logs": len(list(store.comp.log_dir.glob("*.jsonl"))), "files": len(files),
            "mb": float(f"{dest.stat().st_size / 2**20:.2g}")}


def import_export(store: Store, archive: Path) -> dict:
    """A `kx export` file: its logs join these (a log here already longer is kept: logs only grow), its run records
    are taken as from a sync."""
    added = []
    with tempfile.TemporaryDirectory(prefix="kx-import-", dir=store.comp.store) as tmp:
        _extract(archive, Path(tmp))
        for p in sorted((Path(tmp) / "log").glob("*.jsonl")):
            here = store.comp.log_dir / p.name
            if store.mine(p.stem) and here.exists():
                continue  # this machine's own log is never replaced
            if here.exists() and here.stat().st_size >= p.stat().st_size:
                continue
            if here.exists() and not p.read_bytes().startswith(here.read_bytes()):
                raise KxError(f"{p.name} in {archive} is not a continuation of the one here",
                              fix="two different histories under one writer name: rename one (machine.yaml name:)")
            shutil.copy2(p, here)
            added.append(p.stem)
        cache = store.catch_up()
        runs = take_records(store, Path(tmp))
    return {"source": str(archive), "logs": added, "cache": cache, "runs": runs}
