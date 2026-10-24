"""Locate and load the competition's comp.yaml."""

from __future__ import annotations

import functools
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_NAME = "comp.yaml"
KINDS = ("file", "code", "simulation")
MODES = ("predict", "optimize", "rl")
DEFAULT_Z = 2.0
HARNESSES = ("claude", "codex")


class KxError(Exception):
    """User-facing error: printed without a traceback. `fix` is the command or step that resolves it; `code` is the
    process exit code (1 error, 3 needs confirmation, 4 refused by a budget or limit, 5 not comparable)."""

    def __init__(self, message: str, fix: str | None = None, code: int = 1, data: dict | None = None):
        super().__init__(message)
        self.fix, self.code, self.data = fix, code, data


@dataclass
class Comp:
    root: Path
    slug: str
    kind: str
    mode: str
    metric: str
    direction: str
    store: Path
    username: str | None = None
    reference: str | None = None   # initial reference protocol name; the ledger setting wins once set
    z: float = DEFAULT_Z
    submission: dict = field(default_factory=dict)
    budgets: dict = field(default_factory=dict)
    workers: dict = field(default_factory=dict)
    max_workers: int = 2           # worker sessions at once (subscription plans share one allowance with you)
    tags: list = field(default_factory=list)

    @property
    def scoring(self):
        from kx.kaggle import scoring
        return scoring.get(self.mode)

    @property
    def min_episodes(self) -> int:
        return int(self.submission.get("min_episodes", 30))

    @property
    def db_path(self) -> Path:
        return self.store / "ledger.sqlite"

    @property
    def runs_dir(self) -> Path:
        return self.store / "runs"

    @property
    def main_root(self) -> Path:
        return main_repo_root(self.root)


@functools.lru_cache(maxsize=32)
def main_repo_root(root: Path) -> Path:
    """The main checkout of the repo containing root: task worktrees resolve to the same store. Cached: it runs git,
    which is slow on some disks, and kx asks for it many times per command."""
    try:
        r = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=root,
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return Path(root).resolve()
    common = r.stdout.strip()
    if r.returncode != 0 or not common:
        return Path(root).resolve()
    return Path(common).parent.resolve()


KX_DIR = ".kx"  # in the main checkout, git-ignored: the store and the task worktrees


def default_store(root: Path) -> Path:
    """<repo>/.kx/store: inside the main checkout, git-ignored, shared by all worktrees."""
    return main_repo_root(root) / KX_DIR / "store"


def worktrees_dir(main: Path) -> Path:
    return main / KX_DIR / "worktrees"


def find_root(start: Path | None = None) -> Path:
    if env := os.environ.get("KX_ROOT"):
        return Path(env).expanduser().resolve()
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / CONFIG_NAME).is_file():
            return d
    raise KxError(f"no {CONFIG_NAME} in {here} or its parents", fix="cd into a competition repo, or `kx init` there")


def _mapping(raw: dict, key: str, path: Path) -> dict:
    val = raw.get(key) or {}
    if not isinstance(val, dict):
        raise KxError(f"{path}: '{key}' must be a mapping, got {val!r}")
    return val


def load(start: Path | None = None) -> Comp:
    root = find_root(start)
    path = root / CONFIG_NAME
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise KxError(f"{path}: invalid YAML: {e}") from None
    if not isinstance(raw, dict):
        raise KxError(f"{path}: expected a mapping at top level")
    if "slug" not in raw:
        raise KxError(f"{path}: missing 'slug'")
    slug = str(raw["slug"])
    kind = raw.get("kind", "file")
    if kind not in KINDS:
        raise KxError(f"{path}: kind must be one of {KINDS}, got {kind!r}")
    mode = raw.get("mode") or ("rl" if kind == "simulation" else "predict")
    if mode not in MODES:
        raise KxError(f"{path}: mode must be one of {MODES}, got {mode!r}")
    metric = raw.get("metric") or {}
    if isinstance(metric, str):
        metric = {"name": metric}
    if not isinstance(metric, dict):
        raise KxError(f"{path}: metric must be a name or {{name, direction}}")
    direction = metric.get("direction", "max")
    if direction not in ("max", "min"):
        raise KxError(f"{path}: metric.direction must be 'max' or 'min'")
    try:
        z = float(raw.get("z", DEFAULT_Z))
        if z <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise KxError(f"{path}: z must be a positive number") from None
    store = Path(os.path.expandvars(str(raw.get("store") or default_store(root)))).expanduser()
    if not store.is_absolute():  # relative to the main checkout, so task worktrees find the same store
        store = (main_repo_root(root) / store).resolve()
    budgets = _mapping(raw, "budgets", path)
    from kx.guard.budgets import validate
    validate(budgets, path)
    workers = _mapping(raw, "workers", path)
    for name, w in workers.items():
        if not isinstance(w, dict) or w.get("harness") not in HARNESSES:
            raise KxError(f"{path}: workers.{name} needs harness: {' | '.join(HARNESSES)}")
    tags = raw.get("tags")
    if tags is not None and (not isinstance(tags, list) or not all(isinstance(t, str) for t in tags)):
        raise KxError(f"{path}: 'tags' must be a list of names")
    return Comp(
        root=root, slug=slug, kind=kind, mode=mode, metric=str(metric.get("name", "score")), direction=direction,
        store=store, username=raw.get("username"), reference=raw.get("reference"), z=z,
        submission=_mapping(raw, "submission", path), budgets=budgets, workers=workers,
        max_workers=int(raw.get("max_workers", 2)), tags=[t.strip().lower() for t in tags or []],
    )


def workspace_of(comp: Comp) -> Path:
    """The folder holding the competition repos (and kx)."""
    return comp.main_root.parent


def competitions(workspace: Path) -> list[Path]:
    """Main checkouts with a comp.yaml directly under the workspace."""
    out = []
    for d in sorted(Path(workspace).iterdir()):
        if d.is_dir() and not d.name.startswith((".", "_")) and (d / CONFIG_NAME).is_file():
            out.append(d)
    return out
