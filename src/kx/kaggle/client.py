"""Thin wrapper around the `kaggle` CLI.

The CLI is used instead of the Python API because the API changes between
releases while the CLI's JSON/CSV output is stable. All Kaggle calls go
through this module.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kx.ledger.config import KxError

TOKEN = re.compile(r"Next Page Token = (\S+)")


# The CLI prints some failures and still exits 0.
SOFT_ERRORS = re.compile(r"(push error|creation error|submission error|^error:)", re.I | re.M)
MAX_PAGES = 10_000


def _exe() -> str:
    exe = shutil.which("kaggle")
    if exe is None:
        raise KxError("`kaggle` CLI not found on PATH (uv tool install kaggle)")
    return exe


# `kaggle auth login` (OAuth) keeps an access token that expires after hours. The CLI (2.2.3) neither refreshes it
# for API calls nor saves the one `kaggle auth print-access-token` gets from the refresh token, so every call failed
# with "Authentication required" once it expired. kx refreshes it itself and hands it over as KAGGLE_API_TOKEN.
AUTH_FAILED = re.compile(r"Authentication required|401|Unauthenticated", re.I)
_fresh: tuple[str, datetime] | None = None


def credentials_problem(env: dict | None = None) -> str | None:
    """Why a kaggle call would fail to authenticate here, or None (only what can be told without calling Kaggle)."""
    env = os.environ if env is None else env
    if env.get("KAGGLE_API_TOKEN") or (env.get("KAGGLE_USERNAME") and env.get("KAGGLE_KEY")):
        return None
    home = Path(env.get("KAGGLE_CONFIG_DIR") or Path.home() / ".kaggle")
    if any((home / f).exists() for f in ("credentials.json", "kaggle.json", "access_token")):
        return None
    return f"no Kaggle credentials on this machine (nothing in {home}): run `kaggle auth login`"


def auth_env(force: bool = False) -> dict:
    """The environment for a kaggle call, with a fresh access token when the stored OAuth one is expired."""
    global _fresh
    env = dict(os.environ)
    if env.get("KAGGLE_API_TOKEN") or env.get("KAGGLE_KEY"):
        return env  # the user's own token or key wins
    now = datetime.now(timezone.utc)
    if not force:
        try:
            cred = json.loads((Path.home() / ".kaggle" / "credentials.json").read_text())
            expires = datetime.fromisoformat(str(cred["access_token_expiration"]))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
        except (OSError, ValueError, KeyError, TypeError):
            return env  # an API key file or no OAuth login: the CLI handles it
        if expires - now > timedelta(minutes=5):
            return env
        if _fresh and _fresh[1] - now > timedelta(minutes=5):
            env["KAGGLE_API_TOKEN"] = _fresh[0]
            return env
    try:
        r = subprocess.run([_exe(), "auth", "print-access-token"], capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return env
    token = r.stdout.strip().splitlines()[-1].strip() if r.returncode == 0 and r.stdout.strip() else ""
    if token and " " not in token:
        _fresh = (token, now + timedelta(minutes=50))
        env["KAGGLE_API_TOKEN"] = token
    return env


def _run(*args: str, timeout: int | None = 300, _retry: bool = True, tmpdir: Path | None = None) -> subprocess.CompletedProcess:
    env = auth_env()
    if tmpdir is not None:  # where the CLI zips folders before uploading: not a small RAM-backed /tmp
        tmpdir.mkdir(parents=True, exist_ok=True)
        env = {**env, "TMPDIR": str(tmpdir)}
    try:
        r = subprocess.run([_exe(), *args], capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise KxError(f"kaggle {' '.join(args[:2])} timed out after {timeout}s (Kaggle slow or unreachable?)") from None
    except OSError as e:
        raise KxError(f"cannot run kaggle: {e}") from None
    if _retry and AUTH_FAILED.search(r.stdout + r.stderr) and "KAGGLE_API_TOKEN" not in os.environ:
        global _fresh
        _fresh = None
        auth_env(force=True)  # the token was refused although not expired: refresh once and retry
        if _fresh:
            return _run(*args, timeout=timeout, _retry=False, tmpdir=tmpdir)
    if r.returncode != 0 or AUTH_FAILED.search(r.stdout + r.stderr):
        out = (r.stdout + r.stderr).strip()
        hint = ""
        if AUTH_FAILED.search(out):
            hint = "\n(Kaggle login failed and could not be refreshed: run `kaggle auth login` once in a terminal)"
        if "403" in out or "Forbidden" in out:
            hint = "\n(403: have you accepted the competition rules on kaggle.com?)"
        raise KxError(f"kaggle {' '.join(args[:2])} failed:\n{out[-2000:]}{hint}")
    return r


PROGRESS = re.compile(r"\d+%\|")  # tqdm upload/download bars: thousands of lines, no information


def without_progress(out: str) -> str:
    """CLI output without progress-bar lines (an 8 GB upload prints megabytes of them, hiding the one error line)."""
    lines = (ln for chunk in out.splitlines() for ln in chunk.split("\r"))
    return "\n".join(ln for ln in lines if ln.strip() and not PROGRESS.search(ln))


def _cli(*args: str, timeout: int | None = 300, check_output: bool = False, tmpdir: Path | None = None) -> str:
    r = _run(*args, timeout=timeout, tmpdir=tmpdir)
    out = without_progress(r.stdout + r.stderr)
    if check_output and SOFT_ERRORS.search(out):
        raise KxError(f"kaggle {' '.join(args[:2])} failed:\n{out.strip()[-2000:]}")
    return out


def _json(out: str) -> list[dict]:
    """Parse JSON output, skipping chatter such as 'Next Page Token = ...'."""
    lines = [ln for ln in out.splitlines() if not TOKEN.match(ln.strip())]
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("[", "{")):
            try:  # the first JSON value only: the CLI appends hints after it ('Use "kaggle competitions replay ...')
                data, _ = json.JSONDecoder().raw_decode("\n".join(lines[i:]).lstrip())
            except ValueError as e:
                raise KxError(f"unreadable kaggle output ({e}):\n{out.strip()[:1000]}") from None
            return data if isinstance(data, list) else [data]
    if "No " in out and "found" in out:
        return []
    raise KxError(f"unexpected kaggle output:\n{out.strip()[:2000]}")


def _paged(*args: str) -> list[dict]:
    rows, token = [], None
    for _ in range(MAX_PAGES):
        extra = ["--page-token", token] if token else []
        r = _run(*args, "--format", "json", "--page-size", "200", *extra)
        page = _json(r.stdout)
        rows += page
        m = TOKEN.search(r.stdout + r.stderr)
        if not page or not m or m.group(1) == token:
            return rows
        token = m.group(1)
    raise KxError(f"kaggle {' '.join(args[:2])}: more than {MAX_PAGES} pages, giving up")


def submissions(slug: str) -> list[dict]:
    return _paged("competitions", "submissions", slug)


def competition(slug: str) -> dict:
    """ref, deadline, category, reward, teamCount, userHasEntered."""
    for query in (slug, slug.replace("-", " ")):
        for c in _json(_cli("competitions", "list", "-s", query, "--format", "json")):
            if str(c.get("ref", "")).rstrip("/").lower().endswith("/" + slug.lower()):
                return c
    raise KxError(f"competition {slug!r} not found on Kaggle")


def pages(slug: str) -> list[dict]:
    """[{name, content}] — description, evaluation, rules, data description, ..."""
    return _json(_cli("competitions", "pages", slug, "--content", "--format", "json"))


def files(slug: str) -> list[dict]:
    """[{name, size, creationDate}] for every competition data file."""
    return _paged("competitions", "files", slug)


def download(slug: str, dest: Path, file: str | None = None, quiet: bool = False):
    """Download all files (one zip) or a single file into dest. Progress goes to the terminal."""
    dest.mkdir(parents=True, exist_ok=True)
    args = [_exe(), "competitions", "download", slug, "-p", str(dest), "-o"]
    if file:
        args += ["-f", file]
    if quiet:
        args += ["-q"]
    r = subprocess.run(args, env=auth_env())
    if r.returncode != 0:
        raise KxError(f"download failed (exit {r.returncode}); have you accepted the rules on kaggle.com?")


def submit_file(slug: str, path: Path, message: str) -> str:
    return _cli("competitions", "submit", slug, "-f", str(path), "-m", message, timeout=1800, check_output=True)


def submit_kernel(slug: str, kernel: str, version: str | int, output_file: str, message: str) -> str:
    """Code competitions: the CLI requires an explicit kernel version."""
    args = ["competitions", "submit", slug, "-k", kernel, "-f", output_file, "-v", str(version), "-m", message]
    return _cli(*args, timeout=600, check_output=True)


def kernel_push(folder: Path) -> tuple[int | None, str]:
    """Push a folder with kernel-metadata.json; returns (version number, CLI output)."""
    out = _cli("kernels", "push", "-p", str(folder), timeout=900, check_output=True)
    m = re.search(r"Kernel version (\d+) successfully pushed", out)
    if not m and "successfully pushed" not in out:
        raise KxError(f"kaggle kernels push: unexpected output:\n{out.strip()[-2000:]}")
    return (int(m.group(1)) if m else None), out


def kernel_status(kernel: str, version: int | str | None = None) -> tuple[str, str]:
    """(status, raw output); status lowercased without the enum prefix: queued | running | complete | error ..."""
    ref = f"{kernel}/{version}" if version is not None else kernel
    out = _cli("kernels", "status", ref)
    m = re.search(r'has status "([^"]+)"', out)
    if not m:
        raise KxError(f"kaggle kernels status {ref}: unexpected output:\n{out.strip()[-1000:]}")
    return m.group(1).split(".")[-1].lower(), out


def dataset_status(ref: str) -> tuple[str, int | None]:
    """(status, current version number) of a dataset; status lowercased: ready | pending | error ..."""
    out = _cli("datasets", "status", ref, "--format", "json(status,current_version_number)")
    try:
        d = json.loads(out[out.index("{"):out.rindex("}") + 1])
    except ValueError:
        return out.strip().split()[-1].lower() if out.strip() else "unknown", None
    status = str(d.get("status") or d.get("Status") or "unknown").split(".")[-1].lower()
    v = d.get("current_version_number") or d.get("currentVersionNumber")
    return status, int(v) if v is not None else None


def dataset_version(folder: Path, message: str, *, create: bool = False, tmpdir: Path | None = None) -> str:
    """Upload a folder with dataset-metadata.json as a new dataset version (or create the dataset). Subfolders are
    zipped first, in tmpdir (default: next to the folder, on its disk)."""
    tmpdir = tmpdir or folder.parent / ".kx-upload-tmp"
    if create:
        args = ["datasets", "create", "-p", str(folder), "-r", "zip"]
    else:
        args = ["datasets", "version", "-p", str(folder), "-m", message, "-r", "zip"]
    try:
        return _cli(*args, timeout=7200, check_output=True, tmpdir=tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def username() -> str | None:
    try:
        out = subprocess.run([_exe(), "config", "view"], capture_output=True, text=True, timeout=60, env=auth_env()).stdout
    except (OSError, subprocess.TimeoutExpired, KxError):
        return None
    m = re.search(r"username:\s*(\S+)", out)
    return m.group(1) if m else None


def leaderboard(slug: str) -> list[dict]:
    """Full public leaderboard as a list of rows (Rank, TeamId, TeamName, Score, TeamMemberUserNames, ...)."""
    with tempfile.TemporaryDirectory() as d:
        _cli("competitions", "leaderboard", slug, "-d", "-p", d, "-q")
        for z in Path(d).glob("*.zip"):
            with zipfile.ZipFile(z) as zf:
                name = next(n for n in zf.namelist() if n.endswith(".csv"))
                text = zf.read(name).decode("utf-8-sig")
                return list(csv.DictReader(io.StringIO(text)))
        for c in Path(d).glob("*.csv"):
            return list(csv.DictReader(io.StringIO(c.read_text(encoding="utf-8-sig"))))
    raise KxError("leaderboard download produced no file")


# `kaggle competitions episodes` leaves out each game's agents (who played, the reward, which one is ours); the API
# object has them. It is read with the kaggle tool's own interpreter, so kx does not depend on its Python API.
_EPISODES_WITH_AGENTS = """
import json, sys
from kaggle.api.kaggle_api_extended import KaggleApi
api = KaggleApi(); api.authenticate()
iso = lambda t: t.isoformat() if t else None
print(json.dumps([{"id": e.id, "createTime": iso(e.create_time), "endTime": iso(e.end_time), "state": str(e.state),
    "type": str(e.type), "agents": [{"submission_id": g.submission_id, "index": g.index, "reward": g.reward,
    "team_name": g.team_name} for g in (e.agents or [])]} for e in api.competition_list_episodes(int(sys.argv[1]))]))
