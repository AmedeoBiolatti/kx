"""Competition data: downloaded once into the store, symlinked as `data/` into each worktree (never in git)."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

from kx.kaggle import client as kaggle
from kx.ledger.config import Comp, KxError
from kx.ledger.store import now

MANIFEST = ".kx-manifest.json"
GB = 1024**3
PER_FILE_MIN_BYTES = 1 * GB  # above this (and with a flat file list) download file by file to limit peak disk use
PER_FILE_MAX_NESTED = 50  # nested layouts: update up to this many files one by one, else re-fetch the archive


def data_dir(comp: Comp) -> Path:
    return comp.store / "data"


def _manifest_path(comp: Comp) -> Path:
    return data_dir(comp) / MANIFEST


def kaggle_files(comp: Comp) -> list[str]:
    """The competition's own files (relative paths) and the manifest: what a data version is. Anything else in the
    data folder (derived/ caches runs made) is not part of it, and does not travel with it."""
    return [MANIFEST, *sorted(manifest(comp)["files"])]


def manifest(comp: Comp) -> dict:
    p = _manifest_path(comp)
    return json.loads(p.read_text()) if p.exists() else {"slug": comp.slug, "files": {}}


def _save(comp: Comp, m: dict):
    p = _manifest_path(comp)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, indent=2, sort_keys=True))
    tmp.replace(p)


def version(comp: Comp) -> str:
    """Identifier of the local data (Kaggle file list + dates); put it in a Protocol spec."""
    files = manifest(comp)["files"]
    if not files:
        raise KxError("no data downloaded yet (kx data)")
    return version_of(files)


def version_of(files: dict) -> str:
    """The version of a file list ({name: {size, creationDate}}): the local manifest's, or Kaggle's own."""
    canon = json.dumps({k: [v.get("size"), v.get("creationDate")] for k, v in sorted(files.items())})
    return "kaggle:" + hashlib.sha256(canon.encode()).hexdigest()[:10]


def status(comp: Comp, remote: list[dict] | None = None) -> list[dict]:
    remote = kaggle.files(comp.slug) if remote is None else remote
    have = manifest(comp)["files"]
    out = []
    for f in remote:
        name, local = f["name"], have.get(f["name"])
        if local is None:
            state = "missing"
        elif local.get("creationDate") != f.get("creationDate"):
            state = "updated on kaggle"
        elif not (data_dir(comp) / name).exists():
            state = "deleted locally"
        else:
            state = "ok"
        out.append({**f, "state": state})
    remote_names = {f["name"] for f in remote}
    out += [{"name": n, "size": v.get("size"), "state": "not on kaggle anymore"} for n, v in have.items()
            if n not in remote_names]
    return out


def _extract(zpath: Path, dest: Path) -> list[str]:
    with zipfile.ZipFile(zpath) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        zf.extractall(dest)
    zpath.unlink()
    return names


def _fetch_one(comp: Comp, name: str, dest: Path):
    """Download one competition file (possibly nested, e.g. 'train/a.csv') to dest/name."""
    tmp = dest / ".download"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        kaggle.download(comp.slug, tmp, file=name)
        base = name.rsplit("/", 1)[-1]
        for z in list(tmp.glob("*.zip")):  # the CLI zips large single files
            if z.name != base:
                _extract(z, tmp)
        found = [p for p in tmp.rglob(base) if p.is_file()]
        if not found:
            got = [str(p.relative_to(tmp)) for p in tmp.rglob("*") if p.is_file()]
            raise KxError(f"downloaded {name} but found no {base} (got {got})")
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        found[0].replace(target)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _check_space(comp: Comp, need: int, what: str):
    target = data_dir(comp)
    target.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(target).free
    if free < need:
        raise KxError(
            f"not enough disk space for {what}: need ~{need / GB:.1f} GB, {free / GB:.1f} GB free on {target}.\n"
            f"  Options: free space, or point `store:` in comp.yaml to a bigger disk."
        )


def download(comp: Comp, *, log=print) -> list[str]:
    """Download missing/updated files. Returns names written."""
    remote = kaggle.files(comp.slug)
    todo = [f for f in status(comp, remote) if f["state"] in ("missing", "updated on kaggle", "deleted locally")]
    if not todo:
        log("data is up to date")
        return []
    dest = data_dir(comp)
    dest.mkdir(parents=True, exist_ok=True)
    margin = GB  # left free after the download
    total = sum(f.get("size") or 0 for f in todo)
    from kx.guard import rules
    rules.check_room(comp, total, "the competition data")
    biggest = max(f.get("size") or 0 for f in todo)
    flat = all("/" not in f["name"] for f in todo)
    m = manifest(comp)
    written = []

    small_update = len(todo) < len(remote) and (flat or len(todo) <= PER_FILE_MAX_NESTED)
    if small_update or (flat and total >= PER_FILE_MIN_BYTES):
        # file by file: peak usage = total + one compressed copy
        _check_space(comp, total + biggest + margin, f"{len(todo)} file(s)")
        for f in todo:
            name = f["name"]
            log(f"downloading {name} ({(f.get('size') or 0) / GB:.2f} GB)")
            _fetch_one(comp, name, dest)
            m["files"][name] = {"size": f.get("size"), "creationDate": f.get("creationDate"), "downloaded": now()}
            _save(comp, m)  # after each file, so an interrupted download resumes
            written.append(name)
    else:
        # one archive for everything (keeps directory structure)
        all_total = sum(f.get("size") or 0 for f in remote)
        _check_space(comp, 2 * all_total + margin, "the full archive")
        log(f"downloading all {len(remote)} files ({all_total / GB:.2f} GB) as one archive")
        tmp = dest / ".download"
        shutil.rmtree(tmp, ignore_errors=True)
        kaggle.download(comp.slug, tmp)
        for z in tmp.glob("*.zip"):
            _extract(z, dest)
        shutil.rmtree(tmp, ignore_errors=True)
        for f in remote:
            if (dest / f["name"]).exists():
                m["files"][f["name"]] = {"size": f.get("size"), "creationDate": f.get("creationDate"),
                                         "downloaded": now()}
                written.append(f["name"])
        _save(comp, m)
        missing = [f["name"] for f in remote if f["name"] not in written]
        if missing:
            log(f"warning: {len(missing)} listed file(s) not found after extraction, e.g. {missing[:3]}")
    return written


def link(comp: Comp, root: Path | None = None) -> str:
    """Make <worktree>/data point at the store's data dir."""
    root = root or comp.root
    target = data_dir(comp)
    target.mkdir(parents=True, exist_ok=True)
    ln = root / "data"
    from kx.runs import git
    git.exclude("/data", root)  # the link must never make a worktree look dirty, .gitignore or not
    if ln.is_symlink():
        if ln.resolve() == target.resolve():
            return f"{ln} -> {target} (ok)"
        ln.unlink()
    elif ln.exists():
        return f"{ln} is a real directory; not replacing it (move it to {target} and run `kx data --link`)"
    ln.symlink_to(target, target_is_directory=True)
    return f"linked {ln} -> {target}"


def ensure_link(comp: Comp, root: Path | None = None):
    """Silent best-effort link, used when a run starts in a fresh worktree."""
    try:
        if data_dir(comp).exists() and not (root or comp.root).joinpath("data").exists():
            print("kx: " + link(comp, root), file=sys.stderr)
    except OSError:
        pass
