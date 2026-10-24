"""Kernels and datasets pushed to Kaggle, each tied to the run whose code/artifacts they contain."""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from kx.kaggle import client as kaggle
from kx.kaggle.submit import check_kernel_ref
from kx.ledger.config import KxError
from kx.ledger.store import Store, now, rid
from kx.runs import git


def _metadata(folder: Path, name: str) -> dict:
    p = folder / name
    if not p.exists():
        raise KxError(f"{folder} has no {name}")
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise KxError(f"{p}: invalid JSON ({e})") from None


def _record(store: Store, run_id: int, kind: str, target: str, version, sha, source: str, message: str):
    store.x("INSERT INTO pushes (run_id, kind, target, version, git_sha, source, created, message)"
            " VALUES (?,?,?,?,?,?,?,?)", (run_id, kind, target, version, sha, source, now(), message))


def push_kernel(store: Store, run_id: int, *, dry_run: bool = False) -> dict | str:
    """Push the kernel exactly as of the run: `submission.kernel_dir` at the run's git sha, never the current working
    tree. With dry_run, what would be pushed (the preview)."""
    comp = store.comp
    kernel = check_kernel_ref(comp.submission.get("kernel"))
    run = store.get_run(run_id)
    if run["status"] != "done":
        raise KxError(f"{rid(run_id)} is {run['status']}, not done")
    with tempfile.TemporaryDirectory() as tmp:
        kdir = str(comp.submission.get("kernel_dir", "kernel")).strip("/")
        sha = run["git_sha"]
        if not sha:
            raise KxError(f"{rid(run_id)} has no git sha")
        if run["git_dirty"]:
            raise KxError(f"{rid(run_id)} ran on a dirty tree: its code is not exactly at {sha[:10]}")
        if not git.has_commit(sha, comp.root):
            raise KxError(f"commit {sha[:10]} is not in this repo (fetch it, or run from its worktree)")
        if not git.archive(sha, kdir, Path(tmp), comp.root):
            raise KxError(f"commit {sha[:10]} has no '{kdir}/' (submission.kernel_dir in comp.yaml); "
                          f"commit the kernel folder with kernel-metadata.json")
        folder, source = Path(tmp) / kdir, f"git:{sha[:10]}:{kdir}"
        meta = _metadata(folder, "kernel-metadata.json")
        datasets = _attached_datasets(store, meta, check_ready=not dry_run)
        code = folder / str(meta.get("code_file") or "")
        if code.is_file() and PLACEHOLDER in code.read_text():
            code.write_text(fill_placeholder(code.read_text(), {"run": rid(run_id), "git_sha": run["git_sha"],
                                                                "datasets": {d: v["run"] for d, v in datasets.items()}}))
        if meta.get("id") != kernel:
            raise KxError(f"kernel-metadata.json id is {meta.get('id')!r}, comp.yaml submission.kernel is {kernel!r}")
        note = ""
        want = kernel.split("/", 1)[1]
        if slugify(meta.get("title") or "") != want:
            # Kaggle names a new notebook after its title, not its id: "CASMI 2026 offline inference" became
            # casmi-2026-offline-inference while kx recorded (and would submit) casmi26-submission
            meta["title"] = want.replace("-", " ")
            (folder / "kernel-metadata.json").write_text(json.dumps(meta, indent=2) + "\n")
            note = f" (title set to {meta['title']!r} so Kaggle names it {want})"
        if dry_run:
            files = sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())
            gpu = bool(meta.get("enable_gpu")) or str(meta.get("machine_shape", "")).lower().startswith("gpu")
            return {"kernel": kernel, "source": source + note, "files": files[:30] + (["…"] if len(files) > 30 else []),
                    "gpu": gpu, "run": rid(run_id),
                    "datasets": [f"{d}: {v['run']} ({v['status']}{', v' + str(v['version']) if v['version'] else ''})"
                                 for d, v in datasets.items()] or None}
        version, out = kaggle.kernel_push(folder)
    landed = re.search(r"kaggle\.com/code/([\w-]+/[\w-]+)", out)
    if landed and landed[1].lower() != kernel.lower():
        _record(store, run_id, "kernel", landed[1], version, run["git_sha"], source, f"meant for {kernel}")
        raise KxError(f"Kaggle pushed the notebook as {landed[1]}, not {kernel}: set comp.yaml submission.kernel and "
                      f"kernel-metadata.json id to {landed[1]} (or rename it on Kaggle), then push again\n{out.strip()}")
    _record(store, run_id, "kernel", kernel, version, run["git_sha"], source,
            "; ".join(f"{d} from {v['run']} v{v['version']}" for d, v in datasets.items()))
    return (f"pushed {kernel} version {version if version is not None else '?'} for {rid(run_id)} ({source}){note}\n"
            f"{out.strip()}\nwhen it completes: kx kernel status {rid(run_id)}, then kx submit {rid(run_id)}")


PLACEHOLDER = "__KX_PUSH__"  # in the kernel code: replaced at push by {"run", "git_sha", "datasets": {ref: run}}


