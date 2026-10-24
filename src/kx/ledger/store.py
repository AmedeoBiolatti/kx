"""The ledger: one SQLite file per competition, shared by every worktree (WAL, busy timeout)."""

from __future__ import annotations

import getpass
import json
import os
import re
import socket
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kx.ledger.config import Comp, KxError

SCHEMA = [
    """CREATE TABLE runs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        created    TEXT NOT NULL,
        finished   TEXT,
        status     TEXT NOT NULL,                 -- running | done | failed
        kind       TEXT NOT NULL DEFAULT 'experiment',  -- experiment | probe
        hypothesis TEXT,
        config     TEXT,
        command    TEXT,
        author     TEXT,
        task       INTEGER,
        parent     INTEGER,
        grp        TEXT,
        seed       INTEGER,
        git_sha    TEXT,
        git_branch TEXT,
        git_dirty  INTEGER,
        worktree   TEXT,
        patch      TEXT,                          -- sha of the saved uncommitted diff (probes)
        host       TEXT,
        pid        INTEGER,
        pid_start  TEXT,                          -- process start time, to tell a reused pid apart
        heartbeat  TEXT,
        env        TEXT,
        gpus       INTEGER,                       -- GPUs the run declared (kx exec --gpus): charged as gpu_hours
        metrics    TEXT,
        error      TEXT
    )""",
    "CREATE INDEX runs_task ON runs(task)",
    "CREATE INDEX runs_status ON runs(status)",
    """CREATE TABLE protocols (
        id          TEXT PRIMARY KEY,             -- name@fingerprint
        name        TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        spec        TEXT NOT NULL,
        direction   TEXT NOT NULL,
        noise       TEXT NOT NULL DEFAULT 'independent',
        description TEXT,
        created     TEXT NOT NULL
    )""",
    """CREATE TABLE scores (
        run_id      INTEGER NOT NULL,
        protocol_id TEXT NOT NULL,
        value       REAL NOT NULL,
        se          REAL,
        n           INTEGER,
        breakdown   TEXT,
        created     TEXT NOT NULL,
        PRIMARY KEY (run_id, protocol_id)
    )""",
    "CREATE INDEX scores_protocol ON scores(protocol_id)",
    "CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, ts TEXT NOT NULL, author TEXT, text TEXT NOT NULL)",
    "CREATE INDEX notes_run ON notes(run_id)",
    "CREATE TABLE tags (run_id INTEGER NOT NULL, tag TEXT NOT NULL, PRIMARY KEY (run_id, tag))",
    """CREATE TABLE findings (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ts            TEXT NOT NULL,
        author        TEXT,
        text          TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'open',  -- open | confirmed | withdrawn | superseded
        evidence      TEXT,                          -- JSON list of run ids
        task          INTEGER,
        reviewed_by   TEXT,
        reviewed      TEXT,
        review_note   TEXT,
        superseded_by INTEGER
    )""",
    "CREATE TABLE finding_tags (finding_id INTEGER NOT NULL, tag TEXT NOT NULL, PRIMARY KEY (finding_id, tag))",
    """CREATE TABLE tasks (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        title      TEXT NOT NULL,
        body       TEXT,
        priority   INTEGER NOT NULL DEFAULT 0,
        depends    TEXT,                          -- JSON list of task ids
        status     TEXT NOT NULL DEFAULT 'open',  -- open | active | waiting | done | dropped
        owner      TEXT,
        waiting_on TEXT,                          -- what a waiting task waits for (run:r12, T3, a command, free text)
        result     TEXT,                          -- positive | negative | inconclusive
        outcome    TEXT,
        branch     TEXT,
        worktree   TEXT,
        created    TEXT NOT NULL,
        created_by TEXT,
        updated    TEXT
    )""",
    "CREATE TABLE task_notes (id INTEGER PRIMARY KEY AUTOINCREMENT, task INTEGER NOT NULL, ts TEXT NOT NULL, author TEXT, text TEXT NOT NULL)",
    "CREATE INDEX task_notes_task ON task_notes(task)",
    """CREATE TABLE artifacts (
        sha       TEXT PRIMARY KEY,               -- sha256 of the file that gets uploaded
        run_id    INTEGER NOT NULL,
        name      TEXT NOT NULL,
        path      TEXT NOT NULL,
        size      INTEGER,
        manifest  TEXT,
        smoke     TEXT NOT NULL DEFAULT 'unchecked',  -- unchecked | passed | failed
        smoke_log TEXT,
        created   TEXT NOT NULL,
        actor     TEXT
    )""",
    """CREATE TABLE submissions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ref         TEXT UNIQUE,                  -- Kaggle's id, NULL until synced
        run_id      INTEGER,
        task        INTEGER,
        created     TEXT NOT NULL,
        actor       TEXT,
        message     TEXT,
        why         TEXT,
        expect      TEXT,
        kind        TEXT,                         -- file | code | simulation | external
        target      TEXT,
        artifact    TEXT,
        status      TEXT,                         -- inflight | unknown | submitted | pending | complete | error
        public      REAL,
        private     REAL,
        error       TEXT,
        episodes    INTEGER,
        episodes_at TEXT,
        updated     TEXT
    )""",
    """CREATE TABLE submission_episodes (
        submission_id INTEGER NOT NULL,
        episode       INTEGER NOT NULL,
        created       TEXT,
        ended         TEXT,
        state         TEXT,
        type          TEXT,
        players       TEXT,
        rewards       TEXT,
        seat          INTEGER,
        PRIMARY KEY (submission_id, episode)
    )""",
    "CREATE TABLE submission_scores (submission_id INTEGER NOT NULL, ts TEXT NOT NULL, public REAL, episodes INTEGER)",
    """CREATE TABLE pushes (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id  INTEGER NOT NULL,
        kind    TEXT NOT NULL,                    -- kernel | dataset
        target  TEXT NOT NULL,
        version INTEGER,
        git_sha TEXT,
        source  TEXT,
        created TEXT NOT NULL,
        message TEXT
    )""",
    """CREATE TABLE lb_snapshots (
        ts TEXT NOT NULL, n_teams INTEGER, top REAL, our_rank INTEGER, our_score REAL, gold REAL, silver REAL, bronze REAL
    )""",
    """CREATE TABLE usage (
        ts     TEXT NOT NULL,
        actor  TEXT NOT NULL,
        metric TEXT NOT NULL,                     -- submissions | kaggle_gpu_hours | gpu_hours | tokens
        amount REAL NOT NULL,
        ref    TEXT
    )""",
    "CREATE INDEX usage_metric ON usage(metric, ts)",
    """CREATE TABLE workers (
        id         TEXT PRIMARY KEY,              -- launch id
        agent      TEXT NOT NULL,
        harness    TEXT NOT NULL,
        model      TEXT,
        task       INTEGER,
        pid        INTEGER,                       -- the kx runner process
        harness_pid INTEGER,                      -- the agent harness (its own process group)
        host       TEXT,
        worktree   TEXT,
        argv       TEXT,                          -- JSON: the harness command line
        started    TEXT NOT NULL,
        updated    TEXT,
        finished   TEXT,
        code       INTEGER,
        stop       TEXT,                          -- why kx stopped it (budget | asked)
        turns      INTEGER,
        tokens_in  INTEGER,
        tokens_out INTEGER,
        cost_usd   REAL,
        plan_5h    REAL,
        last_action TEXT,
        result     TEXT,
        transcript TEXT
    )""",
    "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)",
]
SCHEMA_VERSION = 1
# MIGRATIONS[i] upgrades user_version i+1 -> i+2. Append only.
MIGRATIONS: list[list[str]] = []

