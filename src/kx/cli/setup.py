"""Commands: init, meta, data, backup, doctor."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from kx.cli.common import Out, S
from kx.ledger import config
from kx.ledger.config import KxError
from kx.views.text import dim, table


def cmd_init(a):
    import subprocess
    from kx.kaggle import competition, data
    from kx.setup import scaffold
    root = Path.cwd()
    done = []
    if subprocess.run(["git", "rev-parse", "--git-dir"], cwd=root, capture_output=True).returncode != 0:
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        done.append("git init")
    cy = root / "comp.yaml"
    meta = None
    if cy.exists():
        comp = config.load(root)
    else:
        slug = a.slug or root.name
        meta = competition.fetch(slug)
        inf = competition.infer(meta)["values"]
        kind = a.kind or inf.get("kind", "file")
        if kind == "code" and not a.kernel:  # the notebook that will produce submissions: <user>/<slug> by default
            from kx.kaggle import client
            user = client.username()
            a.kernel = f"{user}/{re.sub(r'[^a-z0-9]+', '-', slug.lower()).strip('-')[:50]}" if user else None
        vals = {"slug": slug, "kind": kind, "mode": a.mode or inf.get("mode", "rl" if kind == "simulation" else "predict"),
                "metric": a.metric or "score", "direction": a.direction or "max", "store": a.store,
                "submission": {"daily_limit": inf.get("daily_limit", 5),
                               "file": a.file or ("submission.tar.gz" if kind == "simulation" else "submission.csv"),
                               **({"kernel": a.kernel, "kernel_dir": "kernel"} if kind == "code" and a.kernel else {})}}
        cy.write_text(scaffold.render_comp_yaml(vals))
        done.append(f"wrote comp.yaml (kind {kind}, mode {vals['mode']}: review it)")
        comp = config.load(root)
    done += scaffold.write_agent_files(root)
    if not (root / competition.META_FILE).exists():
        try:
            competition.write(comp, meta or competition.fetch(comp.slug))
            done.append(f"wrote {competition.META_FILE}")
        except KxError as e:
            done.append(f"{competition.META_FILE} not written: {str(e).splitlines()[0]}")
    done += scaffold.write_skeleton(root, {"slug": comp.slug, "kind": comp.kind, "mode": comp.mode, "metric": comp.metric,
                                           "direction": comp.direction, "submission": comp.submission},
                                    meta or competition.load_cached(comp))
    if not a.no_data:
        try:
            got = data.download(comp, log=lambda s: print(f"kx: {s}", file=sys.stderr))
            done.append(f"data: {len(got)} file(s) downloaded" if got else "data is up to date")
        except KxError as e:
            done.append(f"data not downloaded: {str(e).splitlines()[0]}")
    try:
        done.append(data.link(comp))
    except OSError as e:
        done.append(f"data link: {e}")
    done.append("next: uv sync && git add -A && git commit -m init && kx doctor")
    return Out([d for d in done if d], "\n".join(f"  {d}" for d in done if d))


def cmd_meta(a):
    from kx.kaggle import competition
    comp = config.load()
    p = competition.write(comp, competition.fetch(comp.slug))
    return Out({"path": str(p)}, f"wrote {p}")


def cmd_data(a):
    from kx.kaggle import data
    comp = config.load()
    if a.link:
        return Out(None, data.link(comp))
    if a.status:
        rows = data.status(comp)
        return Out(rows, table([[r["name"], f"{(r.get('size') or 0) / 2**20:.1f} MB", r["state"]] for r in rows], ["file", "size", "state"]))
    got = data.download(comp, log=lambda s: print(f"kx: {s}", file=sys.stderr))
    data.link(comp)
    return Out(got, f"{len(got)} file(s) downloaded" if got else "data is up to date")


def cmd_backup(a):
    with S() as st:
        p = st.backup(a.to)
    return Out({"path": str(p)}, f"backup: {p}")


def cmd_doctor(a):
    import shutil as sh
    from kx.kaggle import client as kaggle
    checks = []

    def add(ok, what, fix=None):
        checks.append({"ok": ok, "check": what, "fix": fix})
    comp = config.load()
    add(True, f"comp.yaml: {comp.slug} ({comp.kind}/{comp.mode})")
    add(sh.which("kaggle") is not None, "kaggle CLI on PATH", "uv tool install kaggle")
    prob = kaggle.credentials_problem()
    add(prob is None, "Kaggle credentials", prob)
    from kx.runs import git
    g = git.info(comp.root)
    add(g is not None and g["sha"] is not None, "git repository with a commit", "git init && git add -A && git commit -m init")
    from kx.kaggle import data
    try:
        data.version(comp)
        add(True, "data downloaded")
    except KxError:
        add(False, "data downloaded", "kx data")
    add((comp.root / "docs" / "kx.md").exists(), "docs/kx.md (the rules for agents)", "kx init")
    add((comp.root / "pyproject.toml").exists(), "pyproject.toml (so code can import kx and comp)", "kx init")
    gpu = sh.which("nvidia-smi") is not None
    for name, w in comp.workers.items():
        exe = {"claude": "claude", "codex": "codex"}.get(w["harness"])
        add(sh.which(exe) is not None, f"worker {name}: {exe} on PATH", f"install {exe}")
        if gpu and w["harness"] == "codex" and w.get("sandbox", "workspace-write") != "danger-full-access":
            add(False, f"worker {name}: Codex's sandbox hides the GPU (no /dev/nvidia*)",
                "give GPU tasks to claude workers, or set sandbox: danger-full-access for this worker")
    if comp.kind == "code":
        try:
            from kx.kaggle.submit import check_kernel_ref
            check_kernel_ref(comp.submission.get("kernel"))
            add(True, "submission.kernel set")
        except KxError as e:
            add(False, "submission.kernel set", str(e))
    text = "\n".join(("  ok    " if c["ok"] else "  FIX   ") + c["check"] + (dim(f"  → {c['fix']}") if not c["ok"] and c["fix"] else "")
                     for c in checks)
    return Out(checks, text, 0 if all(c["ok"] for c in checks) else 1)


def register(cmd, group, action):
    s = cmd("init", cmd_init, "set up kx in this repo (or refresh its docs/kx.md and skeleton)")
    s.add_argument("--slug")
    s.add_argument("--kind", choices=config.KINDS)
    s.add_argument("--mode", choices=config.MODES)
    s.add_argument("--metric")
    s.add_argument("--direction", choices=("max", "min"))
    s.add_argument("--file")
    s.add_argument("--kernel")
    s.add_argument("--store", help="ledger and data folder (default: .kx/store in the repo); a second repo for the "
                                    "same competition needs its own")
    s.add_argument("--no-data", action="store_true")
    cmd("meta", cmd_meta, "regenerate docs/competition.md from the Kaggle pages")
    s = cmd("data", cmd_data, "download missing or updated data and link it as data/")
    s.add_argument("--status", action="store_true")
    s.add_argument("--link", action="store_true")
    s = cmd("backup", cmd_backup, "copy the ledger")
    s.add_argument("--to")
    cmd("doctor", cmd_doctor, "is this repo ready? (Kaggle, git, data, files, workers)")
