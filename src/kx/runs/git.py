from __future__ import annotations

import subprocess
from pathlib import Path


def _git(*args: str, cwd: Path | None = None) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return r.stdout.rstrip()


def info(cwd: Path | None = None) -> dict | None:
    """sha/branch/dirty/worktree of the repo at cwd, or None if not in a git repo.

    Untracked files count as dirty (gitignored ones do not): a new module that was never
    `git add`ed is code the recorded sha does not contain.
    """
    top = _git("rev-parse", "--show-toplevel", cwd=cwd)
    if top is None:
        return None
    sha = _git("rev-parse", "HEAD", cwd=cwd)  # None on a repo without commits
    status = _git("status", "--porcelain", "--untracked-files=all", cwd=cwd) or ""
    return {
        "sha": sha,
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd),
        "dirty": bool(status) or sha is None,
        "worktree": top,
        "changes": status.splitlines(),
    }


def exclude(pattern: str, cwd: Path):
    """Add a pattern to the repository-wide info/exclude (applies to every worktree, never committed)."""
    common = _git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd)
    if common is None:
        return
    f = Path(common) / "info" / "exclude"
    f.parent.mkdir(parents=True, exist_ok=True)
    lines = f.read_text().splitlines() if f.exists() else []
    if pattern not in lines:
        f.write_text("\n".join(lines + [pattern]) + "\n")


def has_commit(sha: str, cwd: Path) -> bool:
    return _git("cat-file", "-e", f"{sha}^{{commit}}", cwd=cwd) is not None


def archive(sha: str, path: str, dest: Path, cwd: Path) -> bool:
    """Extract `path` as of commit `sha` into dest (dest/path/...). False if path is not in that commit."""
    if _git("cat-file", "-e", f"{sha}:{path}", cwd=cwd) is None:
        return False
    dest.mkdir(parents=True, exist_ok=True)
    tar = subprocess.run(["git", "archive", "--format=tar", sha, path], cwd=cwd, capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(dest)], input=tar.stdout, check=True)
    return True

