"""`kx init`: the files and folders a competition repo starts with.

<repo>/
  README.md  AGENTS.md  CLAUDE.md  comp.yaml  pyproject.toml
  docs/competition.md   the Kaggle pages        docs/kx.md   the rules for whoever works here
  src/comp/evaluate.py  the evaluation protocol (the one place scores are defined)
  scripts/  notebooks/  kernel/ (code competitions) or agent/ (simulations)
  data -> .kx/store/data      .kx/ (git-ignored): the store and the task worktrees
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


TEMPLATE = Path(__file__).parent / "KX.md"
BLOCK = re.compile(r"<!-- kx:start -->.*?<!-- kx:end -->\n?", re.S)
GITIGNORE = ["/data", ".kx/", ".venv/", "__pycache__/", "*.sqlite"]
GUIDE = "docs/kx.md"

AGENTS_BLOCK = """<!-- kx:start -->
## kx: experiment tracking, tasks and submissions

This repository is worked with **kx**. Before anything else run `kx context` (your task, the reference protocol,
best runs, open tasks, recent findings, the rules) and read `docs/kx.md`. `kx status` shows what is going on.
The competition itself is described in `docs/competition.md`.

Record every run with kx, score it under the reference protocol, write conclusions as findings, and finish tasks
with an outcome, negative ones included. Submissions and kernel/dataset pushes need `--yes` after the preview:
ask the person you work with before passing it. Never submit or push with the kaggle CLI directly.
<!-- kx:end -->
"""

# Claude Code: kx commands run freely; the raw Kaggle CLI would bypass kx's checks, so it asks first.
ALLOW = ["Bash(kx:*)"]
ASK = ["Bash(kaggle competitions submit:*)", "Bash(kaggle kernels push:*)", "Bash(kaggle datasets version:*)",
       "Bash(kaggle datasets create:*)"]


def _q(v) -> str:
    s = str(v)
    return s if re.fullmatch(r"[A-Za-z0-9_./@+-]+", s) else json.dumps(s)


COMMENT_COL = 34


def _align(lines: list[str]) -> list[str]:
    """Put trailing `# comments` in one column."""
    out = []
    for ln in lines:
        code, sep, comment = ln.partition("  # ") if not ln.lstrip().startswith("#") else (ln, "", "")
        if sep and len(code.rstrip()) < COMMENT_COL:
            ln = code.rstrip().ljust(COMMENT_COL) + "# " + comment.strip()
        out.append(ln)
    return out


def render_comp_yaml(v: dict) -> str:
    """A new competition's comp.yaml, with the options it may want later as comments."""
    sub = v.get("submission") or {}
    lines = [
        "# kx competition config: see docs/kx.md",
        f"slug: {_q(v['slug'])}",
        f"kind: {v['kind']}                    # how you submit: file | code | simulation",
        f"mode: {v['mode']}                    # what a score is: predict (public≠private) | optimize (public==private) | rl (rating)",
        "metric:",
        f"  name: {_q(v.get('metric') or 'score')}",
        f"  direction: {v.get('direction') or 'max'}             # max | min",
    ]
    if v.get("store"):
        lines.append(f"store: {_q(v['store'])}")
    lines += ["# reference: <protocol name>    # the protocol runs are ranked under (or: kx protocol use NAME)",
              "submission:",
              f"  daily_limit: {sub.get('daily_limit', 5)}               # Kaggle's limit, from the rules",
              f"  file: {_q(sub.get('file', 'submission.csv'))}"]
    for k in ("kernel", "kernel_dir"):
        if sub.get(k):
            lines.append(f"  {k}: {_q(sub[k])}")
    if v["kind"] == "code" and not sub.get("kernel"):
        lines.append("  kernel: <user>/<kernel-slug>  # TODO: the notebook that produces the submission")
    lines.append("  # smoke: python smoke.py {artifact}   # must pass on the exact file before it is submitted")
    if v["mode"] == "rl":
        lines += ["  min_episodes: 30             # games a submission plays before it is judged or retired",
                  "  # active: 2                 # only the last N submissions play on the ladder"]
    lines += ["budgets:                         # checked before the action (kx budget)",
              "  # submissions_per_day: 3",
              "  # kaggle_gpu_hours_per_week: 25",
              "  # tokens_per_day: 30000000      # all workers together",
              "workers:                         # who `kx spawn` may start; harness: claude | codex",
              "  # codex-1: {harness: codex, model: gpt-6-luna, effort: medium, memory: 5G, cpus: 8}",
              "  # sonnet-1: {harness: claude, model: sonnet}",
              "tags: [algorithm, speed, data, eval, infra, bug]  # suggested topics for runs and findings"]
    return "\n".join(_align(lines)) + "\n"


