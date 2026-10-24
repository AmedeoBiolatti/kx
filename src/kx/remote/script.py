"""run.sh: what a remote run does on its host, ssh or mailbox alike. kx writes it into the run's job folder next to
the code (code.tar.gz, plus a probe's patch); it unpacks the code, makes or enters the project's environment, links
the data, and runs the command, confined when the host has bubblewrap.

On the host, under the host's dir (an ssh host's `dir:`, a mailbox agent's --dir):

    kx/                          the kx sources (the run imports kx; with no ledger there it writes files instead)
    <slug>/data/<data version>/  the competition data (code/data links to it)
    <slug>/venvs/<lock hash>/    the project's environment (uv sync --frozen), shared by runs with the same uv.lock
    <slug>/runs/<run>/           the job folder: run.sh, code.tar.gz; code/ unpacked from it
    <slug>/runs/<run>/out/       its KX_RUN_DIR, copied back; out/.remote has its pid, times and exit code
"""

from __future__ import annotations

import hashlib
import re
import shlex
import subprocess
from pathlib import Path

q = shlex.quote
TORCH_INDEX = "https://download.pytorch.org/whl/"


def env_spec(root: Path, sha: str, torch: str | None = None) -> tuple[str, bool, str | None]:
    """(key, locked, torch version) of the project's environment at sha. Environments are shared by runs whose uv.lock
    (else pyproject.toml) is the same, on hosts with the same torch index; with one, the lock's torch version is
    installed from it."""
    for name in ("uv.lock", "pyproject.toml"):
        p = subprocess.run(["git", "show", f"{sha}:{name}"], cwd=root, capture_output=True)
        if p.returncode == 0:
            key = hashlib.sha256(p.stdout).hexdigest()[:12] + (f"-{torch}" if torch else "")
            m = re.search(rb'^\[\[package\]\]\nname = "torch"\nversion = "([^"]+)"', p.stdout, re.M) if name == "uv.lock" else None
            return key, name == "uv.lock", m.group(1).decode().split("+")[0] if (torch and m) else None
    return "none" + (f"-{torch}" if torch else ""), False, None


def run_sh(h, run_id: int, name: str, *, cmd: list[str], cwd: str, data_version: str | None, env: tuple,
           timeout: float | None) -> str:
    """The job's run.sh; everything it prints lands in out/*.log. `env` is env_spec()'s answer."""
    exports = {"KX_REMOTE": "1", "KX_RUN_ID": str(run_id), "KX_HOST": h.name, **{str(k): str(v) for k, v in h.env.items()}}
    lines = [
        "#!/usr/bin/env bash",
        f"# kx exec --on {h.name}: {name}",
        'cd "$(dirname "$0")" || exit 1',
        'R="$PWD"; B="$(cd ../.. && pwd)"; M="$R/out/.remote"',
        'mkdir -p "$M" "$R/code"; echo $$ > "$M/pid"; date +%s > "$M/started"',
        'exec >>"$R/out/stdout.log" 2>>"$R/out/stderr.log" </dev/null',
        'finish() { date +%s > "$M/ended"; echo "$1" > "$M/exit.tmp" && mv "$M/exit.tmp" "$M/exit"; exit "$1"; }',
        "trap 'finish 143' TERM",
        "trap 'finish 129' HUP",
        "export GIT_CEILING_DIRECTORIES=\"$R\"",
        'tar -xzf "$R/code.tar.gz" -C "$R/code" || finish 1',
        '[ -s "$R/uncommitted.patch" ] && { (cd "$R/code" && git apply --whitespace=nowarn "$R/uncommitted.patch") || finish 1; }',
        '[ -f "$R/untracked.tar.gz" ] && { tar -xzf "$R/untracked.tar.gz" -C "$R/code" || finish 1; }',
    ]
    if data_version:
        lines.append(f'ln -sfn "$B/data/{data_version}" "$R/code/data"')
        exports |= {"KX_DATA_VERSION": data_version}
    if h.python:
        # a wrapper, not a symlink: a venv's interpreter reached through a symlink elsewhere loses its venv
        lines += ['mkdir -p "$R/bin"', f"printf '#!/bin/sh\\nexec %s \"$@\"\\n' {q(q(h.python))} > \"$R/bin/python\"",
                  'chmod +x "$R/bin/python"', 'export PATH="$R/bin:$PATH"']
    else:
        lines += ['C="$R/code"', *env_lines(*env, h.torch)]
    # the project's own package is not installed (environments are shared by runs of different commits)
    lines.append('export PYTHONPATH="$R/code/src${PYTHONPATH:+:$PYTHONPATH}"')
    lines.append("export " + " ".join(f"{k}={q(v)}" for k, v in exports.items()) + ' KX_RUN_DIR="$R/out"')
    if data_version:
        lines.append(f'export KX_DATA_DIR="$B/data/{data_version}"')
    lines.append(f'cd "$R/code/{cwd}" || finish 1' if cwd not in ("", ".") else 'cd "$R/code" || finish 1')
    run = shlex.join(cmd)
    if timeout is not None:
        run = f"if command -v timeout >/dev/null; then timeout -k 10 {timeout:g} {run}; else {run}; fi"
    lines += confine_lines(h, data_version)
    lines += [f'"${{SBX[@]}}" bash -c {q(run)} &', 'wait $!', 'finish $?']
    return "\n".join(lines) + "\n"


