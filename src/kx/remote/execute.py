"""kx exec --on HOST: run a command on another machine as a tracked run of this ledger.

The ledger stays here. kx ships the run to the host as a job folder (run.sh and the commit's code: kx.remote.script),
with the kx sources and the competition's data when the host lacks that version, and starts it there: an ssh host
runs it in its own session, so it survives a dropped connection; a mailbox host's agent takes it (kx.remote.mailbox).
While it runs, or later with `kx remote sync`, kx copies the run's out/ back into the local run dir (files over 32 MB
only when asked: kx fetch), and records the run once it has ended.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import kx
from kx.guard import actors, budgets, rules
from kx.kaggle import data as _data
from kx.ledger import config as _config
from kx.ledger.config import KxError
from kx.ledger.store import Store, age_s, now
from kx.remote import hosts
from kx.remote.agent import BIG, LIVE_FILES, join
from kx.remote.script import env_lines, env_spec, run_sh
from kx.runs import tracking
from kx.runs.execute import ProgressSniffer
from kx.runs.objects import atomic_json

q = shlex.quote
INFO = "remote.json"
GONE_AFTER = 3             # polls without the process nor an exit code before the run counts as lost
SYNC_STALE_S = 1800        # a running remote run not synced for this long shows in kx status
OURS = ("/remote.json", "/run.sh", "/code/")  # kx's own files in the run dir: a run's output never overwrites them
LOW_DISK = 5 * 2**30       # with less free here, kx fetch leaves big files on the host: a full disk breaks the ledger


def info(store: Store, run_id: str) -> dict | None:
    p = store.run_dir(run_id) / INFO
    return json.loads(p.read_text()) if p.exists() else None


def _save(run_dir: Path, inf: dict):
    atomic_json(run_dir / INFO, inf)


def _host(inf: dict) -> hosts.Host:
    """The host as it was when the run started (a later edit of hosts.yaml does not lose the run)."""
    if inf.get("kind") == "mailbox":
        return hosts.Host(name=inf["host"], kind="mailbox", mailbox=inf["mailbox"], dir=inf["dir"])
    return hosts.Host(name=inf["host"], ssh=inf.get("ssh"), local=bool(inf.get("local")), dir=inf["dir"],
                      ssh_opts=inf.get("ssh_opts") or [])


def _where(h: hosts.Host, slug: str, run: str | None = None) -> dict:
    """Where a host keeps the kx sources and the competition's data (a folder per version), and a run's job folder
    and the folder whose out/ comes back (one folder on an ssh host; two in a mailbox: kx.remote.agent's layout)."""
    if h.kind == "mailbox":
        w = {"kx": join(h.mailbox, "_kx"), "data": join(h.mailbox, slug, "data")}
        return w | ({"job": join(h.mailbox, "_hosts", h.name, "jobs", f"{slug}~{run}"),
                     "path": join(h.mailbox, slug, "runs", run)} if run else {})
    base = f"{h.dir}/{slug}"
    return {"kx": f"{h.dir}/kx", "data": f"{base}/data"} | ({"job": f"{base}/runs/{run}", "path": f"{base}/runs/{run}"} if run else {})


def _kx_source() -> Path:
    root = Path(kx.__file__).resolve().parents[2]
    if not (root / "pyproject.toml").exists() or not (root / "src" / "kx").is_dir():
        raise KxError(f"kx is not installed from a source checkout ({root}): cannot copy it to the host",
                      fix="install kx with `uv tool install --editable <kx repo>`, or set `python:` for the host "
                          "to an interpreter that already has kx")
    return root


def ship_kx(t, dest: str) -> str:
    """The kx sources on the host (or in the mailbox) when they changed: runs import kx from them."""
    src = _kx_source()
    files = [src / "pyproject.toml", *sorted(p for p in (src / "src").rglob("*") if p.is_file() and "__pycache__" not in p.parts)]
    digest = hashlib.sha256("\n".join(f"{p.relative_to(src)} {p.stat().st_size} {p.stat().st_mtime_ns}"
                                      for p in files).encode()).hexdigest()[:16]
    if (t.read(f"{dest}/.kx-hash") or "").strip() != digest:
        print("kx: copying the kx sources", file=sys.stderr)
        if t.ARCHIVE:  # a mailbox: one file, not one upload per source file (a few seconds each on Google Drive)
            with tempfile.TemporaryDirectory(prefix="kx-src-") as tmp:
                with tarfile.open(Path(tmp) / "kx.tar.gz", "w:gz") as tf:
                    for p in files:
                        tf.add(p, arcname=str(p.relative_to(src)))
                t.push(Path(tmp) / "kx.tar.gz", f"{dest}/kx.tar.gz")
        else:
            t.push(src / "src", f"{dest}/src", delete=True, excludes=["__pycache__"])
            t.push(src / "pyproject.toml", f"{dest}/pyproject.toml")
        t.write(f"{dest}/.kx-hash", digest + "\n")
    return digest


def ship_data(t, comp, dest: str, needs=()) -> str | None:
    """The competition's own files, once per data version (not derived/ caches runs made here), and the parts of the
    data folder a run asked for with --needs, when missing or changed. Returns the version (None: no data here)."""
    try:
        version = _data.version(comp)
    except KxError:
        return None
    d, dest = _data.data_dir(comp), f"{dest}/{version}"
    if t.read(f"{dest}/.kx-complete") is None:
        files = _data.kaggle_files(comp)
        size = sum((d / f).stat().st_size for f in files if (d / f).is_file()) / 2**30
        print(f"kx: copying the data ({size:.1f} GB, version {version}); once per data version", file=sys.stderr)
        t.push(d, dest, files=files)
        t.write(f"{dest}/.kx-complete", "\n")
    if needs:
        print(f"kx: {', '.join(needs)}, where missing or changed", file=sys.stderr)
        t.push(d, dest, files=needs)
    return version


def check_needs(comp, needs) -> list[str]:
    """`kx exec --needs` paths: inside the data folder, there, and not the competition's own files (those always go)."""
    d = _data.data_dir(comp)
    out = []
    for n in needs or ():
        rel = Path(n.strip("/"))
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise KxError(f"--needs {n}: a path inside the data folder, like derived/fp-v1", code=2)
        if rel.parts[0] == "data" and not (d / rel).exists():  # data/derived/x as the repo's data link shows it
            rel = Path(*rel.parts[1:])
        if not (d / rel).exists():
            raise KxError(f"--needs {n}: no {d / rel} here", fix="ls " + str(d))
        out.append(rel.as_posix())
    return out


def _code_archive(root: Path, sha: str, dest: Path):
    p = subprocess.run(["git", "archive", "--format=tar.gz", "-o", str(dest), sha], cwd=root, capture_output=True, text=True)
    if p.returncode != 0:
        raise KxError(f"git archive {sha[:12]}: {p.stderr.strip()[-500:]}")


def run_remote(cmd: list[str], host_name: str, *, hypothesis=None, kind="experiment", config=None, tags=(), group=None,
               seed=None, parent=None, timeout=None, gpus: int = 0, detach=False,
               echo=True, needs=(), task=None) -> tuple[int, int]:
    """Run cmd on a host as a tracked run; returns (run id, exit code). With detach, returns once it started."""
    if not cmd:
        raise KxError("nothing to run", fix="kx exec --on HOST [options] -- <command> ...")
    comp = _config.load()
    needs = check_needs(comp, needs)
    h = hosts.get(host_name, comp)
    if h.usd_per_hour:
        with Store(comp) as st:
            budgets.check(st, actors.current(), {"cloud_usd": h.usd_per_hour / 60})
    act = tracking.begin(hypothesis or shlex.join(cmd), config, parent=parent, tags=tags, group=group, seed=seed,
                         kind=kind, command=cmd, gpus=0, host=h.name, task=task)
    store, r = act.store, act.run
    try:
        row = store.get_run(r.id)
        root, sha = Path(row["worktree"]), row["git_sha"]
        cwd = os.path.relpath(Path.cwd().resolve(), root.resolve())
        store.update("runs", {"host": h.name, "gpus": gpus or h.gpus or None}, id=r.id)
        t, w = h.transport, _where(h, comp.slug, r.name)
        print(f"kx: {r.name} on {h.name}: shipping the code and data it needs", file=sys.stderr)
        kx_hash = None if h.python else ship_kx(t, w["kx"])
        version = ship_data(t, comp, w["data"], needs)
        script = run_sh(h, r.id, r.name, cmd=cmd, cwd="." if cwd.startswith("..") else cwd, data_version=version,
                        env=env_spec(root, sha, h.torch), timeout=timeout)
        (r.dir / "run.sh").write_text(script)
        with tempfile.TemporaryDirectory(prefix="kx-job-") as tmp:  # the job folder: run.sh, the code, a probe's patch
            job = Path(tmp)
            (job / "run.sh").write_text(script)
            _code_archive(root, sha, job / "code.tar.gz")
            for f in ("uncommitted.patch", "untracked.tar.gz"):
                if (r.dir / "code" / f).exists():
                    shutil.copy2(r.dir / "code" / f, job / f)
            t.push(job, w["job"])
        inf = {"host": h.name, "kind": h.kind, "dir": h.dir, "path": w["path"], "sha": sha, "data_version": version,
               "usd_per_hour": h.usd_per_hour, "launched": now(), "synced": None}
        inf |= ({"mailbox": h.mailbox, "job": w["job"], "key": f"{comp.slug}/{r.name}"} if h.kind == "mailbox"
                else {"ssh": h.ssh, "local": h.local, "ssh_opts": h.ssh_opts})
        _save(r.dir, inf)
        t.start(w["job"], json.dumps({"run": r.name, "slug": comp.slug, "sha": sha, "kx_hash": kx_hash, "python": bool(h.python),
                                      "data": {"version": version, "needs": needs} if version else None, "queued": now()}))
    except BaseException as e:
        (r.dir / INFO).unlink(missing_ok=True)  # it never started there: close it like a local run
        tracking.end(act, f"could not start on {h.name}: {e}")
        raise
    if detach:
        act.hb.stop()
        store.close()
        print(f"kx: {r.name} runs on {h.name} in the background. kx remote follow {r.name} · kx remote sync · "
              f"kx remote kill {r.name}", file=sys.stderr)
        return r.id, 0
    return r.id, follow(r.id, act=act, echo=echo)


# Hosts kx sets up itself when they lack it: rsync and tar must be there; uv is installed (unless `python:` is set).
PREREQS = r"""
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
for t in rsync tar; do command -v $t >/dev/null || { echo "kx-missing: $t" >&2; exit 1; }; done
[ -n "$KX_NO_UV" ] || command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh 2>/dev/null | sh >/dev/null 2>&1
[ -n "$KX_NO_UV" ] || command -v uv >/dev/null || { echo "kx-missing: uv (kx installs it with curl)" >&2; exit 1; }
"""


def setup(host_name: str) -> dict:
    """Get a host ready for this competition's runs before the first one: the kx sources and the data (what a run
    would ship), and on an ssh host the tools and the environment of the current commit's uv.lock."""
    comp = _config.load()
    h = hosts.get(host_name, comp)
    t, w = h.transport, _where(h, comp.slug)
    if h.kind == "ssh":
        p = t.run(("KX_NO_UV=1\n" if h.python else "") + PREREQS, check=False, timeout=900)
        if p.returncode != 0:
            raise KxError(f"{h.name}: {(p.stderr or p.stdout).strip()[-800:]}", fix=f"install it on {h.name}")
    if not h.python:
        ship_kx(t, w["kx"])
    version = ship_data(t, comp, w["data"])
    out = {"host": h.name, "data_version": version, "env": None}
    if h.kind == "mailbox":
        from kx.remote import mailbox
        return out | {"start": mailbox.start_command(h)}
    if not h.python:  # the environment, built as a run would build it
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=comp.root, capture_output=True, text=True, check=True).stdout.strip()
        spec = env_spec(comp.root, sha, h.torch)
        print(f"kx: the environment ({'uv.lock' if spec[1] else 'pyproject.toml'} {spec[0]}); the first time takes a while",
              file=sys.stderr)
        setup_dir = f"{h.dir}/{comp.slug}/setup"
        with tempfile.TemporaryDirectory(prefix="kx-setup-") as tmp:
            _code_archive(comp.root, sha, Path(tmp) / "code.tar.gz")
            t.push(Path(tmp), setup_dir, delete=True)
        probe = ('import json, sys, kx\ntry:\n    import torch; t = [torch.__version__, torch.cuda.is_available()]\n'
                 'except ImportError:\n    t = [None, None]\nprint(json.dumps({"python": sys.version.split()[0], "torch": t[0], "cuda": t[1]}))')
        script = "\n".join([f'cd {q(setup_dir)} || exit 1', 'B="$(cd .. && pwd)"; C="$PWD/code"', 'finish() { exit "$1"; }',
                            'rm -rf "$C" && mkdir -p "$C" && tar -xzf code.tar.gz -C "$C" || exit 1',
                            *env_lines(*spec, h.torch), f"python -c {q(probe)}"])
        out["env"] = {"key": spec[0], **json.loads(t.run(script, timeout=3600).stdout.strip().splitlines()[-1])}
    return out


