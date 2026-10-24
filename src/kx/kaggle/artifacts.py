"""Submission artifacts: immutable, content-addressed copies of what gets uploaded, smoke-tested first."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

from kx.ledger.config import KxError
from kx.ledger.store import Store, now, rid

SMOKE_TIMEOUT_S = 1800


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def members(path: Path) -> list[dict] | None:
    """Archive contents (name, size) for tar/zip submissions."""
    try:
        if tarfile.is_tarfile(path):
            with tarfile.open(path) as tf:
                return [{"name": m.name, "size": m.size} for m in tf.getmembers() if m.isfile()]
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                return [{"name": i.filename, "size": i.file_size} for i in zf.infolist() if not i.is_dir()]
    except (tarfile.TarError, zipfile.BadZipFile, OSError):
        return None
    return None


def build(store: Store, run_id: int, file: str | None = None, actor: str | None = None):
    """Snapshot the run's submission file into <store>/artifacts/<sha12>/ (read-only); idempotent."""
    run = store.get_run(run_id)
    if run["status"] != "done":
        raise KxError(f"{rid(run_id)} is {run['status']}, not done")
    name = file or store.comp.submission.get("file", "submission.csv")
    src = store.run_dir(run_id) / name
    if not src.is_file():
        raise KxError(f"{src} does not exist", fix=f"write it from the run with r.artifact({name!r}), or pass --file NAME")
    digest = sha256(src)
    row = store.x("SELECT * FROM artifacts WHERE sha=?", (digest,)).fetchone()
    if row is not None:
        verify(row)
        if row["run_id"] != run_id:
            print(f"kx: note: {rid(run_id)}'s {name} is byte-identical to {rid(row['run_id'])}'s artifact")
        return row
    dest_dir = store.comp.store / "artifacts" / digest[:12]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / Path(name).name
    shutil.copy2(src, dest)
    os.chmod(dest, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    manifest = {"sha256": digest, "name": Path(name).name, "size": dest.stat().st_size, "run": rid(run_id),
                "git_sha": run["git_sha"], "git_dirty": bool(run["git_dirty"]), "kind": run["kind"],
                "created": now(), "members": members(dest)}
    (dest_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    store.x("INSERT INTO artifacts (sha, run_id, name, path, size, manifest, created, actor) VALUES (?,?,?,?,?,?,?,?)",
            (digest, run_id, manifest["name"], str(dest), manifest["size"], json.dumps(manifest), now(), actor))
    return store.x("SELECT * FROM artifacts WHERE sha=?", (digest,)).fetchone()


def verify(art) -> None:
    """Artifacts are read-only where the filesystem allows it (not on NTFS/FUSE mounts): always re-hash."""
    path = Path(art["path"])
    if not path.is_file():
        raise KxError(f"artifact {art['sha'][:12]} is missing: {path}")
    if sha256(path) != art["sha"]:
        raise KxError(f"artifact {art['sha'][:12]} was modified on disk ({path}); submit the run again to rebuild it")



def smoke_enabled(comp) -> bool:
    """A smoke test runs before submitting: the configured command, or for simulations the Kaggle-style load."""
    return bool(comp.submission.get("smoke")) or comp.kind == "simulation"


def _confined(argv: list[str], scratch: Path) -> list[str]:
    """The smoke test runs the submission's code: in kx's sandbox, writing only its scratch dir (kx.sandbox)."""
    from kx import sandbox
    if sandbox.available():
        return sandbox.wrap(argv, [scratch])
    if sandbox.role() == "workspace":
        raise KxError("this machine is a workspace and bubblewrap (bwrap) is not usable here: the smoke test would run "
                      "the submission's code unconfined", fix="install bubblewrap (apt install bubblewrap)")
    return argv


def smoke(store: Store, art, timeout: int | None = None) -> tuple[bool, str]:
    """Run submission.smoke on the exact artifact, from a scratch copy; records the result."""
    cmd = store.comp.submission.get("smoke")
    if not cmd and store.comp.kind == "simulation":
        py = next((str(p) for p in (store.comp.root / ".venv" / "bin" / "python",
                                    store.comp.main_root / ".venv" / "bin" / "python") if p.exists()), sys.executable)
        cmd = f"{shlex.quote(py)} -c {shlex.quote(SIM_LOAD)} {{artifact}} {{dir}}/agent"
    if not cmd:
        return False, "no submission.smoke command configured in comp.yaml"
    timeout = timeout or int(store.comp.submission.get("smoke_timeout", SMOKE_TIMEOUT_S))
    verify(art)
    with tempfile.TemporaryDirectory(prefix="kx-smoke-") as tmp:
        copy = Path(tmp) / art["name"]
        shutil.copy2(art["path"], copy)
        os.chmod(copy, stat.S_IRUSR | stat.S_IWUSR)
        argv = [part.replace("{artifact}", str(copy)).replace("{dir}", tmp) for part in shlex.split(cmd)]
        argv = _confined(argv, Path(tmp))
        try:
            r = subprocess.run(argv, cwd=store.comp.root, capture_output=True, text=True, timeout=timeout)
            ok, log = r.returncode == 0, f"$ {' '.join(argv)}\nexit {r.returncode}\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}"
        except subprocess.TimeoutExpired:
            ok, log = False, f"$ {' '.join(argv)}\ntimed out after {timeout}s"
        except OSError as e:
            ok, log = False, f"$ {' '.join(argv)}\ncannot run: {e}"
    store.x("UPDATE artifacts SET smoke=?, smoke_log=? WHERE sha=?", ("passed" if ok else "failed", log, art["sha"]))
    return ok, log