def fill_placeholder(code: str, value: dict) -> str:
    """Put `value` where the kernel code has the placeholder: a quoted one ("__KX_PUSH__" or '__KX_PUSH__', e.g.
    KX = "__KX_PUSH__") becomes a Python string literal holding the JSON; a bare one becomes the JSON itself."""
    text = json.dumps(value)
    for q in ('"', "'"):
        code = code.replace(f"{q}{PLACEHOLDER}{q}", repr(text))
    return code.replace(PLACEHOLDER, text)
DATASET_MARK = "kx-dataset.json"  # written into every dataset kx uploads: {"run", "git_sha", "pushed"}


def _version_now(ref: str) -> int | None:
    try:
        return kaggle.dataset_status(ref)[1]
    except KxError:
        return None


def _attached_datasets(store: Store, meta: dict, check_ready: bool) -> dict:
    """The datasets the kernel attaches that kx uploaded: which run each came from and whether Kaggle has finished
    processing its latest version. A kernel pushed right after a dataset upload gets the previous version."""
    out = {}
    for ref in meta.get("dataset_sources") or []:
        row = store.one("SELECT run_id, version FROM pushes WHERE kind='dataset' AND target=? ORDER BY id DESC LIMIT 1", (ref,))
        if row is None:
            continue  # not ours (or uploaded without kx): nothing to check
        try:
            status, version = kaggle.dataset_status(ref)
        except KxError as e:
            status, version = f"unknown: {str(e).splitlines()[0]}", None
        out[ref] = {"run": rid(row["run_id"]), "status": status, "version": version}
        want = row["version"]
        behind = want is not None and (version is None or version < want)
        if behind and status == "ready":
            status = out[ref]["status"] = f"processing v{want} (Kaggle serves v{version})"
        if check_ready and (status != "ready" or behind):
            raise KxError(f"dataset {ref} ({rid(row['run_id'])}) is {status} on Kaggle: a kernel pushed now would get its "
                          "previous version", fix=f"wait a few minutes (kaggle datasets status {ref} --format json shows the "
                                                  f"current version{f', it must reach {want}' if want else ''}), then push again")
    return out


def slugify(title: str) -> str:
    """The URL slug Kaggle derives from a notebook title."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def kernel_status(store: Store, run_id: int | None) -> str:
    kernel = check_kernel_ref(store.comp.submission.get("kernel"))
    version = None
    if run_id is not None:
        row = store.x("SELECT version FROM pushes WHERE kind='kernel' AND target=? AND run_id=? ORDER BY id DESC"
                      " LIMIT 1", (kernel, run_id)).fetchone()
        if row is None:
            raise KxError(f"no kernel pushed for {rid(run_id)}")
        version = row["version"]
    status, out = kaggle.kernel_status(kernel, version)
    return f"{kernel}{'/' + str(version) if version else ''}: {status}\n{out.strip()}"


def push_dataset(store: Store, run_id: int, path: str, *, message: str = "", create: bool = False,
                 dry_run: bool = False) -> str:
    """Upload a folder (with dataset-metadata.json) as a dataset version, e.g. the run's model weights.
    `path` is looked up in the run's artifact dir first, then relative to the current directory."""
    run = store.get_run(run_id)
    if run["status"] != "done":
        raise KxError(f"{rid(run_id)} is {run['status']}, not done")
    in_run = store.run_dir(run_id) / path
    folder = in_run if in_run.is_dir() else Path(path).resolve()
    if not folder.is_dir():
        raise KxError(f"{path} is not a directory (in {rid(run_id)}'s artifacts or here)")
    dataset = _metadata(folder, "dataset-metadata.json").get("id")
    if not dataset or "/" not in dataset:
        raise KxError(f"{folder}/dataset-metadata.json needs an id <user>/<dataset-slug>")
    source = f"artifact:{path}" if folder == in_run else str(folder)
    full_msg = f"kx:{rid(run_id)} {(run['git_sha'] or 'nogit')[:7]} {message}".strip()
    if dry_run:
        size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
        return {"dataset": dataset, "action": "create" if create else "new version", "source": source,
                "message": full_msg, "size_mb": round(size / 2**20, 1), "run": rid(run_id)}
    (folder / DATASET_MARK).write_text(json.dumps({"run": rid(run_id), "git_sha": run["git_sha"], "pushed": now()}) + "\n")
    before = 0 if create else _version_now(dataset)
    out = kaggle.dataset_version(folder, full_msg, create=create)
    landed = re.search(r"kaggle\.com/datasets/([\w-]+/[\w-]+)", out)
    if landed and landed[1].lower() != dataset.lower():  # never let a task attach a slug that does not exist
        _record(store, run_id, "dataset", landed[1], None, run["git_sha"], source, f"meant for {dataset}")
        raise KxError(f"Kaggle created the dataset as {landed[1]}, not {dataset}: use {landed[1]} (or set "
                      f"dataset-metadata.json id to it)\n{out.strip()}")
    # the version this upload becomes: a kernel must not be pushed before Kaggle has it (`ready` alone describes the
    # previous version while the new one is processing)
    _record(store, run_id, "dataset", dataset, before + 1 if before is not None else None, run["git_sha"], source, full_msg)
    return f"uploaded {dataset} for {rid(run_id)} ({source})\n{out.strip()}"
