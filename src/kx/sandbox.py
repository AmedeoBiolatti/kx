"""What this machine is for, and the sandbox kx starts agents and project code in.

A machine's role (`~/.config/kx/machine.yaml`, `role:`; KX_ROLE overrides it):

    both        the default: one person works and runs here (as before)
    workspace   people and their agents think here; runs go to executors (`kx exec --on`, or `default_host:`)
    executor    it runs jobs (an ssh host, a mailbox agent)

The sandbox is bubblewrap (`bwrap`, no root needed): the whole filesystem read-only, writable only the competition
(its repo and worktrees, its store), the agents' own state (~/.claude, ~/.codex), caches and kx's state; a private
/tmp; logins kx never needs hidden. Whatever runs inside, an agent or a script it tries, can damage at most the
competition folder, which git and the ledger recover.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
from pathlib import Path

import yaml

from kx.ledger.config import KxError

MACHINE_FILE = Path(os.environ.get("KX_MACHINE_FILE", "~/.config/kx/machine.yaml")).expanduser()
ROLES = ("both", "workspace", "executor")
# logins no agent or run needs (Kaggle's stays: kx itself reads it, and workers cannot pass --yes anyway)
HIDDEN = (".config/rclone", ".config/gh", ".aws", ".azure", ".config/gcloud", ".docker", ".netrc", ".git-credentials",
          ".gnupg", ".password-store", ".mozilla", ".config/google-chrome", ".config/chromium")
WRITABLE_HOME = (".claude", ".claude.json", ".codex", ".cache")


def machine() -> dict:
    if not MACHINE_FILE.exists():
        return {}
    try:
        data = yaml.safe_load(MACHINE_FILE.read_text()) or {}
    except yaml.YAMLError as e:
        raise KxError(f"{MACHINE_FILE}: invalid YAML: {e}") from None
    return data if isinstance(data, dict) else {}


def role() -> str:
    r = os.environ.get("KX_ROLE") or machine().get("role") or "both"
    if r not in ROLES:
        raise KxError(f"unknown role {r!r} in {MACHINE_FILE}", fix=f"role: {' | '.join(ROLES)}")
    return r


def no_local_runs(what: str):
    """A workspace runs no project code itself: runs go to executors."""
    if role() == "workspace":
        default = machine().get("default_host")
        raise KxError(f"this machine is a workspace: {what} runs on an executor, not here",
                      fix=f"kx exec --on {default or 'HOST'} ...  (machine role in {MACHINE_FILE})")


@functools.lru_cache(maxsize=1)
def available() -> bool:
    """bwrap is installed and allowed to make its namespace (some distributions restrict that)."""
    if shutil.which("bwrap") is None:
        return False
    p = subprocess.run(["bwrap", "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev", "true"], capture_output=True)
    return p.returncode == 0


def wrap(argv: list[str], writable: list[Path], *, cwd: Path | None = None) -> list[str]:
    """argv inside the sandbox, with `writable` (besides the home's agent state and caches) read-write."""
    home = Path.home()
    args = ["bwrap", "--die-with-parent", "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev", "--tmpfs", "/tmp"]
    for rel in HIDDEN:
        p = home / rel
        if p.is_dir():
            args += ["--tmpfs", str(p)]
        elif p.exists():
            args += ["--ro-bind", "/dev/null", str(p)]
    (home / ".cache").mkdir(exist_ok=True)
    seen = set()
    for p in [*(home / rel for rel in WRITABLE_HOME), *writable]:
        p = Path(p).resolve()
        if p.exists() and p not in seen:
            seen.add(p)
            args += ["--bind", str(p), str(p)]
    args += ["--setenv", "KX_SANDBOX", "1"]
    if cwd is not None:
        args += ["--chdir", str(cwd)]
    return [*args, "--", *argv]


def competition_paths(comp) -> list[Path]:
    """What a sandbox for this competition may write: its repo (with worktrees and .kx) and its store."""
    return [comp.main_root, comp.store]