def confine_lines(h, data_version: str | None) -> list[str]:
    """The command runs in bubblewrap when the host has it (and `confine:` is not false): everything read-only, the
    home hidden (its logins, the mailbox's rclone config, other projects), writable only the run's dir, the data's
    derived/ caches, ~/.cache and a private /tmp. A bug in a run can then damage at most its own run."""
    if not h.confine:
        return ["SBX=()"]
    binds = ['SBX+=(--bind "$R" "$R")']
    if data_version:
        binds += [f'D="$B/data/{data_version}"; mkdir -p "$D/derived"',
                  'SBX+=(--ro-bind "$D" "$D" --bind "$D/derived" "$D/derived")']
    if h.python:  # the interpreter's own tree (a venv's, and the base it links to) may be in the hidden home
        p = q(h.python)
        binds += [f'for d in "$(dirname "$(dirname {p})")" "$(dirname "$(dirname "$(readlink -f {p})")")"; do '
                  'SBX+=(--ro-bind "$d" "$d"); done']
    else:
        binds += ['SBX+=(--ro-bind "$V" "$V" --ro-bind "$(cd "$B/.." && pwd)/kx" "$(cd "$B/.." && pwd)/kx")']
    return [
        "SBX=()",
        'if command -v bwrap >/dev/null && bwrap --ro-bind / / --dev-bind /dev /dev true 2>/dev/null; then',
        '  SBX=(bwrap --die-with-parent --ro-bind / / --dev-bind /dev /dev --tmpfs /tmp --tmpfs "$HOME")',
        '  mkdir -p "$HOME/.cache"; SBX+=(--bind "$HOME/.cache" "$HOME/.cache")',
        '  [ -d "$HOME/.local/share/uv/python" ] && SBX+=(--ro-bind "$HOME/.local/share/uv/python" "$HOME/.local/share/uv/python")',
        *("  " + b for b in binds),
        "else",
        '  echo "kx: bubblewrap is not usable on this host: the run is not confined (apt install bubblewrap)" >&2',
        "fi",
    ]


def env_lines(key: str, locked: bool, torch_version: str | None, torch: str | None) -> list[str]:
    """Shell lines that make (once) and enter the environment for the project in $C, under $B; `finish` on failure.
    Every extra is installed (a host is for the heavy runs). With `torch`, the lock's torch is replaced by the same
    version from that PyTorch index (a GPU the lock's build does not cover)."""
    sync = "uv sync " + ("--frozen " if locked else "") + "--all-extras --no-install-project --no-install-package kx"
    swap = []
    if torch:
        spec = q(f"torch=={torch_version}" if torch_version else "torch")
        why = q(f"no torch {torch_version or ''} build at {TORCH_INDEX}{torch}: pin a torch version that has one")
        swap = [f'  uv pip install -q --python "$V/bin/python" --reinstall-package torch --index-url {q(TORCH_INDEX + torch)} '
                f'--extra-index-url https://pypi.org/simple {spec} || {{ echo {why} >&2; finish 1; }}']
    return [
        'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"',  # where the uv installer puts uv
        f'V="$B/venvs/{key}"; mkdir -p "$B/venvs"',
        'if command -v flock >/dev/null; then exec 9>"$V.lock"; flock 9; fi',  # one setup at a time per env
        'if [ ! -f "$V/.kx-ready" ]; then',
        '  echo "kx: setting up the environment in $V" >&2',
        '  if [ -f "$C/pyproject.toml" ]; then',
        f'    (cd "$C" && UV_PROJECT_ENVIRONMENT="$V" {sync}) || finish 1',
        '  else uv venv -q "$V" || finish 1; fi',
        *swap,
        '  uv pip install -q --python "$V/bin/python" -e "$B/../kx" && touch "$V/.kx-ready" || finish 1',
        'fi',
        'exec 9>&-',
        'export VIRTUAL_ENV="$V" PATH="$V/bin:$PATH"',
    ]