def _echo_new(run_dir: Path, offsets: dict, sniffer: ProgressSniffer, echo: bool):
    for name, stream in (("stdout.log", sys.stdout), ("stderr.log", sys.stderr)):
        p = run_dir / name
        try:
            with p.open("rb") as f:
                f.seek(offsets.get(name, 0))
                chunk = f.read()
        except OSError:
            continue
        if chunk:
            offsets[name] = offsets.get(name, 0) + len(chunk)
            sniffer.write(chunk)
            if echo:
                stream.buffer.write(chunk)
                stream.flush()


def _pull(t, inf: dict, d: Path, full: bool = False, only=()) -> None:
    """Copy the run dir back: files up to BIG and LIVE_FILES; with `full`, everything (when there is room here);
    with `only`, those paths. inf["on_host"] then lists what is still only on the host."""
    src = f"{inf['path']}/out"
    if only:
        t.pull(src, d, only=only)
    elif full and shutil.disk_usage(d if d.exists() else d.parent).free >= LOW_DISK:
        t.pull(src, d, excludes=OURS)
    else:
        if full:
            print(f"kx: under {LOW_DISK / 2**30:.0f} GB free here: {d.name}'s files over {BIG} stay on {inf['host']} "
                  f"(free some space, then kx fetch {d.name})", file=sys.stderr)
        t.pull(src, d, excludes=OURS, max_size=BIG)
        t.pull(src, d, only=LIVE_FILES)
    if full or only:
        _note_on_host(t, inf, d)