HEARTBEAT_S = 60
STALE_AFTER_S = 600
BACKUP_EVERY_S = 86400
BACKUP_KEEP = 14
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,39}$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(s) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def norm_ts(s) -> str:
    d = parse_ts(s)
    return d.astimezone(timezone.utc).isoformat(timespec="seconds") if d else now()


def age_s(s) -> float | None:
    d = parse_ts(s)
    return None if d is None else (datetime.now(timezone.utc) - d).total_seconds()


def since(window: str) -> str:
    """Start of a budget window: 'day' = since 00:00 UTC (Kaggle's reset), 'week' = rolling 7 days."""
    t = datetime.now(timezone.utc)
    start = t.replace(hour=0, minute=0, second=0, microsecond=0) if window == "day" else t - timedelta(days=7)
    return start.isoformat(timespec="seconds")


def dumps(obj) -> str | None:
    return None if obj is None else json.dumps(obj, sort_keys=True, default=str)


def loads(s):
    return None if s is None else json.loads(s)


def rid(run_id: int) -> str:
    return f"r{run_id:04d}"


def author() -> str:
    """Who is recorded on runs and notes: the worker (KX_AGENT) or the user."""
    return os.environ.get("KX_AGENT") or os.environ.get("KX_AUTHOR") or getpass.getuser()