def write_guide(path: Path) -> str:
    """docs/kx.md is kx's: kx init rewrites it to match this kx. Notes for one repo go in its AGENTS.md."""
    fresh = "<!-- written by kx init, which rewrites it: put notes for this repo in AGENTS.md -->\n" + TEMPLATE.read_text()
    if path.exists() and path.read_text() == fresh:
        return f"{GUIDE} is up to date"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fresh)
    return f"wrote {GUIDE}"


def upsert_block(path: Path, block: str) -> str:
    text = path.read_text() if path.exists() else ""
    if BLOCK.search(text):
        new = BLOCK.sub(lambda _: block, text, count=1)
        if new == text:
            return f"{path.name} is up to date"
        path.write_text(new)
        return f"updated the kx section of {path.name}"
    path.write_text(text + ("\n" if text and not text.endswith("\n") else "") + ("\n" if text else "") + block)
    return f"added the kx section to {path.name}"


def write_agent_files(root: Path) -> list[str]:
    done = [write_guide(root / GUIDE), upsert_block(root / "AGENTS.md", AGENTS_BLOCK)]
    claude = root / "CLAUDE.md"
    if not claude.exists() or not claude.read_text().strip():  # Claude Code reads CLAUDE.md: the same instructions
        claude.write_text("@AGENTS.md\n@docs/kx.md\n")
        done.append("CLAUDE.md points to AGENTS.md and docs/kx.md")
    settings = root / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    cur = json.loads(settings.read_text()) if settings.exists() else {}
    perms = cur.setdefault("permissions", {})
    allow = perms.setdefault("allow", [])
    allow.extend(a for a in ALLOW if a not in allow)
    perms["ask"] = perms.get("ask", []) + [a for a in ASK if a not in perms.get("ask", [])]
    settings.write_text(json.dumps(cur, indent=2) + "\n")
    done.append("kx permissions in .claude/settings.json")
    gi = root / ".gitignore"
    lines = gi.read_text().splitlines() if gi.exists() else []
    missing = [g for g in GITIGNORE if g not in lines]
    if missing:
        gi.write_text("\n".join(lines + missing) + "\n")
        done.append(f"added {', '.join(missing)} to .gitignore")
    return done


# -- the code skeleton -------------------------------------------------------------------------------------------

EVALUATE = '''"""The evaluation protocol: the one place a score is defined. Every scored run takes its Protocol from here, so
runs stay comparable (kx ranks only scores under the same protocol id). Change the protocol only on purpose: any
change in its spec gives a new id, and older runs must be re-scored under it."""

import kx


def protocol() -> kx.Protocol:
    """Put in the spec everything that, if changed, makes scores incomparable: folds, metric, data version, seeds."""
    return kx.Protocol("cv5", metric="{metric}", data=kx.data_version(), noise="cv")  # TODO: folds="v1" (bump it when the folds change)


def score(predictions, truth) -> float:
    """The competition metric (docs/competition.md, Evaluation), computed exactly as Kaggle does."""
    raise NotImplementedError("implement the metric, then check it against a known submission score")
'''

KERNEL_RUN = '''"""The Kaggle notebook (code competition): runs offline on Kaggle and writes /kaggle/working/submission.csv.
It cannot import kx (not installed on Kaggle); put model weights and lookup files in a Kaggle dataset you attach.
Until the real pipeline is here it writes the sample submission, so the plumbing can be checked end to end."""

import shutil
from pathlib import Path

INPUT = Path("/kaggle/input/{slug}")
OUT = Path("/kaggle/working/submission.csv")

shutil.copy(INPUT / "sample_submission.csv", OUT)
'''