def _note_on_host(t, inf: dict, d: Path) -> None:
    """inf["on_host"]: the run's big files not here (or not whole), with their sizes: listed by kx show and the
    dashboard, fetched with kx fetch."""
    try:
        big = t.list_big(f"{inf['path']}/out", BIG)
    except KxError:
        return
    inf["on_host"] = [{"path": p, "bytes": n} for p, n in big if not ((d / p).is_file() and (d / p).stat().st_size == n)]


def _whole_here(t, inf: dict, d: Path) -> bool:
    """Is every file of the run's out/ on the host here, whole? (kx's own files are never copied: they do not count.)
    Raises KxError when the host cannot be asked."""
    return all((d / p).is_file() and (d / p).stat().st_size == n for p, n in t.list_big(f"{inf['path']}/out", "0K")
               if not ("/" + p).startswith(OURS))


def bring_home(store: Store, run_id: str, inf: dict) -> None:
    """storage.keep: here, for a run that has ended: copy what is still on its host (when the store has room), check
    that everything is here whole, then delete the host's copy. Whatever stays there (no room, host unreachable) is
    listed in remote.json's on_host, for kx fetch; the next kx remote sync of the run tries again."""
    d = store.run_dir(run_id)
    t = _host(inf).transport
    try:
        _note_on_host(t, inf, d)
        need = sum(x["bytes"] for x in inf.get("on_host", []))
        left = rules.room(store.comp)
        if need and left is not None and need > left:
            print(f"kx: {run_id}: its {need / rules.GB:.1f} GB on {inf['host']} stay there: the store has "
                  f"{max(left, 0) / rules.GB:.1f} GB left (storage.max_gb). kx gc, then kx fetch {run_id}",
                  file=sys.stderr)
            return
        _pull(t, inf, d, full=True)
        if inf.get("on_host") or not _whole_here(t, inf, d):
            print(f"kx: {run_id}: not all of it came back from {inf['host']}: its copy there is kept "
                  f"(kx fetch {run_id})", file=sys.stderr)
            return
        t.remove(inf)
        inf["host_copy"] = "removed"
    except KxError as e:
        print(f"kx: {run_id}: its copy on {inf['host']} is kept ({str(e).splitlines()[0]}); "
              f"kx remote sync {run_id} tries again", file=sys.stderr)
    finally:
        _save(d, inf)


