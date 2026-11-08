"""The competitions on this machine, best effort, for the dashboard's picker.

Nothing registers a competition: kx notes each one it opens here (`remember`, called by config.load) in a small file
under ~/.cache, and `discover` adds what it can find without being told: the competition folders next to those (a
workspace holds several), next to kx's own checkout (as the README lays it out), and a short look around the current
folder and the home folder. Losing the file loses nothing but the list: the next kx command in a project brings it
back.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

FILE = Path(os.environ.get("KX_PROJECTS_FILE", "~/.cache/kx/projects.json")).expanduser()
CONFIG_NAME = "comp.yaml"
SKIP = {"node_modules", "site-packages", "target", "venv", "__pycache__", "snap", "go", "Library", "AppData"}
_noted: set[str] = set()  # remembered by this process already


def _read() -> dict:
    try:
        data = json.loads(FILE.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def remember(root: Path) -> None:
    """Note a competition's main checkout as used now. Writes at most once an hour per project; never fails a
    command (a read-only home, a remote host)."""
    if os.environ.get("KX_REMOTE"):
        return
    key = str(Path(root).resolve())
    if key in _noted:
        return
    _noted.add(key)
    try:
        data = _read()
        if time.time() - float(data.get(key, 0)) < 3600:
            return
        data[key] = time.time()
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_name(f".{FILE.name}.{os.getpid()}")
        tmp.write_text(json.dumps(data, indent=0, sort_keys=True))
        tmp.replace(FILE)
    except (OSError, ValueError, TypeError):
        pass


def known() -> list[Path]:
    """Remembered projects that still have their comp.yaml, the most recently used first."""
    data = _read()
    out = []
    for k, _ in sorted(data.items(), key=lambda kv: -float(kv[1]) if isinstance(kv[1], (int, float)) else 0):
        p = Path(k)
        if (p / CONFIG_NAME).is_file():
            out.append(p)
    return out


def _kx_workspace() -> Path | None:
    """The folder kx's own checkout sits in (with the competitions, as the README sets it up), when kx runs from a
    checkout; the main one when that is a git worktree elsewhere."""
    src = Path(__file__).resolve().parents[3]
    git = src / ".git"
    if git.is_file():  # a worktree: "gitdir: <main>/.git/worktrees/<name>"
        try:
            line = git.read_text().strip()
            if line.startswith("gitdir:"):
                gd = Path(line.split(":", 1)[1].strip())
                if gd.parent.name == "worktrees" and gd.parent.parent.name == ".git":
                    return gd.parent.parent.parent.parent
        except OSError:
            return None
    return src.parent if git.is_dir() else None


def _children(d: Path) -> list[Path]:
    try:
        return sorted(c for c in d.iterdir() if c.is_dir() and not c.name.startswith((".", "_")) and c.name not in SKIP)
    except OSError:
        return []


def _scan(top: Path, depth: int, deadline: float, found: list[Path]) -> None:
    """Folders with a comp.yaml under top, at most `depth` levels down; not inside one (its worktrees, its data)."""
    if time.monotonic() > deadline:
        return
    if (top / CONFIG_NAME).is_file():
        found.append(top)
        return
    if depth > 0:
        for c in _children(top):
            _scan(c, depth - 1, deadline, found)


def discover(extra: list[Path] | tuple = (), *, budget_s: float = 2.0) -> list[Path]:
    """Competition folders on this machine: `extra` and the current one first, then the remembered ones, the ones
    next to them and to kx, then whatever a quick look (`budget_s`) around here and the home folder finds. Main
    checkouts only (a task worktree is its competition), each once."""
    deadline = time.monotonic() + budget_s
    out: dict[str, Path] = {}

    def add(p: Path):
        p = Path(p).resolve()
        if str(p) not in out and (p / CONFIG_NAME).is_file():
            out[str(p)] = p

    here = Path.cwd().resolve()
    for d in (here, *here.parents):
        if (d / CONFIG_NAME).is_file():
            add(d)
            break
    for p in extra:
        found: list[Path] = []
        _scan(Path(p).expanduser(), 2, deadline + budget_s, found)  # asked for: given its own time
        for f in found:
            add(f)
    for p in known():
        add(p)
    for ws in {*(p.parent for p in list(out.values())), *filter(None, [_kx_workspace()])}:
        for c in _children(ws):
            add(c)
    for top, depth in ((here, 2), (Path.home(), 3)):
        found = []
        _scan(top, depth, deadline, found)
        for f in found:
            add(f)
    from kx.ledger.config import main_repo_root
    mains: dict[str, Path] = {}
    for p in out.values():
        m = main_repo_root(p) if (p / ".git").is_file() else p  # a worktree (.git is a file): its main checkout
        if (m / CONFIG_NAME).is_file():
            mains.setdefault(str(m), m)
    return list(mains.values())