"""


def _kaggle_python() -> str | None:
    try:
        first = Path(_exe()).read_text(errors="replace").splitlines()[0]
    except (OSError, IndexError, KxError):
        return None
    return first[2:].strip() if first.startswith("#!") else None


def episodes(submission_ref: str) -> list[dict]:
    """The games of a submission. With the agents (players, rewards) when the kaggle tool's Python can give them,
    else the CLI's plain list (id, times, state, type)."""
    py = _kaggle_python()
    if py:
        try:
            r = subprocess.run([py, "-c", _EPISODES_WITH_AGENTS, str(submission_ref)], capture_output=True, text=True,
                               timeout=300, env=auth_env())
            if r.returncode == 0:
                return json.loads(r.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
    return _json(_cli("competitions", "episodes", str(submission_ref), "--format", "json"))


def logs(episode_id: str, agent_index: int, dest: Path) -> str:
    dest.mkdir(parents=True, exist_ok=True)
    return _cli("competitions", "logs", str(episode_id), str(agent_index), "-p", str(dest))


MEDAL_CATEGORIES = {"featured", "research"}  # Playground / Getting Started / Community award no medals


def awards_medals(category: str | None) -> bool:
    return category is None or category.strip().lower() in MEDAL_CATEGORIES  # unknown: assume yes


def medal_thresholds(n_teams: int) -> dict[str, int]:
    """Lowest rank still earning each medal (Kaggle progression rules)."""
    if n_teams < 100:
        return {"gold": max(1, int(n_teams * 0.10)), "silver": int(n_teams * 0.20), "bronze": int(n_teams * 0.40)}
    if n_teams < 250:
        return {"gold": 10, "silver": int(n_teams * 0.20), "bronze": int(n_teams * 0.40)}
    if n_teams < 1000:
        return {"gold": 10 + int(n_teams * 0.002), "silver": 50, "bronze": 100}
    return {"gold": 10 + int(n_teams * 0.002), "silver": int(n_teams * 0.05), "bronze": int(n_teams * 0.10)}


def to_float(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
