"""Execution environment of a run, recorded for reproducibility."""

from __future__ import annotations

import hashlib
import platform
import socket
import subprocess
import sys
from importlib import metadata
from pathlib import Path


def packages() -> list[str]:
    seen = {}
    for d in metadata.distributions():
        name = d.metadata["Name"]
        if name and name.lower() not in seen:
            seen[name.lower()] = f"{name}=={d.version}"
    return sorted(seen.values(), key=str.lower)


def gpus() -> list[str]:
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()] if r.returncode == 0 else []


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def capture(root: Path) -> tuple[dict, list[str]]:
    """(summary stored with the run, full package list written to the run dir)."""
    pkgs = packages()
    lock = root / "uv.lock"
    summary = {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "platform": platform.platform(),
        "host": socket.gethostname(),
        "packages": _sha("\n".join(pkgs).encode()),
        "uv_lock": _sha(lock.read_bytes()) if lock.exists() else None,
        "gpus": gpus(),
    }
    return summary, pkgs