def _poll(store: Store, run_id: str, inf: dict, again: bool = True) -> tuple[str, int | None]:
    """Copy the run dir back (its big files stay there); ('ended', code), ('running', None), ('gone', None) or
    ('unreachable', None)."""
    d = store.run_dir(run_id)
    t = _host(inf).transport
    try:
        _pull(t, inf, d)
    except KxError:
        return "unreachable", None
    inf["synced"] = now()
    if (d / ".remote" / "exit").exists():
        _note_on_host(t, inf, d)
        _save(d, inf)
        try:
            return "ended", int((d / ".remote" / "exit").read_text().strip())
        except ValueError:
            return "ended", 1
    _save(d, inf)
    state = t.alive(inf, started=(d / ".remote" / "pid").exists())
    if state != "gone":
        return state, None
    if again:  # it may have ended between the copy and the check: copy once more before calling it gone
        return _poll(store, run_id, inf, again=False)
    _note_on_host(t, inf, d)
    _save(d, inf)
    return "gone", None


def _remote_run(st: Store, run_ref) -> tuple[str, dict]:
    run_id = st.resolve(run_ref)
    inf = info(st, run_id)
    if inf is None:
        raise KxError(f"{run_id} is not a remote run", fix=f"its files are here: kx path {run_id}")
    return run_id, inf