GITKEEP = {
    "scripts": "Entry points run with `kx exec -H \"hypothesis\" -- python scripts/<name>.py` (train, evaluate, build a submission).\n",
    "notebooks": "Exploration. Conclusions go in kx findings, results in kx runs.\n",
    "agent": "The simulation agent: main.py is what Kaggle loads (kx submit smoke-tests it the same way).\n",
}


def _readme(v: dict, meta: dict | None) -> str:
    c = (meta or {}).get("competition") or {}
    url = c.get("ref") or f"https://www.kaggle.com/competitions/{v['slug']}"
    title = v["slug"].replace("-", " ").title()
    sub = {"code": "a Kaggle notebook (kernel/)", "simulation": "an agent (agent/)", "file": "a submission file"}[v["kind"]]
    lines = [f"# {title}", "", f"[Kaggle page]({url}) · metric {v.get('metric') or 'score'} ({v.get('direction', 'max')}), "
             f"mode {v['mode']} · submissions are {sub}" + (f" · deadline {c['deadline'][:10]}" if c.get("deadline") else ""), "",
             "Worked with [kx](docs/kx.md): `kx status` shows what is going on, `kx context` what an agent needs first.", "",
             "| | |", "|---|---|",
             "| `docs/competition.md` | the Kaggle pages (`kx meta` regenerates it) |",
             "| `docs/kx.md` | how to work here: runs, protocols, tasks, findings, submissions |",
             "| `src/comp/evaluate.py` | the evaluation protocol: the one place scores are defined |",
             "| `scripts/` | entry points, run with `kx exec` |",
             "| `notebooks/` | exploration |"]
    if v["kind"] == "code":
        lines.append("| `kernel/` | the Kaggle notebook that produces the submission |")
    if v["kind"] == "simulation":
        lines.append("| `agent/` | the agent Kaggle runs (`main.py`) |")
    lines += ["| `data/` | the competition data (a link into `.kx/store`, not in git) |",
              "| `.kx/` | kx's store (ledger, runs, artifacts) and the task worktrees, not in git |", ""]
    return "\n".join(lines)


def _pyproject(v: dict, root: Path) -> str:
    import kx
    kx_repo = Path(kx.__file__).resolve().parents[2]
    source = ""
    if (kx_repo / "pyproject.toml").exists():
        rel = os.path.relpath(kx_repo, root)
        where = rel if not rel.startswith(os.path.join("..", "..", "..")) else str(kx_repo)  # ../kx next door, else absolute
        source = f'\n[tool.uv.sources]\nkx = {{ path = "{where}", editable = true }}\n'
    return f'''[project]
name = "{re.sub(r"[^a-z0-9]+", "-", v["slug"].lower()).strip("-")}"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["kx", "numpy", "pandas", "pyarrow"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/comp"]
{source}'''


def write_skeleton(root: Path, v: dict, meta: dict | None = None) -> list[str]:
    """The folders and starter files; nothing that exists is touched."""
    files = {
        "README.md": _readme(v, meta),
        "pyproject.toml": _pyproject(v, root),
        "src/comp/__init__.py": '"""This competition\'s code: import it as `comp` (e.g. from comp.evaluate import protocol)."""\n',
        "src/comp/evaluate.py": EVALUATE.replace("{metric}", str(v.get("metric") or "score")),
        "scripts/README.md": GITKEEP["scripts"],
        "notebooks/README.md": GITKEEP["notebooks"],
    }
    if v["kind"] == "code":
        kernel = (v.get("submission") or {}).get("kernel") or "<user>/<kernel-slug>"
        files["kernel/run.py"] = KERNEL_RUN.replace("{slug}", v["slug"])
        files["kernel/kernel-metadata.json"] = json.dumps({
            "id": kernel, "title": kernel.split("/")[-1].replace("-", " "), "code_file": "run.py", "language": "python",
            "kernel_type": "script", "is_private": True, "enable_gpu": False, "enable_internet": False,
            "competition_sources": [v["slug"]], "dataset_sources": [], "kernel_sources": []}, indent=2) + "\n"
    if v["kind"] == "simulation":
        files["agent/README.md"] = GITKEEP["agent"]
    done = []
    for rel, text in files.items():
        p = root / rel
        if p.exists():
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        done.append(rel)
    return [f"created {', '.join(done)}"] if done else []