def norm_tags(tags) -> list[str]:
    out = []
    for t in tags or []:
        n = re.sub(r"\s+", "-", str(t).strip().lower())
        if not TAG_RE.match(n):
            raise KxError(f"bad tag {t!r}", fix="use letters, digits, '-', '_', '.', '/' (max 40), e.g. speed or algo/pruning")
        if n not in out:
            out.append(n)
    return out


def proc_start(pid: int) -> str | None:
    """Start time of a process (clock ticks since boot, from /proc): a pid reused by another process differs."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def pid_alive(pid: int | None, start: str | None = None) -> bool | None:
    """True/False on this host; None when it cannot tell."""
    if not pid:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return None
    if start is not None:
        cur = proc_start(pid)
        if cur is not None and cur != start:
            return False
    return True


class Store:
    def __init__(self, comp: Comp, *, auto_backup: bool = False):
        self.comp = comp
        comp.store.mkdir(parents=True, exist_ok=True)
        comp.runs_dir.mkdir(exist_ok=True)
        self.db = sqlite3.connect(comp.db_path, timeout=60, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=60000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self._migrate()
        if auto_backup:
            self.maybe_backup()

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def x(self, sql: str, params=()) -> sqlite3.Cursor:
        return self.db.execute(sql, params)

    def one(self, sql: str, params=()):
        return self.db.execute(sql, params).fetchone()

    def all(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self.db.execute(sql, params).fetchall()

    @contextmanager
    def transaction(self):
        """BEGIN IMMEDIATE: take the write lock up front so concurrent writers serialize."""
        if self.db.in_transaction:
            yield
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def _migrate(self):
        v = self.one("PRAGMA user_version")[0]
        if v == SCHEMA_VERSION:
            return
        if v and v < SCHEMA_VERSION:
            self.backup(tag=f"pre-v{SCHEMA_VERSION}")
        with self.transaction():
            v = self.one("PRAGMA user_version")[0]
            if v > SCHEMA_VERSION:
                raise KxError(f"{self.comp.db_path} has schema v{v}, this kx knows v{SCHEMA_VERSION}",
                              fix="update kx (git pull in the kx repo)")
            if v == 0:
                for stmt in SCHEMA:
                    self.x(stmt)
                v = 1
            for stmts in MIGRATIONS[v - 1:]:
                for stmt in stmts:
                    self.x(stmt)
            self.x(f"PRAGMA user_version={SCHEMA_VERSION}")

    # -- backups -------------------------------------------------------------------------------------------------

    @property
    def backup_dir(self) -> Path:
        return self.comp.store / "backups"

    def backup(self, dest_dir=None, tag: str = "manual") -> Path:
        d = Path(dest_dir).expanduser() if dest_dir else self.backup_dir
        d.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path, i = d / f"ledger-{stamp}-{tag}.sqlite", 1
        while path.exists():
            i += 1
            path = d / f"ledger-{stamp}-{tag}-{i}.sqlite"
        dst = sqlite3.connect(path)
        try:
            self.db.backup(dst)
        finally:
            dst.close()
        if tag == "daily":
            for old in sorted(d.glob("ledger-*-daily*.sqlite"))[:-BACKUP_KEEP]:
                old.unlink(missing_ok=True)
        return path

    def maybe_backup(self):
        daily = sorted(self.backup_dir.glob("ledger-*-daily*.sqlite")) if self.backup_dir.exists() else []
        if daily and datetime.now().timestamp() - daily[-1].stat().st_mtime < BACKUP_EVERY_S:
            return None
        try:
            return self.backup(tag="daily")
        except (OSError, sqlite3.Error):
            return None

    # -- settings ------------------------------------------------------------------------------------------------

    def setting(self, key: str, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return loads(row["value"]) if row else default

    def set_setting(self, key: str, value):
        if value is None:
            self.x("DELETE FROM settings WHERE key=?", (key,))
        else:
            self.x("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, dumps(value)))

    # -- runs ----------------------------------------------------------------------------------------------------

    def run_dir(self, run_id: int) -> Path:
        return self.comp.runs_dir / rid(run_id)

    def get_run(self, run_id: int):
        row = self.one("SELECT * FROM runs WHERE id=?", (run_id,))
        if row is None:
            raise KxError(f"no run {rid(run_id)}", fix="kx runs")
        return row

    def resolve(self, ref) -> int:
        """12, '12', 'r12', 'r0012', 'latest', or 'best' (the best run under the reference protocol)."""
        if isinstance(ref, int):
            return ref
        s = str(ref).strip().lower()
        if s == "best":
            from kx.runs.protocol import best_run
            row = best_run(self)
            if row is None:
                raise KxError("no scored run under the reference protocol yet", fix="kx protocol list")
            return row
        if s == "latest":
            row = self.one("SELECT id FROM runs ORDER BY id DESC LIMIT 1")
            if row is None:
                raise KxError("there are no runs yet")
            return row["id"]
        if s.startswith("r"):
            s = s[1:]
        if not s.isdigit():
            raise KxError(f"not a run id: {ref!r}", fix="use e.g. r0042, latest or best")
        return int(s)

    def stale_runs(self, stale_after: int = STALE_AFTER_S) -> list[tuple]:
        """Runs marked running whose process is gone (same host) or that stopped heart-beating. Runs on a remote host
        (kx exec --on) are never stale here: `kx remote sync` closes them."""
        out, host = [], socket.gethostname()
        for r in self.all("SELECT * FROM runs WHERE status='running'"):
            if (self.run_dir(r["id"]) / "remote.json").exists():
                continue
            if r["host"] == host and pid_alive(r["pid"], r["pid_start"]) is False:
                out.append((r, f"process {r['pid']} is gone"))
                continue
            a = age_s(r["heartbeat"] or r["created"])
            if a is not None and a > stale_after:
                out.append((r, f"no heartbeat for {int(a // 60)} min"))
        return out

    def reap(self, stale_after: int = STALE_AFTER_S) -> list[tuple[int, str]]:
        """Close dead runs: done if they had recorded scores, else failed."""
        closed = []
        for r, why in self.stale_runs(stale_after):
            scored = self.one("SELECT COUNT(*) FROM scores WHERE run_id=?", (r["id"],))[0]
            status = "done" if scored else "failed"
            n = self.x("UPDATE runs SET status=?, finished=?, error=? WHERE id=? AND status='running'",
                       (status, now(), None if scored else f"abandoned: {why}", r["id"])).rowcount
            if n:
                if scored:
                    self.add_note(r["id"], f"kx reap: {why}; its scores were already recorded, kept as done")
                closed.append((r["id"], why))
        return closed

    # -- notes and tags ------------------------------------------------------------------------------------------

    def add_note(self, run_id: int, text: str):
        self.get_run(run_id)
        self.x("INSERT INTO notes (run_id, ts, author, text) VALUES (?,?,?,?)", (run_id, now(), author(), text))

    def notes(self, run_id: int):
        return self.all("SELECT * FROM notes WHERE run_id=? ORDER BY id", (run_id,))

    def last_notes(self) -> dict[int, str]:
        return {r["run_id"]: r["text"] for r in self.all("SELECT run_id, text FROM notes ORDER BY id")}

    def add_tags(self, run_id: int, tags):
        for t in norm_tags(tags):
            self.x("INSERT OR IGNORE INTO tags VALUES (?,?)", (run_id, t))

    def remove_tags(self, run_id: int, tags):
        for t in norm_tags(tags):
            self.x("DELETE FROM tags WHERE run_id=? AND tag=?", (run_id, t))

    def tags(self, run_id: int) -> list[str]:
        return [r[0] for r in self.x("SELECT tag FROM tags WHERE run_id=? ORDER BY tag", (run_id,))]

    def unknown_tags(self, tags) -> list[str]:
        vocab = set(self.comp.tags)
        return [t for t in norm_tags(tags) if vocab and t not in vocab]

    # -- scores --------------------------------------------------------------------------------------------------

    def scores_for(self, run_id: int):
        return self.all("SELECT s.*, p.name, p.direction, p.spec, p.noise FROM scores s JOIN protocols p ON p.id=s.protocol_id"
                        " WHERE run_id=? ORDER BY s.created", (run_id,))

    def protocol(self, pid: str):
        row = self.one("SELECT * FROM protocols WHERE id=?", (pid,))
        if row is None:
            raise KxError(f"unknown protocol {pid!r}", fix="kx protocol list")
        return row


def open_store(start: Path | None = None, **kw) -> Store:
    from kx.ledger import config
    return Store(config.load(start), **kw)