def fetch(run_ref, paths=()) -> dict:
    """Copy a remote run's files that are still on its host here: the given paths (relative to its dir), or all."""
    with Store(_config.load()) as st:
        run_id, inf = _remote_run(st, run_ref)
        d = st.run_dir(run_id)
    rels = [p.strip("/") for p in paths]
    if any(".." in Path(p).parts or not p for p in rels):
        raise KxError("paths are relative to the run's dir, like latest.pt or ckpt/best.pt", code=2)
    if inf.get("host_copy") == "removed":
        return {"run": run_id, "host": inf["host"], "fetched": [], "on_host": [], "all_here": True}
    need = sum(x["bytes"] for x in inf.get("on_host") or [] if not rels or x["path"] in rels)
    rules.check_room(st.comp, need, f"{run_id}'s files")
    _pull(_host(inf).transport, inf, d, full=not rels, only=rels)
    _save(d, inf)
    return {"run": run_id, "host": inf["host"], "fetched": rels or "all", "on_host": inf.get("on_host", [])}


def follow(run_id: str, *, act=None, echo: bool = True, from_start: bool = True) -> int:
    """Stream a remote run's output until it ends, then record it. Ctrl-C stops following; the run goes on."""
    store = act.store if act is not None else Store(_config.load(), auto_backup=True)
    try:
        inf = info(store, run_id)
        if inf is None:
            raise KxError(f"{run_id} is not a remote run")
        _only_ours(store, run_id, "follow")
        d, t = store.run_dir(run_id), _host(inf).transport
        offsets = {} if from_start else {n: (d / n).stat().st_size for n in ("stdout.log", "stderr.log") if (d / n).exists()}
        sniffer, polls, gone, down = ProgressSniffer(d), 0, 0, 0
        try:
            while True:
                state, code = _poll(store, run_id, inf)
                _echo_new(d, offsets, sniffer, echo)
                if state == "ended":
                    break
                gone = gone + 1 if state == "gone" else 0
                if gone >= GONE_AFTER:
                    code = None
                    break
                down = down + 1 if state == "unreachable" else 0
                if down in (1, 30):
                    print("kx: " + t.UNREACHABLE.format(host=inf["host"]), file=sys.stderr)
                time.sleep(t.POLL_S[min(polls, len(t.POLL_S) - 1)])
                polls += 1
        except KeyboardInterrupt:
            if act is not None:
                act.hb.stop()
                store.close()
            print(f"\nkx: stopped following; {run_id} goes on on {inf['host']}. kx remote follow {run_id} · "
                  f"kx remote kill {run_id}", file=sys.stderr)
            return 130
        sniffer.close()
        return finalize(store, run_id, inf, code, act=act)
    finally:
        if act is None:
            store.close()


def finalize(store: Store, run_id: str, inf: dict, code: int | None, act=None, why: str | None = None) -> int:
    """Record a remote run that has ended (scores, notes, status) and charge its cost. Closes store."""
    d = store.run_dir(run_id)
    row = store.get_run(run_id)
    if row["status"] != "running":
        store.close()
        return code if code is not None else 1
    if code == 0:
        error = None
    elif code is None:
        error = why or f"the process on {inf['host']} is gone without an exit code (host rebooted or out of memory?)"
    elif code == 124:
        error = "time limit exceeded"
    elif code in (143, 129) and why:
        error = why
    else:
        tail = (d / "stderr.log").read_bytes()[-3000:].decode(errors="replace") if (d / "stderr.log").exists() else ""
        error = f"exit code {code} on {inf['host']}\n{tail}"
    hours = None
    try:
        started = int((d / ".remote" / "started").read_text())
        ended = int((d / ".remote" / "ended").read_text()) if (d / ".remote" / "ended").exists() else int(time.time())
        hours = max(0, ended - started) / 3600
    except (OSError, ValueError):
        pass
    if inf.get("usd_per_hour") and hours is not None:
        budgets.charge(store, SimpleNamespace(name=row["author"]), "cloud_usd", round(inf["usd_per_hour"] * hours, 4),
                       run_id)
    inf.update(finished=now(), hours=hours, exit=code)
    _save(d, inf)
    if rules.keep_here(store.comp):
        bring_home(store, run_id, inf)
    if act is None:
        act = tracking._Active(store, tracking.Run(store, run_id), None, time.monotonic(), 0)
    try:
        tracking.end(act, error)
    except KxError as e:
        print(f"kx: {e}", file=sys.stderr)
        return code or 1
    return code if code is not None else 1


def ours(store: Store, row) -> bool:
    """A run this machine launched: only it follows, kills and records it (another machine's remote.json comes with
    kx sync, so its files can be fetched from anywhere)."""
    return not row["writer"] or store.mine(row["writer"])


def _only_ours(store: Store, run_id: str, what: str):
    row = store.get_run(run_id)
    if not ours(store, row):
        raise KxError(f"{run_id} was launched from {row['writer']}: {what} it there", fix="kx sync shows how it ends")


def running(store: Store) -> list:
    """This machine's remote runs still going."""
    return [r for r in store.all("SELECT * FROM runs WHERE status='running' ORDER BY n, id")
            if (store.run_dir(r["id"]) / INFO).exists() and ours(store, r)]


def sync(run_ref=None) -> list[dict]:
    """Copy back every running remote run (or one) and record those that have ended. One run that has ended: copy
    what is still on its host (big files, or added since)."""
    comp = _config.load()
    with Store(comp) as st:
        rows = [st.get_run(_remote_run(st, run_ref)[0])] if run_ref else running(st)
    out = []
    for r in rows:
        st = Store(comp, auto_backup=True)
        try:
            inf, d = info(st, r["id"]), st.run_dir(r["id"])
            item = {"run": r["id"], "host": inf["host"], "state": r["status"]}
            if r["status"] == "running" and not ours(st, r):
                item["state"] = f"running, launched from {r['writer']}"
            elif r["status"] != "running" and inf.get("host_copy") == "removed":
                item["complete"] = True
            elif r["status"] != "running" and rules.keep_here(comp) and ours(st, r):
                bring_home(st, r["id"], inf)
                item["complete"] = inf.get("host_copy") == "removed"
            elif r["status"] != "running":
                try:
                    _pull(_host(inf).transport, inf, d, full=True)
                    _save(d, inf)
                    item["complete"] = not inf.get("on_host")
                except KxError as e:
                    item["error"] = str(e)
            else:
                state, code = _poll(st, r["id"], inf)
                item["state"] = state
                if state in ("ended", "gone"):
                    item["exit"] = finalize(st, r["id"], inf, code)
                    with Store(comp) as st2:
                        item["state"] = st2.get_run(r["id"])["status"]
            out.append(item)
        finally:
            st.close()
    return out


def kill(run_ref) -> dict:
    """Stop a remote run (its whole process group), then record it as failed."""
    st = Store(_config.load(), auto_backup=True)
    try:
        run_id, inf = _remote_run(st, run_ref)
        _only_ours(st, run_id, "kill")
        if st.get_run(run_id)["status"] != "running":
            raise KxError(f"{run_id} is not running")
        t = _host(inf).transport
        t.stop(inf)
        state, code, end = "running", None, time.time() + t.STOP_WAIT_S
        while time.time() < end:
            state, code = _poll(st, run_id, inf)
            if state in ("ended", "gone", "unreachable"):
                break
            time.sleep(2)
        else:
            t.stop(inf, force=True)
            _note_on_host(t, inf, st.run_dir(run_id))
        why = f"killed (kx remote kill) on {inf['host']}"
        if state == "unreachable":
            why += f" ({inf['host']} could not be reached: the run may go on there)"
        finalize(st, run_id, inf, code if code is not None else 143, why=why)
        return {"run": run_id, "host": inf["host"], "killed": True}
    finally:
        st.close()


def stale(store: Store) -> list[tuple]:
    """Running remote runs nobody has copied back for a while (their results are not recorded until synced)."""
    out = []
    for r in running(store):
        inf = info(store, r["id"])
        a = age_s(inf.get("synced") or inf["launched"])
        if a is not None and a > SYNC_STALE_S:
            out.append((r, inf, a))
    return out
